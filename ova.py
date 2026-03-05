import os
import time
import json
import subprocess
import xml.etree.ElementTree as ET
import sys
import shutil
import threading
from datetime import datetime
from collections import deque

# =========================
# GLOBAL
# =========================
CONFIG_FILE = "config.json"
PACKAGES_FILE = "packages.json"
monitor_active = False
EXECUTOR_TYPE = "delta"  # Executor type: delta or arceusx

# Account state tracking (seperti PC version)
ACCOUNT_STATE = {}  # pkg -> {launch_time, json_start_time, json_active, last_status}

# Log system - rolling 5 baris terakhir
LOG_BUFFER = deque(maxlen=5)
LAST_DISPLAY_STATUS = {}  # pkg -> status terakhir yang ditampilkan

# =========================
# BASIC UTILS
# =========================
def run_root_cmd(cmd):
    try:
        r = subprocess.run(
            ["su", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=10
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        add_log("Command timeout")
        return ""
    except FileNotFoundError:
        add_log("ERROR: su not found")
        return ""
    except Exception as e:
        add_log(f"Command error: {e}")
        return ""

# =========================
# CPU & RAM MONITORING
# =========================
def get_cpu_count():
    try:
        cpu_count = os.cpu_count()
        if cpu_count:
            return cpu_count
        
        with open('/proc/cpuinfo', 'r') as f:
            cpu_count = len([line for line in f if line.startswith('processor')])
            if cpu_count > 0:
                return cpu_count
        
        return 4
    except:
        return 4


def get_ram_info():
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        
        mem_total_kb = 0
        mem_free_kb = 0
        buffers_kb = 0
        cached_kb = 0
        
        for line in lines:
            if line.startswith('MemTotal:'):
                mem_total_kb = int(line.split()[1])
            elif line.startswith('MemFree:'):
                mem_free_kb = int(line.split()[1])
            elif line.startswith('Buffers:'):
                buffers_kb = int(line.split()[1])
            elif line.startswith('Cached:'):
                cached_kb = int(line.split()[1])
        
        mem_used_kb = mem_total_kb - mem_free_kb - buffers_kb - cached_kb
        
        total_mb = mem_total_kb / 1024
        used_mb = mem_used_kb / 1024
        
        percent = (used_mb / total_mb) * 100 if total_mb > 0 else 0
        
        return {
            "total_mb": round(total_mb,2),
            "used_mb": round(used_mb,2),
            "percent": round(percent,1)
        }
    except:
        return None


def get_package_cpu_usage(package_name):
    try:
        pid_str = run_root_cmd(f"pidof {package_name}")
        if not pid_str:
            return 0.0
        
        pids = pid_str.split()
        total_cpu = 0.0
        
        for pid in pids:
            stat1 = run_root_cmd(f"cat /proc/{pid}/stat 2>/dev/null")
            uptime1 = run_root_cmd("cat /proc/uptime 2>/dev/null")
            if not stat1 or not uptime1:
                continue
            
            parts1 = stat1.split()
            utime1 = int(parts1[13])
            stime1 = int(parts1[14])
            uptime_sec1 = float(uptime1.split()[0])
            
            time.sleep(0.2)
            
            stat2 = run_root_cmd(f"cat /proc/{pid}/stat 2>/dev/null")
            uptime2 = run_root_cmd("cat /proc/uptime 2>/dev/null")
            if not stat2 or not uptime2:
                continue
            
            parts2 = stat2.split()
            utime2 = int(parts2[13])
            stime2 = int(parts2[14])
            uptime_sec2 = float(uptime2.split()[0])
            
            proc_time = ((utime2+stime2)-(utime1+stime1)) / 100.0
            elapsed = uptime_sec2 - uptime_sec1
            
            if elapsed > 0:
                cpu = (proc_time / elapsed) * 100.0
                cpu /= get_cpu_count()
                total_cpu += min(cpu,100)
        
        return round(min(total_cpu,100),1)
    except:
        return 0.0


def get_package_ram_usage(package_name):
    try:
        pid_str = run_root_cmd(f"pidof {package_name}")
        if not pid_str:
            return None
        
        total_rss_kb = 0
        for pid in pid_str.split():
            status_content = run_root_cmd(f"cat /proc/{pid}/status 2>/dev/null")
            if not status_content:
                continue
            
            for line in status_content.splitlines():
                if line.startswith("VmRSS:"):
                    total_rss_kb += int(line.split()[1])
                    break
        
        if total_rss_kb == 0:
            return None
        
        rss_mb = total_rss_kb / 1024.0
        
        ram_info = get_ram_info()
        percent = (rss_mb / ram_info["total_mb"] * 100) if ram_info else 0
        
        return {
            "used_mb": round(rss_mb,2),
            "percent": round(percent,2)
        }
    except:
        return None


def get_all_packages_stats(pkgs):
    stats = {}
    
    for pkg, info in pkgs.items():
        cpu = get_package_cpu_usage(pkg)
        ram = get_package_ram_usage(pkg)
        
        stats[pkg] = {
            "username": info["username"],
            "cpu": cpu,
            "ram_mb": ram["used_mb"] if ram else 0,
            "ram_percent": ram["percent"] if ram else 0
        }
    
    return stats

# =========================
# ANDROID WINDOW GRID MANAGER
# =========================

WINDOW_CONFIG = {
    "width": 530,
    "height": 400,
    "per_row": 4,
    "enabled": False
}

def load_window_config():
    if not os.path.exists("window_config.json"):
        return WINDOW_CONFIG

    try:
        with open("window_config.json") as f:
            data = json.load(f)
            return data
    except:
        return WINDOW_CONFIG


def save_window_config(cfg):
    with open("window_config.json","w") as f:
        json.dump(cfg,f,indent=2)


def get_screen_size():
    """Baca resolusi layar aktual via wm size"""
    out = run_root_cmd("wm size")
    for line in out.splitlines():
        if "x" in line:
            try:
                part = line.split(":")[-1].strip()
                w, h = part.lower().split("x")
                return int(w.strip()), int(h.strip())
            except:
                pass
    return 1080, 2400


def get_running_roblox_tasks():
    """Ambil task ID Roblox dari dumpsys - parse format Android modern"""
    pkgs = load_packages()
    pkg_list = list(pkgs.keys())
    tasks = []

    # dumpsys activity recents lebih lengkap dari cmd activity tasks
    out = run_root_cmd("dumpsys activity recents")
    if not out:
        out = run_root_cmd("dumpsys activity tasks")

    current_task_id = None
    for line in out.splitlines():
        line = line.strip()

        # Deteksi baris task header: "* Recent #0: Task{... #123 ...}"
        if line.startswith("* Recent") or line.startswith("Task{") or "taskId=" in line:
            try:
                if "taskId=" in line:
                    current_task_id = int(line.split("taskId=")[1].split()[0].strip("#,}"))
                elif "#" in line:
                    # format: Task{abc123 #99 ...}
                    for part in line.split():
                        if part.startswith("#"):
                            current_task_id = int(part.strip("#,}:"))
                            break
            except:
                current_task_id = None

        # Cek apakah baris ini atau sekitarnya mengandung package Roblox
        for pkg in pkg_list:
            if pkg in line and current_task_id is not None:
                tasks.append(current_task_id)
                current_task_id = None  # jangan duplikat
                break

    # Fallback: cmd activity tasks (Android lama)
    if not tasks:
        out = run_root_cmd("cmd activity tasks")
        for line in out.splitlines():
            for pkg in pkg_list:
                if pkg in line and "id=" in line:
                    try:
                        tid = int(line.split("id=")[1].split()[0].strip(",:"))
                        tasks.append(tid)
                    except:
                        pass

    result = list(set(t for t in tasks if t and t > 0))
    add_log(f"[Grid] Found tasks: {result}")
    return result


def resize_task(task_id, x, y, w, h):
    """
    Resize + reposition window freeform via wm task size & move
    Android 10+ pakai: wm task (tidak ada) → gunakan am & cmd
    Yang BENAR untuk freeform: cmd activity resize-task ID WINDOWING_MODE left top right bottom
    WINDOWING_MODE=5 = freeform
    """
    right = x + w
    bottom = y + h

    # Step 1: pastikan freeform mode dulu
    run_root_cmd(f"cmd activity set-task-windowing-mode {task_id} 5 true")
    time.sleep(0.05)

    # Step 2: resize dengan bounds
    out = run_root_cmd(
        f"cmd activity resize-task {task_id} 5 "
        f"--bounds {x} {y} {right} {bottom}"
    )

    # Step 3: fallback syntax lama (tanpa --bounds flag)
    if not out or "Error" in out or "error" in out:
        run_root_cmd(
            f"cmd activity resize-task {task_id} "
            f"{x} {y} {right} {bottom}"
        )


def apply_grid_layout():
    """Terapkan grid layout ke semua window Roblox yang berjalan"""
    cfg = load_window_config()

    width  = cfg.get("width",  530)
    height = cfg.get("height", 400)
    per_row = cfg.get("per_row", 4)

    # Auto-size dari layar jika 0
    if width <= 0 or height <= 0:
        sw, sh = get_screen_size()
        width  = sw // max(per_row, 1)
        height = sh // 2
        add_log(f"[Grid] Auto size: {width}x{height}")

    tasks = get_running_roblox_tasks()
    if not tasks:
        add_log("[Grid] No Roblox window detected")
        return

    for i, task in enumerate(tasks):
        row = i // per_row
        col = i % per_row
        x = col * width
        y = row * height
        resize_task(task, x, y, width, height)
        time.sleep(0.2)

    add_log(f"[Grid] ✅ {len(tasks)} windows @ {per_row}/row {width}x{height}")


_grid_thread = None

def auto_fix_grid_loop():
    """Daemon thread: re-apply grid saat task berubah"""
    last_tasks = []
    while True:
        try:
            cfg = load_window_config()
            if cfg.get("enabled"):
                tasks = get_running_roblox_tasks()
                if sorted(tasks) != sorted(last_tasks):
                    apply_grid_layout()
                    last_tasks = list(tasks)
        except Exception as e:
            add_log(f"[Grid] loop error: {e}")
        time.sleep(4)


def start_grid_thread():
    """Mulai daemon thread auto-grid (idempotent)"""
    global _grid_thread
    if _grid_thread is None or not _grid_thread.is_alive():
        _grid_thread = threading.Thread(target=auto_fix_grid_loop, daemon=True)
        _grid_thread.start()
        add_log("[Grid] Auto thread started")


def configure_window_grid():

    cfg = load_window_config()

    clear_screen()

    print("="*60)
    print("🪟 ROBLOX WINDOW GRID CONFIG")
    print("="*60)

    print(f"Current Width   : {cfg['width']}")
    print(f"Current Height  : {cfg['height']}")
    print(f"Windows / Row   : {cfg['per_row']}")
    print(f"Auto Fix Layout : {cfg.get('enabled',False)}")
    print()

    w = input(f"Window Width (enter={cfg['width']}): ").strip()
    h = input(f"Window Height (enter={cfg['height']}): ").strip()
    r = input(f"Windows Per Row (enter={cfg['per_row']}): ").strip()

    auto = input("Enable auto layout fix? (y/n): ").lower().strip()

    if w:
        cfg["width"] = int(w)

    if h:
        cfg["height"] = int(h)

    if r:
        cfg["per_row"] = int(r)

    cfg["enabled"] = True if auto == "y" else False

    save_window_config(cfg)

    print("\n✅ Saved")

    if cfg.get("enabled"):
        start_grid_thread()
        print("🪟 Auto grid thread aktif")

    input("\nPress ENTER...")

# =========================
# DISCORD WEBHOOK
# =========================
def send_discord_webhook(webhook_url, title, message, color=None):
    """Kirim pesan ke Discord webhook - IMPROVED VERSION"""
    if not webhook_url or webhook_url == "":
        return False
    
    try:
        color = color or 16711680  # Red default
        
        # Build JSON payload dengan escaping yang benar
        payload = {
            "embeds": [{
                "title": str(title),
                "description": str(message),
                "color": int(color),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }]
        }
        
        # Convert ke JSON string
        payload_str = json.dumps(payload)
        
        # Escape single quotes untuk shell command
        payload_escaped = payload_str.replace("'", "'\"'\"'")
        
        # Build curl command
        cmd = f"curl -s -X POST -H 'Content-Type: application/json' -d '{payload_escaped}' '{webhook_url}'"
        
        # Execute command
        result = os.system(cmd)
        
        return result == 0
    except Exception as e:
        log(f"Webhook error: {e}")
        return False


def build_webhook_status_message(pkgs, cfg):
    stats = get_all_packages_stats(pkgs)
    ram_info = get_ram_info()
    cpu_cores = get_cpu_count()

    # Hitung total CPU (dibatasi max 100%)
    total_cpu = round(min(sum(s["cpu"] for s in stats.values()), 100.0), 1)

    # RAM persen system
    ram_percent = ram_info["percent"] if ram_info else 0

    # Hitung running
    running_count = 0
    total_count = len(pkgs)

    lines = []

    # SYSTEM
    lines.append("SYSTEM")
    lines.append(f"CPU {total_cpu}% / {cpu_cores} cores")
    lines.append(f"RAM {ram_percent}%")
    lines.append("")

    # ACCOUNTS
    for pkg, info in pkgs.items():
        status = determine_account_status(pkg, info, cfg)
        if status in ("in_game", "waiting"):
            running_count += 1

    lines.append("ACCOUNTS")
    lines.append(f"{running_count} Running / {total_count} Total")
    lines.append("")

    # DETAIL
    lines.append("DETAIL")

    for pkg, info in pkgs.items():
        username = info["username"]
        status = determine_account_status(pkg, info, cfg).upper()

        cpu = stats[pkg]["cpu"]
        ram_mb = stats[pkg]["ram_mb"]

        lines.append(f"{username:<10} CPU {cpu:<4}% RAM {int(ram_mb):<4}MB  {status}")

    return "\n".join(lines)


def clear_screen():
    """Clear screen dengan ANSI escape code"""
    print("\033[2J\033[H", end='')
    sys.stdout.flush()

def add_log(msg):
    """Tambah log ke buffer (max 5 baris)"""
    timestamp = time.strftime('%H:%M:%S')
    LOG_BUFFER.append(f"[{timestamp}] {msg}")

# =========================
# CONFIG
# =========================
def load_config():
    global EXECUTOR_TYPE
    
    default = {
        "game_id": "",
        "check_interval": 10,
        "first_check": 3,  # Grace period dalam menit (seperti PC version)
        "ingame_check": 2,
        "workspace_check_interval": 5,
        "json_suffix": "_checkyum.json",
        "startup_delay": 8,
        "restart_delay": 3,
        "autoexec_enabled": True,
        "webhook_url": "",
        "webhook_enabled": True,
        "webhook_interval": 10,
        "restart_interval": 0,
        "executor": "delta"  # delta or arceusx
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                loaded = json.load(f)
                default.update(loaded)
        except Exception as e:
            add_log(f"Config load error: {e}")
    
    # Set global executor type
    EXECUTOR_TYPE = default.get("executor", "delta")
    
    return default

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        add_log("Config saved")
    except Exception as e:
        add_log(f"Config save error: {e}")


# =========================
# PACKAGE DETECTION
# =========================
def get_roblox_packages():
    out = run_root_cmd("pm list packages | grep com.roblox")
    pkgs = []
    for line in out.splitlines():
        if line.startswith("package:"):
            pkgs.append(line.split(":")[1])
    return pkgs

def get_username_from_prefs(package):
    prefs = f"/data/data/{package}/shared_prefs/prefs.xml"
    xml = run_root_cmd(f"cat {prefs}")
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
        for c in root:
            if c.tag == "string" and c.attrib.get("name") == "username":
                return c.text.strip() if c.text else None
    except Exception as e:
        add_log(f"Prefs parse error {package}: {e}")
    return None

def auto_detect_and_save_packages():
    global EXECUTOR_TYPE
    
    clear_screen()
    print("AUTO DETECT ROBLOX PACKAGES\n")
    pkgs = get_roblox_packages()
    saved = {}

    for pkg in pkgs:
        print(f"Checking {pkg}...", end=" ")
        user = get_username_from_prefs(pkg)
        if user:
            # ArceusX menggunakan global workspace/autoexec
            if EXECUTOR_TYPE == "arceusx":
                package_info = {
                    "username": user,
                    "package_name": pkg,
                    "workspace_dir": "/storage/emulated/0/Arceus X/Workspace",
                    "autoexec_dir": "/storage/emulated/0/Arceus X/Autoexecute",
                    "cache_dir": None,  # ArceusX tidak butuh cache
                    "license_path": None
                }
            elif EXECUTOR_TYPE == "ronix":
                package_info = {
                    "username": user,
                    "package_name": pkg,
                    "workspace_dir": "/storage/emulated/0/RonixExploit/workspace",
                    "autoexec_dir": "/storage/emulated/0/RonixExploit/autoexecute",
                    "cache_dir": None,  # RonixExploit tidak butuh cache
                    "license_path": None
                }
            else:
                # Delta Executor (default)
                package_info = {
                    "username": user,
                    "package_name": pkg,
                    "workspace_dir": f"/storage/emulated/0/Android/data/{pkg}/files/gloop/external/Workspace",
                    "autoexec_dir": f"/storage/emulated/0/Android/data/{pkg}/files/gloop/external/Autoexecute",
                    "cache_dir": f"/storage/emulated/0/Android/data/{pkg}/files/gloop/external/Internals/Cache",
                    "license_path": f"/storage/emulated/0/Android/data/{pkg}/files/gloop/external/Internals/Cache"
                }
            saved[pkg] = package_info
            print(f"{user} ✓")
        else:
            print("SKIP")

    if saved:
        with open(PACKAGES_FILE, "w") as f:
            json.dump(saved, f, indent=2)
        print(f"\nSaved {len(saved)} packages")
    else:
        print("\nNo valid packages")

    input("\nEnter...")

def auto_setup_wizard():
    """Auto setup wizard - detect packages, set game id, choose executor"""
    global EXECUTOR_TYPE
    
    clear_screen()
    print("=" * 70)
    print("🚀 AUTO SETUP WIZARD")
    print("=" * 70)
    print()
    
    # Step 1: Auto detect packages
    print("📋 Step 1/3: Auto Detecting Packages...")
    print("-" * 70)
    
    pkgs_found = get_roblox_packages()
    detected = {}
    
    for pkg in pkgs_found:
        user = get_username_from_prefs(pkg)
        if user:
            detected[pkg] = {"username": user}
    
    if not detected:
        print("\n❌ No Roblox packages found!")
        input("\nPress ENTER to continue...")
        return
    
    print(f"\n✅ Found {len(detected)} package(s):")
    for i, (pkg, info) in enumerate(detected.items(), 1):
        print(f"  {i}. {info['username']} ({pkg})")
    
    time.sleep(2)
    
    # Step 2: Set Game ID
    print("\n" + "=" * 70)
    print("🎮 Step 2/3: Set Game ID")
    print("-" * 70)
    
    cfg = load_config()
    current_id = cfg.get("game_id", "")
    
    if current_id:
        print(f"Current Game ID: {current_id}")
        use_current = input("Use current Game ID? (y/n): ").strip().lower()
        if use_current == "y":
            game_id = current_id
        else:
            game_id = input("Enter new Game ID: ").strip()
    else:
        game_id = input("Enter Game ID: ").strip()
    
    if game_id:
        cfg["game_id"] = game_id
        print(f"✅ Game ID set to: {game_id}")
    
    time.sleep(1)
    
    # Step 3: Choose Executor
    print("\n" + "=" * 70)
    print("⚡ Step 3/3: Choose Executor")
    print("-" * 70)
    print("1. Delta Executor (default)")
    print("2. Arceus X")
    print("3. RonixExploit")
    print()
    
    executor_choice = input("Select executor (1/2/3, default=1): ").strip()
    
    if executor_choice == "2":
        cfg["executor"] = "arceusx"
        EXECUTOR_TYPE = "arceusx"
        print("✅ Executor set to: Arceus X")
        print()
        print("ℹ️  ArceusX Notes:")
        print("   - All accounts use shared workspace")
        print("   - Autoexec: /storage/emulated/0/Arceus X/Autoexecute")
        print("   - Workspace: /storage/emulated/0/Arceus X/Workspace")
        print("   - Cache copy feature disabled")
    elif executor_choice == "3":
        cfg["executor"] = "ronix"
        EXECUTOR_TYPE = "ronix"
        print("✅ Executor set to: RonixExploit")
        print()
        print("ℹ️  RonixExploit Notes:")
        print("   - All accounts use shared workspace")
        print("   - Autoexec: /storage/emulated/0/RonixExploit/autoexecute")
        print("   - Workspace: /storage/emulated/0/RonixExploit/workspace")
        print("   - Cache copy feature disabled")
    else:
        cfg["executor"] = "delta"
        EXECUTOR_TYPE = "delta"
        print("✅ Executor set to: Delta")
    
    save_config(cfg)
    
    # Now save packages with proper paths
    saved = {}
    for pkg, info in detected.items():
        user = info["username"]
        
        if EXECUTOR_TYPE == "arceusx":
            package_info = {
                "username": user,
                "package_name": pkg,
                "workspace_dir": "/storage/emulated/0/Arceus X/Workspace",
                "autoexec_dir": "/storage/emulated/0/Arceus X/Autoexecute",
                "cache_dir": None,
                "license_path": None
            }
        elif EXECUTOR_TYPE == "ronix":
            package_info = {
                "username": user,
                "package_name": pkg,
                "workspace_dir": "/storage/emulated/0/RonixExploit/workspace",
                "autoexec_dir": "/storage/emulated/0/RonixExploit/autoexecute",
                "cache_dir": None,
                "license_path": None
            }
        else:
            package_info = {
                "username": user,
                "package_name": pkg,
                "workspace_dir": f"/storage/emulated/0/Android/data/{pkg}/files/gloop/external/Workspace",
                "autoexec_dir": f"/storage/emulated/0/Android/data/{pkg}/files/gloop/external/Autoexecute",
                "cache_dir": f"/storage/emulated/0/Android/data/{pkg}/files/gloop/external/Internals/Cache",
                "license_path": f"/storage/emulated/0/Android/data/{pkg}/files/gloop/external/Internals/Cache"
            }
        saved[pkg] = package_info
    
    with open(PACKAGES_FILE, "w") as f:
        json.dump(saved, f, indent=2)
    
    # Summary
    print("\n" + "=" * 70)
    print("✅ AUTO SETUP COMPLETED!")
    print("=" * 70)
    print(f"📦 Packages: {len(saved)}")
    print(f"🎮 Game ID: {game_id}")
    print(f"⚡ Executor: {cfg['executor'].upper()}")
    print("=" * 70)
    
    input("\nPress ENTER to return to main menu...")

def load_packages():
    if not os.path.exists(PACKAGES_FILE):
        return {}
    try:
        with open(PACKAGES_FILE) as f:
            return json.load(f)
    except:
        return {}

# =========================
# CACHE FOLDER MANAGEMENT
# =========================
def find_cache_dir(package_name):
    """Cari folder Cache untuk package tertentu"""
    possible_paths = [
        f"/storage/emulated/0/Android/data/{package_name}/files/gloop/external/Internals/Cache",
        f"/sdcard/Android/data/{package_name}/files/gloop/external/Internals/Cache",
    ]
    
    for cache_dir in possible_paths:
        if not os.path.isdir(cache_dir):
            continue
        
        try:
            files = os.listdir(cache_dir)
            if files:
                return cache_dir
        except:
            pass
    
    return None

def copy_all_cache_files(source_cache_dir, dest_cache_dir, use_root=False):
    """
    Copy semua file dari folder Cache source ke destination
    
    Args:
        source_cache_dir: Path folder Cache sumber
        dest_cache_dir: Path folder Cache tujuan
        use_root: Gunakan root command jika True
    
    Returns:
        tuple: (success_count, failed_count, error_msg)
    """
    success = 0
    failed = 0
    error_msg = ""
    
    try:
        # Pastikan source folder ada
        if not os.path.isdir(source_cache_dir):
            return 0, 0, f"Source folder tidak ditemukan: {source_cache_dir}"
        
        # Buat destination folder jika belum ada
        if use_root:
            run_root_cmd(f"mkdir -p '{dest_cache_dir}'")
        else:
            os.makedirs(dest_cache_dir, exist_ok=True)
        
        # List semua file di source
        try:
            source_files = os.listdir(source_cache_dir)
        except PermissionError:
            # Jika tidak bisa list dengan Python, coba dengan root
            if use_root:
                file_list = run_root_cmd(f"ls '{source_cache_dir}'")
                source_files = file_list.split('\n') if file_list else []
            else:
                return 0, 0, "Permission denied - coba gunakan root mode"
        
        if not source_files:
            return 0, 0, "Source folder kosong"
        
        # Copy setiap file
        for filename in source_files:
            if not filename.strip():
                continue
                
            source_file = os.path.join(source_cache_dir, filename)
            dest_file = os.path.join(dest_cache_dir, filename)
            
            try:
                if use_root:
                    result = run_root_cmd(f"cp '{source_file}' '{dest_file}'")
                    if result == "":  # Empty string means success
                        success += 1
                    else:
                        failed += 1
                        error_msg += f"Failed to copy {filename}; "
                else:
                    shutil.copy2(source_file, dest_file)
                    success += 1
            except Exception as e:
                failed += 1
                error_msg += f"Error copying {filename}: {str(e)}; "
        
        return success, failed, error_msg
    
    except Exception as e:
        return 0, 0, f"General error: {str(e)}"

def copy_license_to_all_packages():
    """Menu untuk copy cache files ke semua package - dengan pilihan file"""
    global EXECUTOR_TYPE
    
    # Check executor type
    if EXECUTOR_TYPE in ["arceusx", "ronix"]:
        clear_screen()
        print("=" * 70)
        print("⚠️  CACHE COPY NOT AVAILABLE")
        print("=" * 70)
        executor_name = "Arceus X" if EXECUTOR_TYPE == "arceusx" else "RonixExploit"
        print(f"\nCache copy feature is not available for {executor_name} executor.")
        print(f"{executor_name} uses global autoexec and workspace directories.")
        input("\nPress ENTER...")
        return
    
    clear_screen()
    print("=" * 70)
    print("🗂️ COPY CACHE FILES TO ALL PACKAGES")
    print("=" * 70)
    
    pkgs = load_packages()
    if not pkgs:
        add_log("No packages found")
        input("\nPress ENTER...")
        return
    
    # Cari source package yang punya cache
    print("\n📋 Available packages with cache:")
    available = []
    for i, (pkg, info) in enumerate(pkgs.items(), 1):
        cache_dir = find_cache_dir(pkg)
        if cache_dir:
            available.append((pkg, info, cache_dir))
            print(f"  {i}. {info['username']} (Package: {pkg})")
    
    if not available:
        add_log("❌ No packages with cache files found!")
        input("\nPress ENTER...")
        return
    
    # Pilih source
    choice = input(f"\n📌 Select source package (1-{len(available)}) or Enter for first: ").strip()
    
    if choice == "":
        source_pkg, source_info, source_cache_dir = available[0]
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(available):
                source_pkg, source_info, source_cache_dir = available[idx]
            else:
                add_log("Invalid choice")
                input("\nPress ENTER...")
                return
        except ValueError:
            add_log("Invalid input")
            input("\nPress ENTER...")
            return
    
    add_log(f"\n✅ Source: {source_info['username']} ({source_pkg})")
    add_log(f"📂 Source cache: {source_cache_dir}")
    
    # Tampilkan file yang tersedia
    try:
        files = os.listdir(source_cache_dir)
        if not files:
            add_log("❌ No files found in cache!")
            input("\nPress ENTER...")
            return
        
        print("\n📋 Available files:")
        for i, f in enumerate(sorted(files), 1):
            print(f"  {i}. {f}")
    except Exception as e:
        add_log(f"Error listing files: {e}")
        input("\nPress ENTER...")
        return
    
    # Pilihan: semua atau spesifik
    print("\n" + "-" * 70)
    copy_mode = input("📌 Copy ALL files or SELECT specific? (all/select, default=all): ").strip().lower()
    
    files_to_copy = []
    
    if copy_mode == "select":
        # Mode pilih file tertentu
        print("\n💡 Enter file names to copy (one per line, press ENTER twice to finish):")
        print("   Or enter numbers separated by comma (e.g., 1,3,5)")
        
        file_input = input("\n📌 Files: ").strip()
        
        if not file_input:
            add_log("Cancelled")
            input("\nPress ENTER...")
            return
        
        # Cek apakah input berupa angka
        if ',' in file_input or file_input.isdigit():
            # Input berupa nomor
            try:
                if ',' in file_input:
                    indices = [int(x.strip()) - 1 for x in file_input.split(',')]
                else:
                    indices = [int(file_input) - 1]
                
                sorted_files = sorted(files)
                for idx in indices:
                    if 0 <= idx < len(sorted_files):
                        files_to_copy.append(sorted_files[idx])
                
                if not files_to_copy:
                    add_log("No valid files selected")
                    input("\nPress ENTER...")
                    return
            except ValueError:
                add_log("Invalid input")
                input("\nPress ENTER...")
                return
        else:
            # Input berupa nama file
            files_to_copy = [f.strip() for f in file_input.split(',')]
            # Validasi file exists
            files_to_copy = [f for f in files_to_copy if f in files]
            
            if not files_to_copy:
                add_log("No valid files found")
                input("\nPress ENTER...")
                return
    else:
        # Copy semua file
        files_to_copy = files
    
    # Tampilkan file yang akan di-copy
    add_log(f"\n📄 Files to copy: {len(files_to_copy)}")
    for f in files_to_copy[:5]:
        add_log(f"  - {f}")
    if len(files_to_copy) > 5:
        add_log(f"  ... and {len(files_to_copy) - 5} more files")
    
    confirm = input("\n⚠️ Copy to ALL other packages? (y/n): ").strip().lower()
    if confirm != 'y':
        add_log("Cancelled")
        input("\nPress ENTER...")
        return
    
    # Tanya mode
    mode = input("Use root mode? (y/n, default=n): ").strip().lower()
    use_root = (mode == 'y')
    
    print("\n" + "=" * 70)
    add_log("Starting copy process...")
    print("=" * 70)
    
    total_success = 0
    total_failed = 0
    
    for pkg, info in pkgs.items():
        if pkg == source_pkg:
            continue
        
        username = info['username']
        dest_cache_dir = find_cache_dir(pkg)
        
        if not dest_cache_dir:
            # Coba buat folder
            dest_cache_dir = f"/storage/emulated/0/Android/data/{pkg}/files/gloop/external/Internals/Cache"
            if use_root:
                run_root_cmd(f"mkdir -p '{dest_cache_dir}'")
            else:
                try:
                    os.makedirs(dest_cache_dir, exist_ok=True)
                except:
                    pass
        
        add_log(f"\n📦 Copying to: {username}")
        
        # Copy hanya file yang dipilih
        success = 0
        failed = 0
        error_msg = ""
        
        for filename in files_to_copy:
            src_file = os.path.join(source_cache_dir, filename)
            dst_file = os.path.join(dest_cache_dir, filename)
            
            try:
                if use_root:
                    cmd = f"cp '{src_file}' '{dst_file}'"
                    result = run_root_cmd(cmd)
                    if run_root_cmd(f"test -f '{dst_file}' && echo 'ok'") == "ok":
                        success += 1
                    else:
                        failed += 1
                else:
                    shutil.copy2(src_file, dst_file)
                    success += 1
            except Exception as e:
                failed += 1
                error_msg += f"{filename}: {str(e)}; "
        
        if success > 0:
            add_log(f"  ✅ Success: {success} files")
            total_success += success
        if failed > 0:
            add_log(f"  ❌ Failed: {failed} files")
            total_failed += failed
        if error_msg:
            add_log(f"  ⚠️ Errors: {error_msg[:100]}")
    
    print("\n" + "=" * 70)
    add_log(f"📊 RESULTS:")
    add_log(f"  ✅ Total success: {total_success} files")
    add_log(f"  ❌ Total failed: {total_failed} files")
    print("=" * 70)
    
    input("\nPress ENTER...")

def view_license_status():
    """Lihat status cache untuk semua package"""
    global EXECUTOR_TYPE
    
    clear_screen()
    print("=" * 70)
    print("📊 CACHE FOLDER STATUS")
    print("=" * 70)
    
    # Check executor type
    if EXECUTOR_TYPE in ["arceusx", "ronix"]:
        executor_name = "Arceus X" if EXECUTOR_TYPE == "arceusx" else "RonixExploit"
        print(f"\n⚠️  Cache feature not available for {executor_name} executor")
        print(f"   {executor_name} uses global autoexec directory")
        input("\nPress ENTER...")
        return
    
    pkgs = load_packages()
    if not pkgs:
        add_log("No packages found")
        input("\nPress ENTER...")
        return
    
    print(f"\n{'No.':<4} {'Username':<20} {'Cache Status':<15} {'File Count':<12}")
    print("-" * 70)
    
    for i, (pkg, info) in enumerate(pkgs.items(), 1):
        username = info['username']
        cache_dir = find_cache_dir(pkg)
        
        if cache_dir:
            try:
                files = os.listdir(cache_dir)
                file_count = len(files)
                status = "✅ Found"
                count_str = str(file_count)
            except:
                status = "⚠️ Error"
                count_str = "N/A"
        else:
            status = "❌ Not Found"
            count_str = "0"
        
        print(f"{i:<4} {username:<20} {status:<15} {count_str:<12}")
    
    print("=" * 70)
    input("\nPress ENTER...")

# =========================
# SCRIPT MANAGEMENT
# =========================
def list_executor_scripts(package_info):
    """Mendapatkan daftar script untuk paket tertentu"""
    global EXECUTOR_TYPE
    autoexec_dir = package_info.get("autoexec_dir", "")
    
    if not autoexec_dir:
        return []
    
    # Untuk ArceusX dan RonixExploit, gunakan root command
    if EXECUTOR_TYPE in ["arceusx", "ronix"]:
        cmd = f"ls {autoexec_dir}/*.lua {autoexec_dir}/*.txt 2>/dev/null"
        result = run_root_cmd(cmd)
        if not result:
            return []
        
        scripts = []
        for path in result.splitlines():
            scripts.append(os.path.basename(path.strip()))
        return sorted(scripts)
    else:
        # Delta: gunakan Python biasa
        if not os.path.exists(autoexec_dir):
            return []
        
        try:
            return [
                f for f in os.listdir(autoexec_dir)
                if f.endswith((".lua", ".txt"))
            ]
        except:
            return []

def add_script_to_all_packages():
    """Menambahkan script ke semua paket"""
    global EXECUTOR_TYPE
    
    clear_screen()
    print("=" * 70)
    print("📝 ADD SCRIPT TO ALL PACKAGES")
    print("=" * 70)
    
    pkgs = load_packages()
    if not pkgs:
        add_log("No packages found")
        input("\nPress ENTER...")
        return
    
    print(f"\nFound {len(pkgs)} packages:")
    for i, (pkg, info) in enumerate(pkgs.items(), 1):
        print(f"  {i}. {info['username']}")
    
    print("\n" + "-" * 70)
    print("Paste script content (press ENTER twice to finish):")
    
    lines = []
    empty_count = 0
    
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        
        if line == "":
            empty_count += 1
            if empty_count >= 2:
                break
        else:
            empty_count = 0
            lines.append(line)
    
    script_content = "\n".join(lines).strip()
    
    if not script_content:
        add_log("Empty script, cancelled")
        input("\nPress ENTER...")
        return
    
    script_name = input("\n📌 Script name (without .lua): ").strip()
    if not script_name:
        add_log("Invalid script name")
        input("\nPress ENTER...")
        return
    
    if not script_name.endswith(".lua"):
        script_name += ".lua"
    
    print("\n" + "=" * 70)
    add_log("Adding script to all packages...")
    print("=" * 70)
    
    success_count = 0
    failed_count = 0
    
    for pkg, info in pkgs.items():
        autoexec_dir = info.get("autoexec_dir", "")
        
        if not autoexec_dir:
            add_log(f"❌ {info['username']}: No autoexec dir")
            failed_count += 1
            continue
        
        # Untuk ArceusX dan RonixExploit, gunakan root command
        if EXECUTOR_TYPE in ["arceusx", "ronix"]:
            # Buat folder dengan root
            mkdir_cmd = f"mkdir -p {autoexec_dir}"
            run_root_cmd(mkdir_cmd)
            
            # Write script dengan root
            script_path = f"{autoexec_dir}/{script_name}"
            escaped_content = script_content.replace("'", "'\"'\"'")
            write_cmd = f"echo '{escaped_content}' > {script_path}"
            run_root_cmd(write_cmd)
            
            # Set permissions
            chmod_cmd = f"chmod 644 {script_path}"
            run_root_cmd(chmod_cmd)
            
            add_log(f"✅ {info['username']}")
            success_count += 1
        else:
            # Delta: gunakan Python biasa
            if not os.path.exists(autoexec_dir):
                try:
                    os.makedirs(autoexec_dir, exist_ok=True)
                except Exception as e:
                    add_log(f"❌ {info['username']}: Can't create dir - {e}")
                    failed_count += 1
                    continue
            
            # Tulis script
            script_path = os.path.join(autoexec_dir, script_name)
            try:
                with open(script_path, 'w') as f:
                    f.write(script_content)
                add_log(f"✅ {info['username']}")
                success_count += 1
            except Exception as e:
                add_log(f"❌ {info['username']}: {e}")
                failed_count += 1
    
    print("\n" + "=" * 70)
    add_log(f"📊 RESULTS: {success_count} success, {failed_count} failed")
    print("=" * 70)
    input("\nPress ENTER...")

def delete_script_from_all_packages():
    """Hapus script dari semua package"""
    global EXECUTOR_TYPE
    
    clear_screen()
    print("=" * 70)
    print("🗑️ DELETE SCRIPT FROM ALL PACKAGES")
    print("=" * 70)
    
    pkgs = load_packages()
    if not pkgs:
        add_log("No packages found")
        input("\nPress ENTER...")
        return
    
    # Tampilkan script yang ada
    print("\n📋 Available scripts:")
    all_scripts = set()
    
    for pkg, info in pkgs.items():
        scripts = list_executor_scripts(info)
        all_scripts.update(scripts)
    
    if not all_scripts:
        add_log("No scripts found in any package")
        input("\nPress ENTER...")
        return
    
    for i, script in enumerate(sorted(all_scripts), 1):
        print(f"  {i}. {script}")
    
    # Pilih script
    script_name = input("\n📌 Script name to delete: ").strip()
    if not script_name:
        add_log("Cancelled")
        input("\nPress ENTER...")
        return
    
    confirm = input(f"\n⚠️ Delete '{script_name}' from ALL packages? (y/n): ").strip().lower()
    if confirm != 'y':
        add_log("Cancelled")
        input("\nPress ENTER...")
        return
    
    # Hapus dari semua package
    success_count = 0
    not_found_count = 0
    
    for pkg, info in pkgs.items():
        autoexec_dir = info.get("autoexec_dir", "")
        if not autoexec_dir:
            continue
        
        # Untuk ArceusX dan RonixExploit, gunakan root command
        if EXECUTOR_TYPE in ["arceusx", "ronix"]:
            script_path = f"{autoexec_dir}/{script_name}"
            
            # Check if exists
            check_cmd = f"test -f {script_path} && echo 'exists'"
            check_result = run_root_cmd(check_cmd)
            
            if check_result.strip() == "exists":
                # Delete dengan root
                rm_cmd = f"rm -f {script_path}"
                run_root_cmd(rm_cmd)
                add_log(f"✅ {info['username']}")
                success_count += 1
            else:
                not_found_count += 1
        else:
            # Delta: gunakan Python biasa
            script_path = os.path.join(autoexec_dir, script_name)
            
            if os.path.exists(script_path):
                try:
                    os.remove(script_path)
                    add_log(f"✅ {info['username']}")
                    success_count += 1
                except Exception as e:
                    add_log(f"❌ {info['username']}: {e}")
            else:
                not_found_count += 1
    
    print("\n" + "=" * 70)
    add_log(f"📊 RESULTS: {success_count} deleted, {not_found_count} not found")
    print("=" * 70)
    input("\nPress ENTER...")

def view_scripts_all_packages():
    """Lihat semua script di semua package"""
    clear_screen()
    print("=" * 70)
    print("👁️ VIEW SCRIPTS IN ALL PACKAGES")
    print("=" * 70)
    
    pkgs = load_packages()
    if not pkgs:
        add_log("No packages found")
        input("\nPress ENTER...")
        return
    
    for i, (pkg, info) in enumerate(pkgs.items(), 1):
        username = info['username']
        scripts = list_executor_scripts(info)
        
        print(f"\n{i}. {username}")
        if scripts:
            for script in scripts:
                print(f"   📜 {script}")
        else:
            print("   (No scripts)")
    
    print("\n" + "=" * 70)
    input("\nPress ENTER...")

# =========================
# ROBLOX APP CONTROL
# =========================
def is_app_running(package):
    out = run_root_cmd(f"pidof {package}")
    return bool(out.strip())

def start_app(package, game_id):
    intent = f"am start -n {package}/com.roblox.client.ActivityProtocolLaunch -d roblox://placeID={game_id}"
    run_root_cmd(intent)

def stop_app(package):
    run_root_cmd(f"am force-stop {package}")

# =========================
# JSON WORKSPACE MONITORING (seperti PC version)
# =========================
def get_workspace_json_path(package_info, username, cfg):
    """Mendapatkan path workspace JSON"""
    workspace_dir = package_info.get("workspace_dir", 
                     f"/storage/emulated/0/Android/data/com.roblox.client/files/gloop/external/Workspace")
    os.makedirs(workspace_dir, exist_ok=True)
    return f"{workspace_dir}/{username}{cfg['json_suffix']}"

def get_json_time_diff(package_info, username, cfg):
    """
    Mendapatkan selisih waktu sejak JSON terakhir update
    Returns: float (seconds) atau None jika JSON tidak ada
    """
    json_path = get_workspace_json_path(package_info, username, cfg)
    
    if not os.path.exists(json_path):
        return None
    
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
        
        # Cari field timestamp
        timestamp_field = None
        for field in ["timestamp", "time", "last_update", "updated_at"]:
            if field in data:
                timestamp_field = data[field]
                break
        
        if timestamp_field:
            # Parse timestamp
            if isinstance(timestamp_field, (int, float)):
                json_time = timestamp_field
            elif isinstance(timestamp_field, str):
                # Coba parse ISO format
                if "T" in timestamp_field and "Z" in timestamp_field:
                    dt_str = timestamp_field.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(dt_str)
                    json_time = dt.timestamp()
                else:
                    return None
            else:
                return None
            
            # Hitung selisih dengan waktu sekarang
            current_time = time.time()
            time_diff = current_time - json_time
            return time_diff
        else:
            # Fallback ke modification time
            mtime = os.path.getmtime(json_path)
            return time.time() - mtime
            
    except Exception as e:
        return None

def check_json_running(package_info, username, cfg):
    """
    Check apakah JSON sedang berjalan (< 60 detik seperti PC version)
    """
    time_diff = get_json_time_diff(package_info, username, cfg)
    
    if time_diff is None:
        return False
    
    return time_diff <= 60

def determine_account_status(pkg, package_info, cfg):
    """
    Menentukan status akun berdasarkan logika PC version:
    - offline: Client tidak berjalan
    - waiting: Launch baru, menunggu first check
    - in_game: JSON aktif
    - needs_kill: JSON tidak aktif setelah melewati first check / grace period
    """
    username = package_info["username"]
    
    # Initialize state jika belum ada
    if pkg not in ACCOUNT_STATE:
        ACCOUNT_STATE[pkg] = {
            "launch_time": None,
            "json_start_time": None,
            "json_active": False,
            "last_status": "offline"
        }
    
    acc = ACCOUNT_STATE[pkg]
    
    # 1. Jika client tidak berjalan → OFFLINE
    if not is_app_running(pkg):
        # Reset semua timer saat offline
        acc["json_active"] = False
        acc["json_start_time"] = None
        acc["last_status"] = "offline"
        return "offline"
    
    # 2. Client berjalan, cek JSON
    json_running = check_json_running(package_info, username, cfg)
    current_time = time.time()
    first_check_seconds = cfg["first_check"] * 60
    
    # 3. Jika JSON berjalan
    if json_running:
        # Update JSON timer setiap kali JSON aktif (PENTING!)
        acc["json_active"] = True
        acc["json_start_time"] = current_time
        acc["last_status"] = "in_game"
        return "in_game"
    
    # 4. JSON tidak berjalan - cek kondisi
    launch_time = acc.get("launch_time")
    json_active = acc.get("json_active", False)
    json_start_time = acc.get("json_start_time")
    
    # 4a. Jika baru launch, masih dalam grace period → WAITING
    if launch_time is not None:
        time_since_launch = current_time - launch_time
        if time_since_launch < first_check_seconds:
            acc["last_status"] = "waiting"
            return "waiting"
    
    # 4b. Jika JSON pernah aktif tapi sekarang mati (hop server, dll)
    if json_active and json_start_time is not None:
        ingame_check_seconds = cfg.get("ingame_check", 2) * 60  # pakai ingame_check, bukan first_check
        time_since_json_stop = current_time - json_start_time
        if time_since_json_stop >= ingame_check_seconds:
            acc["last_status"] = "needs_kill"
            return "needs_kill"
        else:
            acc["last_status"] = "in_game"
            return "in_game"
    
    # 4c. Launch sudah lama, JSON belum pernah aktif → NEEDS KILL
    if launch_time is not None:
        time_since_launch = current_time - launch_time
        if time_since_launch >= first_check_seconds:
            acc["last_status"] = "needs_kill"
            return "needs_kill"
    
    # 4d. Kondisi tidak jelas → WAITING
    acc["last_status"] = "waiting"
    return "waiting"

# =========================
# DISPLAY - TABEL RAPI + LOG ROLLING
# =========================
def display_monitor_screen(pkgs, cfg):
    """
    Display monitor screen dengan format:
    - Header + Tabel
    - 5 baris log terakhir
    """
    clear_screen()
    
    # Header
    print("=" * 80)
    print(" " * 30 + "🎮 ROBLOX OVA MONITOR")
    print("=" * 80)
    print()
    
    # Tabel Header
    print(f"{'No':<4} {'Username':<16} {'Package':<22} {'Status':<30}")
    print("-" * 80)
    
    # Tabel Content
    all_in_game = True
    for i, (pkg, info) in enumerate(pkgs.items(), 1):
        username = info["username"]
        pkg_short = pkg.split('.')[-1][:20]  # Singkat package name
        
        status = determine_account_status(pkg, info, cfg)
        
        # Format status display
        if status == "in_game":
            time_diff = get_json_time_diff(info, username, cfg)
            if time_diff is not None:
                status_display = f"🟢 In Game | JSON: {int(time_diff)}s"
            else:
                status_display = "🟢 In Game"
        elif status == "offline":
            status_display = "⚫ Offline"
            all_in_game = False
        elif status == "waiting":
            launch_time = ACCOUNT_STATE[pkg].get("launch_time")
            if launch_time:
                first_check_seconds = cfg["first_check"] * 60
                remaining = max(0, int(first_check_seconds - (time.time() - launch_time)))
                status_display = f"🟡 Waiting ({remaining}s)"
            else:
                status_display = "🟡 Waiting"
            all_in_game = False
        elif status == "needs_kill":
            status_display = "🔴 JSON Dead - Killing"
            all_in_game = False
        else:
            status_display = f"❓ {status}"
            all_in_game = False
        
        # Potong jika terlalu panjang
        username_display = username[:14] if len(username) <= 14 else username[:13] + "."
        status_display = status_display[:28] if len(status_display) <= 28 else status_display[:27] + "."
        
        print(f"{i:<4} {username_display:<16} {pkg_short:<22} {status_display:<30}")
    
    # Footer
    print("-" * 80)
    timestamp = time.strftime('%H:%M:%S')
    status_text = "✅ ALL IN GAME" if all_in_game else "❌ NEEDS ATTENTION"
    print(f"🕒 {timestamp} | 📦 Total: {len(pkgs)} | {status_text}")
    print("=" * 80)
    print()
    
    # Console Log (5 baris terakhir)
    print("📋 CONSOLE LOG:")
    print("-" * 80)
    if LOG_BUFFER:
        for log_line in LOG_BUFFER:
            print(log_line)
    else:
        print("[No logs yet]")
    print("-" * 80)
    print()

# =========================
# SEQUENTIAL STARTUP
# =========================
def sequential_startup(pkgs, cfg):
    """Startup semua package secara sequential"""
    success_count = 0

    add_log("=" * 40)
    add_log("STOPPING ALL EXISTING INSTANCES")
    add_log("=" * 40)

    for pkg, info in pkgs.items():
        if is_app_running(pkg):
            stop_app(pkg)
            # Reset state
            if pkg in ACCOUNT_STATE:
                ACCOUNT_STATE[pkg]["launch_time"] = None
                ACCOUNT_STATE[pkg]["json_start_time"] = None
                ACCOUNT_STATE[pkg]["json_active"] = False

    time.sleep(cfg["restart_delay"] * 2)

    for i, (pkg, info) in enumerate(pkgs.items(), 1):
        username = info["username"]

        add_log(f"Starting {i}/{len(pkgs)}: {username}")

        start_app(pkg, cfg["game_id"])
        
        # Set launch time
        ACCOUNT_STATE.setdefault(pkg, {})
        ACCOUNT_STATE[pkg]["launch_time"] = time.time()
        ACCOUNT_STATE[pkg]["json_active"] = False
        ACCOUNT_STATE[pkg]["json_start_time"] = None
        
        time.sleep(cfg["startup_delay"])

        # Tunggu sampai in_game atau timeout
        max_wait = cfg["first_check"] * 60 + 30  # Grace period + buffer
        start_time = time.time()
        
        while (time.time() - start_time) < max_wait:
            status = determine_account_status(pkg, info, cfg)
            
            if status == "in_game":
                add_log(f"✅ {username} is IN GAME")
                success_count += 1
                break
            elif status == "needs_kill":
                add_log(f"❌ {username} FAILED (JSON dead)")
                break
            
            time.sleep(cfg["workspace_check_interval"])
        else:
            add_log(f"⏱️ {username} TIMEOUT waiting")

        if i < len(pkgs):
            time.sleep(3)

    add_log("=" * 40)
    add_log(f"📊 RESULTS: {success_count}/{len(pkgs)} accounts in game")
    add_log("=" * 40)

    return success_count

# =========================
# MONITOR LOOP
# =========================
def monitor():
    """Main monitor loop"""
    global monitor_active

    cfg = load_config()
    pkgs = load_packages()

    if not cfg["game_id"]:
        add_log("Game ID not set")
        input("Enter...")
        return

    if not pkgs:
        add_log("No packages found")
        input("Enter...")
        return

    monitor_active = True

    threading.Thread(target=auto_fix_grid_loop,daemon=True).start()

    # Sequential startup
    add_log("STARTING SEQUENTIAL STARTUP")
    time.sleep(2)

    online_count = sequential_startup(pkgs, cfg)

    if online_count == 0:
        add_log("No accounts went online")
        input("Enter...")
        return

    add_log(f"{online_count} accounts online, starting monitor...")
    time.sleep(3)

    # Webhook startup jika enabled
    if cfg.get("webhook_enabled") and cfg.get("webhook_url"):
        message = build_webhook_status_message(pkgs, cfg)
        send_discord_webhook(
            cfg["webhook_url"],
            "🚀 Monitor Started",
            message,
            3066993
        )

    try:
        cycle_count = 0
        last_webhook_time = time.time()
        last_restart_time = time.time()
        
        while monitor_active:
            cycle_count += 1
            
            # Display tabel
            display_monitor_screen(pkgs, cfg)
            
            # Monitor dan handle status
            for pkg, info in pkgs.items():
                username = info["username"]
                status = determine_account_status(pkg, info, cfg)
                
                # HANYA handle status yang perlu action
                if status == "needs_kill":
                    add_log(f"🔴 {username}: JSON DEAD → KILLING & RESTART")
                    stop_app(pkg)
                    time.sleep(cfg["restart_delay"])
                    
                    # Reset state
                    ACCOUNT_STATE[pkg]["launch_time"] = time.time()
                    ACCOUNT_STATE[pkg]["json_start_time"] = None
                    ACCOUNT_STATE[pkg]["json_active"] = False
                    
                    # Launch ulang
                    start_app(pkg, cfg["game_id"])
                    time.sleep(cfg["startup_delay"])
                
                elif status == "offline":
                    add_log(f"⚫ {username}: OFFLINE → LAUNCHING")
                    
                    # Reset state
                    ACCOUNT_STATE[pkg]["launch_time"] = time.time()
                    ACCOUNT_STATE[pkg]["json_start_time"] = None
                    ACCOUNT_STATE[pkg]["json_active"] = False
                    
                    # Launch
                    start_app(pkg, cfg["game_id"])
                    time.sleep(cfg["startup_delay"])
            
            # Webhook interval
            current_time = time.time()
            webhook_interval_seconds = cfg.get("webhook_interval", 10) * 60
            
            if cfg.get("webhook_enabled", False) and cfg.get("webhook_url", ""):
                if (current_time - last_webhook_time) >= webhook_interval_seconds:
                    message = build_webhook_status_message(pkgs, cfg)
                    send_discord_webhook(
                        cfg["webhook_url"],
                        "📊 Status Update",
                        message,
                        3447003
                    )
                    last_webhook_time = current_time
                    add_log("📡 Webhook sent")
            
            # Restart interval
            restart_interval_seconds = cfg.get("restart_interval", 0) * 60
            if restart_interval_seconds > 0:
                if (current_time - last_restart_time) >= restart_interval_seconds:
                    add_log("⏰ Auto restart time reached")
                    
                    if cfg.get("webhook_enabled", False) and cfg.get("webhook_url", ""):
                        send_discord_webhook(
                            cfg["webhook_url"],
                            "🔄 Auto Restart",
                            "Restarting all Roblox packages...",
                            16776960
                        )
                    
                    restart_all_roblox(pkgs, cfg)
                    last_restart_time = current_time
            
            time.sleep(cfg["check_interval"])
    
    except Exception as e:
        add_log(f"Monitor error: {e}")
        
        if cfg.get("webhook_enabled", False) and cfg.get("webhook_url", ""):
            send_discord_webhook(
                cfg["webhook_url"],
                "❌ Monitor Error",
                f"Error occurred: {str(e)}",
                16711680
            )
        
        input("Press ENTER...")
    finally:
        monitor_active = False

def restart_all_roblox(pkgs, cfg):
    """Restart semua Roblox package"""
    add_log("=" * 40)
    add_log("RESTARTING ALL ROBLOX PACKAGES")
    add_log("=" * 40)
    
    # Stop semua
    for pkg, info in pkgs.items():
        if is_app_running(pkg):
            add_log(f"Stopping {info['username']}")
            stop_app(pkg)
    
    add_log("Waiting before restart...")
    time.sleep(cfg["restart_delay"] * 2)
    
    # Start semua sequential
    return sequential_startup(pkgs, cfg)

# =========================
# MENU
# =========================
def menu():
    """Main menu"""
    while True:
        clear_screen()
        cfg = load_config()
        pkgs = load_packages()
        
        print("=" * 70)
        print("🤖 ROBLOX MULTI-PACKAGE MANAGER (Termux/Cloudphone) v2")
        print("=" * 70)
        print(f"🎮 Game ID      : {cfg.get('game_id', 'Not set')}")
        print(f"⚡ Executor     : {cfg.get('executor', 'delta').upper()}")
        print(f"📦 Packages     : {len(pkgs)}")
        print(f"⏱️ First Check  : {cfg.get('first_check', 3)} minutes")
        
        # Webhook info
        webhook_status = "✅" if cfg.get("webhook_enabled", False) else "❌"
        print(f"📡 Webhook      : {webhook_status}")
        if cfg.get("webhook_url", ""):
            webhook_display = cfg["webhook_url"][:40] + "..." if len(cfg["webhook_url"]) > 40 else cfg["webhook_url"]
            print(f"   URL          : {webhook_display}")
            print(f"   Interval     : {cfg.get('webhook_interval', 10)} minutes")
            restart_text = f"{cfg.get('restart_interval', 0)} minutes" if cfg.get('restart_interval', 0) > 0 else "Disabled"
            print(f"   Auto Restart : {restart_text}")
        
        if pkgs:
            print("\n📋 Registered Packages:")
            for i, (pkg, info) in enumerate(pkgs.items(), 1):
                if EXECUTOR_TYPE in ["arceusx", "ronix"]:
                    scripts_count = len(list_executor_scripts(info))
                    print(f"  {i}. {info['username']} (📜:{scripts_count})")
                else:
                    cache_dir = find_cache_dir(pkg)
                    cache_status = "✅" if cache_dir else "❌"
                    scripts_count = len(list_executor_scripts(info))
                    print(f"  {i}. {info['username']} (🗂️:{cache_status} 📜:{scripts_count})")
        
        print("=" * 70)
        print("1. 🚀 Start Monitor (All Packages)")
        print("2. 🚀 Auto Setup (Detect + Game ID + Executor)")
        print("3. 🔍 Auto Detect Packages")
        print("4. 📡 Configure Webhook")
        print("5. ⏱️ Set First Check Time")
        print("-" * 40)
        print("6. 📝 Add Script to ALL Packages")
        print("7. 🗑️ Delete Script from ALL Packages")
        print("8. 👁️ View Scripts in ALL Packages")
        print("-" * 40)
        print("9. 🗂️ Copy ALL Cache Files to ALL Packages")
        print("10. 📊 View Cache Folder Status")
        print("11. 🪟 Configure Roblox Window Grid")
        print("12. 🔍 Debug Grid (lihat task ID terdeteksi)")
        print("-" * 40)
        print("0. ❌ Exit\n")
        
        c = input("📌 Select: ").strip()
        
        if c == "1": 
            monitor()
        elif c == "2":
            auto_setup_wizard()
        elif c == "3": 
            auto_detect_and_save_packages()
        elif c == "4":
            clear_screen()
            print("=" * 70)
            print("📡 WEBHOOK CONFIGURATION")
            print("=" * 70)
            
            current_url = cfg.get("webhook_url", "")
            if current_url:
                print(f"Current URL: {current_url}")
                webhook_url = input("🔗 Discord webhook URL (Enter to keep current): ").strip()
                if not webhook_url:
                    webhook_url = current_url  # ← pakai URL lama jika kosong
            else:
                webhook_url = input("🔗 Discord webhook URL (Enter to skip): ").strip()
            
            if webhook_url:
                cfg["webhook_url"] = webhook_url
            
            enable = input("📡 Enable webhook? (y/n, default=y): ").strip().lower()
            cfg["webhook_enabled"] = (enable == "" or enable == "y")
            
            interval = input("⏱️ Webhook interval in minutes (default=10): ").strip()
            if interval:
                try:
                    cfg["webhook_interval"] = int(interval)
                except:
                    cfg["webhook_interval"] = 10
            
            save_config(cfg)
            add_log("✅ Webhook configuration saved")
            input("\nPress ENTER...")
        elif c == "5":
            clear_screen()
            print("=" * 70)
            print("⏱️ CHECK TIME & RESTART CONFIGURATION")
            print("=" * 70)

            current_first = cfg.get("first_check", 3)
            current_ingame = cfg.get("ingame_check", 2)
            current_restart = cfg.get("restart_interval", 0)
            print(f"First Check (saat launch)   : {current_first} menit")
            print(f"Ingame Check (saat hop/mati): {current_ingame} menit")
            restart_text = f"{current_restart} menit" if current_restart > 0 else "Disabled"
            print(f"Auto Restart Interval       : {restart_text}\n")

            new_first = input(f"⏱️ First check time in minutes (Enter={current_first}): ").strip()
            if new_first:
                try:
                    cfg["first_check"] = int(new_first)
                except:
                    add_log("❌ Invalid input")

            new_ingame = input(f"🎮 Ingame check time in minutes (Enter={current_ingame}): ").strip()
            if new_ingame:
                try:
                    cfg["ingame_check"] = int(new_ingame)
                except:
                    add_log("❌ Invalid input")

            new_restart = input(f"🔄 Auto restart interval in minutes (0=disabled, Enter={current_restart}): ").strip()
            if new_restart:
                try:
                    cfg["restart_interval"] = int(new_restart)
                except:
                    add_log("❌ Invalid input")

            save_config(cfg)
            add_log(f"✅ First={cfg['first_check']}m | Ingame={cfg['ingame_check']}m | Restart={cfg['restart_interval']}m")
            input("\nPress ENTER...")
        elif c == "6":
            add_script_to_all_packages()
        elif c == "7":
            delete_script_from_all_packages()
        elif c == "8":
            view_scripts_all_packages()
        elif c == "9":
            copy_license_to_all_packages()
        elif c == "10":
            view_license_status()
        elif c == "11":
            configure_window_grid()
            apply_grid_layout()
            start_grid_thread()
        elif c == "12":
            clear_screen()
            print("=" * 60)
            print("🔍 DEBUG GRID")
            print("=" * 60)
            sw, sh = get_screen_size()
            print(f"Screen: {sw}x{sh}")
            print()
            print("--- dumpsys activity recents (50 baris) ---")
            out = run_root_cmd("dumpsys activity recents")
            for line in out.splitlines()[:50]:
                print(line)
            print()
            tasks = get_running_roblox_tasks()
            print(f"\n✅ Task IDs terdeteksi: {tasks}")
            input("\nPress ENTER...")
        elif c == "0": 
            break

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    menu()
