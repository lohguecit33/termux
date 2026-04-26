import os
import time
import json
import subprocess
import xml.etree.ElementTree as ET
import sys
import shutil
import sqlite3
from datetime import datetime
from collections import deque

# =========================
# GLOBAL
# =========================
CONFIG_FILE = "config.json"
PACKAGES_FILE = "packages.json"
PKG_SERVERS_FILE = "pkg_servers.json"  # per-pkg server config: game_id atau private_link
COOKIE_FILE = "cookie.txt"
COOKIE_MAP_FILE = "cookie_map.json"  # mapping cookie index -> package
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
    lines = []
    ingame_count = 0
    total_count = len(pkgs)

    for pkg, info in pkgs.items():
        username = info["username"]
        try:
            status = determine_account_status(pkg, info, cfg)
        except Exception:
            status = "unknown"

        if status == "in_game":
            emoji = "🟢"
            label = "In Game"
            ingame_count += 1
        elif status == "waiting":
            emoji = "🟡"
            label = "Waiting"
        elif status == "needs_kill":
            emoji = "🔴"
            label = "Needs Kill"
        elif status == "offline":
            emoji = "⚫"
            label = "Offline"
        else:
            emoji = "❓"
            label = status.upper()

        lines.append(f"{emoji} **{username}** — {label}")

    lines.insert(0, f"📦 **{ingame_count}/{total_count} In Game**\n")
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
        "executor": "delta",  # delta or arceusx
        "freeform_enabled": False,
        "windows_per_row": 2,
        "use_fixed_size": False,
        "window_width": 540,
        "window_height": 960,
        "arrange_interval": 30,
        "gap_h": 35,
        "gap_v": 35
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
# COOKIE MANAGEMENT
# =========================
def load_cookies():
    """Baca cookie.txt, return list of cookie strings"""
    if not os.path.exists(COOKIE_FILE):
        return []
    try:
        with open(COOKIE_FILE, "r") as f:
            cookies = []
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    cookies.append(line)
            return cookies
    except Exception as e:
        add_log(f"Cookie load error: {e}")
        return []

def save_cookies(cookies):
    """Tulis list cookie ke cookie.txt"""
    try:
        with open(COOKIE_FILE, "w") as f:
            for cookie in cookies:
                f.write(cookie + "\n")
        add_log(f"✅ {len(cookies)} cookie(s) saved")
    except Exception as e:
        add_log(f"Cookie save error: {e}")

def load_cookie_map():
    """Load mapping cookie index -> package"""
    if not os.path.exists(COOKIE_MAP_FILE):
        return {}
    try:
        with open(COOKIE_MAP_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_cookie_map(cmap):
    """Save mapping cookie index -> package"""
    try:
        with open(COOKIE_MAP_FILE, "w") as f:
            json.dump(cmap, f, indent=2)
    except Exception as e:
        add_log(f"Cookie map save error: {e}")

def validate_cookie(cookie):
    """Validasi cookie via Roblox API. Return (user_id, username) atau None"""
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            "https://users.roblox.com/v1/users/authenticated",
            headers={"Cookie": f".ROBLOSECURITY={cookie}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("id"):
                return (data["id"], data.get("name", "Unknown"))
    except urllib.error.HTTPError:
        return None
    except Exception as e:
        add_log(f"Cookie validate error: {e}")
    return None

def get_cookie_db_path(package):
    """Return path ke Cookies SQLite database untuk package"""
    return f"/data/data/{package}/app_webview/Default/Cookies"

def get_pkg_owner(package):
    """Dapatkan uid:gid owner dari app data"""
    out = run_root_cmd(f"stat -c '%U:%G' /data/data/{package}")
    if out and ":" in out:
        return out.strip()
    # Fallback: coba ls
    out = run_root_cmd(f"ls -ld /data/data/{package}")
    if out:
        parts = out.split()
        if len(parts) >= 4:
            return f"{parts[2]}:{parts[3]}"
    return None

def inject_cookie_to_pkg(package, cookie):
    """
    Inject .ROBLOSECURITY cookie ke Roblox package via SQLite.
    1. Force stop app
    2. Copy DB ke /data/local/tmp
    3. Python sqlite3 DELETE + INSERT
    4. Copy back + fix permissions
    """
    db_path = get_cookie_db_path(package)
    tmp_db = f"/data/local/tmp/cookies_{package.split('.')[-1]}.db"

    # Cek DB exists
    check = run_root_cmd(f"ls {db_path}")
    if not check:
        add_log(f"❌ Cookie DB not found for {package}")
        add_log(f"   App mungkin belum pernah dibuka. Buka manual dulu 1x.")
        return False

    # Step 1: Force stop
    add_log(f"⏹️ Stopping {package}...")
    run_root_cmd(f"am force-stop {package}")
    time.sleep(1)

    # Step 2: Copy DB ke tmp
    run_root_cmd(f"cp {db_path} {tmp_db}")
    run_root_cmd(f"chmod 666 {tmp_db}")

    # Cek tmp file
    if not run_root_cmd(f"ls {tmp_db}"):
        add_log(f"❌ Failed to copy DB to tmp")
        return False

    # Step 3: Python sqlite3 inject
    try:
        conn = sqlite3.connect(tmp_db)
        c = conn.cursor()

        # Cek schema - apakah tabel cookies ada
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cookies'")
        if not c.fetchone():
            add_log(f"❌ Table 'cookies' not found in DB")
            conn.close()
            return False

        # Timestamps (microseconds)
        now = int(time.time() * 1_000_000)
        expires = int((time.time() + 86400 * 365) * 1_000_000)  # 1 tahun

        # Delete existing .ROBLOSECURITY
        c.execute("DELETE FROM cookies WHERE host_key='.roblox.com' AND name='.ROBLOSECURITY'")
        c.execute("DELETE FROM cookies WHERE host_key='auth.roblox.com' AND name='.ROBLOSECURITY'")

        # Insert new cookie
        c.execute("""
            INSERT INTO cookies (
                creation_utc, host_key, name, value, path, expires_utc,
                is_secure, is_httponly, last_access_utc, has_expires,
                is_persistent, priority, encrypted_value, samesite, source_scheme
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now,                # creation_utc
            ".roblox.com",      # host_key
            ".ROBLOSECURITY",   # name
            cookie,             # value
            "/",                # path
            expires,            # expires_utc
            1,                  # is_secure
            1,                  # is_httponly
            now,                # last_access_utc
            1,                  # has_expires
            1,                  # is_persistent
            1,                  # priority
            b"",                # encrypted_value (kosong)
            -1,                 # samesite
            0                   # source_scheme
        ))

        conn.commit()
        conn.close()
        add_log(f"✅ Cookie injected to tmp DB")

    except Exception as e:
        add_log(f"❌ SQLite error: {e}")
        try:
            conn.close()
        except:
            pass
        return False

    # Step 4: Copy back + fix permissions
    owner = get_pkg_owner(package)
    run_root_cmd(f"cp {tmp_db} {db_path}")
    if owner:
        run_root_cmd(f"chown {owner} {db_path}")
    run_root_cmd(f"chmod 600 {db_path}")

    # Cleanup tmp
    run_root_cmd(f"rm {tmp_db}")

    add_log(f"✅ Cookie injected to {package}")
    return True

def manage_cookies_menu():
    """Menu Manage Cookies — add, delete, view cookies"""
    while True:
        clear_screen()
        cookies = load_cookies()

        print("=" * 70)
        print("🍪 MANAGE COOKIES")
        print("=" * 70)
        print(f"📄 File: {COOKIE_FILE}")
        print(f"🍪 Total cookies: {len(cookies)}")
        print()

        if cookies:
            print("📋 Cookie List:")
            print("-" * 70)
            for i, cookie in enumerate(cookies, 1):
                # Tampilkan preview cookie (awal + akhir)
                if len(cookie) > 60:
                    preview = cookie[:25] + "..." + cookie[-15:]
                else:
                    preview = cookie
                print(f"  {i}. {preview}")
            print("-" * 70)
        else:
            print("  (Belum ada cookie)")
        print()

        print("1. ➕ Add Cookie")
        print("2. 🗑️ Delete Cookie")
        print("3. ✅ Validate All Cookies")
        print("0. ↩️ Back\n")

        c = input("📌 Select: ").strip()

        if c == "1":
            # Add cookie
            print()
            print("Paste cookie .ROBLOSECURITY di bawah ini:")
            print("(Format: _|WARNING:-DO-NOT-SHARE-THIS...)")
            print()
            new_cookie = input("🍪 Cookie: ").strip()
            if not new_cookie:
                add_log("❌ Cookie kosong, dibatalkan")
                input("\nPress ENTER...")
                continue

            # Validasi format dasar
            if len(new_cookie) < 50:
                print("⚠️ Cookie terlalu pendek. Yakin ini benar? (y/n)")
                confirm = input("> ").strip().lower()
                if confirm != "y":
                    continue

            # Cek duplikat
            if new_cookie in cookies:
                add_log("⚠️ Cookie sudah ada di list")
                input("\nPress ENTER...")
                continue

            # Validasi online (opsional)
            print("\n🔍 Validating cookie...")
            result = validate_cookie(new_cookie)
            if result:
                user_id, username = result
                print(f"✅ Valid! User: {username} (ID: {user_id})")
            else:
                print("⚠️ Cookie tidak valid atau tidak bisa divalidasi")
                print("   Tetap simpan? (y/n)")
                confirm = input("> ").strip().lower()
                if confirm != "y":
                    continue

            cookies.append(new_cookie)
            save_cookies(cookies)
            print(f"\n✅ Cookie #{len(cookies)} ditambahkan!")
            input("\nPress ENTER...")

        elif c == "2":
            # Delete cookie
            if not cookies:
                add_log("❌ Tidak ada cookie untuk dihapus")
                input("\nPress ENTER...")
                continue

            print()
            idx = input(f"🗑️ Nomor cookie yang mau dihapus (1-{len(cookies)}): ").strip()
            try:
                idx = int(idx)
                if 1 <= idx <= len(cookies):
                    removed = cookies.pop(idx - 1)
                    save_cookies(cookies)
                    preview = removed[:30] + "..." if len(removed) > 30 else removed
                    print(f"✅ Cookie #{idx} dihapus: {preview}")

                    # Update cookie_map jika ada (adjust cookie indices)
                    cmap = load_cookie_map()
                    if cmap:
                        new_map = {}
                        deleted_cookie_idx = idx - 1
                        for pkg_key, cookie_val in cmap.items():
                            if cookie_val == deleted_cookie_idx:
                                pass  # cookie ini dihapus, skip mapping
                            elif cookie_val > deleted_cookie_idx:
                                new_map[pkg_key] = cookie_val - 1
                            else:
                                new_map[pkg_key] = cookie_val
                        save_cookie_map(new_map)
                else:
                    print("❌ Nomor tidak valid")
            except ValueError:
                print("❌ Input harus angka")
            input("\nPress ENTER...")

        elif c == "3":
            # Validate all
            if not cookies:
                add_log("❌ Tidak ada cookie untuk divalidasi")
                input("\nPress ENTER...")
                continue

            print("\n🔍 Validating all cookies...\n")
            for i, cookie in enumerate(cookies, 1):
                preview = cookie[:25] + "..." if len(cookie) > 25 else cookie
                print(f"  [{i}] {preview} → ", end="", flush=True)
                result = validate_cookie(cookie)
                if result:
                    user_id, username = result
                    print(f"✅ {username} (ID: {user_id})")
                else:
                    print("❌ Invalid / Expired")
            input("\nPress ENTER...")

        elif c == "0":
            break

def login_cookies_menu():
    """Menu Login Cookie — assign cookie ke package dan inject"""
    while True:
        clear_screen()
        cookies = load_cookies()
        pkgs = load_packages()
        cmap = load_cookie_map()

        print("=" * 70)
        print("🔑 LOGIN COOKIE")
        print("=" * 70)

        if not cookies:
            print("\n❌ Belum ada cookie! Tambahkan dulu di menu Manage Cookies.")
            input("\nPress ENTER...")
            return

        if not pkgs:
            print("\n❌ Belum ada package! Jalankan Auto Detect dulu.")
            input("\nPress ENTER...")
            return

        # Tampilkan status mapping
        print(f"\n🍪 Cookies: {len(cookies)} | 📦 Packages: {len(pkgs)}")
        print()
        print(f"{'No':<4} {'Package':<25} {'Cookie':<20} {'Status':<20}")
        print("-" * 70)

        pkg_list = list(pkgs.items())
        for i, (pkg, info) in enumerate(pkg_list):
            pkg_short = pkg.split('.')[-1][:23]
            username = info.get("username", "?")

            # Cek apakah ada cookie yang di-assign
            cookie_idx = cmap.get(str(i))
            if cookie_idx is not None and cookie_idx < len(cookies):
                cookie_preview = cookies[cookie_idx][:15] + "..."
                status = f"🍪 Cookie #{cookie_idx + 1}"
            else:
                cookie_preview = "-"
                status = "⚪ No cookie"

            print(f"{i+1:<4} {username:<25} {cookie_preview:<20} {status:<20}")

        print("-" * 70)
        print()

        print("1. 🔄 Auto Assign (cookie 1→pkg 1, cookie 2→pkg 2, ...)")
        print("2. 🎯 Manual Assign (pilih cookie untuk package tertentu)")
        print("3. 🚀 Login All (inject semua cookie yang sudah di-assign)")
        print("4. 🚀 Login Satu Package")
        print("0. ↩️ Back\n")

        c = input("📌 Select: ").strip()

        if c == "1":
            # Auto assign
            new_map = {}
            count = min(len(cookies), len(pkg_list))
            for i in range(count):
                new_map[str(i)] = i
            save_cookie_map(new_map)
            add_log(f"✅ Auto-assigned {count} cookie(s) ke {count} package(s)")
            print(f"\n✅ {count} cookie di-assign secara berurutan!")
            if len(cookies) < len(pkg_list):
                print(f"⚠️ Cookie kurang! {len(pkg_list) - len(cookies)} package tanpa cookie.")
            elif len(cookies) > len(pkg_list):
                print(f"ℹ️ {len(cookies) - len(pkg_list)} cookie tidak terpakai (lebih dari jumlah package).")
            input("\nPress ENTER...")

        elif c == "2":
            # Manual assign
            print()
            pkg_num = input(f"📦 Nomor package (1-{len(pkg_list)}): ").strip()
            try:
                pkg_idx = int(pkg_num) - 1
                if 0 <= pkg_idx < len(pkg_list):
                    cookie_num = input(f"🍪 Nomor cookie (1-{len(cookies)}): ").strip()
                    cookie_idx = int(cookie_num) - 1
                    if 0 <= cookie_idx < len(cookies):
                        cmap[str(pkg_idx)] = cookie_idx
                        save_cookie_map(cmap)
                        pkg_name = pkg_list[pkg_idx][1].get("username", "?")
                        print(f"\n✅ Cookie #{cookie_idx+1} → {pkg_name}")
                    else:
                        print("❌ Nomor cookie tidak valid")
                else:
                    print("❌ Nomor package tidak valid")
            except ValueError:
                print("❌ Input harus angka")
            input("\nPress ENTER...")

        elif c == "3":
            # Login all
            if not cmap:
                print("\n❌ Belum ada cookie yang di-assign! Pilih opsi 1 atau 2 dulu.")
                input("\nPress ENTER...")
                continue

            print("\n🚀 Injecting cookies ke semua package...\n")
            success = 0
            fail = 0
            for pkg_idx_str, cookie_idx in cmap.items():
                pkg_idx = int(pkg_idx_str)
                if pkg_idx >= len(pkg_list):
                    continue
                if cookie_idx >= len(cookies):
                    continue

                pkg, info = pkg_list[pkg_idx]
                username = info.get("username", "?")
                cookie = cookies[cookie_idx]

                print(f"  [{pkg_idx+1}] {username} ← Cookie #{cookie_idx+1}... ", end="", flush=True)
                if inject_cookie_to_pkg(pkg, cookie):
                    print("✅")
                    success += 1
                else:
                    print("❌")
                    fail += 1

            print(f"\n📊 Result: {success} success, {fail} failed")
            input("\nPress ENTER...")

        elif c == "4":
            # Login satu package
            print()
            pkg_num = input(f"📦 Nomor package (1-{len(pkg_list)}): ").strip()
            try:
                pkg_idx = int(pkg_num) - 1
                if 0 <= pkg_idx < len(pkg_list):
                    # Cek apakah sudah ada mapping
                    cookie_idx = cmap.get(str(pkg_idx))
                    if cookie_idx is not None and cookie_idx < len(cookies):
                        pkg, info = pkg_list[pkg_idx]
                        username = info.get("username", "?")
                        cookie = cookies[cookie_idx]
                        print(f"\n🚀 Injecting Cookie #{cookie_idx+1} → {username}...")
                        if inject_cookie_to_pkg(pkg, cookie):
                            print("✅ Success!")
                        else:
                            print("❌ Failed!")
                    else:
                        print("❌ Package ini belum di-assign cookie. Pilih opsi 1 atau 2 dulu.")
                else:
                    print("❌ Nomor package tidak valid")
            except ValueError:
                print("❌ Input harus angka")
            input("\nPress ENTER...")

        elif c == "0":
            break

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
# PKG SERVER CONFIG (Menu 12)
# =========================
def load_pkg_servers():
    """Load per-pkg server config dari pkg_servers.json"""
    if not os.path.exists(PKG_SERVERS_FILE):
        return {}
    try:
        with open(PKG_SERVERS_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_pkg_servers(data):
    """Simpan per-pkg server config ke pkg_servers.json"""
    try:
        with open(PKG_SERVERS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        add_log(f"❌ Gagal save pkg_servers: {e}")

def parse_server_input(raw, default_game_id):
    """
    Parse input user:
    - Kosong / enter → gunakan config game_id (default)
    - Angka saja    → game_id berbeda
    - URL roblox    → private link, extract game_id dari URL
    Returns: (game_id, private_link_or_empty)
    """
    import re
    if not raw or not raw.strip():
        return default_game_id, ""

    raw = raw.strip()

    # Cek apakah URL roblox (private link)
    if "roblox.com" in raw or "ro.blox.com" in raw:
        game_id_match = re.search(r'/games/(\d+)', raw)
        game_id = game_id_match.group(1) if game_id_match else default_game_id
        return game_id, raw

    # Cek apakah angka saja (game_id lain)
    if raw.isdigit():
        return raw, ""

    # Format roblox deep link: roblox://placeID=xxx
    deep_match = re.search(r'placeID=(\d+)', raw)
    if deep_match:
        return deep_match.group(1), ""

    # Tidak dikenali → pakai default
    add_log(f"⚠️ Input tidak dikenali, pakai default game_id")
    return default_game_id, ""

def start_app_2step(package, game_id, private_link=""):
    """
    Launch Roblox 2 step seperti pc.py:
    Step 1: Launch ActivitySplash (splash screen)
    Step 2: Launch ActivityProtocolLaunch dengan game_id atau private_link
    """
    import re

    # Step 1: Splash
    splash = f"am start -n {package}/com.roblox.client.startup.ActivitySplash"
    run_root_cmd(splash)
    time.sleep(10)

    # Step 2: Protocol launch
    if private_link:
        # Extract game_id dan private code dari link
        game_id_match = re.search(r'/games/(\d+)', private_link)
        link_game_id = game_id_match.group(1) if game_id_match else game_id

        private_code = None
        if "privateServerLinkCode=" in private_link:
            m = re.search(r'privateServerLinkCode=([^&]+)', private_link)
            if m:
                private_code = m.group(1)
        elif "share?code=" in private_link or "code=" in private_link:
            m = re.search(r'code=([^&]+)', private_link)
            if m:
                private_code = m.group(1)

        if private_code:
            intent = (
                f"am start -n {package}/com.roblox.client.ActivityProtocolLaunch "
                f"-d \"roblox://placeID={link_game_id}&privateServerLinkCode={private_code}\""
            )
        else:
            # Private link tapi tidak ada code → launch biasa dengan game_id dari link
            intent = (
                f"am start -n {package}/com.roblox.client.ActivityProtocolLaunch "
                f"-d roblox://placeID={link_game_id}"
            )
    else:
        intent = (
            f"am start -n {package}/com.roblox.client.ActivityProtocolLaunch "
            f"-d roblox://placeID={game_id}"
        )

    run_root_cmd(intent)

def get_pkg_launch_params(pkg, cfg):
    """
    Dapatkan (game_id, private_link) untuk pkg tertentu.
    Prioritas: pkg_servers.json > config game_id
    """
    servers = load_pkg_servers()
    if pkg in servers:
        entry = servers[pkg]
        return entry.get("game_id", cfg.get("game_id", "")), entry.get("private_link", "")
    return cfg.get("game_id", ""), ""

def configure_server_per_pkg():
    """Menu 12 — Konfigurasi server per-pkg (game_id atau private link)"""
    pkgs = load_packages()
    cfg  = load_config()

    if not pkgs:
        print("❌ Tidak ada packages terdaftar. Daftar dulu di menu 3.")
        input("Press ENTER...")
        return

    default_game_id = cfg.get("game_id", "")
    servers = load_pkg_servers()

    pkg_list = list(pkgs.items())

    print("\n" + "=" * 60)
    print("🌐 ADD SERVER — Konfigurasi Server Per Package")
    print("=" * 60)
    print(f"Default Game ID (dari config): {default_game_id}")
    print()
    print("Untuk setiap package, isi:")
    print("  • Enter saja       → pakai default game_id dari config")
    print("  • Angka (game_id)  → game berbeda untuk pkg ini")
    print("  • URL roblox       → private server link untuk pkg ini")
    print()

    # Tanya apakah untuk semua pkg atau manual
    print("Apply ke semua package sama? (y/n, default=n): ", end="")
    all_same = input().strip().lower()

    if all_same == "y":
        print(f"\nIsi untuk SEMUA package (Enter = default {default_game_id}): ", end="")
        raw = input().strip()
        gid, plink = parse_server_input(raw, default_game_id)

        for pkg, info in pkg_list:
            if gid == default_game_id and not plink:
                # Hapus override, kembali ke default
                servers.pop(pkg, None)
            else:
                servers[pkg] = {"game_id": gid, "private_link": plink}

        save_pkg_servers(servers)
        print()
        for pkg, info in pkg_list:
            label = f"Private ({gid})" if plink else f"Game ID {gid}"
            print(f"  ✅ {info['username']} → {label}")
        print("\n✅ Semua package diupdate.")

    else:
        # Manual per pkg
        for pkg, info in pkg_list:
            username = info["username"]
            current = servers.get(pkg, {})
            cur_gid   = current.get("game_id", default_game_id)
            cur_plink = current.get("private_link", "")

            if cur_plink:
                cur_label = f"Private link (game {cur_gid})"
            elif cur_gid != default_game_id:
                cur_label = f"Game ID {cur_gid}"
            else:
                cur_label = f"Default ({default_game_id})"

            print(f"\n[{username}] Current: {cur_label}")
            print(f"  Enter = pakai default, angka = game_id lain, URL = private link")
            print(f"  Input: ", end="")
            raw = input().strip()

            gid, plink = parse_server_input(raw, default_game_id)

            if gid == default_game_id and not plink:
                servers.pop(pkg, None)
                print(f"  → Reset ke default ({default_game_id})")
            else:
                servers[pkg] = {"game_id": gid, "private_link": plink}
                if plink:
                    print(f"  → Private server (game {gid})")
                else:
                    print(f"  → Game ID {gid}")

        save_pkg_servers(servers)
        print("\n✅ Konfigurasi server disimpan.")

    input("\nPress ENTER untuk kembali ke menu...")



# =========================
# JSON WORKSPACE MONITORING (seperti PC version)
# =========================
def get_workspace_json_path(package_info, username, cfg):
    """Mendapatkan path workspace JSON"""
    workspace_dir = package_info.get("workspace_dir",
                     f"/storage/emulated/0/Android/data/com.roblox.client/files/gloop/external/Workspace")
    try:
        os.makedirs(workspace_dir, exist_ok=True)
    except Exception:
        pass
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
            raw = f.read()
        if not raw.strip():
            return None
        data = json.loads(raw)

        timestamp_field = None
        for field in ["timestamp", "time", "last_update", "updated_at"]:
            if field in data:
                timestamp_field = data[field]
                break

        if timestamp_field:
            if isinstance(timestamp_field, (int, float)):
                json_time = timestamp_field
            elif isinstance(timestamp_field, str):
                if "T" in timestamp_field and "Z" in timestamp_field:
                    dt_str = timestamp_field.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(dt_str)
                    json_time = dt.timestamp()
                else:
                    return None
            else:
                return None
            return time.time() - json_time
        else:
            mtime = os.path.getmtime(json_path)
            return time.time() - mtime

    except (json.JSONDecodeError, ValueError, OSError):
        return None
    except Exception:
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
    clear_screen()

    print("=" * 80)
    print(" " * 30 + "🎮 ROBLOX OVA MONITOR")
    print("=" * 80)
    print()

    print(f"{'No':<4} {'Username':<16} {'Package':<22} {'Status':<30}")
    print("-" * 80)

    all_in_game = True
    for i, (pkg, info) in enumerate(pkgs.items(), 1):
        username = info["username"]
        pkg_short = pkg.split('.')[-1][:20]

        if pkg not in ACCOUNT_STATE:
            ACCOUNT_STATE[pkg] = {
                "launch_time": None,
                "json_start_time": None,
                "json_active": False,
                "last_status": "offline"
            }

        try:
            status = determine_account_status(pkg, info, cfg)
        except Exception:
            status = "offline"

        if status == "in_game":
            try:
                time_diff = get_json_time_diff(info, username, cfg)
            except Exception:
                time_diff = None
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

        username_display = username[:14] if len(username) <= 14 else username[:13] + "."
        status_display = status_display[:28] if len(status_display) <= 28 else status_display[:27] + "."

        print(f"{i:<4} {username_display:<16} {pkg_short:<22} {status_display:<30}")

    print("-" * 80)
    timestamp = time.strftime('%H:%M:%S')
    status_text = "✅ ALL IN GAME" if all_in_game else "❌ NEEDS ATTENTION"
    print(f"🕒 {timestamp} | 📦 Total: {len(pkgs)} | {status_text}")
    print("=" * 80)
    print()

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
            if pkg in ACCOUNT_STATE:
                ACCOUNT_STATE[pkg]["launch_time"] = None
                ACCOUNT_STATE[pkg]["json_start_time"] = None
                ACCOUNT_STATE[pkg]["json_active"] = False

    time.sleep(cfg["restart_delay"] * 2)

    # Hitung bounds freeform jika enabled
    freeform_enabled = cfg.get("freeform_enabled", False)
    per_row = cfg.get("windows_per_row", 2)
    total = len(pkgs)
    win_w, win_h, screen_w, screen_h = get_window_size(cfg, total)

    # Load cookie map untuk auto-inject
    cookies = load_cookies()
    cmap = load_cookie_map()
    pkg_list_keys = list(pkgs.keys())

    for i, (pkg, info) in enumerate(pkgs.items(), 1):
        username = info["username"]

        add_log(f"Starting {i}/{len(pkgs)}: {username}")

        # Auto-inject cookie jika ada mapping
        pkg_idx = pkg_list_keys.index(pkg)
        cookie_idx = cmap.get(str(pkg_idx))
        if cookie_idx is not None and cookie_idx < len(cookies):
            add_log(f"🍪 Injecting cookie #{cookie_idx+1} → {username}")
            inject_cookie_to_pkg(pkg, cookies[cookie_idx])

        col = (i - 1) % per_row
        row = (i - 1) // per_row
        bounds = calc_bounds(i - 1, per_row, win_w, win_h)
        add_log(f"📐 Bounds: {bounds}")

        start_app_2step(pkg, *get_pkg_launch_params(pkg, cfg))

        ACCOUNT_STATE.setdefault(pkg, {})
        ACCOUNT_STATE[pkg]["launch_time"] = time.time()
        ACCOUNT_STATE[pkg]["json_active"] = False
        ACCOUNT_STATE[pkg]["json_start_time"] = None

        time.sleep(cfg["startup_delay"])

        max_wait = cfg["first_check"] * 60 + 30
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
# FREEFORM WINDOW ARRANGER
# =========================
def get_screen_size():
    """Ambil resolusi layar dari wm size"""
    try:
        out = run_root_cmd("wm size")
        for line in out.splitlines():
            if "size:" in line.lower():
                part = line.split(":")[-1].strip()
                # Handle "Override size" vs "Physical size"
                if " " in part:
                    part = part.split()[-1]
                w, h = part.split("x")
                return int(w), int(h)
    except Exception:
        pass
    return 1080, 1920  # fallback


def calc_bounds(index, per_row, win_w, win_h, gap_h=35, gap_v=35):
    """
    Hitung bounds (left, top, right, bottom).
    Konsep sama seperti pc.py:
      x = col * win_w
      y = row * (win_h + title_bar_height)
    title_bar_height masuk ke dalam perkalian row,
    sehingga jarak antar semua baris selalu konsisten.
    gap_h tidak digunakan (window horizontal rapat).
    """
    title_bar_height = 30
    col    = index % per_row
    row    = index // per_row
    left   = col * win_w
    top    = row * (win_h + title_bar_height)
    right  = left + win_w
    bottom = top  + win_h
    return left, top, right, bottom


def get_window_size(cfg, total_pkgs):
    """
    Hitung ukuran window berdasarkan config.
    - use_fixed_size=True  → pakai window_width x window_height dari config (seperti arrage.py Fixed Size)
    - use_fixed_size=False → bagi rata: win_w = screen_w // per_row, win_h = screen_h // rows
    Returns: (win_w, win_h, screen_w, screen_h)
    """
    screen_w, screen_h = get_screen_size()
    per_row = cfg.get("windows_per_row", 2)

    if cfg.get("use_fixed_size", False):
        win_w = cfg.get("window_width", 540)
        win_h = cfg.get("window_height", 960)
    else:
        rows  = max(1, (total_pkgs + per_row - 1) // per_row)
        win_w = screen_w // per_row
        win_h = screen_h // rows

    return win_w, win_h, screen_w, screen_h


def arrange_roblox_windows(cfg, pkgs=None):
    """
    Arrange semua window Roblox - gabungkan semua metode.
    Method 1: am task resize (paling umum)
    Method 2: am task resize --task --bounds (format alternatif)
    Method 3: wm set-multi-window-position (Android 14+)
    Method 4: am start --launch-bounds --activity-single-top (tanpa kill)
    Method 5: am task move-to-front lalu resize ulang
    """
    import re
    if pkgs is None:
        pkgs = load_packages()
    if not pkgs:
        add_log("❌ No packages found")
        return

    per_row = cfg.get("windows_per_row", 2)
    total   = len(pkgs)
    win_w, win_h, screen_w, screen_h = get_window_size(cfg, total)
    game_id = cfg.get("game_id", "")

    size_mode = "Fixed" if cfg.get("use_fixed_size", False) else "Auto"
    add_log(f"📐 [{size_mode}] {win_w}x{win_h} | {per_row}/row")

    # ── Parsing taskId dari dumpsys ───────────────────────────────────
    dump = run_root_cmd("dumpsys activity activities")
    pkg_task = {}
    current_task = None

    for line in dump.splitlines():
        s = line.strip()
        m = re.match(r'Task id #(\d+)', s)
        if not m:
            m = re.match(r'\*?\s*Task\{[^\}]*#(\d+)', s)
        if m:
            current_task = m.group(1)
            continue
        if current_task:
            for pkg in pkgs.keys():
                if pkg in s and pkg not in pkg_task:
                    pkg_task[pkg] = current_task

    add_log(f"🔍 Tasks: {pkg_task}")

    for i, (pkg, info) in enumerate(pkgs.items()):
        username = info["username"]
        task_id  = pkg_task.get(pkg)

        left, top, right, bottom = calc_bounds(i, per_row, win_w, win_h)
        add_log(f"→ {username} task={task_id} ({left},{top},{right},{bottom})")

        if not task_id:
            add_log(f"⚠️ {username}: task not found, skip")
            continue

        # ── Step 1: Force task masuk freeform stack (stack id 5) ──────
        # Wajib dilakukan agar am task resize bisa bekerja
        # meski dev options freeform dimatikan
        run_root_cmd(f"am stack movetask {task_id} 5 true")
        time.sleep(0.2)

        # ── Step 2: am task resize (format standar) ───────────────────
        run_root_cmd(f"am task resize {task_id} {left} {top} {right} {bottom}")
        time.sleep(0.15)

        # ── Step 3: am task resize (format koma, Android 10+) ────────
        run_root_cmd(f"am task resize {task_id} {left},{top},{right},{bottom}")
        time.sleep(0.1)

        # ── Step 4: am stack resize (resize stack sekalian) ───────────
        run_root_cmd(f"am stack resize 5 {left} {top} {right} {bottom}")
        time.sleep(0.1)

        # ── Step 5: move-to-front lalu resize ulang ───────────────────
        run_root_cmd(f"am task move-to-front {task_id}")
        time.sleep(0.1)
        run_root_cmd(f"am task resize {task_id} {left} {top} {right} {bottom}")
        time.sleep(0.15)

        # ── Step 6: am start --launch-bounds (reposisi tanpa kill) ────
        if game_id:
            run_root_cmd(
                f"am start "
                f"--launch-bounds {left} {top} {right} {bottom} "
                f"--activity-single-top "
                f"-n {pkg}/com.roblox.client.ActivityProtocolLaunch "
                f"-d roblox://placeID={game_id}"
            )
            time.sleep(0.2)

        add_log(f"✅ {username} done")


def configure_freeform_window():
    """Menu 11 - Konfigurasi freeform window arrange"""
    cfg = load_config()
    pkgs = load_packages()

    while True:
        clear_screen()
        screen_w, screen_h = get_screen_size()
        per_row        = cfg.get("windows_per_row", 2)
        freeform_on    = cfg.get("freeform_enabled", False)
        use_fixed      = cfg.get("use_fixed_size", False)
        win_w_fixed    = cfg.get("window_width", 540)
        win_h_fixed    = cfg.get("window_height", 960)
        arr_interval   = cfg.get("arrange_interval", 30)
        gap_h          = cfg.get("gap_h", 35)
        gap_v          = cfg.get("gap_v", 35)
        total          = len(pkgs)

        # Hitung ukuran preview
        win_w, win_h, _, _ = get_window_size(cfg, total)
        rows = (total + per_row - 1) // per_row if total > 0 else 1

        print("=" * 70)
        print("📐 FREEFORM WINDOW ARRANGER")
        print("=" * 70)
        print(f"📱 Screen Size      : {screen_w} x {screen_h}")
        print(f"📦 Total Packages   : {total}")
        print(f"🔲 Windows Per Row  : {per_row}")
        print(f"📏 Size Mode        : {'Fixed (' + str(win_w_fixed) + 'x' + str(win_h_fixed) + ')' if use_fixed else 'Auto (bagi rata)'}")
        print(f"📐 Window Size      : {win_w} x {win_h} per window")
        print(f"↔️  Gap Horizontal   : {gap_h}px (antar window kiri/kanan)")
        print(f"↕️  Gap Vertikal     : {gap_v}px (antar baris atas/bawah)")
        print(f"⏱️  Arrange Interval : {arr_interval}s")
        print(f"🔧 Auto Arrange     : {'✅ ON' if freeform_on else '❌ OFF'}")
        print()

        # Preview grid layout
        print("🗺️  Layout Preview:")
        for r in range(rows):
            row_str = ""
            for c in range(per_row):
                idx = r * per_row + c
                if idx < total:
                    uname = list(pkgs.values())[idx]["username"][:8]
                    row_str += f"[{uname:<8}] "
                else:
                    row_str += "[  ---   ] "
            print("   " + row_str)

        print()
        print("=" * 70)
        print("1. 🔲 Set Windows Per Row")
        print("2. 📏 Set Size Mode (Auto / Fixed)")
        print(f"3. 📐 Set Fixed Size (current: {win_w_fixed}x{win_h_fixed})")
        print(f"4. ↔️  Set Gap Horizontal (current: {gap_h}px)")
        print(f"5. ↕️  Set Gap Vertikal (current: {gap_v}px)")
        print(f"6. ⏱️  Set Arrange Interval (current: {arr_interval}s)")
        print(f"7. 🔧 Toggle Auto Arrange → {'OFF' if freeform_on else 'ON'}")
        print("8. 🚀 Arrange NOW (resize window yang sudah jalan)")
        print("0. ↩️  Back")
        print("=" * 70)

        c = input("📌 Select: ").strip()

        if c == "1":
            try:
                val = int(input(f"Windows per row (current={per_row}, 1-6): ").strip())
                if 1 <= val <= 6:
                    cfg["windows_per_row"] = val
                    save_config(cfg)
                    add_log(f"✅ Windows per row: {val}")
                else:
                    print("❌ Harus antara 1-6")
            except ValueError:
                print("❌ Input tidak valid")
            input("Press ENTER...")

        elif c == "2":
            print("\n1. Auto (bagi rata screen)")
            print("2. Fixed (ukuran manual)")
            mode = input("Pilih (1/2): ").strip()
            if mode == "1":
                cfg["use_fixed_size"] = False
                add_log("✅ Size mode: Auto")
            elif mode == "2":
                cfg["use_fixed_size"] = True
                add_log("✅ Size mode: Fixed")
            save_config(cfg)
            input("Press ENTER...")

        elif c == "3":
            try:
                print(f"\nCurrent fixed size: {win_w_fixed} x {win_h_fixed}")
                w = input(f"Width (Enter={win_w_fixed}): ").strip()
                h = input(f"Height (Enter={win_h_fixed}): ").strip()
                if w:
                    cfg["window_width"] = int(w)
                if h:
                    cfg["window_height"] = int(h)
                save_config(cfg)
                add_log(f"✅ Fixed size: {cfg['window_width']}x{cfg['window_height']}")
            except ValueError:
                print("❌ Input tidak valid")
            input("Press ENTER...")

        elif c == "4":
            try:
                val = int(input(f"Gap horizontal px (current={gap_h}, 0=no gap): ").strip())
                if val >= 0:
                    cfg["gap_h"] = val
                    save_config(cfg)
                    add_log(f"✅ Gap horizontal: {val}px")
                else:
                    print("❌ Minimal 0")
            except ValueError:
                print("❌ Input tidak valid")
            input("Press ENTER...")

        elif c == "5":
            try:
                val = int(input(f"Gap vertikal px (current={gap_v}, 0=no gap): ").strip())
                if val >= 0:
                    cfg["gap_v"] = val
                    save_config(cfg)
                    add_log(f"✅ Gap vertikal: {val}px")
                else:
                    print("❌ Minimal 0")
            except ValueError:
                print("❌ Input tidak valid")
            input("Press ENTER...")

        elif c == "6":
            try:
                val = int(input(f"Arrange interval detik (current={arr_interval}, min=10): ").strip())
                if val >= 10:
                    cfg["arrange_interval"] = val
                    save_config(cfg)
                    add_log(f"✅ Arrange interval: {val}s")
                else:
                    print("❌ Minimal 10 detik")
            except ValueError:
                print("❌ Input tidak valid")
            input("Press ENTER...")

        elif c == "7":
            cfg["freeform_enabled"] = not freeform_on
            save_config(cfg)
            status = "ON" if cfg["freeform_enabled"] else "OFF"
            add_log(f"✅ Auto Arrange: {status}")
            input("Press ENTER...")

        elif c == "8":
            print("\nArranging windows...")
            arrange_roblox_windows(cfg, pkgs)
            input("\nPress ENTER...")

        elif c == "0":
            break


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
        last_arrange_time = time.time()

        while monitor_active:
            cycle_count += 1
            display_monitor_screen(pkgs, cfg)

            for pkg, info in pkgs.items():
                username = info["username"]

                try:
                    status = determine_account_status(pkg, info, cfg)
                except Exception as e:
                    add_log(f"⚠️ Status error {username}: {e}")
                    continue

                # LANGSUNG KILL saat needs_kill
                if status == "needs_kill":
                    add_log(f"🔴 {username}: KILL sekarang!")
                    stop_app(pkg)
                    ACCOUNT_STATE.setdefault(pkg, {})
                    ACCOUNT_STATE[pkg]["launch_time"] = None
                    ACCOUNT_STATE[pkg]["json_start_time"] = None
                    ACCOUNT_STATE[pkg]["json_active"] = False

                # Launch saat giliran cek (offline = sudah di-kill atau memang mati)
                elif status == "offline":
                    add_log(f"⚫ {username}: LAUNCH → tunggu ingame...")
                    ACCOUNT_STATE.setdefault(pkg, {})
                    ACCOUNT_STATE[pkg]["launch_time"] = time.time()
                    ACCOUNT_STATE[pkg]["json_start_time"] = None
                    ACCOUNT_STATE[pkg]["json_active"] = False

                    # Auto-inject cookie jika ada mapping
                    m_cookies = load_cookies()
                    m_cmap = load_cookie_map()
                    m_pkg_keys = list(pkgs.keys())
                    m_idx = m_pkg_keys.index(pkg) if pkg in m_pkg_keys else -1
                    m_cookie_idx = m_cmap.get(str(m_idx))
                    if m_cookie_idx is not None and m_cookie_idx < len(m_cookies):
                        add_log(f"🍪 Re-injecting cookie → {username}")
                        inject_cookie_to_pkg(pkg, m_cookies[m_cookie_idx])

                    # Hitung bounds - selalu dihitung (freeform aktif di device)
                    per_row = cfg.get("windows_per_row", 2)
                    total = len(pkgs)
                    win_w, win_h, _, _ = get_window_size(cfg, total)
                    pkg_list = list(pkgs.keys())
                    idx = pkg_list.index(pkg) if pkg in pkg_list else 0
                    bounds = calc_bounds(idx, per_row, win_w, win_h)

                    start_app_2step(pkg, *get_pkg_launch_params(pkg, cfg))
                    time.sleep(cfg["startup_delay"])

                    # Tunggu ingame, max retry 3x
                    timeout = cfg.get("first_check", 3) * 60
                    max_retry = 3
                    retry = 0

                    while retry < max_retry:
                        deadline = time.time() + timeout
                        ingame_reached = False

                        while time.time() < deadline:
                            display_monitor_screen(pkgs, cfg)
                            try:
                                cur = determine_account_status(pkg, info, cfg)
                            except Exception:
                                cur = "waiting"

                            if cur == "in_game":
                                add_log(f"✅ {username} ingame! Lanjut akun berikutnya.")
                                ingame_reached = True
                                break
                            elif cur in ("needs_kill", "offline"):
                                break
                            time.sleep(cfg["workspace_check_interval"])

                        if ingame_reached:
                            break

                        retry += 1
                        if retry < max_retry:
                            add_log(f"🔁 {username} gagal (retry {retry}/{max_retry})...")
                            stop_app(pkg)
                            time.sleep(cfg["restart_delay"])
                            ACCOUNT_STATE[pkg]["launch_time"] = time.time()
                            ACCOUNT_STATE[pkg]["json_start_time"] = None
                            ACCOUNT_STATE[pkg]["json_active"] = False
                            start_app_2step(pkg, *get_pkg_launch_params(pkg, cfg))
                            time.sleep(cfg["startup_delay"])
                        else:
                            add_log(f"⏱️ {username} max retry habis, skip.")

                display_monitor_screen(pkgs, cfg)

            current_time = time.time()

            # ─── AUTO ARRANGE INTERVAL ───────────────────────────────
            arrange_interval = cfg.get("arrange_interval", 30)
            if (current_time - last_arrange_time) >= arrange_interval:
                add_log("📐 Auto arrange windows...")
                try:
                    arrange_roblox_windows(cfg, pkgs)
                except Exception as e:
                    add_log(f"⚠️ Arrange error: {e}")
                last_arrange_time = current_time

            # ─── WEBHOOK INTERVAL ─────────────────────────────────────
            webhook_interval_seconds = cfg.get("webhook_interval", 10) * 60
            if cfg.get("webhook_enabled", False) and cfg.get("webhook_url", ""):
                if (current_time - last_webhook_time) >= webhook_interval_seconds:
                    try:
                        message = build_webhook_status_message(pkgs, cfg)
                        send_discord_webhook(
                            cfg["webhook_url"],
                            "📊 Status Update",
                            message,
                            3447003
                        )
                        add_log("📡 Webhook sent")
                    except Exception as e:
                        add_log(f"⚠️ Webhook error: {e}")
                    last_webhook_time = current_time

            # ─── RESTART INTERVAL ─────────────────────────────────────
            restart_interval_seconds = cfg.get("restart_interval", 0) * 60
            if restart_interval_seconds > 0:
                if (current_time - last_restart_time) >= restart_interval_seconds:
                    add_log("⏰ Auto restart time reached")
                    if cfg.get("webhook_enabled", False) and cfg.get("webhook_url", ""):
                        try:
                            send_discord_webhook(
                                cfg["webhook_url"],
                                "🔄 Auto Restart",
                                "Restarting all Roblox packages...",
                                16776960
                            )
                        except Exception:
                            pass
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
        print("-" * 40)
        freeform_status = "✅ ON" if cfg.get("freeform_enabled", False) else "❌ OFF"
        per_row = cfg.get("windows_per_row", 2)
        size_mode = "Fixed" if cfg.get("use_fixed_size", False) else "Auto"
        win_w_cfg = cfg.get("window_width", 540)
        win_h_cfg = cfg.get("window_height", 960)
        size_info = f"{win_w_cfg}x{win_h_cfg}" if cfg.get("use_fixed_size", False) else "Auto"
        print(f"11. 📐 Freeform Arranger [{freeform_status} | {per_row}/row | {size_info}]")
        print("12. 🌐 Add Server (per-package game_id / private link)")
        print("-" * 40)
        cookie_count = len(load_cookies())
        print(f"13. 🍪 Manage Cookies [{cookie_count} cookie(s)]")
        print("14. 🔑 Login Cookie")
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
            configure_freeform_window()
        elif c == "12":
            configure_server_per_pkg()
        elif c == "13":
            manage_cookies_menu()
        elif c == "14":
            login_cookies_menu()
        elif c == "0": 
            break

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    menu()
