from flask import Flask, Response, render_template, request, jsonify, make_response, redirect, url_for, session, flash, send_file
# Flask app creation is now below, after TEMPLATES_DIR and STATIC_DIR are set
"""
Simple MQTT-triggered RTSP capture + live viewer
- Starts a live RTSP viewer in a background thread (OpenCV window).
- Subscribes to an MQTT topic (default: anpr/trigger).
- On receiving a trigger message, captures 50 frames from the RTSP stream and saves them to mqtt_frames/<timestamp>/.
- Publishes a JSON result to anpr/result with count and save folder.

Edit the RTSP_URL and MQTT_BROKER constants below if needed.
"""

# ============================================================
# PASSWORD PROTECTION + TEMP FOLDER EXTRACTION SYSTEM
# ============================================================
import os
import sys
import hashlib
import getpass
import zipfile
import tempfile
import shutil
import atexit
import base64

# Import encryption library (needed for decryption)
# Note: This is optional - if cryptography is not installed, encryption features will be disabled
try:
    from cryptography.fernet import Fernet
    ENCRYPTION_AVAILABLE = True
except ImportError:
    # cryptography package not available - encryption features will be disabled
    ENCRYPTION_AVAILABLE = False
    Fernet = None  # Set to None to avoid NameError if referenced

# Import database drivers with availability checks
try:
    import pymysql
    pymysql.install_as_MySQLdb()  # Make it compatible with MySQLdb interface
    MYSQL_AVAILABLE = True
except ImportError:
    MYSQL_AVAILABLE = False

try:
    import psycopg2
    from psycopg2 import sql
    POSTGRESQL_AVAILABLE = True
except ImportError:
    POSTGRESQL_AVAILABLE = False

try:
    import pymongo
    from pymongo import MongoClient
    MONGODB_AVAILABLE = True
except ImportError:
    MONGODB_AVAILABLE = False

# SECURE PASSWORD HASH - Change this to your own password hash
# To generate: hashlib.sha256("YOUR_PASSWORD".encode()).hexdigest()
CORRECT_PASSWORD_HASH = "cb46c5701723fbc455830b5801979c17c8f33c071db01bd7dc7affc6847eee59"  # Password: rajmines@9727

# Global temp directory for extracted resources
TEMP_RESOURCE_DIR = None

def check_password():
    """Verify password before starting application"""
    print("=" * 60)
    print("ANPR WEB SERVER - PASSWORD REQUIRED")
    print("=" * 60)
    print()
    
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            password = getpass.getpass(f"Enter password (attempt {attempt}/{max_attempts}): ")
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            
            if password_hash == CORRECT_PASSWORD_HASH:
                print("\n✓ Password correct! Starting ANPR server...\n")
                print("=" * 60)
                print()
                return True
            else:
                print(f"[!!] INCORRECT PASSWORD!")
                if attempt < max_attempts:
                    print()
                else:
                    print("\n[!!] Maximum attempts reached. Application cannot start.")
                    sys.exit(1)
        except KeyboardInterrupt:
            print("\n\n[!!] Cancelled.")
            sys.exit(1)
    
    return False

def get_decryption_key():
    """Get the encryption key for decrypting resources"""
    secret = b"rajmines@9727_ANPR_SECRET_KEY_2024"  # Must match encrypt_models.py
    key = hashlib.sha256(secret).digest()
    return base64.urlsafe_b64encode(key)

def decrypt_file(encrypted_file, output_zip):
    """Decrypt an encrypted .enc file to a temporary zip"""
    if not ENCRYPTION_AVAILABLE:
        raise RuntimeError("Cryptography library not available for decryption")
    
    key = get_decryption_key()
    fernet = Fernet(key)
    
    with open(encrypted_file, 'rb') as f:
        encrypted_data = f.read()
    
    decrypted_data = fernet.decrypt(encrypted_data)
    
    with open(output_zip, 'wb') as f:
        f.write(decrypted_data)

def extract_hidden_resources():
    """Extract hidden encrypted archives to temporary folder"""
    global TEMP_RESOURCE_DIR
    
    # Determine where to look for encrypted resource files
    if os.environ.get('ANPR_RESOURCE_DIR'):
        # Running from launcher with external resource directory
        app_dir = os.environ['ANPR_RESOURCE_DIR']
    elif getattr(sys, 'frozen', False):
        app_dir = os.path.dirname(sys.executable)
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Create temp directory
    TEMP_RESOURCE_DIR = tempfile.mkdtemp(prefix='anpr_res_')
    
    # Map encrypted files to folder names
    encrypted_archives = {
        '.res2.enc': 'weights',
        '.res3.enc': 'templates',
        '.res4.enc': 'static'
        # .res5.enc removed - PaddleOCR V5 models are auto-downloaded and cached
    }
    
    # Fallback to regular .zip files (for development)
    zip_archives = {
        '.res2.zip': 'weights',
        '.res3.zip': 'templates',
        '.res4.zip': 'static'
    }
    
    print("Loading application resources...")
    print(f"  Looking for encrypted files in: {app_dir}")

    # --- parallel decryption: all three archives decrypted simultaneously ---
    import concurrent.futures as _cf
    _extract_errors = []

    def _extract_one(enc_name, folder_name):
        enc_path = os.path.join(app_dir, enc_name)
        if os.path.exists(enc_path):
            try:
                temp_zip = os.path.join(TEMP_RESOURCE_DIR, f"{folder_name}.zip")
                decrypt_file(enc_path, temp_zip)
                with zipfile.ZipFile(temp_zip, 'r') as zf:
                    zf.extractall(TEMP_RESOURCE_DIR)
                os.remove(temp_zip)
                print(f"  [OK] Decrypted {folder_name}", flush=True)
            except Exception as e:
                _extract_errors.append(f"{folder_name}: {e}")
        else:
            zip_name = enc_name.replace('.enc', '.zip')
            zip_path = os.path.join(app_dir, zip_name)
            if os.path.exists(zip_path):
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(TEMP_RESOURCE_DIR)
                print(f"  [OK] Extracted {folder_name} (zip)", flush=True)
            else:
                regular_folder = os.path.join(app_dir, folder_name)
                if os.path.exists(regular_folder):
                    dest_folder = os.path.join(TEMP_RESOURCE_DIR, folder_name)
                    shutil.copytree(regular_folder, dest_folder)
                    print(f"  [OK] Using {folder_name} folder", flush=True)

    with _cf.ThreadPoolExecutor(max_workers=3) as _pool:
        _futs = [_pool.submit(_extract_one, k, v) for k, v in encrypted_archives.items()]
        _cf.wait(_futs)

    if _extract_errors:
        for _err in _extract_errors:
            print(f"  [!!] FAILED: {_err}", flush=True)
        sys.exit(1)

    print("[OK] Resources ready!\n")
    return TEMP_RESOURCE_DIR

def cleanup_temp_resources():
    """Clean up temporary resource directory on exit"""
    global TEMP_RESOURCE_DIR
    if TEMP_RESOURCE_DIR and os.path.exists(TEMP_RESOURCE_DIR):
        shutil.rmtree(TEMP_RESOURCE_DIR, ignore_errors=True)

# Register cleanup
atexit.register(cleanup_temp_resources)

# Check if running in production mode (from .exe launcher or frozen)
IS_PRODUCTION = getattr(sys, 'frozen', False) or os.environ.get('ANPR_PRODUCTION_MODE') == '1'

# Extract resources when in production mode (password check disabled)
if IS_PRODUCTION:
    # Password check disabled - application starts without password requirement
    # check_password()  # Commented out - no password required
    extract_hidden_resources()
    # PaddleOCR v3 will initialize later using automatic model download (no local models needed)

# ============================================================
# END OF PASSWORD PROTECTION SYSTEM
# ============================================================

import warnings

# ── Fix sys.stderr/stdout = None in frozen windowless exe ────────────────────
# PyInstaller windowless builds set sys.stderr and sys.stdout to None.
# tqdm / paddlex download code writes directly to sys.stderr, which crashes
# the entire PaddleOCR initialization if stderr is None.
import io as _io
if sys.stderr is None:
    sys.stderr = _io.StringIO()
if sys.stdout is None:
    sys.stdout = _io.StringIO()
# ─────────────────────────────────────────────────────────────────────────────

# Get base path - for frozen exe, use exe directory
if IS_PRODUCTION:
    if os.environ.get('ANPR_EXE_DIR'):
        BASE_PATH = os.environ['ANPR_EXE_DIR']
    else:
        BASE_PATH = os.path.dirname(sys.executable)
else:
    BASE_PATH = os.path.dirname(os.path.abspath(__file__))

# Set environment variables BEFORE any other imports (especially PaddleOCR)
os.environ.update({
    # ── CPU thread limiting (must be set before any numpy/paddle import) ──
    # OMP_NUM_THREADS MUST be 1 — this PaddlePaddle binary uses OpenBLAS which does NOT
    # support multi-threading and will crash or error if set > 1.
    'OMP_NUM_THREADS': '1',
    'MKL_NUM_THREADS': '1',
    # Skip remote connectivity check on every startup (models already cached locally)
    'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK': 'True',
    # Suppress PaddlePaddle warnings and logs
    'FLAGS_use_cuda': '0',
    'FLAGS_use_tensorrt': '0',
    'FLAGS_enable_pir_api': '0',  # Disable PIR API to avoid OneDNN errors
    'FLAGS_pir_apply_inplace_pass': '0',
    'GLOG_minloglevel': '2',  # Suppress INFO and WARNING from GLOG
    'GLOG_v': '0',  # Disable verbose logging
    # Suppress other warnings
    'TF_CPP_MIN_LOG_LEVEL': '3',
    'PYTHONWARNINGS': 'ignore',
    # Enable MKL-DNN/oneDNN for Intel CPU acceleration (2-4x faster inference)
    'MKLDNN_DISABLE': '0',
    'DNNL_VERBOSE': '0',
    # Prevent crash when both PyTorch (YOLO) and PaddlePaddle load OpenMP in the same process
    'KMP_DUPLICATE_LIB_OK': 'TRUE',
    # Suppress unauthenticated HuggingFace Hub warning (no token needed for cached models)
    'HF_HUB_VERBOSITY': 'error'
})


# Suppress Python warnings
warnings.filterwarnings('ignore')

# Import required modules
import time
import json
import cv2
import numpy as np
import threading
import concurrent.futures
import logging
import subprocess

# ── Suppress console windows for all subprocess calls on Windows ──────────────
# PaddleOCR / paddlex / paddle spawn subprocesses during initialisation.
# Without this patch each spawned process briefly shows a black cmd window.
# We only add CREATE_NO_WINDOW when the caller has NOT explicitly requested
# CREATE_NEW_CONSOLE (e.g. our own restart-batch helper keeps its console).
if os.name == 'nt':
    _CREATE_NO_WINDOW = 0x08000000
    _CREATE_NEW_CONSOLE = 0x00000010
    _orig_Popen_init = subprocess.Popen.__init__

    def _Popen_no_window(self, args, **kwargs):
        flags = kwargs.get('creationflags', 0)
        if not (flags & _CREATE_NEW_CONSOLE):
            kwargs['creationflags'] = flags | _CREATE_NO_WINDOW
        _orig_Popen_init(self, args, **kwargs)

    subprocess.Popen.__init__ = _Popen_no_window
# ──────────────────────────────────────────────────────────────────────────────

from datetime import datetime
import paho.mqtt.client as mqtt
import re
import queue as queue_module
import Levenshtein
from flask import Flask, Response, render_template, request, jsonify, make_response
import requests
import pyodbc
import base64
from PyQt5.QtCore import QUrl, Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QMainWindow, QFileDialog,
                              QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                              QPushButton, QProgressBar, QTextEdit, QSizePolicy)
from PyQt5.QtGui import QFont as _QFont
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineProfile

# Create a session object for proxy to maintain cookies
proxy_session = requests.Session()

# PaddleOCR initialization - will be initialized after models are confirmed available
ocr = None
_paddle_init_failed = False   # set True on first DLL/import failure to suppress retries
# Keep add_dll_directory handles alive for process lifetime.
# If these objects are GC'd, Windows removes the registered DLL directories.
_dll_dir_handles = []
_dll_dir_seen = set()
# PaddleOCR/PaddleX is NOT thread-safe: concurrent ocr.predict() calls crash the process.
# This lock serializes all OCR calls so only one runs at a time.
_ocr_lock = threading.Lock()


def _register_dll_dir(_dirpath: str):
    """Register a DLL directory once and keep handle alive (Windows/Python 3.8+)."""
    if not _dirpath:
        return
    try:
        _norm = os.path.normcase(os.path.normpath(_dirpath))
    except Exception:
        _norm = _dirpath
    if _norm in _dll_dir_seen:
        return
    _dll_dir_seen.add(_norm)

    if hasattr(os, 'add_dll_directory'):
        try:
            _h = os.add_dll_directory(_dirpath)
            _dll_dir_handles.append(_h)
        except Exception:
            pass

    _current_path = os.environ.get('PATH', '')
    _parts = _current_path.split(os.pathsep) if _current_path else []
    if _dirpath not in _parts:
        os.environ['PATH'] = _dirpath + os.pathsep + _current_path

def clean_corrupted_paddleocr_cache():
    """Delete corrupted/partial PaddleOCR model folders automatically.
    Handles: null bytes, zero-byte files, PermissionError (locked files),
    and partial downloads. Models stored in C:\\Users\\<username>\\.paddlex\\."""
    try:
        cache_base = os.path.join(os.path.expanduser('~'), '.paddlex', 'official_models')

        if not os.path.exists(cache_base):
            return

        print(f"Checking PaddleOCR cache: {cache_base}", flush=True)

        for folder_name in os.listdir(cache_base):
            folder_path = os.path.join(cache_base, folder_name)
            if not os.path.isdir(folder_path):
                continue

            bad = False
            reason = ''

            # Walk every file in the model folder
            try:
                for root, _, files in os.walk(folder_path):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        try:
                            size = os.path.getsize(fpath)
                            if size == 0:
                                bad = True
                                reason = f'zero-byte file: {fname}'
                                break
                            # Check yaml/pdmodel for null bytes (corrupt download)
                            if fname.endswith(('.yml', '.yaml', '.pdmodel')):
                                with open(fpath, 'rb') as f:
                                    chunk = f.read(1024)
                                if b'\x00' in chunk:
                                    bad = True
                                    reason = f'null bytes in {fname}'
                                    break
                        except PermissionError:
                            bad = True
                            reason = f'permission denied: {fname}'
                            break
                        except Exception:
                            bad = True
                            reason = f'unreadable: {fname}'
                            break
                    if bad:
                        break
            except PermissionError:
                bad = True
                reason = 'permission denied on folder'

            if bad:
                print(f"[~] Corrupted model '{folder_name}' ({reason}) — deleting...", flush=True)
                shutil.rmtree(folder_path, ignore_errors=True)
                if not os.path.exists(folder_path):
                    print(f"[OK] Deleted: {folder_path}", flush=True)
                else:
                    print(f"[!] Could not fully delete: {folder_path}", flush=True)

    except Exception as e:
        print(f"Cache cleanup failed: {e}", flush=True)

def initialize_paddleocr():
    """Initialize PaddleOCR with PP-OCRv4 mobile models for fast CPU inference.
    
    PP-OCRv4 mobile_det is ~4x faster than PP-OCRv5 server_det on CPU with no
    meaningful accuracy loss for pre-cropped plate images.
    
    On first run: PaddleX downloads models (~35 MB) from internet and caches to:
      C:\\Users\\<username>\\.paddlex\\official_models\\
    On subsequent runs: uses cached models instantly (no internet needed).
    """
    global ocr, _paddle_init_failed
    if ocr is not None:
        return ocr  # Already initialized
    if _paddle_init_failed:
        return None  # Don't retry after a permanent DLL/import failure

    # Clean corrupted cache before initialization
    clean_corrupted_paddleocr_cache()

    print("Initializing PaddleOCR V4 (mobile models)...", flush=True)
    cache_dir = os.path.join(os.path.expanduser('~'), '.paddlex', 'official_models')
    print(f"  Cache location: {cache_dir}", flush=True)

    # PP-OCRv4 mobile models — lighter and faster on CPU than PP-OCRv5 server_det
    required_models = [
        'PP-OCRv4_mobile_det',
        'en_PP-OCRv4_mobile_rec',
    ]
    models_cached = all(os.path.exists(os.path.join(cache_dir, m)) for m in required_models)

    if models_cached:
        print("  [OK] V4 mobile models already cached - initializing offline", flush=True)
    else:
        print("  V4 mobile models not in cache - downloading from PaddlePaddle CDN...", flush=True)

    # DLL fix for libpaddle.pyd on Windows (Python 3.8+).
    # Walk the ENTIRE paddle package tree and register every directory that
    # contains .dll files — covers any paddle version's layout (libs/, base/, root, etc.)
    # Also prepend to PATH for internal LoadLibrary() calls inside libpaddle.pyd.
    _site_pkgs = os.path.join(sys.prefix, 'Lib', 'site-packages')
    _paddle_root = os.path.join(_site_pkgs, 'paddle')
    if not os.path.isdir(_paddle_root):
        # Fallback: scan sys.path
        for _sp in sys.path:
            _pr = os.path.join(_sp, 'paddle')
            if os.path.isdir(_pr):
                _paddle_root = _pr
                break
    if os.path.isdir(_paddle_root):
        _paddle_libs = os.path.join(_paddle_root, 'libs')
        _paddle_base = os.path.join(_paddle_root, 'base')

        # Step 1: Copy paddle\libs\*.dll → paddle\base\ (per-file, skip locked files)
        if os.path.isdir(_paddle_libs) and os.path.isdir(_paddle_base):
            for _name in os.listdir(_paddle_libs):
                if _name.lower().endswith('.dll'):
                    try:
                        shutil.copy2(os.path.join(_paddle_libs, _name),
                                     os.path.join(_paddle_base, _name))
                    except Exception:
                        pass  # skip locked files silently

        # Step 2: Copy VC++ runtime DLLs into paddle\base\ (per-file, skip if locked/exists)
        # Source priority: pyembed (bundled) → System32
        _vc_dlls = (
            'vcruntime140.dll', 'vcruntime140_1.dll',
            'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
            'concrt140.dll', 'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll',
            'vcomp140.dll',  # OpenMP runtime — required by mkldnn.dll → phi.dll → libpaddle.pyd
        )
        if os.path.isdir(_paddle_base):
            _vc_sources = []
            _pyembed_local = os.path.join(BASE_PATH, 'pyembed')
            if os.path.isdir(_pyembed_local):
                _vc_sources.append(_pyembed_local)
            _sys32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
            if os.path.isdir(_sys32):
                _vc_sources.append(_sys32)
            for _dll in _vc_dlls:
                _dst = os.path.join(_paddle_base, _dll)
                if os.path.isfile(_dst):
                    continue
                for _sdir in _vc_sources:
                    _src = os.path.join(_sdir, _dll)
                    if os.path.isfile(_src):
                        try:
                            shutil.copy2(_src, _dst)
                        except Exception:
                            pass
                        break

        # Step 3: Register paddle DLL directories + System32 with os.add_dll_directory
        _sys32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
        if os.path.isdir(_sys32):
            _register_dll_dir(_sys32)
        for _dirpath, _dirnames, _filenames in os.walk(_paddle_root):
            if any(f.lower().endswith('.dll') for f in _filenames):
                _register_dll_dir(_dirpath)

    # Also include pyembed bundled vcruntime/msvcp DLLs
    _pyembed = os.path.join(BASE_PATH, 'pyembed')
    if os.path.isdir(_pyembed):
        _register_dll_dir(_pyembed)

    try:
        from paddleocr import PaddleOCR
        ocr = PaddleOCR(
            ocr_version='PP-OCRv4',              # mobile_det + mobile_rec (~4x faster than v5 server_det)
            lang='en',
            device='cpu',
            use_doc_orientation_classify=False,  # disable: PP-LCNet doc-ori model not needed for plates
            use_doc_unwarping=False,             # disable: UVDoc unwarping model not needed for plates
            use_textline_orientation=False,       # plates are always horizontal — skips orientation classifier
            det_limit_side_len=640,               # cap det input to 640px — avoids 960px padding overhead
            det_db_unclip_ratio=1.01,
            det_db_box_thresh=0.65,
            det_db_thresh=0.45
        )
        print("[OK] PaddleOCR V4 mobile initialized successfully!", flush=True)
        return ocr

    except Exception as e:
        print(f"[!!] PaddleOCR initialization failed: {e}", flush=True)
        import traceback
        print(traceback.format_exc(), flush=True)
        ocr = None
        _paddle_init_failed = True  # Suppress all further retries
        logger.warning("PaddleOCR initialization failed - number plate detection will not work")
        return None

# Set paths for resources (models folder removed - PaddleOCR uses auto-download)
if IS_PRODUCTION and TEMP_RESOURCE_DIR:
    WEIGHTS_DIR = os.path.join(TEMP_RESOURCE_DIR, "weights")
    TEMPLATES_DIR = os.path.join(TEMP_RESOURCE_DIR, "templates")
    STATIC_DIR = os.path.join(TEMP_RESOURCE_DIR, "static")
else:
    WEIGHTS_DIR = os.path.join(BASE_PATH, "weights")
    TEMPLATES_DIR = os.path.join(BASE_PATH, "templates")
    STATIC_DIR = os.path.join(BASE_PATH, "static")
# PaddleOCR and YOLO initialized in background thread after Flask starts — no blocking at import time

# ----- System log: redirect stdout + logging to system.log -----
# system.log captures ALL output in one chronological sequence:
#   print() startup messages, PaddleOCR init, YOLO load, DB connect,
#   Flask server lines, warmup, API requests — exactly like the old terminal.
_SYSTEM_LOG_FILE = os.path.join(BASE_PATH, "system.log")
_system_log_stream = open(_SYSTEM_LOG_FILE, "a", encoding="utf-8", buffering=1)
# Write a restart separator only when running directly (dev mode).
# In production the launcher writes the banner BEFORE the subprocess starts,
# so it always appears first — before any print() resource-loading output.
if not os.environ.get('ANPR_PRODUCTION_MODE'):
    _now_str = __import__('datetime').datetime.now().strftime('%Y-%m-%d  %H:%M:%S')
    _banner_line = f'= APP STARTED  {_now_str}'.ljust(79) + '='
    _system_log_stream.write("\n" + "=" * 80 + "\n")
    _system_log_stream.write(_banner_line + "\n")
    _system_log_stream.write("=" * 80 + "\n")
    _system_log_stream.write("-" * 80 + "\n")
    _system_log_stream.flush()

import re as _re
_ANSI_ESCAPE_RE = _re.compile(r'\x1b\[[0-9;]*m|\x1b\[[0-9;]*[A-Za-z]')

def _strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from a string."""
    return _ANSI_ESCAPE_RE.sub('', text)


class _PlainFileFormatter(logging.Formatter):
    """Plain (no ANSI) formatter for system.log.

    Two problems solved here:
    1. werkzeug embeds ANSI in its log records — strip from the FINAL formatted
       string (after % substitution) so codes in record.args are caught too.
    2. werkzeug emits its startup banner as a SINGLE multi-line record
       ("WARNING: This is a development server.\\n * Running on all addresses…").
       Split those lines and prefix each with its own timestamp so the file
       stays structured.
    """
    def format(self, record):
        # Let the standard formatter do % substitution first, then strip ANSI
        formatted = _strip_ansi(super().format(record))

        # Re-prefix every continuation line that has its own content
        if '\n' in formatted:
            ts    = self.formatTime(record, self.datefmt)
            level = record.levelname
            pfx   = f"{ts} - {level} - "
            lines = formatted.split('\n')
            out   = [lines[0]]
            for ln in lines[1:]:
                clean = ln.lstrip(' *').strip()   # strip werkzeug's " * " leader
                if clean:
                    out.append(f"{pfx}{clean}")
            formatted = '\n'.join(out)

        # Visually flag ERROR / CRITICAL lines so they stand out in the log
        if record.levelno >= logging.ERROR:
            bar = '!' * 80
            formatted = f"{bar}\n{formatted}\n{bar}"

        return formatted


# Timestamped wrapper so print() calls also get a timestamp prefix in the log
class _TimestampedStream:
    """Wraps the log file so print() output gets a plain timestamp prefix.
    Strips ANSI codes that werkzeug writes to stdout."""
    def __init__(self, stream):
        self._stream = stream
        self._buf    = ''

    def write(self, text):
        self._buf += text
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            line = _strip_ansi(line)
            if line.strip():               # skip blank/whitespace-only lines
                import datetime
                ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
                self._stream.write(f"{ts} - PRINT - {line}\n")
                self._stream.flush()

    def flush(self):
        self._stream.flush()

    def fileno(self):
        return self._stream.fileno()

sys.stdout = _TimestampedStream(_system_log_stream)  # print() → timestamped line in system.log
sys.stderr = _TimestampedStream(_system_log_stream)  # stderr (PaddleOCR ANSI codes) → stripped + logged

_log_handler = logging.StreamHandler(_system_log_stream)
_log_handler.setFormatter(_PlainFileFormatter("%(asctime)s - %(levelname)s - %(message)s"))

def _restore_log_handler():
    """Re-attach our handler to root after PaddlePaddle/PaddleX wipes it.
    PaddlePaddle calls logging.basicConfig() internally which clears root handlers.
    Call this after initialize_paddleocr() to restore logging for all subsequent code."""
    if _log_handler not in logging.root.handlers:
        logging.root.handlers.clear()
        logging.root.addHandler(_log_handler)
    logging.root.setLevel(logging.DEBUG)

# Initial setup — also attached directly to our named logger and werkzeug so
# PaddlePaddle cannot break them even if it clears root handlers.
logging.root.setLevel(logging.DEBUG)
logging.root.handlers.clear()
logging.root.addHandler(_log_handler)

logger = logging.getLogger("simple_mqtt_rtsp")
logger.addHandler(_log_handler)   # own copy — survives PaddlePaddle root wipe
logger.propagate = False           # don't double-log to root

# Suppress matplotlib DEBUG noise — it floods the log with internal paths on every import
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)

# Suppress verbose DEBUG noise from paddlex/huggingface download internals.
# These loggers emit per-byte HTTP header / file-lock acquire/release lines during
# model download and would completely flood system.log on first run.
for _noisy_logger in (
    "httpx",
    "httpcore",
    "httpcore.http11",
    "httpcore.connection",
    "huggingface_hub",
    "huggingface_hub.file_download",
    "huggingface_hub.utils._http",
    "filelock",
    "hf_xet",
    "modelscope",
    "modelscope.hub",
):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)


class SuppressRecordsPollFilter(logging.Filter):
    """Suppress noisy high-frequency request logs that clutter the terminal.
    - GET /api/records (5-second UI poll)
    - GET /api/image/*  (plate image fetches / cache revalidation 304s)
    - GET /api/warmup_status (polled every 5 s during warmup)
    """
    def filter(self, record):
        msg = record.getMessage()
        if "GET /api/records" in msg and "200" in msg:
            return False
        if "GET /api/image/" in msg:
            return False
        if "GET /api/warmup_status" in msg:
            return False
        if "GET /api/system_status" in msg:
            return False
        return True


# Suppress periodic GET /api/records request logs (UI polls every 5 sec)
_werkzeug_logger = logging.getLogger("werkzeug")
_werkzeug_logger.addHandler(_log_handler)   # own copy — survives root wipe
_werkzeug_logger.propagate = False
_werkzeug_logger.addFilter(SuppressRecordsPollFilter())

# ----- YOLO Plate Model -----
# ultralytics (torch) is NOT imported here — torch takes 4-8 s to load and would
# block Flask from starting and the login page from rendering.
# It is imported lazily inside background_init() after Flask is already serving.
YOLO = None  # assigned by _import_yolo_lazy() in background_init

def _import_yolo_lazy():
    """Import ultralytics.YOLO and configure PyTorch threads.
    Called once inside background_init() so the login page renders immediately."""
    global YOLO
    if YOLO is not None:
        return YOLO
    from ultralytics import YOLO as _YOLO  # noqa: E402
    YOLO = _YOLO
    try:
        import torch
        _cpu_cores = os.cpu_count() or 1
        _torch_intraop = max(1, min(8, _cpu_cores))
        torch.set_num_threads(_torch_intraop)
        try:
            torch.set_num_interop_threads(1)
        except Exception:
            pass
        logger.debug(f"PyTorch threads configured: intraop={_torch_intraop}, interop=1")
    except Exception as _e:
        logger.debug(f"PyTorch thread config skipped: {_e}")
    return YOLO

# Path to weights folder - using WEIGHTS_DIR set earlier
PLATE_MODEL_PATH = os.path.join(WEIGHTS_DIR, "latest_best.pt")

# plate_model and blur_model loaded in background thread after Flask starts
plate_model = None
blur_model = None  # Blur model removed; direct OCR path used

# System initialization status — polled by /api/system_status frontend banner
_system_status = {
    'overall': 'loading',
    'paddleocr': 'pending',
    'yolo': 'pending',
    'database': 'pending',
    'camera': 'pending',
    'message': 'Starting up...'
}

# Event set to True once warmup_inference() completes.
# read_n_frames() waits on this so warmup and real inference never run concurrently.
_warmup_complete = threading.Event()


def reload_models_from_weights_dir():
    """Hot-reload plate model from WEIGHTS_DIR into memory.
    Called after a successful model upload so the user doesn't need to restart."""
    global plate_model
    reloaded = []
    errors = []
    plate_path = os.path.join(WEIGHTS_DIR, 'latest_best.pt')
    if os.path.isfile(plate_path):
        try:
            _import_yolo_lazy()
            plate_model = YOLO(plate_path)
            logger.info(f'[RELOAD] Plate model reloaded: {plate_path}')
            reloaded.append('latest_best.pt')
        except Exception as e:
            logger.warning(f'[RELOAD] Plate model reload failed: {e}')
            errors.append(str(e))
    # Kick off background warmup so the first real inference after upload is fast
    if reloaded:
        t = threading.Thread(target=_warmup_yolo_models, daemon=True)
        t.start()
    return reloaded, errors


def _warmup_yolo_models():
    """Run a quick dummy inference on plate model after hot-reload."""
    try:
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        if plate_model is not None:
            try:
                logger.debug('[RELOAD WARMUP] Warming up plate model...')
                plate_model.predict(dummy, conf=0.35, verbose=False, imgsz=640)
                logger.debug('[RELOAD WARMUP] Plate model ready')
            except Exception as e:
                logger.debug(f'[RELOAD WARMUP] Plate warmup error: {e}')
        logger.info('[RELOAD WARMUP] Plate model warmed up and ready for fast inference')
    except Exception as e:
        logger.debug(f'[RELOAD WARMUP] Warmup thread error: {e}')


def warmup_inference():
    """
    Run a warmup pass on YOLO and OCR using static images from the warmup/ folder.
    Falls back to a dummy black frame if the folder is absent or empty.
    """
    try:
        logger.info("Starting warmup inference...")

        # Wait for PaddleOCR to be initialized (with timeout)
        max_wait = 30
        wait_interval = 0.5
        waited = 0
        while ocr is None and waited < max_wait and not _paddle_init_failed:
            time.sleep(wait_interval)
            waited += wait_interval
            if ocr is None and not _paddle_init_failed:
                try:
                    initialize_paddleocr()
                except Exception:
                    pass

        if ocr is None:
            logger.warning("PaddleOCR not available for warmup - will skip OCR warmup")

        # ── Load warmup frame from warmup/ folder (fall back to dummy) ──
        warmup_frame = None
        warmup_dir = os.path.join(BASE_PATH, 'warmup')
        _img_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
        if os.path.isdir(warmup_dir):
            _candidates = sorted([
                f for f in os.listdir(warmup_dir)
                if os.path.splitext(f)[1].lower() in _img_exts
            ])
            for _fname in _candidates:
                _img = cv2.imread(os.path.join(warmup_dir, _fname))
                if _img is not None:
                    warmup_frame = _img
                    logger.info(f"[WARMUP] Using static image: {_fname} ({_img.shape[1]}\u00d7{_img.shape[0]})")
                    break
        if warmup_frame is None:
            warmup_frame = np.zeros((640, 640, 3), dtype=np.uint8)
            logger.info("[WARMUP] No images found in warmup/ folder — using dummy frame")

        # ── Step 1: Warm up YOLO plate detection model ──
        _warmup_plate_crop = None
        if plate_model is not None:
            try:
                logger.debug("Warming up plate detection model...")
                # Warm up with a fixed 640x640 frame to reduce preprocessing overhead.
                _yolo_frame = warmup_frame
                try:
                    if _yolo_frame is not None and (_yolo_frame.shape[0] != 640 or _yolo_frame.shape[1] != 640):
                        _yolo_frame = cv2.resize(_yolo_frame, (640, 640), interpolation=cv2.INTER_AREA)
                except Exception:
                    _yolo_frame = warmup_frame

                _t0 = time.perf_counter()
                _wu_results = plate_model.predict(
                    _yolo_frame,
                    conf=0.20,
                    verbose=False,
                    imgsz=640,
                    max_det=1,
                )
                _dt = time.perf_counter() - _t0
                logger.debug(f"Plate model warmup completed in {_dt:.2f}s")
                # Try to get a plate crop from the detected boxes for OCR warmup
                if _wu_results and len(_wu_results[0].boxes) > 0:
                    _box = _wu_results[0].boxes[0].xyxy[0].cpu().numpy().astype(int)
                    _x1, _y1, _x2, _y2 = (
                        max(0, int(_box[0])), max(0, int(_box[1])),
                        int(_box[2]), int(_box[3])
                    )
                    _crop = _yolo_frame[_y1:_y2, _x1:_x2]
                    if _crop.size > 0:
                        _warmup_plate_crop = cv2.resize(_crop, (640, 128))
                        logger.debug(f"[WARMUP] Plate crop extracted ({_x2-_x1}×{_y2-_y1}px) — using for OCR warmup")
            except Exception as e:
                logger.debug(f"Plate model warmup failed: {e}")

        # ── Step 2: Warm up OCR using the plate crop (or small dummy) ──
        # Never pass the full camera frame — it is too slow.
        if _warmup_plate_crop is None:
            # No plate detected: use a small dummy sized like a plate crop
            _warmup_plate_crop = np.zeros((128, 640, 3), dtype=np.uint8)
            logger.debug("[WARMUP] No plate detected — using small dummy for OCR warmup")

        if ocr is not None:
            try:
                logger.debug("Warming up PaddleOCR...")
                _t0 = time.perf_counter()
                extract_text_from_image(_warmup_plate_crop)
                _dt = time.perf_counter() - _t0
                logger.debug(f"OCR warmup completed in {_dt:.2f}s")
            except Exception as e:
                logger.debug(f"OCR warmup failed: {e}")
        else:
            logger.debug("Skipping OCR warmup - OCR not available")

        logger.info("\u2713 Warmup inference completed - models are ready for fast processing")
        _warmup_complete.set()
    except Exception as e:
        logger.debug(f"Global warmup failed: {e}")
        _warmup_complete.set()  # Always unblock even on error

# ----- CONFIG -----
# Default configuration values (will be overridden by config.json if exists)
RTSP_URL = ""
RTSP_TRANSPORT = "tcp"  # "tcp" for reliable streaming (recommended for port 554 cameras), "udp" for lower latency
ENABLE_MQTT = True  # Set to False to disable MQTT integration
MQTT_BROKER = ""
MQTT_PORT = 1883
MQTT_TRIGGER_TOPIC = "anpr/trigger"
MQTT_PUBLISH_TOPIC = "anpr/result"
CAPTURE_COUNT = 5 # 5 frames: matches backup/server_det approach
SAVE_ROOT = "mqtt_frames"
# Processed image target size
TARGET_WIDTH = 640   # 640 matches server_det inference resolution
TARGET_HEIGHT = 640  # 640 matches server_det inference resolution
USE_LETTERBOX = True
# Frame processing interval (skip frames to reduce CPU)
FRAME_SKIP_INTERVAL = 3  # Process every 3rd frame only
# YOLO inference confidence thresholds (user-configurable)
CONF_THRESH_640  = 0.85  # default confidence for 640 inference
CONF_THRESH_1280 = 0.75  # default confidence for 1280 inference
WEB_STREAM_FPS = 15  # Reduced from 30 FPS
# Live stream control - Set to False to disable web streaming and save more CPU
ENABLE_LIVE_STREAM = True 
# OCR processing resolution (higher for better accuracy)
OCR_CROP_WIDTH = 1280  # Upscale plate crops to this width for OCR
OCR_CROP_HEIGHT = 320  # Upscale plate crops to this height for OCR
# SQL Server Configuration
DB_TYPE = "MSSQL"  # Database type: MSSQL, MySQL, PostgreSQL, MongoDB
SQL_SERVER = ""
SQL_DATABASE = ""
SQL_TABLE = "PlateRecognitions"
SQL_USERNAME = ""
SQL_PASSWORD = ""
ENABLE_SQL_LOGGING = True  # Set to False to disable SQL database logging
# Design baseline resolution (dev machine). Zoom is auto-calculated from actual screen size.
_DESIGN_WIDTH  = 1366
_DESIGN_HEIGHT = 768

def _compute_pyqt_zoom():
    """Return a zoom factor so the UI fits the current screen exactly as it looks
    on the 1366×768 dev machine.  Works before QApplication is created."""
    try:
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return 1.0
        screen = app.primaryScreen()
        if screen is None:
            return 1.0
        geom = screen.availableGeometry()   # excludes taskbar
        sw, sh = geom.width(), geom.height()
        # Scale to fit both axes; use the smaller ratio so nothing is clipped
        zoom = min(sw / _DESIGN_WIDTH, sh / _DESIGN_HEIGHT)
        # Neutralise Windows DPI scaling — AA_EnableHighDpiScaling already scales
        # the entire Qt window by devicePixelRatio. Without dividing here, zoom and
        # DPI scale multiply together making UI elements appear too large.
        dpr = screen.devicePixelRatio()
        if dpr and dpr > 0:
            zoom = zoom / dpr
        # Never scale UP — on screens larger than the design (e.g. 1920×1080)
        # let the web page fill the extra space naturally via its own CSS.
        # Only scale DOWN for screens smaller than the design.
        zoom = min(zoom, 1.0)
        zoom = round(max(zoom, 0.5), 2)
        return zoom
    except Exception:
        return 1.0

# Location name, coordinates, and ID (for config)
LOCATION_NAME = ""
LOCATION_COORDS = ""
LOCATION_ID = ""

def _get_wb_info_path():
    """Return absolute path to wb_info.json next to the exe (production) or CWD (dev)."""
    if IS_PRODUCTION and BASE_PATH:
        return os.path.join(BASE_PATH, 'wb_info.json')
    return 'wb_info.json'

# Header branding (for config) - keep blank unless configured
DEPT_TITLE = "Departments of mines and Geology"
DEPT_SUBTITLE = "Government Of Rajasthan"
# Path relative to /static (e.g. "govt-logo.png" or "branding/department_logo.png")
DEPT_LOGO_FILENAME = "branding/department_logo.png"
DEPT_BRANDING_ENABLED = True
FOOTER_DEPT = "Department of Mines & Geology, Govt. of Rajasthan"

# Detection bounding box padding (pixels to add on each side before OCR; helps tilted plates / cut digits)
BOX_PADDING_WIDTH_PX = 10   # pixels to add on left and right
BOX_PADDING_HEIGHT_PX = 10  # pixels to add on top and bottom
# Blur model disabled — direct OCR path always used
ENABLE_BLUR_MODEL = False
# Regex correction: when True, clean_plate_text() + correct_plate_ocr() are applied after OCR (state code fix, digit/char substitution, plate format regex).
# When False, only basic alphanumeric stripping is done — raw OCR text is used as-is.
ENABLE_REGEX_CORRECTION = True
# Auto-login: when True, the login page auto-submits using the remembered credentials
# (skipping the manual sign-in step). Users can toggle this in System Configuration → Settings.
AUTO_LOGIN = True

# ══════════════════════════════════════════════════════════════════════════════
# DB CREDENTIALS  (.env)
# DB credentials are stored in a plain .env file (KEY=VALUE per line).
# All other settings are stored in the anpr_configuration table inside the DB.
# ══════════════════════════════════════════════════════════════════════════════

def _get_env_path():
    """Return the path to .env (next to exe in production, cwd in dev)."""
    if IS_PRODUCTION and BASE_PATH:
        return os.path.join(BASE_PATH, '.env')
    return '.env'


def load_connection_enc():
    """Load DB credentials and RTSP URL from .env file.
    Returns a dict with db_type/db_server/db_name/db_username/db_password/rtsp_url,
    or an empty dict if the file doesn't exist."""
    path = _get_env_path()
    if not os.path.exists(path):
        return {}
    try:
        creds = {}
        with open(path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                creds[key.strip().lower()] = val.strip()
        # Normalise keys to match expected dict keys
        return {
            'db_type':     creds.get('db_type',     ''),
            'db_server':   creds.get('db_server',   ''),
            'db_name':     creds.get('db_name',     ''),
            'db_username': creds.get('db_username', ''),
            'db_password': creds.get('db_password', ''),
            'rtsp_url':    creds.get('rtsp_url',    ''),
        }
    except Exception as e:
        logger.warning(f"Could not read .env: {e}")
        return {}


def save_connection_enc(creds: dict):
    """Persist DB credentials and RTSP URL to .env file."""
    try:
        path = _get_env_path()
        # Preserve any existing values not supplied in creds (read-merge)
        existing = load_connection_enc()
        lines = [
            f"DB_TYPE={creds.get('db_type', existing.get('db_type', 'MSSQL'))}",
            f"DB_SERVER={creds.get('db_server', existing.get('db_server', ''))}",
            f"DB_NAME={creds.get('db_name', existing.get('db_name', ''))}",
            f"DB_USERNAME={creds.get('db_username', existing.get('db_username', ''))}",
            f"DB_PASSWORD={creds.get('db_password', existing.get('db_password', ''))}",
            f"RTSP_URL={creds.get('rtsp_url', existing.get('rtsp_url', ''))}",
        ]
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
        logger.info("\u2713 Credentials/.env saved to .env")
    except Exception as e:
        logger.error(f"Failed to save .env: {e}")


# ── ANPR_Settings table (stores all non-credential config in the DB) ─────────

_SETTINGS_TABLE = 'anpr_configuration'
# Set to True once the settings table has been confirmed to exist — skips the
# CREATE/ALTER check on subsequent saves so the POST route returns instantly.
_settings_table_ready = False

# Canonical order of settings as they should appear in the DB table
_SETTINGS_ORDER = [
    'rtsp_url', 'rtsp_transport', 'location_coords',
    'dept_title', 'dept_subtitle', 'dept_branding_enabled', 'dept_logo_filename', 'footer_dept',
    'mqtt_enabled', 'mqtt_broker', 'mqtt_port', 'mqtt_subscribe_topic', 'mqtt_publish_topic',
    'box_padding_width_px', 'box_padding_height_px',
    'enable_blur_model', 'enable_regex_correction', 'frame_skip_interval',
    'db_type',
    'conf_thresh_640', 'conf_thresh_1280',
]

def _create_settings_table_sql():
    """Ensure ANPR_Settings table exists in the configured database.
    After the first successful run the module-level flag _settings_table_ready
    is set so subsequent calls return immediately without any DB round-trip."""
    global _settings_table_ready
    if _settings_table_ready:
        return True
    try:
        conn = get_sql_connection()
        if not conn:
            return False
        cur = conn.cursor()
        if DB_TYPE == 'MSSQL':
            cur.execute(f"""
                IF NOT EXISTS (SELECT * FROM sys.tables WHERE name=N'{_SETTINGS_TABLE}')
                CREATE TABLE [{_SETTINGS_TABLE}] (
                    SettingKey   NVARCHAR(100) NOT NULL PRIMARY KEY,
                    SettingValue NVARCHAR(MAX)  NULL,
                    SortOrder    INT            NOT NULL DEFAULT 0
                )""")
            # Add SortOrder column to existing tables that predate this change
            cur.execute(f"""
                IF NOT EXISTS (
                    SELECT 1 FROM sys.columns
                    WHERE object_id=OBJECT_ID(N'[{_SETTINGS_TABLE}]') AND name=N'SortOrder'
                )
                ALTER TABLE [{_SETTINGS_TABLE}] ADD SortOrder INT NOT NULL DEFAULT 0""")
        elif DB_TYPE == 'MySQL':
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS `{_SETTINGS_TABLE}` (
                    SettingKey   VARCHAR(100)  NOT NULL PRIMARY KEY,
                    SettingValue LONGTEXT,
                    SortOrder    INT           NOT NULL DEFAULT 0
                ) CHARACTER SET utf8mb4""")
            cur.execute(f"""
                ALTER TABLE `{_SETTINGS_TABLE}`
                ADD COLUMN IF NOT EXISTS SortOrder INT NOT NULL DEFAULT 0""")
        elif DB_TYPE == 'PostgreSQL':
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{_SETTINGS_TABLE}" (
                    "SettingKey"   VARCHAR(100) NOT NULL PRIMARY KEY,
                    "SettingValue" TEXT,
                    "SortOrder"    INTEGER      NOT NULL DEFAULT 0
                )""")
            cur.execute(f"""
                ALTER TABLE "{_SETTINGS_TABLE}"
                ADD COLUMN IF NOT EXISTS "SortOrder" INTEGER NOT NULL DEFAULT 0""")
        conn.commit()
        cur.close()
        conn.close()
        _settings_table_ready = True
        return True
    except Exception as e:
        logger.error(f"ANPR_Settings table creation failed: {e}")
        return False


def load_settings_from_db():
    """Read all rows from ANPR_Settings → return a plain dict (key→value strings).
    Returns empty dict on any error."""
    try:
        conn = get_sql_connection()
        if not conn:
            return {}
        cur = conn.cursor()
        if DB_TYPE in ('MSSQL',):
            cur.execute(f"SELECT SettingKey, SettingValue FROM [{_SETTINGS_TABLE}] ORDER BY SortOrder, SettingKey")
        elif DB_TYPE == 'MySQL':
            cur.execute(f"SELECT SettingKey, SettingValue FROM `{_SETTINGS_TABLE}` ORDER BY SortOrder, SettingKey")
        else:
            cur.execute(f'SELECT "SettingKey", "SettingValue" FROM "{_SETTINGS_TABLE}" ORDER BY "SortOrder", "SettingKey"')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {str(r[0]): r[1] for r in rows}
    except Exception as e:
        logger.warning(f"Could not read ANPR_Settings: {e}")
        return {}


def save_settings_to_db(settings: dict):
    """Upsert every key/value in *settings* into ANPR_Settings.
    DB credentials (db_server/db_name/db_username/db_password) are stripped — they
    live in .env, not the DB."""
    _skip = {'db_server', 'db_name', 'db_username', 'db_password',
             'location_name', 'location_id'}
    try:
        conn = get_sql_connection()
        if not conn:
            return False
        cur = conn.cursor()
        for key, val in settings.items():
            if key in _skip:
                continue
            val_str = json.dumps(val) if not isinstance(val, str) else val
            sort_idx = _SETTINGS_ORDER.index(key) if key in _SETTINGS_ORDER else len(_SETTINGS_ORDER)
            if DB_TYPE == 'MSSQL':
                cur.execute(f"""
                    IF EXISTS (SELECT 1 FROM [{_SETTINGS_TABLE}] WHERE SettingKey=?)
                        UPDATE [{_SETTINGS_TABLE}] SET SettingValue=?, SortOrder=? WHERE SettingKey=?
                    ELSE
                        INSERT INTO [{_SETTINGS_TABLE}] (SettingKey, SettingValue, SortOrder) VALUES (?,?,?)
                """, key, val_str, sort_idx, key, key, val_str, sort_idx)
            elif DB_TYPE == 'MySQL':
                cur.execute(
                    f"INSERT INTO `{_SETTINGS_TABLE}` (SettingKey, SettingValue, SortOrder) VALUES (%s,%s,%s) "
                    f"ON DUPLICATE KEY UPDATE SettingValue=%s, SortOrder=%s",
                    (key, val_str, sort_idx, val_str, sort_idx))
            else:  # PostgreSQL
                cur.execute(
                    f'INSERT INTO "{_SETTINGS_TABLE}" ("SettingKey","SettingValue","SortOrder") VALUES (%s,%s,%s) '
                    f'ON CONFLICT ("SettingKey") DO UPDATE SET "SettingValue"=EXCLUDED."SettingValue", "SortOrder"=EXCLUDED."SortOrder"',
                    (key, val_str, sort_idx))
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"✓ Settings saved to {_SETTINGS_TABLE} in DB")
        return True
    except Exception as e:
        logger.error(f"save_settings_to_db failed: {e}")
        return False


def _reorder_settings_in_db():
    """Update SortOrder for all existing rows in anpr_configuration to match
    _SETTINGS_ORDER. Called on startup so rows inserted before the SortOrder
    column existed are correctly ordered without requiring a Save Configuration."""
    try:
        conn = get_sql_connection()
        if not conn:
            return
        cur = conn.cursor()
        for idx, key in enumerate(_SETTINGS_ORDER):
            if DB_TYPE == 'MSSQL':
                cur.execute(
                    f"UPDATE [{_SETTINGS_TABLE}] SET SortOrder=? WHERE SettingKey=?",
                    idx, key)
            elif DB_TYPE == 'MySQL':
                cur.execute(
                    f"UPDATE `{_SETTINGS_TABLE}` SET SortOrder=%s WHERE SettingKey=%s",
                    (idx, key))
            else:
                cur.execute(
                    f'UPDATE "{_SETTINGS_TABLE}" SET "SortOrder"=%s WHERE "SettingKey"=%s',
                    (idx, key))
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"✓ SortOrder reordered for all rows in {_SETTINGS_TABLE}")
    except Exception as e:
        logger.warning(f"_reorder_settings_in_db failed: {e}")


def _apply_settings_dict(cfg: dict):
    """Apply a settings dict (from DB rows or config.json) to all globals.
    Values may arrive as JSON-encoded strings (from DB) or native Python types."""
    global RTSP_URL, RTSP_TRANSPORT, ENABLE_MQTT, MQTT_BROKER, MQTT_PORT
    global MQTT_TRIGGER_TOPIC, MQTT_PUBLISH_TOPIC, DB_TYPE
    global LOCATION_NAME, LOCATION_COORDS, LOCATION_ID
    global DEPT_TITLE, DEPT_SUBTITLE, DEPT_LOGO_FILENAME, DEPT_BRANDING_ENABLED, FOOTER_DEPT
    global BOX_PADDING_WIDTH_PX, BOX_PADDING_HEIGHT_PX
    global ENABLE_BLUR_MODEL, FRAME_SKIP_INTERVAL, ENABLE_REGEX_CORRECTION, AUTO_LOGIN

    def _str(key, default=''):
        v = cfg.get(key, default)
        if v is None:
            return default
        try:
            parsed = json.loads(v) if isinstance(v, str) else v
            return str(parsed) if not isinstance(parsed, str) else parsed
        except Exception:
            return str(v)

    def _bool(key, default=False):
        v = cfg.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v.lower())
            except Exception:
                return v.lower() in ('true', '1', 'yes')
        return bool(v)

    def _int(key, default=0):
        v = cfg.get(key, default)
        try:
            return int(json.loads(v) if isinstance(v, str) else v)
        except Exception:
            return default

    RTSP_URL           = _str('rtsp_url', RTSP_URL)
    RTSP_TRANSPORT     = _str('rtsp_transport', RTSP_TRANSPORT).lower()
    ENABLE_MQTT        = _bool('mqtt_enabled', ENABLE_MQTT)
    MQTT_BROKER        = _str('mqtt_broker', MQTT_BROKER)
    MQTT_PORT          = _int('mqtt_port', MQTT_PORT)
    MQTT_TRIGGER_TOPIC = _str('mqtt_subscribe_topic', MQTT_TRIGGER_TOPIC)
    MQTT_PUBLISH_TOPIC = _str('mqtt_publish_topic', MQTT_PUBLISH_TOPIC)
    DB_TYPE            = _str('db_type', DB_TYPE)
    LOCATION_NAME      = _str('location_name', LOCATION_NAME)
    LOCATION_COORDS    = _str('location_coords', LOCATION_COORDS)
    LOCATION_ID        = _str('location_id', LOCATION_ID)
    DEPT_TITLE         = _str('dept_title', DEPT_TITLE) or DEPT_TITLE
    DEPT_SUBTITLE      = _str('dept_subtitle', DEPT_SUBTITLE) or DEPT_SUBTITLE
    DEPT_LOGO_FILENAME = _str('dept_logo_filename', DEPT_LOGO_FILENAME) or "branding/department_logo.png"
    FOOTER_DEPT            = _str('footer_dept', FOOTER_DEPT) or "Department of Mines & Geology, Govt. of Rajasthan"
    DEPT_BRANDING_ENABLED  = _bool('dept_branding_enabled', DEPT_BRANDING_ENABLED)
    BOX_PADDING_WIDTH_PX   = _int('box_padding_width_px', BOX_PADDING_WIDTH_PX)
    BOX_PADDING_HEIGHT_PX  = _int('box_padding_height_px', BOX_PADDING_HEIGHT_PX)
    ENABLE_BLUR_MODEL      = _bool('enable_blur_model', ENABLE_BLUR_MODEL)
    FRAME_SKIP_INTERVAL    = _int('frame_skip_interval', FRAME_SKIP_INTERVAL)
    ENABLE_REGEX_CORRECTION = _bool('enable_regex_correction', ENABLE_REGEX_CORRECTION)
    AUTO_LOGIN             = _bool('auto_login', AUTO_LOGIN)
    # Persist auto_login to remember_me.json so it survives restarts
    try:
        _rm_path = _get_remember_me_file()
        _rm_data = {}
        if os.path.exists(_rm_path):
            try:
                with open(_rm_path, 'r', encoding='utf-8') as _f:
                    _rm_data = json.load(_f)
            except Exception:
                pass
        _rm_data['auto_login'] = AUTO_LOGIN
        with open(_rm_path, 'w', encoding='utf-8') as _f:
            json.dump(_rm_data, _f)
    except Exception:
        pass

    global CONF_THRESH_640, CONF_THRESH_1280
    def _float(key, default=0.0):
        v = cfg.get(key, default)
        try:
            return float(json.loads(v) if isinstance(v, str) else v)
        except Exception:
            return default
    CONF_THRESH_640  = _float('conf_thresh_640',  CONF_THRESH_640)
    CONF_THRESH_1280 = _float('conf_thresh_1280', CONF_THRESH_1280)


def load_configuration():
    """Load configuration using the new encrypted-credentials + DB-settings approach.

    Priority order:
      1. Read .env → get DB credentials → connect → read anpr_configuration from DB
      2. If DB unavailable or table empty → use hardcoded defaults
      3. wb_info.json always applied last (location_name / location_id override)
    """
    global SQL_SERVER, SQL_DATABASE, SQL_USERNAME, SQL_PASSWORD, DB_TYPE
    global RTSP_URL, RTSP_TRANSPORT, ENABLE_MQTT, MQTT_BROKER, MQTT_PORT, MQTT_TRIGGER_TOPIC, MQTT_PUBLISH_TOPIC
    global LOCATION_NAME, LOCATION_COORDS, LOCATION_ID
    global DEPT_TITLE, DEPT_SUBTITLE, DEPT_LOGO_FILENAME, FOOTER_DEPT
    global DEPT_BRANDING_ENABLED
    global BOX_PADDING_WIDTH_PX, BOX_PADDING_HEIGHT_PX
    global ENABLE_BLUR_MODEL
    global FRAME_SKIP_INTERVAL
    global ENABLE_REGEX_CORRECTION, AUTO_LOGIN

    # ── Load auto_login preference from remember_me.json (written by config save) ──
    try:
        _rm_path = _get_remember_me_file()
        if os.path.exists(_rm_path):
            with open(_rm_path, 'r', encoding='utf-8') as _f:
                _rm = json.load(_f)
            if 'auto_login' in _rm:
                AUTO_LOGIN = bool(_rm['auto_login'])
    except Exception:
        pass

    # ── Step 1: Load DB credentials + RTSP URL from .env ────────────────────
    creds = load_connection_enc()
    _db_server_val = creds.get('db_server', '').strip() if creds else ''
    if creds and _db_server_val:
        SQL_SERVER   = _db_server_val
        SQL_DATABASE = creds.get('db_name',      SQL_DATABASE)
        SQL_USERNAME = creds.get('db_username',  SQL_USERNAME)
        SQL_PASSWORD = creds.get('db_password',  SQL_PASSWORD)
        DB_TYPE      = creds.get('db_type',      DB_TYPE) or DB_TYPE
        logger.info(f"DB credentials loaded from .env — server: {SQL_SERVER}/{SQL_DATABASE}")
    else:
        logger.warning("WARNING: Database not configured — open System Configuration to set DB credentials")
        _system_status['database'] = 'not_configured'

    # Apply RTSP URL from .env as the baseline (DB settings override this below if DB is available)
    _env_rtsp = (creds.get('rtsp_url', '') if creds else '').strip()
    if _env_rtsp:
        RTSP_URL = _env_rtsp
        logger.info(f"RTSP URL loaded from .env: {RTSP_URL}")

    # ── Step 2: Load all other settings from ANPR_Settings table ─────────────
    if SQL_SERVER and SQL_SERVER.strip():
        try:
            _create_settings_table_sql()
            _reorder_settings_in_db()
            db_settings = load_settings_from_db()
            if db_settings:
                _apply_settings_dict(db_settings)
                logger.info(f"Configuration loaded from DB ({_SETTINGS_TABLE} — {len(db_settings)} keys)")
                logger.info(f"RTSP URL: {RTSP_URL}")
                logger.info(f"MQTT Enabled: {ENABLE_MQTT}")
            else:
                logger.info("ANPR_Settings table is empty — using hardcoded defaults until first Save Configuration")
        except Exception as e:
            logger.warning(f"Could not load settings from DB: {e} — using hardcoded defaults")
    else:
        logger.warning("WARNING: Database not configured — skipping DB settings load. Configure via System Configuration.")

    _wb_override()


def _wb_override():
    """Override LOCATION_NAME / LOCATION_ID from wb_info.json if non-empty."""
    global LOCATION_NAME, LOCATION_ID
    try:
        wb_path = _get_wb_info_path()
        if os.path.exists(wb_path):
            with open(wb_path, 'r', encoding='utf-8-sig') as _wbf:
                _wb = json.load(_wbf)
            _data = _wb.get('Data', {})
            _wb_name = (_data.get('wb_name') or '').strip()
            _wb_id   = (_data.get('wb_id')   or '').strip()
            if _wb_name:
                LOCATION_NAME = _wb_name
            if _wb_id:
                LOCATION_ID = _wb_id
    except Exception as _e:
        logger.warning(f"Could not read wb_info.json: {_e}")

# Track whether we've logged successful connection for each database type (to avoid spamming logs)
_db_connection_logged = {}
# Track whether we've already warned about a failed connection (suppress repeat error spam)
_db_fail_logged = set()

# ----- SQL Server Functions -----
def get_sql_connection():
    """Create and return database connection based on DB_TYPE."""
    global DB_TYPE
    
    try:
        if DB_TYPE == "MSSQL":
            return get_mssql_connection()
        elif DB_TYPE == "MySQL":
            return get_mysql_connection()
        elif DB_TYPE == "PostgreSQL":
            return get_postgresql_connection()
        elif DB_TYPE == "MongoDB":
            return get_mongodb_connection()
        else:
            logger.error(f"Unknown database type: {DB_TYPE}")
            return None
    except Exception as e:
        logger.error(f"Failed to connect to {DB_TYPE}: {e}")
        return None

def get_mssql_connection():
    """Connect to Microsoft SQL Server using pyodbc."""
    if not ENABLE_SQL_LOGGING:
        return None
    if not SQL_SERVER:
        return None
    
    try:
        # Try modern SQL Server drivers first, fallback to older ones
        drivers = [
            'ODBC Driver 17 for SQL Server',
            'ODBC Driver 13 for SQL Server',
            'ODBC Driver 11 for SQL Server',
            'SQL Server Native Client 11.0',
            'SQL Server'
        ]
        
        available_drivers = [d.strip() for d in pyodbc.drivers()]
        selected_driver = None
        
        for driver in drivers:
            if driver in available_drivers:
                selected_driver = driver
                break
        
        if not selected_driver:
            selected_driver = 'SQL Server'
        
        conn_str = (
            f"DRIVER={{{selected_driver}}};"
            f"SERVER={SQL_SERVER};"
            f"DATABASE={SQL_DATABASE};"
            f"UID={SQL_USERNAME};"
            f"PWD={SQL_PASSWORD};"
            "Trusted_Connection=no;"
            "Connection Timeout=1;"
            "Login Timeout=1;"
        )
        
        conn = pyodbc.connect(conn_str, timeout=1)
        
        # Only log connection success once to avoid spam
        global _db_connection_logged
        if _db_connection_logged.get('MSSQL') != SQL_SERVER:
            logger.info(f"✓ Connected to MSSQL: {SQL_SERVER}/{SQL_DATABASE}")
            _db_connection_logged['MSSQL'] = SQL_SERVER
        _db_fail_logged.discard('MSSQL')
        return conn
        
    except Exception as e:
        if 'MSSQL' not in _db_fail_logged:
            logger.warning(f"Database not connected (MSSQL): {SQL_SERVER} — retrying in background")
            _db_fail_logged.add('MSSQL')
        return None

def get_mysql_connection():
    """Connect to MySQL using pymysql."""
    if not MYSQL_AVAILABLE:
        logger.error("pymysql not installed. Install: pip install pymysql")
        return None
    
    if not ENABLE_SQL_LOGGING:
        return None
    if not SQL_SERVER:
        return None
    
    try:
        # Split SERVER:PORT if provided (e.g., "localhost:3306")
        server_parts = SQL_SERVER.split(':')
        host = server_parts[0]
        port = int(server_parts[1]) if len(server_parts) > 1 else 3306
        
        conn = pymysql.connect(
            host=host,
            port=port,
            user=SQL_USERNAME,
            password=SQL_PASSWORD,
            database=SQL_DATABASE,
            connect_timeout=3,
            charset='utf8mb4'
        )
        
        # Only log connection success once to avoid spam
        global _db_connection_logged
        if _db_connection_logged.get('MySQL') != f"{host}:{port}":
            logger.info(f"✓ Connected to MySQL: {host}:{port}/{SQL_DATABASE}")
            _db_connection_logged['MySQL'] = f"{host}:{port}"
        _db_fail_logged.discard('MySQL')
        return conn
        
    except Exception as e:
        if 'MySQL' not in _db_fail_logged:
            logger.warning(f"Database not connected (MySQL): {host}:{port} — retrying in background")
            _db_fail_logged.add('MySQL')
        return None

def get_postgresql_connection():
    """Connect to PostgreSQL using psycopg2."""
    if not POSTGRESQL_AVAILABLE:
        logger.error("psycopg2 not installed. Install: pip install psycopg2")
        return None
    
    if not ENABLE_SQL_LOGGING:
        return None
    if not SQL_SERVER:
        return None
    
    try:
        # Split SERVER:PORT if provided (e.g., "localhost:5432")
        server_parts = SQL_SERVER.split(':')
        host = server_parts[0]
        port = int(server_parts[1]) if len(server_parts) > 1 else 5432
        
        conn = psycopg2.connect(
            host=host,
            port=port,
            user=SQL_USERNAME,
            password=SQL_PASSWORD,
            database=SQL_DATABASE,
            connect_timeout=3
        )
        
        # Only log connection success once to avoid spam
        global _db_connection_logged
        if _db_connection_logged.get('PostgreSQL') != f"{host}:{port}":
            logger.info(f"✓ Connected to PostgreSQL: {host}:{port}/{SQL_DATABASE}")
            _db_connection_logged['PostgreSQL'] = f"{host}:{port}"
        _db_fail_logged.discard('PostgreSQL')
        
        return conn
        
    except Exception as e:
        if 'PostgreSQL' not in _db_fail_logged:
            logger.warning(f"Database not connected (PostgreSQL): {SQL_SERVER} — retrying in background")
            _db_fail_logged.add('PostgreSQL')
        return None

def get_mongodb_connection():
    """Connect to MongoDB using pymongo."""
    if not MONGODB_AVAILABLE:
        logger.error("pymongo not installed. Install: pip install pymongo")
        return None
    
    if not ENABLE_SQL_LOGGING:
        return None
    if not SQL_SERVER:
        return None
    
    try:
        # MongoDB connection string format: mongodb://username:password@host:port/database
        if SQL_USERNAME and SQL_PASSWORD:
            connection_string = f"mongodb://{SQL_USERNAME}:{SQL_PASSWORD}@{SQL_SERVER}/{SQL_DATABASE}"
        else:
            connection_string = f"mongodb://{SQL_SERVER}/{SQL_DATABASE}"
        
        client = pymongo.MongoClient(
            connection_string,
            serverSelectionTimeoutMS=10000
        )
        
        # Test connection
        client.admin.command('ping')
        
        # Only log connection success once to avoid spam
        global _db_connection_logged
        if _db_connection_logged.get('MongoDB') != SQL_SERVER:
            logger.info(f"✓ Connected to MongoDB: {SQL_SERVER}/{SQL_DATABASE}")
            _db_connection_logged['MongoDB'] = SQL_SERVER
        _db_fail_logged.discard('MongoDB')
        
        return client  # Returns MongoClient (different from SQL connections)
        
    except Exception as e:
        if 'MongoDB' not in _db_fail_logged:
            logger.warning(f"Database not connected (MongoDB): {SQL_SERVER} — retrying in background")
            _db_fail_logged.add('MongoDB')
        return None

# Load configuration at startup — must be after all get_*_connection() functions are defined
load_configuration()

def create_database_and_table():
    """Create database and table based on DB_TYPE."""
    global DB_TYPE
    
    if not ENABLE_SQL_LOGGING:
        return False
    
    # Skip entirely when no server is configured
    if not SQL_SERVER or not SQL_SERVER.strip():
        logger.info("DB server not configured - skipping database initialization")
        return False
    
    if DB_TYPE == "MongoDB":
        return create_mongodb_collection()
    else:
        return create_sql_table()  # Works for MSSQL, MySQL, PostgreSQL

def create_sql_table():
    """Create table for SQL databases (MSSQL, MySQL, PostgreSQL)."""
    try:
        if DB_TYPE == "MSSQL":
            # For MSSQL, create database first if needed
            try:
                conn_str = (
                    f"DRIVER={{SQL Server}};"
                    f"SERVER={SQL_SERVER};"
                    f"DATABASE=master;"
                    f"UID={SQL_USERNAME};"
                    f"PWD={SQL_PASSWORD};"
                )
                conn = pyodbc.connect(conn_str, timeout=3)
                conn.autocommit = True
                cursor = conn.cursor()
                
                cursor.execute(f"""
                    IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = N'{SQL_DATABASE}')
                    BEGIN
                        CREATE DATABASE [{SQL_DATABASE}]
                    END
                """)
                logger.info(f"Database '{SQL_DATABASE}' is ready")
                cursor.close()
                conn.close()
            except Exception as e:
                logger.warning(f"Database creation skipped: {e}")
        
        # Connect to the database and create table
        conn = get_sql_connection()
        if not conn:
            return False
        
        cursor = conn.cursor()
        
        # Create table with syntax based on DB_TYPE
        if DB_TYPE == "MSSQL":
            create_table_sql = f"""
            IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = N'{SQL_TABLE}')
            BEGIN
                CREATE TABLE [{SQL_TABLE}] (
                    ID INT IDENTITY(1,1) PRIMARY KEY,
                    Timestamp DATETIME2 NOT NULL DEFAULT GETDATE(),
                    RawText NVARCHAR(100),
                    CleanedText NVARCHAR(50),
                    CorrectedText NVARCHAR(50),
                    InputVehicle NVARCHAR(50),
                    RFID NVARCHAR(100),
                    Confidence FLOAT,
                    MatchScore FLOAT,
                    FrameIndex INT,
                    SaveDirectory NVARCHAR(500),
                    TriggerTopic NVARCHAR(100),
                    ProcessingTime FLOAT,
                    ImageFileName NVARCHAR(255),
                    CreatedAt DATETIME2 DEFAULT GETDATE()
                )
            END
            """
        elif DB_TYPE == "MySQL":
            create_table_sql = f"""
            CREATE TABLE IF NOT EXISTS `{SQL_TABLE}` (
                ID INT AUTO_INCREMENT PRIMARY KEY,
                Timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                RawText VARCHAR(100),
                CleanedText VARCHAR(50),
                CorrectedText VARCHAR(50),
                InputVehicle VARCHAR(50),
                RFID VARCHAR(100),
                Confidence FLOAT,
                MatchScore FLOAT,
                FrameIndex INT,
                SaveDirectory VARCHAR(500),
                TriggerTopic VARCHAR(100),
                ProcessingTime FLOAT,
                ImageFileName VARCHAR(255),
                CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        elif DB_TYPE == "PostgreSQL":
            create_table_sql = f"""
            CREATE TABLE IF NOT EXISTS {SQL_TABLE} (
                ID SERIAL PRIMARY KEY,
                Timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                RawText VARCHAR(100),
                CleanedText VARCHAR(50),
                CorrectedText VARCHAR(50),
                InputVehicle VARCHAR(50),
                RFID VARCHAR(100),
                Confidence FLOAT,
                MatchScore FLOAT,
                FrameIndex INT,
                SaveDirectory VARCHAR(500),
                TriggerTopic VARCHAR(100),
                ProcessingTime FLOAT,
                ImageFileName VARCHAR(255),
                CreatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        
        cursor.execute(create_table_sql)
        conn.commit()
        
        logger.info(f"{DB_TYPE} table '{SQL_TABLE}' is ready")
        cursor.close()
        conn.close()
        return True
        
    except Exception as e:
        logger.error(f"Failed to create {DB_TYPE} table: {e}")
        return False

def create_mongodb_collection():
    """Create MongoDB collection and indexes."""
    try:
        client = get_mongodb_connection()
        if not client:
            return False
        
        db = client[SQL_DATABASE]
        collection = db[SQL_TABLE]
        
        # Create indexes for better query performance
        collection.create_index([("Timestamp", pymongo.DESCENDING)])
        collection.create_index([("CorrectedText", pymongo.ASCENDING)])
        
        logger.info(f"MongoDB collection '{SQL_TABLE}' ready with indexes")
        return True
        
    except Exception as e:
        logger.error(f"Failed to create MongoDB collection: {e}")
        return False

# Plate images stored in folder (image1.jpg, image2.jpg, ...); no base64 in DB
IMAGE_FOLDER_NAME = 'image_folder'

def _get_image_folder_candidates():
    """Return ordered unique candidate directories that may contain image_folder.

    Runtime mode can vary between app dir and pyembed dir across restarts,
    so we probe both for backward compatibility when serving images.
    """
    candidates = []

    def _add(path):
        if not path:
            return
        p = os.path.abspath(path)
        if p not in candidates:
            candidates.append(p)

    exe_dir = os.environ.get('ANPR_EXE_DIR')
    resource_dir = os.environ.get('ANPR_RESOURCE_DIR')
    _add(exe_dir)
    _add(resource_dir)
    _add(BASE_PATH)
    _add(os.path.dirname(sys.executable))
    _add(os.path.dirname(os.path.abspath(__file__)))
    _add(os.getcwd())

    # Common alternate locations observed in packaged deployments.
    if exe_dir:
        _add(os.path.join(exe_dir, 'pyembed'))
    if resource_dir:
        _add(os.path.join(resource_dir, 'pyembed'))

    return [os.path.join(base, IMAGE_FOLDER_NAME) for base in candidates]

def get_image_folder():
    """Return preferred write path for image_folder; create it if needed."""
    preferred_base = (
        os.environ.get('ANPR_EXE_DIR')
        or os.environ.get('ANPR_RESOURCE_DIR')
        or BASE_PATH
    )
    folder = os.path.join(preferred_base, IMAGE_FOLDER_NAME)
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as e:
        logger.warning(f"Could not create image_folder: {e}")
    return folder

def save_plate_image_and_get_filename(bgr_image):
    """Save plate image to image_folder with sequential filename (image1.jpg, image2.jpg, …).
    Counter is seeded from the highest existing imageN.jpg in the folder on first call,
    so numbering continues from where it left off after every restart.
    Returns filename e.g. 'image96.jpg' or None on failure."""
    if bgr_image is None or bgr_image.size == 0:
        return None
    folder = get_image_folder()
    try:
        with _image_save_lock:
            # Seed counter from highest existing imageN.jpg on first call
            if not _image_counter['initialized']:
                max_n = 0
                try:
                    for fname in os.listdir(folder):
                        m = re.match(r'^image(\d+)\.jpg$', fname, re.IGNORECASE)
                        if m:
                            n = int(m.group(1))
                            if n > max_n:
                                max_n = n
                except Exception:
                    pass
                _image_counter['value'] = max_n
                _image_counter['initialized'] = True
                logger.info(f"[IMAGE] Counter seeded from folder scan — last image was image{max_n}.jpg, next will be image{max_n+1}.jpg")
            _image_counter['value'] += 1
            filename = f"image{_image_counter['value']}.jpg"
            path = os.path.join(folder, filename)
            if not cv2.imwrite(path, bgr_image, [cv2.IMWRITE_JPEG_QUALITY, 90]):
                logger.warning(f"Failed to write plate image to {path}")
                _image_counter['value'] -= 1  # rollback on failure
                return None
            return filename
    except Exception as e:
        logger.warning(f"Failed to save plate image: {e}")
        return None

# Lock to serialise concurrent image saves and prevent filename collisions.
# _image_counter is a dict so it can be mutated inside nested functions without 'global'.
# 'initialized' flag triggers a one-time folder scan on first save after each startup.
_image_save_lock = threading.Lock()
_image_counter = {'value': 0, 'initialized': False}

# UI refresh: when a new record is inserted (API or Test button), push to connected clients so they refresh
_refresh_subscribers = []
_refresh_lock = threading.Lock()

def notify_ui_refresh():
    """Notify all connected UI clients to refresh records (called after insert_plate_recognition)."""
    with _refresh_lock:
        for q in _refresh_subscribers:
            try:
                q.put_nowait('refresh')
            except Exception:
                pass

def insert_plate_recognition(data, log_to_terminal=False):
    """Insert plate recognition result into database (SQL or MongoDB).
    
    Args:
        data: dict with keys: timestamp, raw_text, cleaned_text, corrected_text,
              input_vehicle, confidence, match_score, frame_idx, save_dir,
              trigger_topic, processing_time, image_filename (filename in image_folder, e.g. image1.jpg)
        log_to_terminal: if True, log the record to terminal (only set True for API/test-button detections).
    """
    global DB_TYPE
    
    if not ENABLE_SQL_LOGGING:
        return
    
    # Do not insert when no number plate was detected (avoid 0% / "No image" records in UI)
    confidence = data.get('confidence')
    corrected = (data.get('corrected_text') or '').strip()
    try:
        conf_val = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        conf_val = 0.0
    # Reject zero-confidence, low-confidence OCR reads, and partial plate strings.
    # Water reflections and non-plate detections commonly produce short garbage text
    # (e.g. "E3300") with confidence < 0.85; a valid Indian plate is ≥ 8 characters
    # and matches the state-code + district + series + 4-digit-number pattern.
    # 640 pass keeps the high bar (0.85); 1280 pass allows 0.75 (higher-res already ran).
    _pass_lbl = (data.get('pass') or '640')
    _conf_min = 0.75 if _pass_lbl == '1280' else 0.85
    if conf_val < _conf_min:
        logger.debug(f"insert_plate_recognition: skipping '{corrected}' — conf {conf_val:.3f} < {_conf_min} (pass={_pass_lbl})")
        return
    if not corrected or corrected.upper() == 'NO_DETECTION':
        return
    if len(corrected) < 8 or not re.search(r'[A-Z]{2}\d{2}[A-Z0-9]{1,3}\d{4}', corrected):
        logger.debug(f"insert_plate_recognition: skipping '{corrected}' — too short or invalid format")
        return
    
    if DB_TYPE == "MongoDB":
        insert_mongodb_document(data, log_to_terminal)
    else:
        insert_sql_record(data, log_to_terminal)

def insert_sql_record(data, log_to_terminal=False):
    """Insert plate recognition result into SQL database (MSSQL, MySQL, PostgreSQL)."""
    try:
        conn = get_sql_connection()
        if not conn:
            logger.warning("No SQL connection available, skipping insert")
            return
        
        cursor = conn.cursor()
        
        # Build INSERT query with database-specific syntax
        if DB_TYPE == "MSSQL":
            # SQL Server uses ? placeholders and brackets
            insert_query = f"""
                INSERT INTO [{SQL_TABLE}] 
                (Timestamp, RawText, CleanedText, CorrectedText, InputVehicle, RFID,
                 Confidence, MatchScore, FrameIndex, SaveDirectory, TriggerTopic, ProcessingTime, ImageFileName)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        elif DB_TYPE == "MySQL":
            # MySQL uses %s placeholders and backticks
            insert_query = f"""
                INSERT INTO `{SQL_TABLE}` 
                (Timestamp, RawText, CleanedText, CorrectedText, InputVehicle, RFID,
                 Confidence, MatchScore, FrameIndex, SaveDirectory, TriggerTopic, ProcessingTime, ImageFileName)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        elif DB_TYPE == "PostgreSQL":
            # PostgreSQL uses %s placeholders and no quotes
            insert_query = f"""
                INSERT INTO {SQL_TABLE} 
                (Timestamp, RawText, CleanedText, CorrectedText, InputVehicle, RFID,
                 Confidence, MatchScore, FrameIndex, SaveDirectory, TriggerTopic, ProcessingTime, ImageFileName)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        
        cursor.execute(insert_query, (
            data.get('timestamp'),
            data.get('raw_text'),
            data.get('cleaned_text'),
            data.get('corrected_text'),
            data.get('input_vehicle'),
            data.get('rfid'),
            data.get('confidence'),
            data.get('match_score'),
            data.get('frame_idx'),
            data.get('save_dir'),
            data.get('trigger_topic'),
            data.get('processing_time'),
            data.get('image_filename')
        ))
        
        conn.commit()
        cursor.close()
        conn.close()
        if log_to_terminal:
            logger.info(f"✓ Inserted result to {DB_TYPE}: {data.get('corrected_text')}")
        notify_ui_refresh()
    except Exception as e:
        logger.error(f"Failed to insert to {DB_TYPE}: {e}")

def insert_mongodb_document(data, log_to_terminal=False):
    """Insert plate recognition result into MongoDB collection."""
    try:
        client = get_mongodb_connection()
        if not client:
            logger.warning("No MongoDB connection available, skipping insert")
            return
        
        db = client[SQL_DATABASE]
        collection = db[SQL_TABLE]
        
        # Create document with auto-incrementing ID
        # Get max ID from collection
        max_id_doc = collection.find_one(sort=[("ID", pymongo.DESCENDING)])
        next_id = 1 if not max_id_doc else max_id_doc.get("ID", 0) + 1
        
        document = {
            "ID": next_id,
            "Timestamp": data.get('timestamp'),
            "RawText": data.get('raw_text'),
            "CleanedText": data.get('cleaned_text'),
            "CorrectedText": data.get('corrected_text'),
            "InputVehicle": data.get('input_vehicle'),
            "RFID": data.get('rfid'),
            "Confidence": data.get('confidence'),
            "MatchScore": data.get('match_score'),
            "FrameIndex": data.get('frame_idx'),
            "SaveDirectory": data.get('save_dir'),
            "TriggerTopic": data.get('trigger_topic'),
            "ProcessingTime": data.get('processing_time'),
            "ImageFileName": data.get('image_filename')
        }
        
        collection.insert_one(document)
        
        if log_to_terminal:
            logger.info(f"✓ Inserted result to MongoDB: {data.get('corrected_text')}")
        notify_ui_refresh()
    except Exception as e:
        logger.error(f"Failed to insert to MongoDB: {e}")

def process_single_frame(frame, target_w=640, target_h=640, letterbox=True, save_path=None):
    """Resize and optionally letterbox a frame to target size.

    Args:
        frame: input BGR image (numpy array).
        target_w, target_h: desired output size.
        letterbox: if True, preserve aspect ratio and pad; else stretch to fit.
        save_path: if provided, write processed image to this path.

    Returns:
        processed_image, meta_dict
    """
    h, w = frame.shape[:2]
    if w == target_w and h == target_h:
        if save_path:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            cv2.imwrite(save_path, frame)
        return frame, {"scale": 1.0, "pad": (0, 0), "original_size": (w, h)}

    if not letterbox:
        resized = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
        if save_path:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            cv2.imwrite(save_path, resized)
        return resized, {"scale": min(target_w / w, target_h / h), "pad": (0, 0), "original_size": (w, h)}

    scale = min(target_w / w, target_h / h)
    new_w, new_h = int(w * scale), int(h * scale)
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

    pad_w = target_w - new_w
    pad_h = target_h - new_h
    left, right = pad_w // 2, pad_w - (pad_w // 2)
    top, bottom = pad_h // 2, pad_h - (pad_h // 2)

    canvas = np.full((target_h, target_w, 3), 128, dtype=np.uint8)
    canvas[top:top + new_h, left:left + new_w] = resized

    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        cv2.imwrite(save_path, canvas)

    meta = {
        "scale": scale,
        "pad": (left, top),
        "pad_right_bottom": (right, bottom),
        "original_size": (w, h),
        "resized_size": (new_w, new_h),
    }
    return canvas, meta


def correct_plate_ocr(plate, state_corrections=None, valid_state_codes=None, char_to_digit=None):
    plate = re.sub(r'\s+', '', (plate or '').strip().upper())
    state_corrections = state_corrections or {'0': 'O', '1': 'I', '2': 'Z', '5': 'S', '6': 'G', '8': 'B', '$': 'S'}
    valid_state_codes = valid_state_codes or {'OD', 'MH', 'DL', 'KA', 'TN', 'AP', 'TS', 'RJ', 'GJ', 'MP', 'UP',
                                              'BR', 'PB', 'HR', 'CH', 'JK', 'LA', 'UK', 'HP', 'JH', 'WB', 'CG',
                                              'KL', 'GA', 'MN', 'ML', 'MZ', 'NL', 'TR', 'AR', 'AS', 'SK', 'PY',
                                              'DN', 'DD', 'LD'}
    # C->0 in numeric suffix (common OCR error when shadow makes 0 look like C)
    # NOTE: Do NOT convert 'D' -> '0' here. It can corrupt valid plates when OCR
    # returns split tokens (e.g. '0D14A' + 'E3900') or when token ordering flips.
    char_to_digit = char_to_digit or {'O': '0', 'C': '0', 'I': '1', 'L': '1', 'Z': '2', 'S': '5', 'G': '6', 'R': '6', 'B': '8', '$': '5'}

    state_raw = plate[:2]
    corrected_state = ''.join(state_corrections.get(c, c) for c in state_raw)
    if corrected_state not in valid_state_codes:
        if corrected_state != state_raw:
            # state_corrections changed something (e.g. '00'->'OO', digit misread as letter).
            # Allow one fuzzy match because this is a known OCR digit-substitution error.
            for code in valid_state_codes:
                if sum(a != b for a, b in zip(code, corrected_state)) == 1:
                    corrected_state = code
                    break
            else:
                corrected_state = state_raw  # fuzzy found nothing — leave as-is
        else:
            # state_corrections changed nothing (e.g. 'AD' stayed 'AD') — these are
            # genuinely wrong letters, not digit substitutions. Do NOT guess a state code.
            corrected_state = state_raw
    plate = corrected_state + plate[2:]
    if len(plate) > 2:
        rto_first_char = char_to_digit.get(plate[2], plate[2])
        plate = plate[:2] + rto_first_char + plate[3:]
    if len(plate) > 3:
        rto_second_char = char_to_digit.get(plate[3], plate[3])
        plate = plate[:3] + rto_second_char + plate[4:]
    if len(plate) > 6:
        middle_part = plate[4:-4]
        # Apply digit→letter first in the alphabetic series segment.
        _digit_to_letter = {'0': 'O', '1': 'I', '2': 'Z', '5': 'S', '6': 'G', '8': 'B'}
        middle_fixed = ''.join(_digit_to_letter.get(c, c) for c in middle_part)
        middle_fixed = middle_fixed.replace('$', 'S')
        plate = plate[:4] + middle_fixed + plate[-4:]
    if len(plate) >= 4:
        suffix_raw = plate[-4:]
        suffix_fixed = ''.join(char_to_digit.get(c, c) for c in suffix_raw)
        plate = plate[:-4] + suffix_fixed
    return plate


def clean_plate_text(text):
    # Keep only letters and digits
    text = re.sub(r'[^A-Za-z0-9]', '', (text or '').strip().upper())
    # Note: IND/INDIA removal is handled dynamically in extract_text_from_image()
    # via bounding-box height filtering - no hardcoded text removal needed here.
    # Fix common OCR error: leading 0 misread for O in state code (e.g. 0D14A -> OD14A)
    text = re.sub(r'^0(?=[A-Z])', 'O', text)
    # Full string match: exact Indian plate format
    match = re.match(r'^([A-Z]{2}\d{2})([A-Z0-9]+)(\d{4})$', text)
    if match:
        prefix, middle, suffix = match.groups()
        # Convert digits to letters in the middle series before stripping.
        _digit_to_letter = {'0': 'O', '1': 'I', '2': 'Z', '5': 'S', '6': 'G', '8': 'B'}
        middle_converted = ''.join(_digit_to_letter.get(c, c) for c in middle)
        middle_cleaned = re.sub(r'\d', '', middle_converted)
        return prefix + middle_cleaned + suffix
    # Extract first valid Indian plate pattern if junk remains
    search = re.search(r'([A-Z]{2}\d{2}[A-Z]{0,3}\d{4})', text)
    if search:
        extracted = search.group(1)
        if extracted != text:
            logger.debug(f"Extracted plate pattern from OCR text: '{text}' -> '{extracted}'")
        return extracted
    return text


def calculate_match_percentage(s1, s2):
    s1, s2 = (s1 or '').upper(), (s2 or '').upper()
    max_len = max(len(s1), len(s2))
    if max_len == 0:
        return 0.0
    dist = Levenshtein.distance(s1, s2)
    return round((1 - dist / max_len) * 100, 2)


def ocr_blurred_crops(save_dir, input_vehicle=None):
    logger.info(f"[OCR_CROPS] Starting OCR on blurred crops in: {save_dir}, input_vehicle={input_vehicle}")
    results = []
    best = None
    best_score = -1.0
    total_processed = 0
    total_failed = 0
    
    for item in sorted(os.listdir(save_dir)):
        if not item.endswith('_blurred'):
            continue
        folder = os.path.join(save_dir, item)
        logger.debug(f"[OCR_CROPS] Processing blurred folder: {folder}")
        
        for fname in sorted(os.listdir(folder)):
            if not fname.lower().endswith(('.jpg', '.jpeg', '.png')):
                continue
            path = os.path.join(folder, fname)
            total_processed += 1
            logger.debug(f"[OCR_CROPS] Processing image #{total_processed}: {path}")
            
            try:
                img = cv2.imread(path)
                if img is None:
                    logger.warning(f"[OCR_CROPS] Failed to read image: {path}")
                    total_failed += 1
                    continue
                
                logger.debug(f"[OCR_CROPS] Image loaded: {path}, shape={img.shape}")
                
                # Use the simple OCR helper (defined below) to extract text and avg confidence
                text_raw = ''
                conf = 0.0
                try:
                    if ocr is not None:
                        # extract_text_from_image accepts either a file path or numpy array
                        logger.debug(f"[OCR_CROPS] Running OCR extraction on: {path}")
                        t, c = extract_text_from_image(img)
                        text_raw = t or ''
                        conf = float(c) if c is not None else 0.0
                        logger.debug(f"[OCR_CROPS] OCR extraction result for {path}: raw='{text_raw}', conf={conf}")
                    else:
                        # OCR unavailable
                        logger.debug(f"[OCR_CROPS] OCR engine unavailable for: {path}")
                        text_raw = ''
                        conf = 0.0
                except Exception as e:
                    logger.error(f"[OCR_CROPS] OCR helper error on {path}: {e}")
                    total_failed += 1
                    text_raw = ''
                    conf = 0.0

                logger.debug(f"[OCR_CROPS] Cleaning plate text: '{text_raw}'")
                if ENABLE_REGEX_CORRECTION:
                    cleaned = clean_plate_text(text_raw)
                    logger.debug(f"[OCR_CROPS] Cleaned: '{cleaned}'")
                    logger.debug(f"[OCR_CROPS] Correcting plate text: '{cleaned}'")
                    corrected = correct_plate_ocr(cleaned)
                else:
                    cleaned = corrected = re.sub(r'[^A-Z0-9]', '', (text_raw or '').strip().upper())
                    logger.debug(f"[OCR_CROPS] Regex OFF — raw strip only: '{corrected}'")
                logger.debug(f"[OCR_CROPS] Corrected: '{corrected}'")
                
                match_score = None
                if input_vehicle:
                    match_score = round(calculate_match_percentage(corrected, input_vehicle), 2)
                    logger.debug(f"[OCR_CROPS] Match score vs '{input_vehicle}': {match_score}%")

                result_entry = {
                    'file': path,
                    'raw': text_raw,
                    'cleaned': cleaned,
                    'corrected': corrected,
                    'conf': conf,
                    'match_score': match_score
                }
                results.append(result_entry)
                logger.info(f"[OCR_CROPS] Result #{total_processed}: {result_entry}")

                if match_score is not None and match_score > best_score:
                    best_score = match_score
                    best = results[-1]
                    logger.info(f"[OCR_CROPS] New best match found! Score: {best_score}%, corrected: '{corrected}'")
                    
            except Exception as e:
                logger.error(f"[OCR_CROPS] OCR processing FAILED for {path}: {e}", exc_info=True)
                total_failed += 1

    logger.info(f"[OCR_CROPS] Completed OCR processing: total={total_processed}, failed={total_failed}, results={len(results)}")
    if best:
        logger.info(f"[OCR_CROPS] Best result: {best}")
    else:
        logger.info(f"[OCR_CROPS] No best result found")
    
    return results, best


def open_rtsp_capture(rtsp_url, transport="udp"):
    """
    Open RTSP VideoCapture with specified transport protocol.
    
    Args:
        rtsp_url: RTSP stream URL
        transport: "udp" for faster streaming (default), "tcp" for reliable streaming
        
    Returns:
        cv2.VideoCapture object or None if failed
    """
    try:
        # Set RTSP transport environment variable for FFMPEG
        # UDP is faster with lower latency but may drop packets
        # TCP is more reliable but has higher latency
        transport_lower = transport.lower()
        if transport_lower not in ["udp", "tcp"]:
            logger.warning(f"Invalid RTSP transport '{transport}', defaulting to UDP")
            transport_lower = "udp"
        
        # Set OpenCV FFMPEG options for RTSP transport
        os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = f'rtsp_transport;{transport_lower}'
        
        # Open VideoCapture with explicit FFMPEG backend
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        
        if cap.isOpened():
            # Set buffer size to 1 to minimize latency (get most recent frame)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # Set timeouts for faster detection of disconnects
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)  # 5 second timeout for opening
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000)  # 2 second timeout for reading
            logger.info(f"RTSP capture opened with {transport_lower.upper()} transport (buffer=1 for low latency)")
            return cap
        else:
            logger.error(f"Failed to open RTSP stream with {transport_lower.upper()} transport")
            return None
    except Exception as e:
        logger.error(f"Error opening RTSP capture: {e}")
        return None


def extract_text_from_image(img_or_path):
    """PaddleOCR helper with dynamic IND removal via bounding-box height filtering.

    Instead of hardcoded text removal, small boxes (like the IND hologram strip)
    are automatically excluded because their height is < 50% of the main plate text.

    Args:
        img_or_path: filesystem path (str) or numpy BGR image.

    Returns:
        (text, avg_confidence) -- text is string ('' if none), avg_confidence is float or None
    """
    global ocr
    img_name = img_or_path if isinstance(img_or_path, str) else "<numpy_array>"
    logger.debug(f"[OCR] Starting OCR on: {img_name}")

    # Lazy initialization
    if ocr is None:
        logger.debug(f"[OCR] OCR engine is None, attempting initialization...")
        try:
            initialize_paddleocr()
            if ocr is None:
                logger.warning(f"[OCR] OCR initialization failed, skipping: {img_name}")
                return '', None
        except Exception as e:
            logger.error(f"[OCR] Failed to initialize OCR: {e}")
            return '', None

    try:
        if isinstance(img_or_path, str):
            img = cv2.imread(img_or_path)
            if img is None:
                logger.warning(f"[OCR] Failed to read image: {img_name}")
                return '', None
        else:
            img = img_or_path

        # Upscale 2x only if the crop is small — zoom_in crops are already 2× size
        # and would balloon to 4× if upscaled again, wasting DBNet time.
        _h, _w = img.shape[:2]
        if min(_h, _w) < 80:
            img = cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

        logger.debug(f"[OCR] Calling PaddleOCR predict() on: {img_name}")
        with _ocr_lock:  # serialise: PaddleOCR is NOT thread-safe
            if hasattr(ocr, 'predict'):
                result = ocr.predict(img)
            else:
                result = ocr.ocr(img, rec=True)
        logger.debug(f"[OCR] PaddleOCR returned {len(result) if result else 0} results")
    except Exception as e:
        logger.error(f"[OCR] PaddleOCR call FAILED for {img_name}: {e}")
        return '', None

    detections = []  # [{text, height, y_min, score}]

    if result:
        for res_idx, res in enumerate(result):
            # --- PaddleOCR 3.x dict format ---
            if isinstance(res, dict):
                boxes  = res.get("dt_polys",   [])
                texts  = res.get("rec_texts",  [])
                scores = res.get("rec_scores", [])

                for box, text, score in zip(boxes, texts, scores):
                    if not text or score < 0.7:
                        continue
                    text = text.strip().upper()
                    box_np = np.array(box).astype(int)
                    x_min = int(np.min(box_np[:, 0]))
                    y_min = int(np.min(box_np[:, 1]))
                    y_max = int(np.max(box_np[:, 1]))
                    height = y_max - y_min
                    logger.debug(f"[OCR] Det: text='{text}' height={height} score={score:.3f}")
                    detections.append({"text": text, "height": height, "y_min": y_min, "x_min": x_min, "score": score})

            # --- Older list-of-lists format ---
            elif isinstance(res, list):
                for line in res:
                    try:
                        if not (isinstance(line, list) and len(line) >= 2):
                            continue
                        # Extract text
                        if isinstance(line[1], str):
                            text = line[1]
                        elif isinstance(line[1], (list, tuple)) and len(line[1]) > 0:
                            text = line[1][0]
                        else:
                            continue
                        if not text:
                            continue
                        text = text.strip().upper()
                        # Extract score
                        if isinstance(line[1], (list, tuple)) and len(line[1]) > 1:
                            score = float(line[1][1]) if line[1][1] is not None else 1.0
                        else:
                            score = 1.0
                        if score < 0.7:
                            continue
                        # Extract box height from polygon (line[0])
                        if line[0] is not None:
                            box_np = np.array(line[0]).astype(int)
                            x_min = int(np.min(box_np[:, 0]))
                            y_min = int(np.min(box_np[:, 1]))
                            y_max = int(np.max(box_np[:, 1]))
                            height = y_max - y_min
                        else:
                            height = 999  # assume large if no box
                            y_min = 0
                            x_min = 0
                        detections.append({"text": text, "height": height, "y_min": y_min, "x_min": x_min, "score": score})
                    except Exception as ex:
                        logger.warning(f"[OCR] Failed to parse list-format line: {ex}")
                        continue

    if not detections:
        logger.info(f"[OCR] No detections for {img_name}")
        return '', None

    # ------------------------------------------------------------------
    # Two-layer IND / garbage removal:
    #
    # Layer 1 — Height filter at 30%:
    #   IND hologram strip is typically ~25-28% of main plate char height,
    #   so 30% catches it reliably while keeping stacked-plate second rows
    #   safe (perspective-foreshortened rows are rarely below 40-50%).
    #   60% was too aggressive — it could drop a valid short second row.
    #
    # Layer 2 — Explicit IND text match:
    #   Catches any IND variant (INO, IN0, INDI, INDIA, INB etc.) that
    #   passes the height filter (e.g. stamp unusually large / misdetected).
    # ------------------------------------------------------------------
    max_height = max(d["height"] for d in detections)
    detections = [
        d for d in detections
        if d["height"] >= 0.30 * max_height
        and not re.match(r'^IN[D0OB][IA]?$', d["text"].strip().upper())
    ]
    logger.debug(f"[OCR] After height+IND filter (>=30% of {max_height}px): {len(detections)} detections")

    # Substring dedup: if one detection's text is fully contained inside another, drop it.
    # e.g. '4469' inside 'N4469' → drop '4469' so the real rows are not displaced.
    def _is_substring_of_another(det, all_dets):
        t = det["text"]
        return any(t != d["text"] and t in d["text"] for d in all_dets)
    detections = [d for d in detections if not _is_substring_of_another(d, detections)]

    # Sort detections: group tokens that belong to the same visual row, then sort
    # left→right within each row, and rows top→bottom.
    # This handles when OCR splits a single plate row (e.g. 'GD2170') into separate
    # tokens ('GD2', '170') — they share a similar y_min but differ in x_min.
    def _sort_into_rows(dets):
        if not dets:
            return dets
        avg_h = sum(d["height"] for d in dets) / len(dets)
        row_gap = max(avg_h * 0.5, 8)  # vertical tolerance to group same-row tokens
        sorted_by_y = sorted(dets, key=lambda x: x["y_min"])
        rows = []
        for det in sorted_by_y:
            placed = False
            for row in rows:
                if abs(det["y_min"] - row[0]["y_min"]) <= row_gap:
                    row.append(det)
                    placed = True
                    break
            if not placed:
                rows.append([det])
        for row in rows:
            row.sort(key=lambda x: x.get("x_min", 0))  # left→right within row
            # Drop isolated single-char noise within a row when multi-char tokens exist.
            # e.g. ['RJ03', 'B', 'GA7268'] → drop 'B' → 'RJ03GA7268'
            # (Does NOT drop single chars when ALL tokens are single chars, e.g. warmup '4F1')
            multi_in_row = [d for d in row if len(d["text"]) >= 2]
            if multi_in_row and len(multi_in_row) < len(row):
                row[:] = multi_in_row
        return [det for row in rows for det in row]

    detections = _sort_into_rows(detections)

    all_texts  = [d["text"]  for d in detections]
    all_scores = [d["score"] for d in detections]

    # Indian plate line ordering: suffix line (>3 digits) should come second
    if len(all_texts) == 2:
        def _digit_count(s):
            return sum(1 for c in (s or '') if c.isdigit())
        d0, d1 = _digit_count(all_texts[0]), _digit_count(all_texts[1])
        if d0 > 3 and d1 <= 3:
            # First line has the numeric suffix - swap
            all_texts  = [all_texts[1],  all_texts[0]]
            all_scores = [all_scores[1], all_scores[0]]

    combined_text  = ''.join(all_texts).strip()
    avg_confidence = round(sum(all_scores) / len(all_scores), 3) if all_scores else None

    logger.debug(f"[OCR] Detections used: {[(d['text'], d['height']) for d in detections]}")
    logger.info(f"[OCR] ✓ OCR completed for {img_name}: '{combined_text}' (Confidence: {avg_confidence})")

    return combined_text, avg_confidence

# ----- Live viewer thread -----
class SharedCamera:
    """Background single RTSP capture used by viewer and capture routines.

    Keeps the latest frame and a timestamp. Provides a helper to read N fresh
    frames (waiting for new frames) so captured frames are live.
    """
    def __init__(self, rtsp_url):
        self.rtsp_url = rtsp_url
        self.cap = None
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        self.frame = None
        self.frame_time = 0.0

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        logger.info("SharedCamera: start requested")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
        logger.info("SharedCamera: stopped")

    def _capture_loop(self):
        frame_count = 0
        _rtsp_blank_logged = False
        _consecutive_failures = 0
        # HEVC/H.265 cameras drop many frames at startup (missing VPS/PPS until first
        # keyframe). Allow up to 60 consecutive read failures before actually reconnecting
        # so we don't thrash the connection during the normal HEVC settling window.
        _MAX_CONSECUTIVE_FAILURES = 60
        while self.running:
            # Skip connection entirely when no RTSP URL is configured
            if not self.rtsp_url or not self.rtsp_url.strip():
                if not _rtsp_blank_logged:
                    logger.info("SharedCamera: no RTSP URL configured - waiting for URL to be set")
                    _rtsp_blank_logged = True
                time.sleep(2)
                continue
            _rtsp_blank_logged = False  # URL became non-empty; reset flag
            if self.cap is None:
                _consecutive_failures = 0  # reset on fresh connect
                try:
                    # Use UDP transport for faster streaming with lower latency
                    self.cap = open_rtsp_capture(self.rtsp_url, transport=RTSP_TRANSPORT)
                except Exception as e:
                    logger.error(f"SharedCamera: exception opening RTSP: {e}")
                    self.cap = None
                # Auto-fallback: if UDP fails, transparently retry with TCP
                if (self.cap is None or not self.cap.isOpened()) and RTSP_TRANSPORT.lower() == "udp":
                    logger.warning("SharedCamera: UDP failed, auto-falling back to TCP...")
                    try:
                        self.cap = open_rtsp_capture(self.rtsp_url, transport="tcp")
                    except Exception as e:
                        logger.error(f"SharedCamera: TCP fallback exception: {e}")
                        self.cap = None
                if self.cap is None or not self.cap.isOpened():
                    if not getattr(self, '_rtsp_fail_logged', False):
                        logger.warning(f"Camera not connected: RTSP stream unavailable — retrying in background")
                        self._rtsp_fail_logged = True
                    time.sleep(0.5)
                    continue

            ret, frame = self.cap.read()
            if not ret or frame is None:
                _consecutive_failures += 1
                # During HEVC startup, VPS/PPS frames decode as failures — tolerate up to
                # _MAX_CONSECUTIVE_FAILURES before treating as a real disconnection.
                if _consecutive_failures < _MAX_CONSECUTIVE_FAILURES:
                    time.sleep(0.033)
                    continue
                if not getattr(self, '_rtsp_fail_logged', False):
                    logger.warning("Camera not connected: frame read failed — reconnecting")
                    self._rtsp_fail_logged = True
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None
                _consecutive_failures = 0
                time.sleep(0.5)
                continue

            _consecutive_failures = 0  # good frame resets the counter

            # Good frame received — clear failure flag so next disconnect logs again
            if getattr(self, '_rtsp_fail_logged', False):
                logger.info("Camera reconnected: RTSP stream is delivering frames")
                self._rtsp_fail_logged = False

            frame_count += 1
            # Only update every Nth frame to reduce CPU
            if frame_count % FRAME_SKIP_INTERVAL == 0:
                with self.lock:
                    # store latest frame and a timestamp so callers can detect freshness
                    self.frame = frame.copy()
                    self.frame_time = time.time()
            
            # Small delay to prevent CPU spinning
            time.sleep(0.033)  # ~30ms delay

    def get_frame(self):
        with self.lock:
            if self.frame is None:
                return None, 0.0
            return self.frame.copy(), self.frame_time

    def read_n_frames(self, n, save_root, input_vehicle=None, timeout_per_frame=0.1):
        """Read n fresh frames, process on-the-fly, and save only the best matching frame.

        This avoids opening a second VideoCapture and ensures frames are live.
        """
        # Block until plate model warmup finishes (~10s typical).
        # OCR warmup runs concurrently and serialises via _ocr_lock — no need to wait for it.
        if not _warmup_complete.is_set():
            logger.info("SharedCamera: waiting for plate model warmup to complete...")
            _warmup_complete.wait(timeout=30)
        # Don't create timestamped folder yet - only create if needed (no detection)
        saved = 0
        start = time.time()
        last_time = 0.0
        logger.info("SharedCamera: processing %d live frames (saving only best match)", n)
        
        best_frame_data = None
        best_score = -1.0
        best_conf = 0.0
        captured_frames = []  # Store frames temporarily in case we need to save them
        
        while saved < n:
            frame, ftime = self.get_frame()
            if frame is None:
                time.sleep(0.1)
                if time.time() - start > timeout_per_frame:
                    logger.warning("SharedCamera: no frames available yet")
                continue
            if ftime <= last_time:
                time.sleep(0.01)
                continue
            
            try:
                # Keep original frame for high-res OCR cropping
                original_frame = frame.copy()
                original_h, original_w = original_frame.shape[:2]
                
                # Store frame temporarily (in case we need to save for no detection)
                captured_frames.append(frame.copy())
                
                saved += 1
                last_time = ftime

                # Fast plate detection path:
                # - Prepare a 640x640 processed frame for YOLO (fast)
                # - Run YOLO with imgsz=640 on that processed frame
                # - Map detected boxes back to original_frame coordinates for high-res OCR cropping
                processed, meta = process_single_frame(frame, TARGET_WIDTH, TARGET_HEIGHT, USE_LETTERBOX, save_path=None)

                if plate_model is not None:
                    # ── Two-pass inference: 640 first, 1280 only if OCR incomplete ──
                    # Pass 1 (always): 640 YOLO on processed frame → OCR [normal + zoom-in]
                    #   • If OCR gives a complete valid Indian plate (len≥8, regex, conf≥0.85)
                    #     → done, 1280 never runs (fast common case ~8-10s)
                    # Pass 2 (lazy): 1280 YOLO on original frame → OCR [zoom-out + normal + zoom-in]
                    #   → only runs when Pass 1 OCR gave partial/short/low-conf result
                    #
                    # 1280 YOLO is NOT pre-run upfront — it is called lazily after Pass 1 OCR
                    # so the common case pays zero cost for the 1280 prediction.
                    results_640 = plate_model.predict(processed, conf=0.20, verbose=False, imgsz=640)
                    _conf_640 = 0.0
                    if (results_640 and len(results_640) > 0
                            and results_640[0].boxes is not None
                            and len(results_640[0].boxes) > 0):
                        _conf_640 = float(results_640[0].boxes.conf.max().cpu().numpy())
                    logger.info(f"[INFERENCE] 640 conf={_conf_640:.3f}")

                    # _passes: list of (results, use_original_coords, pass_label)
                    # 1280 pass is appended lazily after 640 OCR quality check.
                    _passes = [(results_640, False, "640")]
                    _1280_results_cache = None
                    _pass_idx = 0
                    _dual_early_exit = False

                    while _pass_idx < len(_passes) and not _dual_early_exit:
                        _results, _use_orig, _plabel = _passes[_pass_idx]
                        _pass_idx += 1

                        if not (_results and len(_results) > 0):
                            # 640 returned no results at all — fallback to 1280
                            if not _use_orig and len(_passes) == 1 and not _dual_early_exit:
                                logger.info("[INFERENCE] 640 returned no results — falling back to 1280")
                                if _1280_results_cache is None:
                                    _1280_results_cache = plate_model.predict(
                                        original_frame, conf=0.20, verbose=False, imgsz=1280
                                    )
                                if (_1280_results_cache and len(_1280_results_cache) > 0
                                        and _1280_results_cache[0].boxes is not None
                                        and len(_1280_results_cache[0].boxes) > 0):
                                    _conf_1280 = float(_1280_results_cache[0].boxes.conf.max().cpu().numpy())
                                    logger.info(f"[INFERENCE] 1280 conf={_conf_1280:.3f} — queuing 1280 OCR pass")
                                    _passes.append((_1280_results_cache, True, "1280"))
                                else:
                                    logger.info("[INFERENCE] 1280 also found no plate box — no further retry")
                            continue
                        r = _results[0]
                        if not (hasattr(r, "boxes") and r.boxes is not None and len(r.boxes) > 0):
                            # 640 found no boxes — fallback to 1280
                            if not _use_orig and len(_passes) == 1 and not _dual_early_exit:
                                logger.info("[INFERENCE] 640 found no boxes — falling back to 1280")
                                if _1280_results_cache is None:
                                    _1280_results_cache = plate_model.predict(
                                        original_frame, conf=0.20, verbose=False, imgsz=1280
                                    )
                                if (_1280_results_cache and len(_1280_results_cache) > 0
                                        and _1280_results_cache[0].boxes is not None
                                        and len(_1280_results_cache[0].boxes) > 0):
                                    _conf_1280 = float(_1280_results_cache[0].boxes.conf.max().cpu().numpy())
                                    logger.info(f"[INFERENCE] 1280 conf={_conf_1280:.3f} — queuing 1280 OCR pass")
                                    _passes.append((_1280_results_cache, True, "1280"))
                                else:
                                    logger.info("[INFERENCE] 1280 also found no plate box — no further retry")
                            continue
                        boxes = r.boxes.xyxy.cpu().numpy()

                        # Normalise boxes to original_frame coordinates
                        if _use_orig:
                            # 1280 pass: YOLO boxes already in original_frame space
                            orig_boxes = [(int(b[0]), int(b[1]), int(b[2]), int(b[3])) for b in boxes]
                        else:
                            # 640 pass: unmap from processed(640) space via letterbox meta
                            pad_left, pad_top = meta.get('pad', (0, 0))
                            scale = float(meta.get('scale') or 1.0)
                            if scale <= 0:
                                scale = 1.0
                            orig_boxes = [
                                (
                                    int((int(b[0]) - pad_left) / scale),
                                    int((int(b[1]) - pad_top) / scale),
                                    int((int(b[2]) - pad_left) / scale),
                                    int((int(b[3]) - pad_top) / scale),
                                )
                                for b in boxes
                            ]

                        for i, (x1_orig, y1_orig, x2_orig, y2_orig) in enumerate(orig_boxes):

                            # FIXED PADDING ONLY (auto padding removed)
                            expand_w = max(0, int(BOX_PADDING_WIDTH_PX))
                            expand_h = max(0, int(BOX_PADDING_HEIGHT_PX))
                            logger.debug(
                                f"[{_plabel}][FIXED PAD] box={x2_orig - x1_orig}x{y2_orig - y1_orig}px "
                                f"-> pad_w={expand_w}px pad_h={expand_h}px"
                            )

                            x1_orig = max(0, x1_orig - expand_w)
                            y1_orig = max(0, y1_orig - expand_h)
                            x2_orig = min(original_w, x2_orig + expand_w)
                            y2_orig = min(original_h, y2_orig + expand_h)

                            # Crop from ORIGINAL resolution frame
                            crop_original = original_frame[y1_orig:y2_orig, x1_orig:x2_orig]

                            if crop_original.size == 0:
                                logger.debug(f"[{_plabel}] Original crop is empty, skipping")
                                continue

                            if ocr is None:
                                pass
                            else:
                                _pw = x2_orig - x1_orig
                                _ph = y2_orig - y1_orig

                                if _use_orig:
                                    # 1280 pass: normal + zoom-in (2 crops)
                                    # zoom_out removed — wide-context crop adds background noise and is
                                    # rarely better than normal for isolated plate crops.
                                    crop_zoom_in  = cv2.resize(
                                        crop_original,
                                        (max(1, _pw * 2), max(1, _ph * 2)),
                                        interpolation=cv2.INTER_CUBIC
                                    )
                                    _ocr_crops = [crop_original, crop_zoom_in]
                                    logger.info(
                                        f"[1280] OCR on 2 crops: "
                                        f"normal={_pw}x{_ph}px  "
                                        f"zoom_in={_pw*2}x{_ph*2}px"
                                    )
                                else:
                                    # 640 pass: normal crop only
                                    # zoom_in removed — if normal fails, 1280 HQ pass runs anyway
                                    _ocr_crops = [crop_original]
                                    logger.debug(
                                        f"[640] OCR on 1 crop: normal={_pw}x{_ph}px"
                                    )

                                logger.info(f"[{_plabel}] Direct OCR: plate crop sent to OCR")
                                _hq_early_exit = False
                                for _ocr_crop in _ocr_crops:
                                    if _ocr_crop.size == 0:
                                        continue
                                    try:
                                        text_raw, conf = extract_text_from_image(_ocr_crop)
                                        text_raw = text_raw or ''
                                        conf = float(conf) if conf is not None else 0.0
                                        if ENABLE_REGEX_CORRECTION:
                                            cleaned = clean_plate_text(text_raw)
                                            corrected = correct_plate_ocr(cleaned)
                                        else:
                                            cleaned = corrected = re.sub(r'[^A-Z0-9]', '', text_raw.strip().upper())
                                        if input_vehicle:
                                            match_score = round(calculate_match_percentage(corrected, input_vehicle), 2)
                                        else:
                                            match_score = round(conf * 100, 2)
                                        logger.debug(f"[{_plabel}] Frame {saved}: raw='{text_raw}', corrected='{corrected}', score={match_score}, conf={conf} (regex={'ON' if ENABLE_REGEX_CORRECTION else 'OFF'})")
                                        if match_score > best_score or (match_score == best_score and conf > best_conf):
                                            best_score = match_score
                                            best_conf = conf
                                            best_frame_data = {
                                                'frame_idx': saved,
                                                'processed': processed.copy(),
                                                'crop': crop_original.copy(),      # always normal plate crop
                                                'blurred': crop_original.copy(),   # always normal plate crop — saved to image_folder
                                                'blurred_hq': crop_original.copy(),
                                                'raw': text_raw,
                                                'cleaned': cleaned,
                                                'corrected': corrected,
                                                'conf': conf,
                                                'match_score': match_score,
                                                'box': (x1_orig, y1_orig, x2_orig, y2_orig),
                                                'original_frame': original_frame.copy(),
                                                'pass': _plabel,  # '640' or '1280' — used for pass-specific conf thresholds
                                            }
                                            logger.info(f"[{_plabel}] New best match at frame {saved}: '{corrected}' (score={match_score:.2f}, conf={conf:.3f})")
                                            if match_score >= 95.0 and conf >= 0.85:
                                                logger.info(f"High-confidence match found, stopping early (score={match_score:.2f}, conf={conf:.3f})")
                                                saved = n
                                                _hq_early_exit = True
                                                _dual_early_exit = True
                                                break
                                            # Format-valid shortcut: complete plate already read —
                                            # skip remaining crops and the 1280 pass entirely.
                                            if (conf >= 0.85
                                                    and len(corrected) >= 8
                                                    and re.search(r'[A-Z]{2}\d{2}[A-Z0-9]{1,3}\d{4}', corrected)):
                                                logger.info(
                                                    f"[{_plabel}] Complete plate '{corrected}' "
                                                    f"(conf={conf:.3f}) — skipping remaining crops & 1280 pass"
                                                )
                                                saved = n
                                                _hq_early_exit = True
                                                _dual_early_exit = True
                                                break
                                    except Exception as e:
                                        logger.debug(f"[{_plabel}] OCR processing failed for frame {saved}: {e}")
                                if _hq_early_exit:
                                    break

                        # ── Lazy 1280 gate: enqueue only if 640 OCR was incomplete ──
                        # Runs after all 640 plate boxes are processed.
                        # If the best result so far is already a valid full plate, skip 1280.
                        if not _use_orig and len(_passes) == 1 and not _dual_early_exit:
                            _fast_best = (best_frame_data or {}).get('corrected', '')
                            _fast_valid = (
                                len(_fast_best) >= 8
                                and bool(re.search(r'[A-Z]{2}\d{2}[A-Z0-9]{1,3}\d{4}', _fast_best))
                                and (best_frame_data or {}).get('conf', 0.0) >= 0.85
                            )
                            if _fast_valid:
                                logger.info(
                                    f"[INFERENCE] 640 OCR complete ('{_fast_best}') "
                                    f"— 1280 pass skipped"
                                )
                            else:
                                logger.info(
                                    f"[INFERENCE] 640 OCR incomplete ('{_fast_best}') "
                                    f"— running 1280 YOLO on original frame"
                                )
                                if _1280_results_cache is None:
                                    _1280_results_cache = plate_model.predict(
                                        original_frame, conf=0.20, verbose=False, imgsz=1280
                                    )
                                _conf_1280 = 0.0
                                if (_1280_results_cache and len(_1280_results_cache) > 0
                                        and _1280_results_cache[0].boxes is not None
                                        and len(_1280_results_cache[0].boxes) > 0):
                                    _conf_1280 = float(_1280_results_cache[0].boxes.conf.max().cpu().numpy())
                                    logger.info(f"[INFERENCE] 1280 conf={_conf_1280:.3f} — queuing 1280 OCR pass")
                                    _passes.append((_1280_results_cache, True, "1280"))
                                else:
                                    logger.info("[INFERENCE] 1280 found no plate box — no further retry")

                    # Propagate early-exit to the outer frame loop
                    if saved >= n:
                        break
                
            except Exception as e:
                logger.warning("SharedCamera: failed processing frame: %s", e)
        
        # Save frames based on detection status
        save_dir = None
        if best_frame_data:
            # DETECTION SUCCESS: Save the best truck frame to success_image folder
            logger.info(f"Best match found: frame {best_frame_data['frame_idx']}, score={best_frame_data['match_score']:.2f}")
            try:
                success_dir = os.path.join(BASE_PATH, 'success_image')
                os.makedirs(success_dir, exist_ok=True)
                orig_frame = best_frame_data.get('original_frame')
                if orig_frame is not None and orig_frame.size > 0:
                    existing = [f for f in os.listdir(success_dir) if f.startswith('image') and f.endswith('.jpg')]
                    nums = []
                    for f in existing:
                        try:
                            nums.append(int(f[5:-4]))
                        except ValueError:
                            pass
                    next_num = max(nums, default=0) + 1
                    success_fname = os.path.join(success_dir, f"image{next_num}.jpg")
                    cv2.imwrite(success_fname, orig_frame)
                    save_dir = success_dir
                    logger.info(f"Saved success frame to: {success_fname}")
                else:
                    logger.warning("No original frame stored in best_frame_data - skipping success_image save")
            except Exception as e:
                logger.error(f"Failed to save success frame: {e}")
        else:
            # NO DETECTION: Save all captured frames to single no_detection folder
            logger.warning(f"No number plate detected in {saved} frames - saving all frames for review")
            try:
                # Use single no_detection folder (not timestamped)
                no_detection_dir = os.path.join(save_root, "no_detection")
                os.makedirs(no_detection_dir, exist_ok=True)
                
                # Save all captured frames
                for i, frame in enumerate(captured_frames):
                    if frame is not None:
                        fname = os.path.join(no_detection_dir, f"undetected_{int(time.time())}_{i:03d}.jpg")
                        cv2.imwrite(fname, frame)
                
                save_dir = no_detection_dir
                logger.info(f"Saved {len(captured_frames)} frames to {no_detection_dir}")
            except Exception as e:
                logger.error(f"Failed to save undetected frames: {e}")

        elapsed = time.time() - start
        logger.info("SharedCamera: processed %d frames in %.2fs", saved, elapsed)
        return {
            "saved": 1 if best_frame_data else len(captured_frames), 
            "dir": save_dir, 
            "elapsed_s": elapsed,
            "best_result": best_frame_data
        }


# ----- Capture routine -----
def capture_frames(rtsp_url, count, save_root):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = os.path.join(save_root, ts)
    os.makedirs(save_dir, exist_ok=True)

    # Use UDP transport for faster capture
    cap = open_rtsp_capture(rtsp_url, transport=RTSP_TRANSPORT)
    if not cap or not cap.isOpened():
        logger.error(f"capture_frames: failed to open RTSP stream with {RTSP_TRANSPORT.upper()} transport")
        return None

    saved = 0
    start = time.time()
    logger.info("capture_frames: capturing %d frames to %s", count, save_dir)
    while saved < count:
        ret, frame = cap.read()
        if not ret:
            logger.warning("capture_frames: read failed, reconnecting briefly")
            time.sleep(0.1)
            continue
        fname = os.path.join(save_dir, f"frame_{saved:03d}.jpg")
        try:
            cv2.imwrite(fname, frame)
            saved += 1
        except Exception as e:
            logger.warning("capture_frames: failed writing frame: %s", e)
            break
    cap.release()
    elapsed = time.time() - start
    logger.info("capture_frames: saved %d frames in %.2fs", saved, elapsed)
    return {"saved": saved, "dir": save_dir, "elapsed_s": elapsed}

# ----- MQTT callbacks -----
class MQTTHandler:
    def __init__(self, broker, port, trigger_topic, result_topic, rtsp_url=None, shared_cam=None):
        # Use callback_api_version to avoid deprecation warning
        try:
            self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):
            # Fallback for older paho-mqtt versions
            self.client = mqtt.Client()
        self.broker = broker
        self.port = port
        self.trigger_topic = trigger_topic
        self.result_topic = result_topic
        self.rtsp_url = rtsp_url
        self.shared_cam = shared_cam

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def start(self):
        logger.info("Connecting to MQTT broker %s:%d", self.broker, self.port)
        try:
            self.client.connect(self.broker, self.port, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            raise ConnectionError(
                f"MQTT is not connected — could not reach broker '{self.broker}:{self.port}'. "
                f"Reason: {e}"
            ) from e

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def on_connect(self, client, userdata, flags, rc):
        logger.info("MQTT connected with rc=%s", rc)
        client.subscribe(self.trigger_topic)
        logger.info("Subscribed to %s", self.trigger_topic)

    def on_message(self, client, userdata, msg):
        logger.info("MQTT message on %s: %s", msg.topic, msg.payload)
        # Parse incoming message for optional vehicle number and rfid
        input_vehicle = None
        rfid = None
        try:
            decoded = msg.payload.decode('utf-8')
            try:
                j = json.loads(decoded)
                input_vehicle = j.get('Vehicle_Number') or j.get('vehicle_number') or j.get('VehicleNumber') or j.get('vehicle')
                rfid = j.get('rfid') or j.get('RFID') or j.get('Rfid')
                if input_vehicle:
                    input_vehicle = input_vehicle.strip().upper()
                if rfid:
                    rfid = rfid.strip()
            except Exception:
                # not JSON, treat raw string as possible vehicle
                s = decoded.strip()
                if s:
                    input_vehicle = s.upper()
        except Exception:
            input_vehicle = None
            rfid = None

        # Log MQTT trigger
        if _API_LOGGER_AVAILABLE:
            try:
                log_mqtt_event(
                    event_type="TRIGGER_RECEIVED",
                    topic=msg.topic,
                    payload={"vehicle_number": input_vehicle, "rfid": rfid},
                )
            except Exception:
                pass

        # On trigger, capture frames
        try:
            if self.shared_cam is not None:
                result = self.shared_cam.read_n_frames(CAPTURE_COUNT, SAVE_ROOT, input_vehicle=input_vehicle)
            else:
                result = capture_frames(self.rtsp_url, CAPTURE_COUNT, SAVE_ROOT)
            
            # Build payload with best result
            payload = {
                "timestamp": datetime.now().isoformat(),
                "trigger_topic": msg.topic,
                "saved": result["saved"] if result else 0,
                "dir": result["dir"] if result else None,
                "input_vehicle": input_vehicle,
                "rfid": rfid,
                "elapsed_s": result.get("elapsed_s", 0),
                "detection_status": "success" if (result and result.get('best_result')) else "no_plate_detected"
            }
            
            # Add best result if available
            if result and result.get('best_result'):
                best = result['best_result']
                
                # Save best plate image to image_folder regardless of SQL logging setting
                image_filename = None
                try:
                    best_img = best.get('blurred')
                    if best_img is not None and best_img.size > 0:
                        image_filename = save_plate_image_and_get_filename(best_img)
                except Exception as e:
                    logger.warning(f"Failed to save plate image: {e}")
                
                payload['best_match'] = {
                    'frame_idx': best['frame_idx'],
                    'raw_text': best['raw'],
                    'cleaned_text': best['cleaned'],
                    'corrected_text': best['corrected'],
                    'confidence': best['conf'],
                    'match_score': best['match_score'],
                    'image_filename': image_filename,
                    'image_path': os.path.join(get_image_folder(), image_filename) if image_filename else None
                }
                logger.info(f"Best match: '{best['corrected']}' (score={best['match_score']:.2f})")
                
                # Insert result into SQL database
                if ENABLE_SQL_LOGGING:
                    # image_filename already saved above
                    
                    sql_data = {
                        'timestamp': datetime.now(),
                        'raw_text': best['raw'],
                        'cleaned_text': best['cleaned'],
                        'corrected_text': best['corrected'],
                        'input_vehicle': input_vehicle,
                        'rfid': rfid,
                        'confidence': best['conf'],
                        'match_score': best['match_score'],
                        'frame_idx': best['frame_idx'],
                        'save_dir': result.get('dir'),
                        'trigger_topic': msg.topic,
                        'processing_time': result.get('elapsed_s'),
                        'image_filename': image_filename,
                        'pass': best.get('pass', '640'),
                    }
                    insert_plate_recognition(sql_data)  # image already saved above
            else:
                # No plate detected: do NOT insert blank/no-detection record so UI preserves previous record
                logger.debug("No plate detected - skipping DB insert to preserve last detection on UI")
        except Exception as e:
            logger.error("Error during capture: %s", e)
            payload = {"error": str(e), "timestamp": datetime.now().isoformat()}
        # Publish result
        try:
            client.publish(self.result_topic, json.dumps(payload))
            logger.info("Published result to %s", self.result_topic)
            # Log MQTT result
            if _API_LOGGER_AVAILABLE:
                try:
                    log_mqtt_event(
                        event_type="RESULT_PUBLISHED",
                        topic=self.result_topic,
                        payload={"vehicle_number": input_vehicle, "rfid": rfid},
                        result=payload,
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Failed to publish result: %s", e)


 # Set paths for models and resources
if IS_PRODUCTION and TEMP_RESOURCE_DIR:
    TEMPLATES_DIR = os.path.join(TEMP_RESOURCE_DIR, "templates")
    STATIC_DIR = os.path.join(TEMP_RESOURCE_DIR, "static")
else:
    TEMPLATES_DIR = os.path.join(BASE_PATH, "templates")
    STATIC_DIR = os.path.join(BASE_PATH, "static")

print(f"Flask configuration:", flush=True)
print(f"  Templates folder: {TEMPLATES_DIR} - Exists: {os.path.isdir(TEMPLATES_DIR)}", flush=True)
print(f"  Static folder: {STATIC_DIR} - Exists: {os.path.isdir(STATIC_DIR)}", flush=True)

app = Flask(__name__, 
            template_folder=TEMPLATES_DIR,
            static_folder=STATIC_DIR)
app.secret_key = 'anpr_secret_key_2026'  # Change to a secure random value in production

# ─── API Access (no file logging) ─────────────────────────────────────────────
import time as _time

# Endpoints that are too noisy / not useful to log (polling, assets)
_LOG_SKIP_ENDPOINTS = {
    "/api/records", "/api/image/", "/video_feed", "/api/refresh-stream",
    "/favicon.ico", "/static/",
}

# Dedicated log for /api/detect: full request (who, body) and full response (status, body) for tracing
_API_DETECT_LOG_FILE = os.path.join(BASE_PATH, "api_detect.log")
_API_DETECT_LOG_LOCK = threading.Lock()

def _safe_json_log(data):
    """Compact JSON for log file, never raises."""
    try:
        return json.dumps(data, ensure_ascii=False, default=str, separators=(',', ':'))
    except Exception:
        return "{}"

def log_detect_request(caller_ip, user_agent, request_body, response_status, response_body, processing_time_ms=None):
    """Log each /api/detect — caller identity and response summary only."""
    ts = _log_ts()
    _ms = processing_time_ms or 0
    _secs = int(_ms / 1000)
    _time_str = (f"{_secs // 60}m {_secs % 60:02d}s" if _secs >= 60 else f"{_secs}s")

    # Derive result fields from response body
    rb = response_body or {}
    bm = rb.get("best_match") or {}
    plate = rb.get("detected_plate") or (bm.get("vehicle_ocr_value") if isinstance(bm, dict) else None)
    confidence = bm.get("confidence") if isinstance(bm, dict) else None
    match_score = bm.get("match_score") if isinstance(bm, dict) else None
    error_msg = rb.get("error")

    # Build result line
    if response_status == 200:
        http_label = "HTTP 200 OK"
    elif response_status == 500:
        http_label = "HTTP 500 ERROR"
    else:
        http_label = f"HTTP {response_status}"

    # Input fields from request body
    veh = (request_body or {}).get("vehicle_number") or (request_body or {}).get("Vehicle_Number") or "(none)"
    rfid = (request_body or {}).get("rfid") or (request_body or {}).get("RFID") or "(none)"

    if plate:
        result_parts = [f"plate={plate}"]
        if confidence is not None:
            result_parts.append(f"conf={confidence:.2f}%")
        if match_score is not None and match_score != "N/A":
            result_parts.append(f"score={match_score}")
        result_parts.append(f"time={_time_str}")
        result_line = "  |  ".join(result_parts)
    elif error_msg:
        result_line = f"error={str(error_msg)[:80]}  |  time={_time_str}"
    else:
        result_line = f"no plate detected  |  time={_time_str}"

    # Console summary
    outcome = f"plate={plate}" if plate else (f"error={str(error_msg)[:40]}" if error_msg else "no_plate")
    logger.info("[api/detect] %s from=%s %s time=%s", ts, caller_ip, outcome, _time_str)

    # File: clean focused entry — who called us + what we responded
    try:
        with _API_DETECT_LOG_LOCK:
            with open(_API_DETECT_LOG_FILE, "a", encoding="utf-8") as f:
                f.write("-" * 80 + "\n")
                f.write(f"[{ts}]  FROM: {caller_ip}  |  {http_label}\n")
                f.write(f"  INPUT  : vehicle={veh}  |  rfid={rfid}\n")
                f.write(f"  RESULT : {result_line}\n")
                f.write("-" * 80 + "\n")
    except Exception as e:
        logger.debug("Could not write api_detect.log: %s", e)

def _should_skip(endpoint):
    return any(endpoint.startswith(p) for p in _LOG_SKIP_ENDPOINTS)

def _log_ts():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def _get_caller_ip():
    """Return real caller IP, honouring X-Forwarded-For for reverse-proxied setups."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"

def _safe_json(data):
    """Compact JSON string, never raises."""
    try:
        return json.dumps(data, ensure_ascii=False, default=str, separators=(',', ':'))
    except Exception:
        return "{}"

def _extract_response_body(response):
    """
    Return the full JSON response body as a dict for API endpoints.
    Falls back to an empty dict for non-JSON or on errors.
    """
    try:
        if response.is_json:
            data = response.get_json(silent=True)
            if isinstance(data, dict):
                # Flatten nested best_match to surface key detection fields at top level
                flat = {k: v for k, v in data.items()
                        if k not in ('image_path',) and not isinstance(v, (dict, list))}
                bm = data.get("best_match")
                if isinstance(bm, dict):
                    flat["detected_plate"]  = bm.get("vehicle_ocr_value")
                    flat["confidence_pct"]  = bm.get("confidence")
                    flat["match_score"]     = bm.get("match_score")
                    flat["image_filename"]  = bm.get("image_filename")
                # Remove None values for cleanliness
                return {k: v for k, v in flat.items() if v is not None}
            return {}
    except Exception:
        return {}

def log_api_call(caller_ip, user_agent, method, endpoint, query_string,
                 request_body, response_status, response_body, processing_time_ms):
    if _should_skip(endpoint):
        return

def log_mqtt_event(event_type, topic, payload, result=None):
    pass

def log_login_attempt(caller_ip, username, success):
    pass

def _log_shutdown(caller_ip):
    pass

_API_LOGGER_AVAILABLE = True
# ─────────────────────────────────────────────────────────────────────────────

@app.before_request
def _before_request_timer():
    """Record request start time for processing-time calculation."""
    request._start_time = _time.time()

@app.after_request
def _after_request_logger(response):
    """Log every API request with full caller info, request body, and response body."""
    try:
        if _should_skip(request.path):
            return response
        elapsed_ms = (_time.time() - getattr(request, '_start_time', _time.time())) * 1000
        caller_ip = _get_caller_ip()
        user_agent = request.headers.get("User-Agent", "")

        # Capture request body (POST / PUT / PATCH) for logging
        req_body = {}
        if request.method in ('POST', 'PUT', 'PATCH'):
            try:
                req_body = request.get_json(silent=True, force=True) or {}
                if not req_body and request.form:
                    req_body = {k: v for k, v in request.form.items()
                                if k.lower() not in ('password', 'passwd', 'pwd')}
            except Exception:
                pass

        # Dedicated log for /api/detect: who sent request + our full response (trace)
        if request.path == "/api/detect":
            full_response_body = response.get_json(silent=True) if response.is_json else {}
            log_detect_request(
                caller_ip=caller_ip,
                user_agent=user_agent,
                request_body=req_body,
                response_status=response.status_code,
                response_body=full_response_body or _extract_response_body(response),
                processing_time_ms=elapsed_ms,
            )

        # Capture query string params (GET requests)
        query_string = request.query_string.decode("utf-8", errors="replace") or ""

        log_api_call(
            caller_ip        = caller_ip,
            user_agent       = user_agent,
            method           = request.method,
            endpoint         = request.path,
            query_string     = query_string,
            request_body     = req_body,
            response_status  = response.status_code,
            response_body    = _extract_response_body(response),
            processing_time_ms = elapsed_ms,
        )
    except Exception:
        pass  # never let logging break the response
    return response
# ─────────────────────────────────────────────────────────────────────────────

# Favicon route removed — no window icon used
@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('truck2.png')

# --- LOGIN ROUTE ---
REMEMBER_COOKIE_DAYS = 30

# ── Remember-Me: DB-backed encrypted storage ────────────────────────────────
# Credentials are stored in ANPR_RememberMe table using Fernet symmetric
# encryption derived from the app secret so they are never stored in plaintext.
_REMEMBER_ME_SECRET = b'rajmines@9727_ANPR_SECRET_KEY_2024'

def _get_remember_fernet():
    """Return a Fernet instance derived from the app secret, or None if unavailable."""
    try:
        import hashlib as _hl2
        import base64 as _b64
        from cryptography.fernet import Fernet
        key = _b64.urlsafe_b64encode(_hl2.sha256(_REMEMBER_ME_SECRET).digest())
        return Fernet(key)
    except Exception:
        return None

def _rm_encrypt(value):
    f = _get_remember_fernet()
    if f is None:
        return None
    try:
        return f.encrypt(value.encode('utf-8')).decode('utf-8')
    except Exception:
        return None

def _rm_decrypt(token):
    f = _get_remember_fernet()
    if f is None:
        return None
    try:
        return f.decrypt(token.encode('utf-8')).decode('utf-8')
    except Exception:
        return None

def _ensure_remember_me_table(cursor):
    """Create ANPR_RememberMe table if it does not exist (call inside an open cursor)."""
    if DB_TYPE == 'MSSQL':
        cursor.execute("""
            IF NOT EXISTS (
                SELECT * FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_NAME = 'ANPR_RememberMe'
            )
            CREATE TABLE ANPR_RememberMe (
                Id INT IDENTITY(1,1) PRIMARY KEY,
                EncryptedUsername NVARCHAR(2000) NOT NULL,
                EncryptedPassword NVARCHAR(2000) NOT NULL,
                UpdatedAt DATETIME DEFAULT GETDATE()
            )
        """)
    elif DB_TYPE == 'MySQL':
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ANPR_RememberMe (
                Id INT AUTO_INCREMENT PRIMARY KEY,
                EncryptedUsername VARCHAR(2000) NOT NULL,
                EncryptedPassword VARCHAR(2000) NOT NULL,
                UpdatedAt DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
    elif DB_TYPE == 'PostgreSQL':
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ANPR_RememberMe (
                Id SERIAL PRIMARY KEY,
                EncryptedUsername VARCHAR(2000) NOT NULL,
                EncryptedPassword VARCHAR(2000) NOT NULL,
                UpdatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

def _get_remember_me_file():
    """Return absolute path to remember_me.json (next to exe in production, or cwd in dev)."""
    if IS_PRODUCTION and BASE_PATH:
        return os.path.join(BASE_PATH, 'remember_me.json')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'remember_me.json')

def _read_remember_me_file():
    """Read credentials from remember_me.json fallback file. Returns (username, password) or (None, None)."""
    try:
        path = _get_remember_me_file()
        if not os.path.exists(path):
            return (None, None)
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        u = data.get('username') or ''
        p = data.get('password') or ''
        if u:
            return (u, p)
    except Exception:
        pass
    return (None, None)

def _write_remember_me_file(username, password):
    """Write credentials to remember_me.json fallback file."""
    try:
        path = _get_remember_me_file()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'username': username, 'password': password}, f)
    except Exception as e:
        logger.warning(f"Could not write remember_me.json: {e}")

def _clear_remember_me_file():
    """Clear the remember_me.json fallback file."""
    try:
        path = _get_remember_me_file()
        if os.path.exists(path):
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'username': '', 'password': ''}, f)
    except Exception:
        pass

def _read_remember_me():
    """Read encrypted remember-me credentials from DB, falling back to remember_me.json file.
    Returns (username, password) or (None, None)."""
    # Try DB first (only when server is configured)
    if SQL_SERVER and SQL_SERVER.strip():
        try:
            conn = get_sql_connection()
            if conn is not None:
                try:
                    if DB_TYPE == 'MongoDB':
                        db = conn[SQL_DATABASE]
                        row = db['ANPR_RememberMe'].find_one()
                        conn.close()
                        if not row:
                            return (None, None)
                        enc_u = row.get('EncryptedUsername', '')
                        enc_p = row.get('EncryptedPassword', '')
                    else:
                        cursor = conn.cursor()
                        _ensure_remember_me_table(cursor)
                        if DB_TYPE == 'MSSQL':
                            cursor.execute("SELECT TOP 1 EncryptedUsername, EncryptedPassword FROM ANPR_RememberMe ORDER BY Id DESC")
                        else:
                            cursor.execute("SELECT EncryptedUsername, EncryptedPassword FROM ANPR_RememberMe ORDER BY Id DESC LIMIT 1")
                        row = cursor.fetchone()
                        conn.commit()
                        cursor.close()
                        conn.close()
                        if not row:
                            return (None, None)
                        enc_u, enc_p = row[0], row[1]
                    u = _rm_decrypt(enc_u)
                    p = _rm_decrypt(enc_p)
                    if u:
                        return (u, p or '')
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
        except Exception:
            pass
    # Fallback: read from local remember_me.json file
    return _read_remember_me_file()

def _write_remember_me(username, password):
    """Encrypt and store remember-me credentials in ANPR_RememberMe DB table.
    Falls back to remember_me.json file when DB is unavailable."""
    # Always write to file as reliable fallback
    _write_remember_me_file(username, password)

    enc_u = _rm_encrypt(username)
    enc_p = _rm_encrypt(password)
    if enc_u is None or enc_p is None:
        return  # File fallback already written above
    if not SQL_SERVER or not SQL_SERVER.strip():
        return  # No DB configured; file is the only store
    try:
        conn = get_sql_connection()
        if conn is None:
            return  # File fallback already written
        try:
            if DB_TYPE == 'MongoDB':
                db = conn[SQL_DATABASE]
                col = db['ANPR_RememberMe']
                col.delete_many({})
                col.insert_one({'EncryptedUsername': enc_u, 'EncryptedPassword': enc_p})
                conn.close()
            else:
                cursor = conn.cursor()
                _ensure_remember_me_table(cursor)
                cursor.execute("DELETE FROM ANPR_RememberMe")
                if DB_TYPE == 'MSSQL':
                    cursor.execute(
                        "INSERT INTO ANPR_RememberMe (EncryptedUsername, EncryptedPassword) VALUES (?, ?)",
                        (enc_u, enc_p)
                    )
                else:
                    cursor.execute(
                        "INSERT INTO ANPR_RememberMe (EncryptedUsername, EncryptedPassword) VALUES (%s, %s)",
                        (enc_u, enc_p)
                    )
                conn.commit()
                cursor.close()
                conn.close()
            logger.info("Remember-me credentials stored in DB (encrypted).")
        except Exception as e:
            logger.debug(f"Could not write remember-me to DB (file fallback used): {e}")
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Could not write remember-me to DB (file fallback used): {e}")

def _clear_remember_me():
    """Remove remember-me record from DB and clear the local file fallback."""
    # Always clear file too
    _clear_remember_me_file()

    if not SQL_SERVER or not SQL_SERVER.strip():
        return  # No DB configured
    try:
        conn = get_sql_connection()
        if conn is None:
            return
        try:
            if DB_TYPE == 'MongoDB':
                db = conn[SQL_DATABASE]
                db['ANPR_RememberMe'].delete_many({})
                conn.close()
            else:
                cursor = conn.cursor()
                try:
                    cursor.execute("DELETE FROM ANPR_RememberMe")
                    conn.commit()
                except Exception:
                    pass
                cursor.close()
                conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass
# ────────────────────────────────────────────────────────────────────────────


def _verify_password_from_db(password):
    """Check password against ANPR_PasswordStore table.
    Returns True if match, False if wrong, None if table/DB not available (fallback to default).
    Runs inside a daemon thread with a hard 5-second timeout so login never hangs
    even when the DB server IP is configured but unreachable."""
    import hashlib as _hl
    import threading as _threading
    # Skip DB entirely when no server is configured
    if not SQL_SERVER or not SQL_SERVER.strip():
        return None

    _result = [None]  # mutable container for thread result

    def _db_check():
        try:
            conn = get_sql_connection()
            if conn is None:
                return
            try:
                cursor = conn.cursor()
                if DB_TYPE == 'MongoDB':
                    db = conn[SQL_DATABASE]
                    row = db['ANPR_PasswordStore'].find_one()
                    conn.close()
                    if not row:
                        return
                    salt = row.get('Salt', '')
                    stored_hash = row.get('PasswordHash', '')
                else:
                    cursor.execute("SELECT TOP 1 PasswordHash, Salt FROM ANPR_PasswordStore" if DB_TYPE == 'MSSQL'
                                   else "SELECT PasswordHash, Salt FROM ANPR_PasswordStore LIMIT 1")
                    row = cursor.fetchone()
                    cursor.close()
                    conn.close()
                    if not row:
                        return
                    stored_hash, salt = row[0], row[1]
                computed = _hl.sha256((salt + password).encode('utf-8')).hexdigest()
                _result[0] = (computed == stored_hash)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            pass

    t = _threading.Thread(target=_db_check, daemon=True)
    t.start()
    t.join(timeout=5)  # hard 5-second ceiling — never block login longer than this
    if t.is_alive():
        logger.warning("[LOGIN] DB password check timed out after 5s — falling back to default")
    return _result[0]  # None if timed out or DB unavailable → fallback to admin/admin

@app.route('/login', methods=['GET', 'POST'])
def login():
    login_error = False
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        remember_me = request.form.get('remember_me') == 'on'

        # Check DB password first; admin/admin always works as an emergency fallback
        # so the operator can never be permanently locked out.
        db_result = _verify_password_from_db(password)
        if db_result is True:
            password_ok = (username == 'admin')
        elif db_result is False:
            # DB has a custom password and it didn't match —
            # still allow the default admin/admin as an emergency backdoor.
            password_ok = (username == 'admin' and password == 'admin')
        else:
            # DB not available or table empty — fall back to default
            password_ok = (username == 'admin' and password == 'admin')

        if password_ok:
            session['logged_in'] = True
            if _API_LOGGER_AVAILABLE:
                try:
                    log_login_attempt(request.remote_addr or "unknown", username, success=True)
                except Exception:
                    pass
            resp = make_response(redirect(url_for('index')))
            if remember_me:
                resp.set_cookie('remembered_username', username, max_age=REMEMBER_COOKIE_DAYS * 24 * 3600, path='/', samesite='Lax')
                resp.set_cookie('remembered_password', password, max_age=REMEMBER_COOKIE_DAYS * 24 * 3600, path='/', samesite='Lax')
                _write_remember_me(username, password)
            else:
                resp.set_cookie('remembered_username', '', max_age=0, path='/')
                resp.set_cookie('remembered_password', '', max_age=0, path='/')
                _clear_remember_me()
            return resp
        else:
            if _API_LOGGER_AVAILABLE:
                try:
                    log_login_attempt(request.remote_addr or "unknown", username, success=False)
                except Exception:
                    pass
            flash('Invalid username or password', 'error')
            login_error = True
    # GET or failed POST: pre-fill from cookies, then from DB remember-me,
    # and finally fall back to initial defaults admin/admin.
    if request.method == 'POST':
        default_username = (request.form.get('username') or '').strip()
        default_password = ''  # never pre-fill password after a failed login attempt
        remembered_checked = request.form.get('remember_me') == 'on'
    else:
        default_username = request.cookies.get('remembered_username') or ''
        default_password = request.cookies.get('remembered_password') or ''
        if not default_username or not default_password:
            db_user, db_pass = _read_remember_me()
            if db_user:
                default_username = db_user
                default_password = db_pass
        # If still no remembered credentials, pre-fill with initial default admin/admin
        if not default_username and not default_password:
            default_username = 'admin'
            default_password = 'admin'
            remembered_checked = False
        else:
            remembered_checked = bool(default_username and default_password)
    return render_template('login.html', default_username=default_username, default_password=default_password, remembered_checked=remembered_checked, login_error=login_error, auto_login_enabled=(AUTO_LOGIN and not login_error))

# --- LOGOUT ROUTE ---
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# Global reference to shared camera
web_shared_cam = None
# Global reference to MQTT handler (set in main) so save_config can update its camera when RTSP URL changes
MQTT_HANDLER_REF = None


def _reinitialize_rtsp_camera():
    """Stop the current RTSP camera and start a new one with the updated RTSP_URL. Called after config save."""
    global web_shared_cam, MQTT_HANDLER_REF
    try:
        old_cam = web_shared_cam
        if old_cam is not None:
            try:
                old_cam.stop()
            except Exception as e:
                logger.warning(f"Error stopping old camera: {e}")
            web_shared_cam = None
        if not RTSP_URL or not RTSP_URL.strip():
            logger.info("RTSP URL is empty after config save; video feed will show no stream until URL is set.")
            _system_status['camera'] = 'skipped'
            return
        new_cam = SharedCamera(RTSP_URL)
        new_cam.start()
        web_shared_cam = new_cam
        if MQTT_HANDLER_REF is not None:
            MQTT_HANDLER_REF.shared_cam = new_cam
        # Wait up to 15 s for the first frame to confirm connectivity.
        # HEVC/H.265 cameras need to see a keyframe before any frame decodes
        # successfully — keyframe interval is typically 2-5 s, so 5 s was too short.
        _cam_deadline = time.time() + 15.0
        while time.time() < _cam_deadline:
            _f, _ft = new_cam.get_frame()
            if _f is not None:
                break
            time.sleep(0.25)
        _f, _ft = new_cam.get_frame()
        if _f is None:
            _system_status['camera'] = 'failed'
            logger.warning("Camera: RTSP stream did not deliver a frame within 15 s after reinit")
        else:
            _system_status['camera'] = 'ready'
            logger.info("✓ Camera stream reconnected and delivering frames")
        logger.info("RTSP camera reinitialized with new URL.")
    except Exception as e:
        _system_status['camera'] = 'failed'
        logger.error(f"Failed to reinitialize RTSP camera: {e}", exc_info=True)


def _reinitialize_mqtt():
    """Stop current MQTT connection and start a new one with updated broker/port/topics (or leave stopped if disabled). Called after config save."""
    global MQTT_HANDLER_REF
    try:
        if MQTT_HANDLER_REF is not None:
            try:
                MQTT_HANDLER_REF.stop()
            except Exception as e:
                logger.warning(f"Error stopping MQTT handler: {e}")
            MQTT_HANDLER_REF = None
        if ENABLE_MQTT and MQTT_BROKER and str(MQTT_BROKER).strip():
            new_mqtt = MQTTHandler(MQTT_BROKER, MQTT_PORT, MQTT_TRIGGER_TOPIC, MQTT_PUBLISH_TOPIC, rtsp_url=None, shared_cam=web_shared_cam)
            try:
                new_mqtt.start()
                MQTT_HANDLER_REF = new_mqtt
                logger.info("MQTT reinitialized with new settings.")
                _system_status['mqtt'] = 'connected'
            except ConnectionError as e:
                logger.warning(str(e))
                logger.warning("MQTT is not connected — skipping MQTT. App will continue without it.")
                MQTT_HANDLER_REF = None
                _system_status['mqtt'] = 'not_connected'
        else:
            logger.info("MQTT disabled or broker empty; MQTT handler stopped.")
            _system_status['mqtt'] = 'disabled'
    except Exception as e:
        logger.error(f"Failed to reinitialize MQTT: {e}", exc_info=True)


def draw_plate_detections(frame):
    """Draw bounding boxes around detected plates on frame"""
    if plate_model is None:
        return frame
    
    try:
        # Run plate detection with optimized settings
        results = plate_model.predict(frame, conf=0.35, verbose=False, imgsz=640)
        if results and len(results) > 0:
            r = results[0]
            if hasattr(r, "boxes") and r.boxes is not None and len(r.boxes) > 0:
                boxes = r.boxes.xyxy.cpu().numpy()
                for i, box in enumerate(boxes):
                    x1, y1, x2, y2 = map(int, box)
                    
                    # Draw green bounding box only
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
    except Exception as e:
        logger.debug(f"Plate detection error in web view: {e}")
    
    return frame

def generate_frames():
    """Generate frames for MJPEG streaming (raw video only, no detection)"""
    frame_counter = 0
    while True:
        if web_shared_cam is not None:
            frame, _ = web_shared_cam.get_frame()
            if frame is not None:
                frame_counter += 1
                # Skip frames to reduce CPU (process every 2nd frame)
                if frame_counter % 2 != 0:
                    time.sleep(1.0 / WEB_STREAM_FPS)
                    continue
                
                # Resize for web to reduce bandwidth
                h, w = frame.shape[:2]
                if w > 854:  # 854p resolution for web stream
                    scale = 854 / w
                    new_w, new_h = int(w * scale), int(h * scale)
                    frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                
                # NO DETECTION - just show raw stream
                # Detection only happens on MQTT trigger in read_n_frames()
                
                # Encode frame as JPEG with lower quality
                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ret:
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        time.sleep(1.0 / WEB_STREAM_FPS)  # Dynamic FPS control

@app.route('/')
def index():
    # Require login
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    # Read wb_info.json for WeighBridge Name and ID
    _wb_name = LOCATION_NAME
    _wb_id   = LOCATION_ID
    try:
        _wbp = _get_wb_info_path()
        if os.path.exists(_wbp):
            with open(_wbp, 'r', encoding='utf-8-sig') as _wbf:
                _wbd = json.load(_wbf).get('Data', {})
            if (_wbd.get('wb_name') or '').strip():
                _wb_name = _wbd['wb_name'].strip()
            if (_wbd.get('wb_id') or '').strip():
                _wb_id = _wbd['wb_id'].strip()
    except Exception:
        pass
    return render_template('index.html', 
                           rtsp_url=RTSP_URL,
                           mqtt_broker=MQTT_BROKER,
                           mqtt_port=MQTT_PORT,
                           location_name=LOCATION_NAME,
                           location_coords=LOCATION_COORDS,
                           wb_name=_wb_name,
                           wb_id=_wb_id,
                           dept_title=DEPT_TITLE,
                           dept_subtitle=DEPT_SUBTITLE,
                           dept_logo_filename=DEPT_LOGO_FILENAME,
                           dept_branding_enabled=DEPT_BRANDING_ENABLED,
                           footer_dept=FOOTER_DEPT)

@app.route('/browser')
def browser():
    """Browser page with URL bar"""
    return render_template('browser.html')

@app.route('/external')
def external():
    """External URL page with persistent navigation bar"""
    url = request.args.get('url', 'https://www.google.com')
    return render_template('external.html', url=url)

@app.route('/proxy', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def proxy():
    """Proxy endpoint to bypass X-Frame-Options, CSP, and CSRF restrictions"""
    
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = make_response('', 200)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS, PATCH'
        response.headers['Access-Control-Allow-Headers'] = '*'
        return response
    
    target_url = request.args.get('url', '')
    
    if not target_url:
        return "No URL provided", 400
    
    # Skip double-proxied URLs
    if target_url and '/proxy?url=' in target_url and target_url.startswith('http://192.168.10.208'):
        # Extract the actual external URL
        import urllib.parse
        match = re.search(r'/proxy\?url=(https?://[^"\']+)', target_url)
        if match:
            target_url = urllib.parse.unquote(match.group(1))
        else:
            return "<html><body><h2>Error</h2><p>Invalid proxy URL format</p></body></html>", 400
    
    # Validate URL before proceeding
    if not target_url or not target_url.strip():
        return "<html><body><h2>Error</h2><p>No URL provided</p></body></html>", 400
    
    try:
        from urllib.parse import urlparse, urljoin
        parsed_target = urlparse(target_url)
        
        # Validate parsed URL has required components
        if not parsed_target.scheme or not parsed_target.netloc:
            return f"<html><body><h2>Error</h2><p>Invalid URL: {target_url}</p><p>URL must include protocol (http:// or https://) and domain</p></body></html>", 400
        
        base_origin = f"{parsed_target.scheme}://{parsed_target.netloc}"
        
        # Get CSRF token from session cookies if available for this domain
        csrf_cookie_names = ['_xsrf', 'csrftoken', 'csrf_token', 'XSRF-TOKEN']
        csrf_token = None
        for cookie_name in csrf_cookie_names:
            if cookie_name in proxy_session.cookies:
                csrf_token = proxy_session.cookies.get(cookie_name)
                break
        
        # Prepare headers to mimic a browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
        # Add CSRF token to headers if available and this is a state-changing request
        if csrf_token and request.method in ['POST', 'PUT', 'DELETE', 'PATCH']:
            headers['X-CSRFToken'] = csrf_token
            headers['X-XSRFToken'] = csrf_token
            headers['X-XSRF-TOKEN'] = csrf_token
        
        # Forward relevant headers from client (including Authorization for camera auth)
        # But don't forward Cookie header - we'll use proxy_session cookies instead
        for header in ['Referer', 'Content-Type', 'X-Requested-With', 'X-CSRFToken', 'X-XSRFToken', 'Authorization']:
            if header in request.headers:
                headers[header] = request.headers[header]
        
        # Merge client cookies with proxy_session cookies
        # First, add any cookies from the client request to proxy_session
        if 'Cookie' in request.headers:
            client_cookies = request.headers['Cookie']
            # Parse and add client cookies to session
            for cookie_pair in client_cookies.split(';'):
                cookie_pair = cookie_pair.strip()
                if '=' in cookie_pair:
                    name, value = cookie_pair.split('=', 1)
                    # Don't set domain - let requests handle it automatically
                    proxy_session.cookies.set(name.strip(), value.strip())
        
        # Also check Flask request cookies (these are parsed cookies from the client)
        for cookie_name, cookie_value in request.cookies.items():
            # Don't set domain - let requests handle it automatically
            proxy_session.cookies.set(cookie_name, cookie_value)
        
        # Note: requests.Session automatically includes cookies in requests
        # We don't need to manually set the Cookie header
        
        # Set proper referer and origin
        headers['Referer'] = target_url
        headers['Origin'] = base_origin
        
        # Support for HTTP Basic/Digest authentication (common in cameras)
        auth = None
        if parsed_target.username and parsed_target.password:
            # Extract credentials from URL (e.g., http://user:pass@192.168.1.100:8080)
            from requests.auth import HTTPBasicAuth, HTTPDigestAuth
            auth = HTTPDigestAuth(parsed_target.username, parsed_target.password)
            # Try basic auth if digest fails
            # Reconstruct URL without credentials
            target_url = f"{parsed_target.scheme}://{parsed_target.hostname}"
            if parsed_target.port:
                target_url += f":{parsed_target.port}"
            target_url += (parsed_target.path or '/')
            if parsed_target.query:
                target_url += f"?{parsed_target.query}"
        
        # Ensure target_url is valid
        if not target_url:
            return "<html><body><h2>Error</h2><p>Invalid URL provided</p></body></html>", 400
        
        # Prepare request data
        data = None
        files = None
        
        if request.method in ['POST', 'PUT', 'PATCH']:
            content_type = request.headers.get('Content-Type', '')
            
            if 'multipart/form-data' in content_type:
                # Handle file uploads
                files = {}
                for key in request.files:
                    files[key] = request.files[key]
                data = request.form.to_dict()
                
                # Add CSRF token to form data if available
                if csrf_token and '_xsrf' not in data and 'csrfmiddlewaretoken' not in data:
                    data['_xsrf'] = csrf_token
                    
            elif 'application/x-www-form-urlencoded' in content_type:
                # Handle form data - convert to dict
                data = request.form.to_dict()
                
                # Add CSRF tokens if present in session cookies and not already in form
                if csrf_token:
                    if '_xsrf' not in data:
                        data['_xsrf'] = csrf_token
                    if 'csrfmiddlewaretoken' not in data:
                        data['csrfmiddlewaretoken'] = csrf_token
            else:
                # Handle raw data
                data = request.get_data()
        
        # Make request using session to maintain cookies across requests
        if request.method == 'POST':
            resp = proxy_session.post(target_url, headers=headers, data=data, files=files, auth=auth,
                                     timeout=15, verify=False, allow_redirects=True)
        elif request.method == 'PUT':
            resp = proxy_session.put(target_url, headers=headers, data=data, auth=auth,
                                    timeout=15, verify=False, allow_redirects=True)
        elif request.method == 'DELETE':
            resp = proxy_session.delete(target_url, headers=headers, auth=auth,
                                       timeout=15, verify=False, allow_redirects=True)
        elif request.method == 'PATCH':
            resp = proxy_session.patch(target_url, headers=headers, data=data, auth=auth,
                                      timeout=15, verify=False, allow_redirects=True)
        else:  # GET
            resp = proxy_session.get(target_url, headers=headers, auth=auth,
                                    timeout=15, verify=False, allow_redirects=True)
        
        # Get content type
        content_type = resp.headers.get('Content-Type', '')
        
        # For HTML content, rewrite URLs to go through proxy
        if 'text/html' in content_type:
            content = resp.text
            final_url = resp.url  # Use final URL after redirects
            parsed_final = urlparse(final_url)
            final_origin = f"{parsed_final.scheme}://{parsed_final.netloc}"
            
            # Extract ALL types of CSRF tokens from response and store in session
            # JupyterHub uses _xsrf
            csrf_token_match = re.search(r'<input[^>]+name=["\']_xsrf["\'][^>]+value=["\']([^"\']+)["\']', content)
            if not csrf_token_match:
                csrf_token_match = re.search(r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']_xsrf["\']', content)
            if csrf_token_match:
                token_value = csrf_token_match.group(1)
                proxy_session.cookies.set('_xsrf', token_value)
            
            # Django/Label Studio uses csrfmiddlewaretoken
            csrf_django = re.search(r'<input[^>]+name=["\']csrfmiddlewaretoken["\'][^>]+value=["\']([^"\']+)["\']', content)
            if not csrf_django:
                csrf_django = re.search(r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']csrfmiddlewaretoken["\']', content)
            if csrf_django:
                token_value = csrf_django.group(1)
                proxy_session.cookies.set('csrftoken', token_value)
            
            # Extract from meta tags
            csrf_meta = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', content)
            if csrf_meta:
                token_value = csrf_meta.group(1)
                proxy_session.cookies.set('csrf_token', token_value)
            
            # Rewrite absolute URLs (http:// and https://)
            content = re.sub(
                r'(href|src|action)=(["\'])(https?://[^"\']+)\2',
                lambda m: f'{m.group(1)}={m.group(2)}/proxy?url={m.group(3)}{m.group(2)}',
                content
            )
            
            # Rewrite protocol-relative URLs (//example.com/path)
            content = re.sub(
                r'(href|src|action)=(["\'])(//[^"\']+)\2',
                lambda m: f'{m.group(1)}={m.group(2)}/proxy?url=http:{m.group(3)}{m.group(2)}',
                content
            )
            
            # Rewrite root-relative URLs (/path/to/resource) - exclude already proxied URLs
            content = re.sub(
                r'(href|src|action)=(["\'])(/(?!proxy)[^"\']+)\2',
                lambda m: f'{m.group(1)}={m.group(2)}/proxy?url={final_origin}{m.group(3)}{m.group(2)}',
                content
            )
            
            response = make_response(content, resp.status_code)
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
        else:
            # For non-HTML content (images, CSS, JS, fonts, etc.)
            response = make_response(resp.content, resp.status_code)
            if content_type:
                response.headers['Content-Type'] = content_type
        
        # Remove blocking headers and add permissive ones
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Content-Security-Policy'] = "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; frame-ancestors *;"
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = '*'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['X-XSS-Protection'] = '0'  # Disable XSS protection to allow form resubmission
        
        # Forward all Set-Cookie headers from the response
        # The proxy_session automatically stores cookies from resp.cookies
        # We need to forward them to the client so they persist in the browser
        for cookie in resp.cookies:
            # Calculate max_age from expires if available
            max_age = None
            if cookie.expires:
                try:
                    if isinstance(cookie.expires, (int, float)):
                        max_age = int(cookie.expires)
                    else:
                        # Convert datetime to seconds
                        from datetime import datetime
                        if hasattr(cookie.expires, 'timestamp'):
                            max_age = int(cookie.expires.timestamp() - datetime.now().timestamp())
                except:
                    max_age = None
            
            response.set_cookie(
                cookie.name,
                cookie.value,
                max_age=max_age,
                path=cookie.path if cookie.path else '/',
                domain=None,  # Don't set domain to allow cross-domain cookies
                secure=False,
                httponly=cookie.has_nonstandard_attr('HttpOnly'),
                samesite='Lax'
            )
        
        # Also forward cookies from proxy_session that might have been set earlier
        # This ensures cookies persist across requests
        for cookie_name, cookie_value in proxy_session.cookies.items():
            # Only set if not already set from resp.cookies
            if not any(c.name == cookie_name for c in resp.cookies):
                response.set_cookie(
                    cookie_name,
                    cookie_value,
                    path='/',
                    domain=None,
                    secure=False,
                    httponly=False,
                    samesite='Lax'
                )
        
        return response
        
    except requests.exceptions.Timeout:
        url_display = target_url if target_url else "unknown"
        return f"<html><body><h2>Timeout Error</h2><p>Server at {url_display} took too long to respond</p></body></html>", 504
    except requests.exceptions.ConnectionError:
        url_display = target_url if target_url else "unknown"
        return f"<html><body><h2>Connection Error</h2><p>Unable to connect to {url_display}</p><p>Make sure the server is running and accessible.</p></body></html>", 502
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logging.error(f"Proxy error for URL '{target_url}': {error_details}")
        return f"<html><body><h2>Error</h2><p>Failed to load URL: {str(e)}</p><pre>{error_details}</pre></body></html>", 500

@app.route('/video_feed')
def video_feed():
    """Video streaming route"""
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/records')
def get_records():
    """API endpoint to fetch OCR records from database. Images are in image_folder; UI loads via /api/image/<filename>."""
    global DB_TYPE
    
    if DB_TYPE == "MongoDB":
        return get_mongodb_records()
    else:
        return get_sql_records()

def get_sql_records():
    """Fetch records from SQL databases (MSSQL, MySQL, PostgreSQL)."""
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        
        conn = get_sql_connection()
        if not conn:
            return jsonify({'error': 'Database connection failed', 'records': [], 'total_count': 0})
        
        cursor = conn.cursor()
        
        # Get total count of valid records (excluding NO_DETECTION)
        # Adjust SQL syntax based on database type
        if DB_TYPE == "MSSQL":
            count_query = f"""
                SELECT COUNT(*)
                FROM [{SQL_TABLE}]
                WHERE CorrectedText != 'NO_DETECTION'
                  AND NOT (Confidence = 0.0 AND ImageFileName IS NULL)
            """
            select_query = f"""
                SELECT TOP {limit}
                    ID, Timestamp, RawText, CleanedText, CorrectedText,
                    InputVehicle, RFID, Confidence, MatchScore, FrameIndex,
                    SaveDirectory, TriggerTopic, ProcessingTime, ImageFileName
                FROM [{SQL_TABLE}]
                ORDER BY Timestamp DESC
            """
        elif DB_TYPE == "MySQL":
            count_query = f"""
                SELECT COUNT(*)
                FROM `{SQL_TABLE}`
                WHERE CorrectedText != 'NO_DETECTION'
                  AND NOT (Confidence = 0.0 AND ImageFileName IS NULL)
            """
            select_query = f"""
                SELECT 
                    ID, Timestamp, RawText, CleanedText, CorrectedText,
                    InputVehicle, RFID, Confidence, MatchScore, FrameIndex,
                    SaveDirectory, TriggerTopic, ProcessingTime, ImageFileName
                FROM `{SQL_TABLE}`
                ORDER BY Timestamp DESC
                LIMIT {limit}
            """
        elif DB_TYPE == "PostgreSQL":
            count_query = f"""
                SELECT COUNT(*)
                FROM {SQL_TABLE}
                WHERE CorrectedText != 'NO_DETECTION'
                  AND NOT (Confidence = 0.0 AND ImageFileName IS NULL)
            """
            select_query = f"""
                SELECT 
                    ID, Timestamp, RawText, CleanedText, CorrectedText,
                    InputVehicle, RFID, Confidence, MatchScore, FrameIndex,
                    SaveDirectory, TriggerTopic, ProcessingTime, ImageFileName
                FROM {SQL_TABLE}
                ORDER BY Timestamp DESC
                LIMIT {limit}
            """
        
        cursor.execute(count_query)
        total_count = cursor.fetchone()[0]
        
        cursor.execute(select_query)
        rows = cursor.fetchall()
        
        records = []
        for row in rows:
                # Skip NO_DETECTION / blank records so UI shows only real detections and preserves previous
                if (row[4] == 'NO_DETECTION' or (row[7] is not None and float(row[7]) == 0.0 and row[13] is None)):
                    continue
                # Ensure Timestamp is always ISO string
                ts = row[1]
                if ts is None:
                    ts_out = None
                elif hasattr(ts, 'isoformat'):
                    ts_out = ts.isoformat()
                else:
                    ts_out = str(ts)
                img_filename = os.path.basename(row[13].strip()) if (row[13] and isinstance(row[13], str)) else None
                records.append({
                    'ID': row[0],
                    'Timestamp': ts_out,
                    'RawText': row[2],
                    'CleanedText': row[3],
                    'CorrectedText': row[4],
                    'InputVehicle': row[5],
                    'RFID': row[6],
                    'Confidence': row[7],
                    'MatchScore': row[8],
                    'FrameIndex': row[9],
                    'SaveDirectory': row[10],
                    'TriggerTopic': row[11],
                    'ProcessingTime': row[12],
                    'ImageFileName': img_filename
                })
        
        cursor.close()
        conn.close()
        
        return jsonify({'records': records, 'count': len(records), 'total_count': total_count})
        
    except Exception as e:
        logger.error(f"Error fetching {DB_TYPE} records: {e}")
        return jsonify({'error': str(e), 'records': [], 'total_count': 0})

def get_mongodb_records():
    """Fetch records from MongoDB collection."""
    try:
        limit = min(int(request.args.get('limit', 50)), 100)
        
        client = get_mongodb_connection()
        if not client:
            return jsonify({'error': 'MongoDB connection failed', 'records': [], 'total_count': 0})
        
        db = client[SQL_DATABASE]
        collection = db[SQL_TABLE]
        
        # Count query (exclude NO_DETECTION)
        total_count = collection.count_documents({
            "CorrectedText": {"$ne": "NO_DETECTION", "$exists": True}
        })
        
        # Fetch latest records
        cursor = collection.find(
            {"CorrectedText": {"$ne": "NO_DETECTION", "$exists": True}},
            {"_id": 0}  # Exclude MongoDB _id field
        ).sort("Timestamp", pymongo.DESCENDING).limit(limit)
        
        records = []
        for doc in cursor:
            # Ensure Timestamp is ISO string
            ts = doc.get('Timestamp')
            if ts is None:
                ts_out = None
            elif hasattr(ts, 'isoformat'):
                ts_out = ts.isoformat()
            else:
                ts_out = str(ts)
            
            records.append({
                'ID': doc.get('ID'),
                'Timestamp': ts_out,
                'RawText': doc.get('RawText', ''),
                'CleanedText': doc.get('CleanedText', ''),
                'CorrectedText': doc.get('CorrectedText', ''),
                'InputVehicle': doc.get('InputVehicle', ''),
                'RFID': doc.get('RFID', ''),
                'Confidence': doc.get('Confidence', 0.0),
                'MatchScore': doc.get('MatchScore', 0.0),
                'FrameIndex': doc.get('FrameIndex', 0),
                'SaveDirectory': doc.get('SaveDirectory', ''),
                'TriggerTopic': doc.get('TriggerTopic', ''),
                'ProcessingTime': doc.get('ProcessingTime', 0.0),
                'ImageFileName': os.path.basename((doc.get('ImageFileName') or '').strip()) or None
            })
        
        return jsonify({'records': records, 'count': len(records), 'total_count': total_count})
        
    except Exception as e:
        logger.error(f"Error fetching MongoDB records: {e}")
        return jsonify({'error': str(e), 'records': [], 'total_count': 0})


@app.route('/api/image/<path:filename>')
def serve_plate_image(filename):
    """Serve plate image from image_folder. Accepts sequential (imageN.jpg) and legacy timestamp-based (plate_*.jpg) filenames."""
    no_cache_headers = {
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0'
    }
    # Strip any path separators and whitespace — handles legacy full-path values in DB
    filename = os.path.basename((filename or '').strip())
    # Accept sequential format (imageN.jpg) and legacy timestamp format (plate_YYYYMMDD_HHMMSS_ffffff.jpg)
    if not filename or not re.match(
        r'^(image\d+|plate_\d{8}_\d{6}_\d{6})\.jpg$', filename, re.IGNORECASE
    ):
        resp = make_response('', 404)
        for k, v in no_cache_headers.items():
            resp.headers[k] = v
        return resp
    path = None
    for folder in _get_image_folder_candidates():
        candidate = os.path.join(folder, filename)
        if os.path.isfile(candidate):
            path = candidate
            break
    if not path:
        resp = make_response('', 404)
        for k, v in no_cache_headers.items():
            resp.headers[k] = v
        return resp
    try:
        # No caching — image filenames are timestamp-based and unique but we keep no-cache for safety
        resp = make_response(send_file(path, mimetype='image/jpeg'))
        for k, v in no_cache_headers.items():
            resp.headers[k] = v
        return resp
    except Exception as e:
        logger.warning(f"Failed to send image {filename}: {e}")
        resp = make_response('', 404)
        for k, v in no_cache_headers.items():
            resp.headers[k] = v
        return resp


@app.route('/api/open_pdf_report', methods=['POST'])
def open_pdf_report():
    """Open the saved PDF with the OS default PDF viewer."""
    try:
        data = request.get_json()
        filename = (data or {}).get('filename', 'ANPR_Report.pdf')
        import re as _re
        safe_name = _re.sub(r'[^A-Za-z0-9_\-.]', '_', filename)
        pdf_path = os.path.join(BASE_PATH, 'reports', safe_name)
        if not os.path.isfile(pdf_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        import subprocess as _sp
        _sp.Popen(['start', '', pdf_path], shell=True)
        logger.info(f'[PDF REPORT] Opened: {pdf_path}')
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f'[PDF REPORT] Open failed: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/save_pdf_report', methods=['POST'])
def save_pdf_report():
    """Receive a base64-encoded PDF from the frontend and save it to BASE_PATH/reports/"""
    try:
        data = request.get_json()
        if not data or not data.get('pdf_base64') or not data.get('filename'):
            return jsonify({'success': False, 'error': 'Missing pdf_base64 or filename'}), 400
        # Sanitise filename — only allow alphanumeric, underscore, hyphen, dot
        import re as _re
        safe_name = _re.sub(r'[^A-Za-z0-9_\-.]', '_', data['filename'])
        if not safe_name.endswith('.pdf'):
            safe_name += '.pdf'
        reports_dir = os.path.join(BASE_PATH, 'reports')
        os.makedirs(reports_dir, exist_ok=True)
        pdf_bytes = base64.b64decode(data['pdf_base64'])
        save_path = os.path.join(reports_dir, safe_name)
        with open(save_path, 'wb') as f:
            f.write(pdf_bytes)
        logger.info(f'[PDF REPORT] Saved to: {save_path}')
        return jsonify({'success': True, 'path': save_path})
    except Exception as e:
        logger.error(f'[PDF REPORT] Save failed: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def refresh_stream():
    """Server-Sent Events: push 'refresh' when a new record is inserted so UI updates only when API/Test adds data."""
    def generate():
        q = queue_module.Queue()
        with _refresh_lock:
            _refresh_subscribers.append(q)
        try:
            while True:
                try:
                    q.get(timeout=45)
                    yield "data: refresh\n\n"
                except queue_module.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with _refresh_lock:
                try:
                    _refresh_subscribers.remove(q)
                except ValueError:
                    pass
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'}
    )


def restart_application():
    """Restart the application after a short delay. Uses a batch helper on Windows to avoid PyInstaller 'Failed to start embedded python interpreter' when spawning the exe from within the app."""
    def delayed_restart():
        time.sleep(1)  # Wait 1 second so the user sees the popup and response is sent
        logger.info("Restarting application...")
        try:
            env = os.environ.copy()
            if getattr(sys, 'frozen', False):
                exe_path = sys.executable
                app_dir = os.path.dirname(exe_path)
                _spawn_restart_exe(app_dir, exe_path, env, is_bundled=True)
            elif env.get('ANPR_EXE_DIR'):
                app_dir = env['ANPR_EXE_DIR']
                launcher_exe = os.path.join(app_dir, 'ANPR_WebServer.exe')
                if os.path.isfile(launcher_exe):
                    _spawn_restart_exe(app_dir, launcher_exe, env, is_bundled=True)
                else:
                    python = sys.executable
                    script = sys.argv[0]
                    if script and script != '-c' and os.path.isfile(script):
                        subprocess.Popen([python, script], cwd=BASE_PATH, env=env)
                    else:
                        logger.error("Cannot restart: ANPR_WebServer.exe not found and no script to run")
                        os._exit(1)
            else:
                python = sys.executable
                script = sys.argv[0]
                subprocess.Popen([python, script], cwd=BASE_PATH, env=env)
            os._exit(0)
        except Exception as e:
            logger.error(f"Failed to restart application: {e}")
            os._exit(1)
    
    restart_thread = threading.Thread(target=delayed_restart, daemon=True)
    restart_thread.start()


def _spawn_restart_exe(app_dir, exe_path, env, is_bundled=False):
    """Start the launcher exe after a short delay so the current exe can exit and remove its
    temp dir (_MEI*) first. Run the batch via cmd.exe so it reliably runs (startfile can fail in embedded context)."""
    if os.name == 'nt' and is_bundled:
        bat_name = '_anpr_restart_helper.bat'
        bat_path = os.path.join(app_dir, bat_name)
        exe_name = os.path.basename(exe_path)
        bat_content = (
            '@echo off\r\n'
            'timeout /t 2 /nobreak >nul\r\n'
            'start "" /D "%~dp0" "%~dp0{}"\r\n'
            'del "%~f0"\r\n'
        ).format(exe_name)
        try:
            with open(bat_path, 'w') as f:
                f.write(bat_content)
            # Run batch via cmd so it always starts (startfile may not work from embedded PyQt/venv)
            flags = 0
            if hasattr(subprocess, 'CREATE_NEW_CONSOLE'):
                flags |= subprocess.CREATE_NEW_CONSOLE  # batch runs in own console, survives our exit
            elif hasattr(subprocess, 'DETACHED_PROCESS'):
                flags |= subprocess.DETACHED_PROCESS
            if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP'):
                flags |= subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                ['cmd', '/c', bat_path],
                cwd=app_dir,
                creationflags=flags,
                close_fds=True,
                env=env
            )
            return
        except Exception as e:
            logger.warning(f"Restart batch failed ({e}), trying startfile then Popen")
            try:
                os.startfile(bat_path)
                return
            except Exception:
                pass
            try:
                os.startfile(exe_path)
                return
            except Exception:
                pass
    subprocess.Popen([exe_path], cwd=app_dir, env=env)

@app.route('/api/config', methods=['GET'])
def get_config():
    """API endpoint to retrieve current configuration"""
    try:
        # Always read wb_info.json fresh so the modal pre-fills correctly every time
        _live_name = LOCATION_NAME
        _live_id   = LOCATION_ID
        try:
            _wb_path = _get_wb_info_path()
            if os.path.exists(_wb_path):
                with open(_wb_path, 'r', encoding='utf-8-sig') as _f:
                    _wb = json.load(_f)
                _d = _wb.get('Data', {})
                _n = (_d.get('wb_name') or '').strip()
                _i = (_d.get('wb_id')   or '').strip()
                if _n:
                    _live_name = _n
                if _i:
                    _live_id = _i
        except Exception:
            pass

        config = {
            'rtsp_url': RTSP_URL,
            'rtsp_transport': RTSP_TRANSPORT,  # "udp" or "tcp"
            'mqtt_enabled': ENABLE_MQTT,
            'mqtt_broker': MQTT_BROKER,
            'mqtt_port': MQTT_PORT,
            'mqtt_subscribe_topic': MQTT_TRIGGER_TOPIC,
            'mqtt_publish_topic': MQTT_PUBLISH_TOPIC,
            'location_name': _live_name,
            'location_coords': LOCATION_COORDS,
            'location_id': _live_id,
            'dept_title': DEPT_TITLE,
            'dept_subtitle': DEPT_SUBTITLE,
            'dept_logo_filename': DEPT_LOGO_FILENAME,
            'dept_branding_enabled': DEPT_BRANDING_ENABLED,
            'footer_dept': FOOTER_DEPT,
            'db_type': DB_TYPE,
            'db_server': SQL_SERVER,
            'db_name': SQL_DATABASE,
            'db_username': SQL_USERNAME,
            'db_password': SQL_PASSWORD,  # sent to localhost only — pre-fills modal
            'box_padding_width_px': BOX_PADDING_WIDTH_PX,
            'box_padding_height_px': BOX_PADDING_HEIGHT_PX,
            'enable_blur_model': ENABLE_BLUR_MODEL,
            'frame_skip_interval': FRAME_SKIP_INTERVAL,
            'enable_regex_correction': ENABLE_REGEX_CORRECTION,
            'conf_thresh_640':  CONF_THRESH_640,
            'conf_thresh_1280': CONF_THRESH_1280,
            'auto_login': AUTO_LOGIN
        }
        return jsonify(config)
    except Exception as e:
        logger.error(f"Error getting config: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/wb_info', methods=['GET'])
def get_wb_info():
    """Return wb_name and wb_id directly from wb_info.json."""
    try:
        wb_path = _get_wb_info_path()
        if os.path.exists(wb_path):
            with open(wb_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            d = data.get('Data', {})
            return jsonify({'wb_name': (d.get('wb_name') or '').strip(),
                            'wb_id':   (d.get('wb_id')   or '').strip()})
        return jsonify({'wb_name': '', 'wb_id': ''})
    except Exception as e:
        logger.error(f"Error reading wb_info.json: {e}")
        return jsonify({'wb_name': '', 'wb_id': ''}), 500

@app.route('/api/upload_dept_logo', methods=['POST'])
def upload_dept_logo():
    """Upload department logo used in the header and store it under static/branding/."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file field provided'}), 400

        f = request.files['file']
        if not f or not getattr(f, 'filename', ''):
            return jsonify({'success': False, 'error': 'No file selected'}), 400

        filename = f.filename.lower()
        allowed_exts = {'.png', '.jpg', '.jpeg', '.svg', '.ico', '.webp'}
        ext = os.path.splitext(filename)[1]
        if ext not in allowed_exts:
            return jsonify({'success': False, 'error': f'Invalid file type. Allowed: {", ".join(sorted(allowed_exts))}'}), 400

        branding_dir = os.path.join(STATIC_DIR, 'branding')
        os.makedirs(branding_dir, exist_ok=True)

        # Keep a stable name so the UI doesn't accumulate many files
        out_name = f"department_logo{ext}"
        out_path = os.path.join(branding_dir, out_name)
        f.save(out_path)

        # Return path relative to /static
        return jsonify({'success': True, 'dept_logo_filename': f'branding/{out_name}'})
    except Exception as e:
        logger.error(f"Error uploading department logo: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/upload_model', methods=['POST'])
def upload_model_file():
    """Upload a .pt model file to replace or add to the weights inside .res2.enc.

    If .res2.enc exists: decrypts, replaces/adds the .pt, re-encrypts.
    If .res2.enc does NOT exist: creates a fresh weights/ folder with the .pt
    and encrypts it as a new .res2.enc.
    Also copies the .pt to live WEIGHTS_DIR so it is usable immediately.
    """
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400

        f = request.files['file']
        if not f or not getattr(f, 'filename', ''):
            return jsonify({'success': False, 'error': 'No file selected'}), 400

        fname = os.path.basename(f.filename.strip())
        if not fname.lower().endswith('.pt'):
            return jsonify({'success': False, 'error': 'Only .pt files are allowed'}), 400

        if not ENCRYPTION_AVAILABLE:
            return jsonify({'success': False, 'error': 'Encryption library not available'}), 500

        # Locate application directory
        if os.environ.get('ANPR_RESOURCE_DIR'):
            app_dir = os.environ['ANPR_RESOURCE_DIR']
        elif getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.abspath(__file__))

        enc_path = os.path.join(app_dir, '.res2.enc')
        res2_exists = os.path.exists(enc_path)

        work_dir = tempfile.mkdtemp(prefix='anpr_model_upload_')
        try:
            weights_work = os.path.join(work_dir, 'weights')
            os.makedirs(weights_work, exist_ok=True)

            if res2_exists:
                # Decrypt existing .res2.enc and extract current weights
                zip_path = os.path.join(work_dir, 'weights.zip')
                decrypt_file(enc_path, zip_path)
                extract_dir = os.path.join(work_dir, 'extracted')
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(extract_dir)
                # Copy existing .pt files into weights_work
                extracted_weights = os.path.join(extract_dir, 'weights')
                src_dir = extracted_weights if os.path.isdir(extracted_weights) else extract_dir
                for wf in os.listdir(src_dir):
                    src_file = os.path.join(src_dir, wf)
                    if os.path.isfile(src_file):
                        shutil.copy2(src_file, os.path.join(weights_work, wf))
                logger.info(f"[UPLOAD MODEL] Existing .res2.enc decrypted")
            else:
                # .res2.enc does not exist — start with an empty weights folder
                logger.info(f"[UPLOAD MODEL] .res2.enc not found — will create new one")

            # Save uploaded .pt (replace if same name exists, add otherwise)
            dest_pt = os.path.join(weights_work, fname)
            action = 'Replaced' if os.path.exists(dest_pt) else 'Added'
            f.save(dest_pt)
            logger.info(f"[UPLOAD MODEL] {action} '{fname}' in weights folder")

            # Zip the weights folder
            new_zip_path = os.path.join(work_dir, 'weights_new.zip')
            with zipfile.ZipFile(new_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files_in_dir in os.walk(weights_work):
                    for wfile in files_in_dir:
                        fpath = os.path.join(root, wfile)
                        arcname = os.path.join('weights', os.path.relpath(fpath, weights_work))
                        zf.write(fpath, arcname)

            # Encrypt to .res2.enc (create or overwrite)
            key = get_decryption_key()
            fernet = Fernet(key)
            with open(new_zip_path, 'rb') as zf:
                encrypted = fernet.encrypt(zf.read())
            new_enc_path = os.path.join(work_dir, '.res2.enc')
            with open(new_enc_path, 'wb') as ef:
                ef.write(encrypted)
            shutil.move(new_enc_path, enc_path)
            logger.info(f"[UPLOAD MODEL] .res2.enc {'updated' if res2_exists else 'created'} successfully")

            # Copy to live WEIGHTS_DIR so model is usable immediately without restart
            reload_note = ''
            if WEIGHTS_DIR:
                os.makedirs(WEIGHTS_DIR, exist_ok=True)  # create dir if .res2.enc was absent at startup
                live_dest = os.path.join(WEIGHTS_DIR, fname)
                shutil.copy2(dest_pt, live_dest)
                logger.info(f"[UPLOAD MODEL] Live weights updated: {live_dest}")
                # Hot-reload both models from WEIGHTS_DIR so changes take effect immediately
                reloaded, reload_errors = reload_models_from_weights_dir()
                if reloaded:
                    reload_note = f" Models active: {', '.join(reloaded)}."  
                else:
                    reload_note = ' Restart the application to activate the model.'
            else:
                reload_note = ' Restart the application to activate the model.'

            return jsonify({
                'success': True,
                'filename': fname,
                'action': action,
                'message': f"'{fname}' {action.lower()} successfully.{reload_note}",
                'restart_required': not bool(reload_note and 'active' in reload_note)
            })

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    except Exception as e:
        logger.error(f"[UPLOAD MODEL] Failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/upload_warmup_image', methods=['POST'])
def upload_warmup_image():
    """Save an uploaded plate image as the persistent OCR warmup reference."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        f = request.files['file']
        if not f or not f.filename:
            return jsonify({'success': False, 'error': 'Empty file'}), 400

        allowed_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed_exts:
            return jsonify({'success': False, 'error': f'Unsupported format. Use: {", ".join(sorted(allowed_exts))}'}), 400

        # Decode through OpenCV and re-encode to ensure a valid JPEG
        file_bytes = np.frombuffer(f.read(), dtype=np.uint8)
        img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        if img is None:
            return jsonify({'success': False, 'error': 'Cannot decode image — ensure it is a valid image file'}), 400

        save_path = os.path.join(BASE_PATH, 'warmup_plate.jpg')
        cv2.imwrite(save_path, img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        logger.info(f"[WARMUP IMAGE] Saved: {save_path} ({img.shape[1]}x{img.shape[0]})")
        return jsonify({'success': True, 'message': f'Warmup image saved ({img.shape[1]}\u00d7{img.shape[0]}). Will be used on next startup.'})

    except Exception as e:
        logger.error(f"[WARMUP IMAGE] Upload failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/warmup_models', methods=['POST'])
def warmup_models_api():
    """Trigger an immediate model warmup in a background thread (uses images from warmup/ folder)."""
    try:
        def _run():
            _warmup_complete.clear()
            warmup_inference()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        warmup_dir = os.path.join(BASE_PATH, 'warmup')
        _img_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
        _has_images = (
            os.path.isdir(warmup_dir)
            and any(os.path.splitext(f)[1].lower() in _img_exts for f in os.listdir(warmup_dir))
        )

        if _has_images:
            msg = 'Warmup started using static images from the warmup/ folder. Models will be ready shortly.'
        else:
            msg = ('Warmup started using a dummy frame (no images found in warmup/ folder). '
                   'Place one or more plate images in the warmup/ folder for better warmup coverage.')

        return jsonify({'success': True, 'message': msg})

    except Exception as e:
        logger.error(f"[WARMUP MODELS] Failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/warmup_status', methods=['GET'])
def warmup_status_api():
    """Return whether model warmup has completed and whether DB is connected."""
    db_state = _system_status.get('database', 'unknown')
    return jsonify({
        'ready': _warmup_complete.is_set(),
        'db_connected': db_state == 'ready'
    })

@app.route('/api/system_status')
def get_system_status():
    """Return current initialization status of all subsystems.
    Polled by the frontend startup banner until overall == 'ready'."""
    return jsonify(_system_status)

@app.route('/api/save_db_credentials', methods=['POST'])
def api_save_db_credentials():
    """Persist DB credentials to .env file.
    Called by the Check & Save button after a successful connection test.
    Also creates the anpr_configuration table so settings can be stored immediately."""
    try:
        global SQL_SERVER, SQL_DATABASE, SQL_USERNAME, SQL_PASSWORD, DB_TYPE
        data = request.get_json() or {}
        creds = {
            'db_type':     data.get('db_type',     'MSSQL'),
            'db_server':   data.get('db_server',   '').strip(),
            'db_name':     data.get('db_name',     '').strip(),
            'db_username': data.get('db_username', '').strip(),
            'db_password': data.get('db_password', ''),
        }
        if not creds['db_server']:
            return jsonify({'success': False, 'error': 'db_server is required'})

        # Persist to encrypted file
        save_connection_enc(creds)

        # Update in-memory globals immediately
        SQL_SERVER   = creds['db_server']
        SQL_DATABASE = creds['db_name']
        SQL_USERNAME = creds['db_username']
        SQL_PASSWORD = creds['db_password']
        DB_TYPE      = creds['db_type']

        # Ensure settings table exists for subsequent saves
        _create_settings_table_sql()
        _reorder_settings_in_db()

        # Mark DB as ready so the banner clears immediately on the next poll
        _system_status['database'] = 'ready'

        return jsonify({'success': True, 'message': 'DB credentials saved to .env'})
    except Exception as e:
        logger.error(f"save_db_credentials failed: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test_db_connection', methods=['POST'])
def test_db_connection():
    """Test database connectivity using credentials sent from the UI (without saving them)."""
    try:
        data = request.get_json() or {}
        db_type    = data.get('db_type', 'MSSQL')
        db_server  = data.get('db_server', '').strip()
        db_name    = data.get('db_name', '').strip()
        db_user    = data.get('db_username', '').strip()
        db_pass    = data.get('db_password', '').strip()

        if not db_server:
            return jsonify({'success': False, 'error': 'Database Server is required'})

        if db_type == 'MSSQL':
            import pyodbc
            drivers = [
                'ODBC Driver 17 for SQL Server',
                'ODBC Driver 13 for SQL Server',
                'ODBC Driver 11 for SQL Server',
                'SQL Server Native Client 11.0',
                'SQL Server'
            ]
            available_drivers = [d.strip() for d in pyodbc.drivers()]
            selected_driver = next((d for d in drivers if d in available_drivers), 'SQL Server')
            conn_str = (
                f"DRIVER={{{selected_driver}}};"
                f"SERVER={db_server};"
                f"DATABASE={db_name};"
                f"UID={db_user};"
                f"PWD={db_pass};"
                "Trusted_Connection=no;"
                "Connection Timeout=10;"
            )
            conn = pyodbc.connect(conn_str)
            conn.close()

        elif db_type == 'MySQL':
            if not MYSQL_AVAILABLE:
                return jsonify({'success': False, 'error': 'pymysql not installed on server'})
            parts = db_server.split(':')
            host = parts[0]; port = int(parts[1]) if len(parts) > 1 else 3306
            conn = pymysql.connect(host=host, port=port, user=db_user, password=db_pass,
                                   database=db_name, connect_timeout=10, charset='utf8mb4')
            conn.close()

        elif db_type == 'PostgreSQL':
            if not POSTGRESQL_AVAILABLE:
                return jsonify({'success': False, 'error': 'psycopg2 not installed on server'})
            parts = db_server.split(':')
            host = parts[0]; port = int(parts[1]) if len(parts) > 1 else 5432
            conn = psycopg2.connect(host=host, port=port, user=db_user, password=db_pass,
                                    database=db_name, connect_timeout=10)
            conn.close()

        elif db_type == 'MongoDB':
            if not MONGODB_AVAILABLE:
                return jsonify({'success': False, 'error': 'pymongo not installed on server'})
            if db_user and db_pass:
                conn_str = f"mongodb://{db_user}:{db_pass}@{db_server}/{db_name}"
            else:
                conn_str = f"mongodb://{db_server}/{db_name}"
            client = pymongo.MongoClient(conn_str, serverSelectionTimeoutMS=10000)
            client.admin.command('ping')
            client.close()

        else:
            return jsonify({'success': False, 'error': f'Unknown database type: {db_type}'})

        return jsonify({'success': True, 'message': f'Connected to {db_type} successfully'})

    except Exception as e:
        logger.error(f"DB connection test failed: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/update_password', methods=['POST'])
def update_password():
    """Store a salted+hashed password in ANPR_PasswordStore table in the configured database.
    The table is created automatically if it does not exist.
    Password is stored as: sha256(salt + password) with a random 32-byte salt (hex-encoded).
    """
    import os as _os
    import hashlib as _hashlib

    try:
        data = request.get_json() or {}
        new_password = data.get('new_password', '').strip()

        if not new_password:
            return jsonify({'success': False, 'error': 'Password is required'})
        if len(new_password) < 6:
            return jsonify({'success': False, 'error': 'Password must be at least 6 characters'})

        # Generate a cryptographically random 32-byte salt
        salt = _os.urandom(32).hex()
        pwd_hash = _hashlib.sha256((salt + new_password).encode('utf-8')).hexdigest()

        conn = get_sql_connection()
        if conn is None:
            return jsonify({'success': False, 'error': 'Database not connected. Please configure database first.'})

        try:
            cursor = conn.cursor()

            if DB_TYPE == 'MSSQL':
                cursor.execute("""
                    IF NOT EXISTS (
                        SELECT * FROM INFORMATION_SCHEMA.TABLES
                        WHERE TABLE_NAME = 'ANPR_PasswordStore'
                    )
                    CREATE TABLE ANPR_PasswordStore (
                        Id INT IDENTITY(1,1) PRIMARY KEY,
                        PasswordHash NVARCHAR(64) NOT NULL,
                        Salt NVARCHAR(64) NOT NULL,
                        UpdatedAt DATETIME DEFAULT GETDATE()
                    )
                """)
                cursor.execute("DELETE FROM ANPR_PasswordStore")
                cursor.execute(
                    "INSERT INTO ANPR_PasswordStore (PasswordHash, Salt) VALUES (?, ?)",
                    (pwd_hash, salt)
                )

            elif DB_TYPE == 'MySQL':
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS ANPR_PasswordStore (
                        Id INT AUTO_INCREMENT PRIMARY KEY,
                        PasswordHash VARCHAR(64) NOT NULL,
                        Salt VARCHAR(64) NOT NULL,
                        UpdatedAt DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                """)
                cursor.execute("DELETE FROM ANPR_PasswordStore")
                cursor.execute(
                    "INSERT INTO ANPR_PasswordStore (PasswordHash, Salt) VALUES (%s, %s)",
                    (pwd_hash, salt)
                )

            elif DB_TYPE == 'PostgreSQL':
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS ANPR_PasswordStore (
                        Id SERIAL PRIMARY KEY,
                        PasswordHash VARCHAR(64) NOT NULL,
                        Salt VARCHAR(64) NOT NULL,
                        UpdatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cursor.execute("DELETE FROM ANPR_PasswordStore")
                cursor.execute(
                    "INSERT INTO ANPR_PasswordStore (PasswordHash, Salt) VALUES (%s, %s)",
                    (pwd_hash, salt)
                )

            elif DB_TYPE == 'MongoDB':
                db = conn[SQL_DATABASE]
                col = db['ANPR_PasswordStore']
                col.delete_many({})
                col.insert_one({'PasswordHash': pwd_hash, 'Salt': salt})
                conn.close()
                _clear_remember_me()
                logger.info("Password updated in MongoDB ANPR_PasswordStore")
                return jsonify({'success': True})

            conn.commit()
            cursor.close()
            conn.close()
            _clear_remember_me()
            logger.info("Password updated in ANPR_PasswordStore table")
            return jsonify({'success': True})

        except Exception as db_err:
            logger.error(f"Failed to update password in DB: {db_err}")
            try:
                conn.close()
            except Exception:
                pass
            return jsonify({'success': False, 'error': str(db_err)})

    except Exception as e:
        logger.error(f"update_password error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/config', methods=['POST'])
def save_config():
    """Save non-credential settings to ANPR_Settings DB table (primary path).
    Falls back to config.json if DB is not yet configured.
    DB credentials (db_server/db_name/db_username/db_password) are intentionally
    ignored here — they are handled exclusively by /api/save_db_credentials."""
    try:
        config_data = request.get_json()
        if not config_data:
            return jsonify({'success': False, 'error': 'No configuration data provided'}), 400

        # Strip DB credential keys — they must not be stored here
        for _k in ('db_server', 'db_name', 'db_username', 'db_password'):
            config_data.pop(_k, None)

        _prev_regex   = ENABLE_REGEX_CORRECTION
        _prev_blur    = ENABLE_BLUR_MODEL
        _prev_skip    = FRAME_SKIP_INTERVAL
        _prev_db_type = DB_TYPE

        # ── Sync wb_info.json (location_name / location_id always local) ──
        try:
            wb_path = _get_wb_info_path()
            wb_data = {'Data': {'wb_id': '', 'wb_name': ''}}
            if os.path.exists(wb_path):
                try:
                    with open(wb_path, 'r', encoding='utf-8-sig') as _wbf:
                        wb_data = json.load(_wbf)
                except Exception:
                    pass
            if 'Data' not in wb_data:
                wb_data['Data'] = {}
            _new_name = (config_data.get('location_name') or '').strip()
            _new_id   = (config_data.get('location_id')   or '').strip()
            if _new_name:
                wb_data['Data']['wb_name'] = _new_name
            if _new_id:
                wb_data['Data']['wb_id'] = _new_id
            with open(wb_path, 'w', encoding='utf-8') as _wbf:
                json.dump(wb_data, _wbf, indent=2)
            logger.info(f"wb_info.json updated: wb_name='{wb_data['Data']['wb_name']}' wb_id='{wb_data['Data']['wb_id']}'")
        except Exception as _e:
            logger.warning(f"Could not update wb_info.json: {_e}")

        # ── Apply settings to globals immediately (no DB wait) ────────────
        _apply_settings_dict(config_data)
        _wb_override()

        # Log toggled settings
        if ENABLE_REGEX_CORRECTION != _prev_regex:
            logger.info(f"[CONFIG] Regex Correction → {'ON ✓' if ENABLE_REGEX_CORRECTION else 'OFF ✗'}")
        if ENABLE_BLUR_MODEL != _prev_blur:
            logger.info(f"[CONFIG] Blur Model → {'ON ✓' if ENABLE_BLUR_MODEL else 'OFF ✗'}")
        if FRAME_SKIP_INTERVAL != _prev_skip:
            logger.info(f"[CONFIG] Frame Skip Interval: {_prev_skip} → {FRAME_SKIP_INTERVAL}")
        if DB_TYPE != _prev_db_type:
            logger.info(f"[CONFIG] DB Type: {_prev_db_type} → {DB_TYPE}")
            global _db_connection_logged
            _db_connection_logged.clear()
            _db_fail_logged.clear()

        # ── Background: DB save + RTSP/MQTT/DB reinit ────────────────────
        # All DB work is off the request thread so the HTTP response returns
        # in <5 ms regardless of network latency to the database server.
        _cfg_snapshot = dict(config_data)  # snapshot before thread starts
        def _bg_reinit():
            # 0. Persist RTSP URL to .env immediately — survives restarts even without DB
            _new_rtsp = _cfg_snapshot.get('rtsp_url', '')
            if _new_rtsp is not None:  # save even if empty (user cleared the field)
                try:
                    save_connection_enc({'rtsp_url': _new_rtsp})
                except Exception as _ee:
                    logger.warning(f"Could not save rtsp_url to .env: {_ee}")
            # 1. Persist settings to DB (table check is skipped if already ready)
            if SQL_SERVER and SQL_SERVER.strip():
                try:
                    _create_settings_table_sql()
                    saved = save_settings_to_db(_cfg_snapshot)
                    _dest = 'ANPR_Settings (DB)' if saved else 'memory only (DB save failed)'
                except Exception as _dbe:
                    logger.warning(f"DB settings save failed: {_dbe}")
                    _dest = 'memory only (DB error)'
            else:
                _dest = 'memory only (DB not configured)'
            logger.info(f"Configuration saved to {_dest}.")
            # 2. Reinitialise RTSP stream
            try:
                _reinitialize_rtsp_camera()
            except Exception as e:
                logger.error(f"Background RTSP reinit failed: {e}")
            # 3. Reinitialise MQTT
            try:
                _reinitialize_mqtt()
            except Exception as e:
                logger.error(f"Background MQTT reinit failed: {e}")
            # 4. Reinitialise DB tables
            # Note: do NOT set 'loading' transiently — the banner hides permanently
            # the moment it sees zero failures, so a brief 'loading' would erase it.
            try:
                if ENABLE_SQL_LOGGING and SQL_SERVER and SQL_SERVER.strip():
                    if create_database_and_table():
                        logger.info(f"✓ {DB_TYPE} database reinitialised")
                        _system_status['database'] = 'ready'
                    else:
                        logger.warning(f"⚠ {DB_TYPE} database reinit failed")
                        _system_status['database'] = 'failed'
                elif not (SQL_SERVER and SQL_SERVER.strip()):
                    _system_status['database'] = 'skipped'
            except Exception as e:
                _system_status['database'] = 'failed'
                logger.error(f"Background DB reinit failed: {e}")
        threading.Thread(target=_bg_reinit, daemon=True).start()

        logger.info("Configuration applied to memory. DB persist + reinit running in background.")
        return jsonify({
            'success': True,
            'message': 'Configuration saved successfully.',
            'db_configured': bool(SQL_SERVER and SQL_SERVER.strip()),
        })


    except Exception as e:
        logger.error(f"Error saving config: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/upload_image', methods=['POST'])
def upload_image_for_ocr():
    """Accept an uploaded image and run the same YOLO→OCR pipeline used for RTSP frames.

    Multipart field: 'file'  (jpg/jpeg/png/bmp/webp)
    Returns JSON with detection result identical to /api/detect.
    """
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        f = request.files['file']
        if not f or not f.filename:
            return jsonify({'success': False, 'error': 'Empty file'}), 400

        allowed_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed_exts:
            return jsonify({'success': False, 'error': f'Unsupported format. Use: {", ".join(sorted(allowed_exts))}'}), 400

        file_bytes = np.frombuffer(f.read(), dtype=np.uint8)
        original_frame = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        if original_frame is None:
            return jsonify({'success': False, 'error': 'Cannot decode image'}), 400

        original_h, original_w = original_frame.shape[:2]
        logger.info(f"[UPLOAD OCR] Image: {f.filename} ({original_w}x{original_h})")

        if plate_model is None:
            return jsonify({'success': False, 'error': 'Plate detection model not loaded'}), 503
        if not _warmup_complete.is_set():
            _warmup_complete.wait(timeout=30)

        start_time = time.time()
        no_save = request.form.get('no_save', '0') in ('1', 'true', 'yes')
        best_frame_data = None
        best_score = -1.0
        best_conf  = 0.0

        processed, meta = process_single_frame(original_frame, TARGET_WIDTH, TARGET_HEIGHT, USE_LETTERBOX, save_path=None)

        # Two-pass inference identical to read_n_frames
        results_640 = plate_model.predict(processed, conf=0.20, verbose=False, imgsz=640)
        _passes = [(results_640, False, "640")]
        _1280_results_cache = None
        _pass_idx = 0
        _dual_early_exit = False

        while _pass_idx < len(_passes) and not _dual_early_exit:
            _results, _use_orig, _plabel = _passes[_pass_idx]
            _pass_idx += 1

            if not (_results and len(_results) > 0):
                if not _use_orig and len(_passes) == 1:
                    if _1280_results_cache is None:
                        _1280_results_cache = plate_model.predict(original_frame, conf=0.20, verbose=False, imgsz=1280)
                    if (_1280_results_cache and len(_1280_results_cache) > 0
                            and _1280_results_cache[0].boxes is not None
                            and len(_1280_results_cache[0].boxes) > 0):
                        _passes.append((_1280_results_cache, True, "1280"))
                continue

            r = _results[0]
            if not (hasattr(r, "boxes") and r.boxes is not None and len(r.boxes) > 0):
                if not _use_orig and len(_passes) == 1:
                    if _1280_results_cache is None:
                        _1280_results_cache = plate_model.predict(original_frame, conf=0.20, verbose=False, imgsz=1280)
                    if (_1280_results_cache and len(_1280_results_cache) > 0
                            and _1280_results_cache[0].boxes is not None
                            and len(_1280_results_cache[0].boxes) > 0):
                        _passes.append((_1280_results_cache, True, "1280"))
                continue

            boxes = r.boxes.xyxy.cpu().numpy()
            if _use_orig:
                orig_boxes = [(int(b[0]), int(b[1]), int(b[2]), int(b[3])) for b in boxes]
            else:
                pad_left, pad_top = meta.get('pad', (0, 0))
                scale = float(meta.get('scale') or 1.0) or 1.0
                orig_boxes = [
                    (int((int(b[0]) - pad_left) / scale), int((int(b[1]) - pad_top) / scale),
                     int((int(b[2]) - pad_left) / scale), int((int(b[3]) - pad_top) / scale))
                    for b in boxes
                ]

            for x1o, y1o, x2o, y2o in orig_boxes:
                ew = max(0, int(BOX_PADDING_WIDTH_PX))
                eh = max(0, int(BOX_PADDING_HEIGHT_PX))
                x1o = max(0, x1o - ew); y1o = max(0, y1o - eh)
                x2o = min(original_w, x2o + ew); y2o = min(original_h, y2o + eh)
                crop = original_frame[y1o:y2o, x1o:x2o]
                if crop.size == 0 or ocr is None:
                    continue

                pw, ph = x2o - x1o, y2o - y1o
                _ocr_crops = [crop, cv2.resize(crop, (max(1, pw*2), max(1, ph*2)), interpolation=cv2.INTER_CUBIC)] if _use_orig else [crop]

                _hq_exit = False
                for _ocr_crop in _ocr_crops:
                    if _ocr_crop.size == 0:
                        continue
                    try:
                        text_raw, conf = extract_text_from_image(_ocr_crop)
                        text_raw = text_raw or ''
                        conf = float(conf) if conf is not None else 0.0
                        if ENABLE_REGEX_CORRECTION:
                            cleaned = clean_plate_text(text_raw)
                            corrected = correct_plate_ocr(cleaned)
                        else:
                            cleaned = corrected = re.sub(r'[^A-Z0-9]', '', text_raw.strip().upper())
                        match_score = round(conf * 100, 2)
                        if match_score > best_score or (match_score == best_score and conf > best_conf):
                            best_score = match_score
                            best_conf  = conf
                            best_frame_data = {
                                'frame_idx': 1, 'raw': text_raw, 'cleaned': cleaned,
                                'corrected': corrected, 'conf': conf, 'match_score': match_score,
                                'blurred': crop.copy(), 'box': (x1o, y1o, x2o, y2o),
                                'original_frame': original_frame.copy(), 'pass': _plabel,
                            }
                        if (conf >= 0.85 and len(corrected) >= 8
                                and re.search(r'[A-Z]{2}\d{2}[A-Z0-9]{1,3}\d{4}', corrected)):
                            _hq_exit = True; _dual_early_exit = True; break
                    except Exception as e:
                        logger.debug(f"[UPLOAD OCR] OCR crop error: {e}")
                if _hq_exit:
                    break

            # Lazy 1280 gate
            if not _use_orig and len(_passes) == 1 and not _dual_early_exit:
                _fast_best = (best_frame_data or {}).get('corrected', '')
                _fast_valid = (len(_fast_best) >= 8
                               and bool(re.search(r'[A-Z]{2}\d{2}[A-Z0-9]{1,3}\d{4}', _fast_best))
                               and (best_frame_data or {}).get('conf', 0.0) >= 0.85)
                if not _fast_valid:
                    if _1280_results_cache is None:
                        _1280_results_cache = plate_model.predict(original_frame, conf=0.20, verbose=False, imgsz=1280)
                    if (_1280_results_cache and len(_1280_results_cache) > 0
                            and _1280_results_cache[0].boxes is not None
                            and len(_1280_results_cache[0].boxes) > 0):
                        _passes.append((_1280_results_cache, True, "1280"))

        elapsed = time.time() - start_time

        # Validate plate (same rules as /api/detect)
        if best_frame_data:
            _corrected_chk = (best_frame_data.get('corrected') or '').strip()
            _conf_chk = float(best_frame_data.get('conf') or 0.0)
            _pass_label = best_frame_data.get('pass', '640')
            _conf_thr = 0.75 if _pass_label == '1280' else 0.85
            _plate_valid = (
                _conf_chk >= _conf_thr
                and len(_corrected_chk) >= 8
                and bool(re.search(r'[A-Z]{2}\d{2}[A-Z0-9]{1,3}\d{4}', _corrected_chk))
            )
            if not _plate_valid:
                logger.warning(f"[UPLOAD OCR] Plate '{_corrected_chk}' rejected (conf={_conf_chk:.3f})")
                best_frame_data = None

        image_filename = None
        if best_frame_data:
            try:
                if best_frame_data.get('blurred') is not None and best_frame_data['blurred'].size > 0:
                    image_filename = save_plate_image_and_get_filename(best_frame_data['blurred'])
            except Exception as e:
                logger.warning(f"[UPLOAD OCR] Failed to save plate image: {e}")

            if ENABLE_SQL_LOGGING and not no_save:
                try:
                    insert_plate_recognition({                        'timestamp': datetime.now(),
                        'raw_text':       best_frame_data['raw'],
                        'cleaned_text':   best_frame_data['cleaned'],
                        'corrected_text': best_frame_data['corrected'],
                        'input_vehicle':  '',
                        'rfid':           '',
                        'confidence':     best_frame_data['conf'],
                        'match_score':    0.0,
                        'frame_idx':      1,
                        'save_dir':       None,
                        'trigger_topic':  'api/upload_image',
                        'processing_time': elapsed,
                        'image_filename': image_filename,
                        'pass':           best_frame_data.get('pass', '640'),
                    }, log_to_terminal=True)
                    logger.info("[UPLOAD OCR] Saved to database")
                except Exception as e:
                    logger.warning(f"[UPLOAD OCR] DB save failed: {e}")

            return jsonify({
                'success': True,
                'detected': True,
                'plate': best_frame_data['corrected'],
                'confidence': round(best_frame_data['conf'] * 100, 1),
                'raw_text': best_frame_data['raw'],
                'image_filename': image_filename,
                'processing_time_s': round(elapsed, 2),
            })
        else:
            return jsonify({
                'success': True,
                'detected': False,
                'plate': None,
                'processing_time_s': round(elapsed, 2),
            })

    except Exception as e:
        logger.error(f"[UPLOAD OCR] Error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/detect', methods=['POST'])
def detect_vehicle():
    """API endpoint to trigger vehicle detection and OCR
    
    Request body:
    {
        "vehicle_number": "OD02AB1234"  // optional - for match scoring
    }
    
    Response:
    {
        "success": true,
        "timestamp": "2025-11-18T10:30:45.123456",
        "input_vehicle": "OD02AB1234",
        "frames_processed": 3,
        "processing_time_s": 2.45,
        "best_match": {
            "frame_index": 2,
            "raw_text": "OD 02 AB 1234",
            "cleaned_text": "OD02AB1234",
            "corrected_text": "OD02AB1234",
            "confidence": 0.95,
            "match_score": 100.0,
            "image_filename": "image1.jpg"
        },
        "save_directory": "mqtt_frames/20251118_103045"
    }
    """
    try:
        # Parse request body
        data = request.get_json()
        if not data:
            data = {}
        
        vehicle_number = data.get('vehicle_number') or data.get('Vehicle_Number') or data.get('VehicleNumber')
        rfid = data.get('rfid') or data.get('RFID') or data.get('Rfid')
        
        if vehicle_number:
            vehicle_number = vehicle_number.strip().upper()
            # Treat empty string as None (no match comparison needed)
            if not vehicle_number:
                vehicle_number = None
        
        if rfid:
            rfid = rfid.strip()
            if not rfid:
                rfid = None
        
        # Dotted separator before each API call block for easy visual separation in log
        _system_log_stream.write("-" * 80 + "\n")
        _system_log_stream.flush()
        if vehicle_number:
            logger.info(f"[API DETECT] Request received for vehicle: {vehicle_number}, rfid: {rfid}")
        else:
            logger.info(f"[API DETECT] Request received without vehicle number - will process single frame, rfid: {rfid}")
        
        # Check if shared camera is available
        if web_shared_cam is None:
            logger.error("[API DETECT] Shared camera not initialized")
            return jsonify({
                'success': False,
                'error': 'Camera not initialized'
            }), 500
        
        # Trigger detection using the same pipeline as MQTT
        # Process only 1 frame if no vehicle number provided, otherwise process CAPTURE_COUNT frames
        frames_to_process = 1 if not vehicle_number else CAPTURE_COUNT
        start_time = time.time()
        result = web_shared_cam.read_n_frames(frames_to_process, SAVE_ROOT, input_vehicle=vehicle_number)
        elapsed = time.time() - start_time
        
        if not result:
            logger.error("[API DETECT] Detection failed - no result")
            return jsonify({
                'success': False,
                'error': 'Detection failed'
            }), 500
        
        # Build response similar to MQTT payload
        response = {
            'timestamp': datetime.now().isoformat(),
            'processing_time_s': round(elapsed, 2)
        }
        
        # Add best match details if available
        if result.get('best_result'):
            best = result['best_result']

            # Validate plate format before treating as a successful detection.
            # Apply the same rules as insert_plate_recognition so a short/garbage
            # OCR read (e.g. "AR2435", 6 chars) is never reported as success.
            _corrected_chk = (best.get('corrected') or '').strip()
            _conf_chk = float(best.get('conf') or 0.0)
            # 640 pass: keep the high bar (0.85) — if it wasn't good enough, 1280 already ran.
            # 1280 pass: allow 0.75 — higher-res inference already did its best.
            _pass_label = best.get('pass', '640')
            _conf_threshold = 0.75 if _pass_label == '1280' else 0.85
            _plate_valid = (
                _conf_chk >= _conf_threshold
                and len(_corrected_chk) >= 8
                and bool(re.search(r'[A-Z]{2}\d{2}[A-Z0-9]{1,3}\d{4}', _corrected_chk))
            )
            if not _plate_valid:
                logger.warning(
                    f"[API DETECT] Plate '{_corrected_chk}' rejected "
                    f"(len={len(_corrected_chk)}, conf={_conf_chk:.3f}, pass={_pass_label}, threshold={_conf_threshold}) — no number plate detected"
                )
                result['best_result'] = None  # fall through to no-detection branch

        if result.get('best_result'):
            best = result['best_result']

            # Save plate image to image_folder (image1.jpg, image2.jpg, ...)
            image_filename = None
            try:
                if best.get('blurred') is not None and best['blurred'].size > 0:
                    image_filename = save_plate_image_and_get_filename(best['blurred'])
            except Exception as e:
                logger.warning(f"[API DETECT] Failed to save plate image: {e}")
            
            # Set match_score to N/A if no input vehicle was provided
            match_score_value = round(best['match_score'], 2)
            # Show 100 instead of 100.0 for perfect match
            match_score_display = 'N/A' if not vehicle_number else (int(match_score_value) if match_score_value == 100.0 else match_score_value)
            
            response['best_match'] = {
                'vehicle_ocr_value': best['corrected'],
                'confidence': round(best['conf'] * 100, 2),  # Convert to percentage
                'match_score': match_score_display,
                'image_filename': image_filename,
                'image_path': os.path.join(get_image_folder(), image_filename) if image_filename else None,
                'rfid': rfid
            }
            
            # Insert into database if SQL logging is enabled
            if ENABLE_SQL_LOGGING:
                try:
                    # Store actual match_score for database (0.0 if no input vehicle)
                    db_match_score = best['match_score'] if vehicle_number else 0.0
                    
                    insert_plate_recognition({
                        'timestamp': datetime.now(),
                        'raw_text': best['raw'],
                        'cleaned_text': best['cleaned'],
                        'corrected_text': best['corrected'],
                        'input_vehicle': vehicle_number or '',
                        'rfid': rfid or '',
                        'confidence': best['conf'],
                        'match_score': db_match_score,
                        'frame_idx': best['frame_idx'],
                        'save_dir': result.get('dir'),
                        'trigger_topic': 'api/detect',
                        'processing_time': elapsed,
                        'image_filename': image_filename,
                        'pass': best.get('pass', '640'),
                    }, log_to_terminal=True)
                    logger.info("[API DETECT] Result saved to database")
                except Exception as e:
                    logger.warning(f"[API DETECT] Failed to save to database: {e}")
            
            logger.info(f"[API DETECT] Success: '{best['corrected']}' (score={best['match_score']:.2f})")
        else:
            logger.warning("[API DETECT] No plate detected in frames")
            response['best_match'] = None
        
        # Dotted separator after each API call block
        _system_log_stream.write("-" * 80 + "\n")
        _system_log_stream.flush()
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"[API DETECT] Error: {e}", exc_info=True)
        _system_log_stream.write("-" * 80 + "\n")
        _system_log_stream.flush()
        return jsonify({
            'success': False,
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    """API endpoint to shutdown the application"""
    try:
        logger.info("Shutdown request received from web interface")
        if _API_LOGGER_AVAILABLE:
            try:
                _log_shutdown(request.remote_addr or "unknown")
            except Exception:
                pass
        
        # Shutdown Flask server
        def shutdown_server():
            time.sleep(1)  # Give time for response to be sent
            os._exit(0)  # Force exit the entire application
        
        shutdown_thread = threading.Thread(target=shutdown_server, daemon=True)
        shutdown_thread.start()
        
        return jsonify({'success': True, 'message': 'Application shutting down...'})
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/open_browser', methods=['POST'])
def open_browser():
    """API endpoint to open system default browser"""
    try:
        import webbrowser
        import subprocess
        import platform
        
        # Try to open browser with Google as homepage
        try:
            webbrowser.open('https://www.google.com', new=2)
        except:
            # Fallback: open browser directly using system commands
            if platform.system() == 'Windows':
                subprocess.Popen(['start', 'chrome'], shell=True)
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error opening browser: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/check_resolution', methods=['POST'])
def check_resolution():
    """Check the resolution (width x height) of a video/RTSP stream URL."""
    try:
        data = request.get_json() or {}
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'success': False, 'error': 'No URL provided'}), 400

        cap = cv2.VideoCapture(url)
        if not cap.isOpened():
            return jsonify({'success': False, 'error': 'Could not open stream. Check the URL.'}), 400

        width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        if width == 0 or height == 0:
            return jsonify({'success': False, 'error': 'Stream opened but could not read resolution.'}), 400

        return jsonify({'success': True, 'width': width, 'height': height})
    except Exception as e:
        logger.error(f"check_resolution error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def run_flask_app():
    """Run Flask in a separate thread"""
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, use_reloader=False)

# ----- PyQt5 Desktop Window -----
class ANPRWebEnginePage(QWebEnginePage):
    """Custom page that overrides chooseFiles so file input doesn't hang the UI.
    Shows Qt file dialog on the main thread instead of the default WebEngine handler."""
    def createWindow(self, window_type):
        """Intercept all new-window / new-tab / popup requests and load in the same view.
        Returning self prevents Qt from creating any additional OS window."""
        return self

    def chooseFiles(self, mode, old_files, accepted_mime_types):
        parent = self.view() if self.view() else None
        filter_str = "Images (*.png *.jpg *.jpeg *.svg *.ico *.webp);;All files (*.*)"
        if mode == QWebEnginePage.FileSelectOpenMultiple:
            paths, _ = QFileDialog.getOpenFileNames(parent, "Choose files", "", filter_str)
            return paths if paths else []
        else:
            path, _ = QFileDialog.getOpenFileName(parent, "Choose file", "", filter_str)
            return [path] if path else []


# ══════════════════════════════════════════════════════════════════════════════
# OTA UPDATE SYSTEM
# ══════════════════════════════════════════════════════════════════════════════
# version.json on GitHub (raw URL):
#   { "version": "1.0.5", "build_date": "2026-04-29",
#     "release_notes": "...",
#     "files": [ { "name": "_internal_server.pyd",
#                  "dest": "_internal_server.pyd",
#                  "url":  "https://raw.githubusercontent.com/OWNER/REPO/main/updates/_internal_server.pyd",
#                  "sha256": "abc123...", "size": 1234567 }, ... ] }
# ──────────────────────────────────────────────────────────────────────────────

# UPDATE SERVER — change these two lines to point at your GitHub repo
_UPDATE_VERSION_URL = "https://raw.githubusercontent.com/shreyadya/ANPR/main/version.json"
_UPDATE_CHECK_DELAY_S = 2           # seconds after startup before first check
_UPDATE_CHECK_RETRY_INTERVAL_S = 8  # retry during the same run to ride out GitHub propagation delay
_UPDATE_CHECK_MAX_WINDOW_S = 180    # keep checking for a short window after launch
_UPDATE_CHECK_ENABLED = True        # set False to disable updates entirely


def _get_local_version():
    """Return the currently installed version string, or '0.0.0' if not found."""
    try:
        _vp = os.path.join(BASE_PATH, 'version.json')
        if os.path.exists(_vp):
            with open(_vp, 'r', encoding='utf-8') as _f:
                return json.load(_f).get('version', '0.0.0')
    except Exception:
        pass
    return '0.0.0'


def _version_newer(remote, local):
    """Return True if remote version is strictly newer than local (semver tuples)."""
    def _t(v):
        try:
            return tuple(int(x) for x in str(v).strip().split('.'))
        except Exception:
            return (0,)
    return _t(remote) > _t(local)


def _sha256_of_file(path):
    import hashlib as _hl
    h = _hl.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest().lower()


def _manifest_requires_update(remote_manifest):
    """Return (needs_update, reason) by comparing remote manifest to installed files."""
    remote_ver = str(remote_manifest.get('version', '0'))
    local_ver = _get_local_version()
    if _version_newer(remote_ver, local_ver):
        return True, f'version newer ({local_ver} -> {remote_ver})'

    for item in remote_manifest.get('files', []):
        name = item.get('name', '')
        dest_rel = item.get('dest', name)
        sha = str(item.get('sha256', '')).lower()
        expected_size = item.get('size')
        local_path = os.path.join(BASE_PATH, dest_rel)

        if not os.path.exists(local_path):
            return True, f'missing file: {dest_rel}'

        try:
            actual_size = os.path.getsize(local_path)
            if expected_size and actual_size != int(expected_size):
                return True, f'size mismatch for {dest_rel}: local={actual_size} remote={expected_size}'
            if sha:
                actual_sha = _sha256_of_file(local_path)
                if actual_sha != sha:
                    return True, f'hash mismatch for {dest_rel}: local={actual_sha} remote={sha}'
        except Exception as exc:
            return True, f'file check failed for {dest_rel}: {exc}'

    return False, 'installed files already match remote manifest'


class UpdateCheckerThread(QThread):
    """Background thread that polls the update server once."""
    update_available = pyqtSignal(dict)   # emits remote version.json dict

    def run(self):
        import time as _time
        from urllib.parse import urlencode as _urlencode
        _time.sleep(_UPDATE_CHECK_DELAY_S)
        if not _UPDATE_CHECK_ENABLED:
            return
        # Skip if placeholder URL not yet configured
        if 'OWNER/REPO' in _UPDATE_VERSION_URL:
            return
        _deadline = _time.time() + _UPDATE_CHECK_MAX_WINDOW_S
        _attempt = 0
        while _time.time() <= _deadline:
            _attempt += 1
            try:
                _sep = '&' if '?' in _UPDATE_VERSION_URL else '?'
                _url = f"{_UPDATE_VERSION_URL}{_sep}{_urlencode({'ts': int(_time.time())})}"
                resp = requests.get(
                    _url,
                    timeout=15,
                    headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
                )
                if resp.status_code != 200:
                    logger.info(f"OTA check skipped: attempt={_attempt} status={resp.status_code} url={_url}")
                else:
                    remote = resp.json()
                    local_ver = _get_local_version()
                    remote_ver = str(remote.get('version', '0'))
                    logger.info(f"OTA check: attempt={_attempt} local={local_ver} remote={remote_ver} url={_url}")
                    needs_update, reason = _manifest_requires_update(remote)
                    logger.info(f"OTA decision: attempt={_attempt} needs_update={needs_update} reason={reason}")
                    if needs_update:
                        self.update_available.emit(remote)
                        return
            except Exception:
                logger.exception(f"OTA check failed on attempt={_attempt}")

            _remaining = _deadline - _time.time()
            if _remaining <= 0:
                break
            _time.sleep(min(_UPDATE_CHECK_RETRY_INTERVAL_S, _remaining))


class UpdateDownloadThread(QThread):
    """Downloads update files one at a time, emitting progress signals."""
    progress      = pyqtSignal(str, int)   # (status_text, 0-100 overall %)
    file_done     = pyqtSignal(str)        # file name completed
    finished_ok   = pyqtSignal()
    finished_err  = pyqtSignal(str)

    def __init__(self, manifest, parent=None):
        super().__init__(parent)
        self._manifest = manifest

    def run(self):
        import hashlib as _hl
        files = self._manifest.get('files', [])
        if not files:
            self.finished_ok.emit()
            return
        pending_dir = os.path.join(BASE_PATH, '_pending_update')
        try:
            os.makedirs(pending_dir, exist_ok=True)
        except Exception as e:
            self.finished_err.emit(f"Cannot create temp folder: {e}")
            return

        total = len(files)
        for idx, finfo in enumerate(files):
            name  = finfo.get('name',  '')
            url   = finfo.get('url',   '')
            sha   = finfo.get('sha256', '')
            dest  = os.path.join(pending_dir, name)
            self.progress.emit(f"Downloading {name}…", int(idx * 100 / total))
            try:
                # Stream download with 60s timeout per chunk — handles slow links
                with requests.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    total_bytes = int(r.headers.get('content-length', 0))
                    downloaded  = 0
                    hasher      = _hl.sha256()
                    with open(dest, 'wb') as fout:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                fout.write(chunk)
                                hasher.update(chunk)
                                downloaded += len(chunk)
                                if total_bytes:
                                    file_pct = int(downloaded * 100 / total_bytes)
                                    overall  = int((idx + file_pct / 100) * 100 / total)
                                    self.progress.emit(f"Downloading {name}… {file_pct}%", overall)
                # Verify hash
                if sha and hasher.hexdigest() != sha.lower():
                    self.finished_err.emit(
                        f"Hash mismatch for {name}.\n"
                        "The downloaded file may be corrupted. Please try again.")
                    return
            except Exception as e:
                self.finished_err.emit(f"Failed to download {name}:\n{e}")
                return
            self.file_done.emit(name)

        # Write manifest so updater.exe knows what to replace
        manifest_path = os.path.join(BASE_PATH, '_update_manifest.json')
        try:
            with open(manifest_path, 'w', encoding='utf-8') as mf:
                json.dump(self._manifest, mf, indent=2)
        except Exception as e:
            self.finished_err.emit(f"Could not write update manifest: {e}")
            return
        self.finished_ok.emit()


class UpdateDialog(QDialog):
    """Qt dialog that shows update info, downloads, then triggers updater.exe."""

    def __init__(self, manifest, parent=None):
        super().__init__(parent)
        self._manifest = manifest
        self._dl_thread = None
        self.setWindowTitle("ANPR Update Available")
        self.setMinimumWidth(500)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # Header
        title = QLabel(f"  Update {self._manifest.get('version', '')} is available")
        f = _QFont("Segoe UI", 13, _QFont.Bold)
        title.setFont(f)
        title.setStyleSheet("color:#1797A1;")
        layout.addWidget(title)

        # Release notes
        notes_box = QTextEdit()
        notes_box.setReadOnly(True)
        notes_box.setPlainText(self._manifest.get('release_notes', 'No release notes.'))
        notes_box.setFixedHeight(80)
        notes_box.setStyleSheet(
            "background:#f8fafc;border:1px solid #cbd5e1;border-radius:6px;"
            "font-size:12px;padding:4px;")
        layout.addWidget(notes_box)

        # Files to be updated
        files = self._manifest.get('files', [])
        file_list = QLabel("Files to update:\n" + "\n".join(
            f"  • {fi.get('name','')}  ({fi.get('size',0)//1024:,} KB)" for fi in files))
        file_list.setStyleSheet("font-size:12px;color:#334155;")
        layout.addWidget(file_list)

        # Progress bar (hidden initially)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setStyleSheet(
            "QProgressBar{border:1px solid #cbd5e1;border-radius:5px;height:18px;text-align:center;}"
            "QProgressBar::chunk{background:#1797A1;border-radius:5px;}")
        self._progress.hide()
        layout.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setStyleSheet("font-size:11px;color:#64748b;")
        self._status.hide()
        layout.addWidget(self._status)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._later_btn = QPushButton("Later")
        self._later_btn.setFixedWidth(90)
        self._later_btn.setStyleSheet(
            "QPushButton{border:1px solid #cbd5e1;border-radius:5px;padding:6px 12px;"
            "background:#f1f5f9;color:#334155;font-size:12px;}"
            "QPushButton:hover{background:#e2e8f0;}")
        self._later_btn.clicked.connect(self.reject)

        self._update_btn = QPushButton("Download & Install")
        self._update_btn.setFixedWidth(160)
        self._update_btn.setStyleSheet(
            "QPushButton{border:none;border-radius:5px;padding:6px 12px;"
            "background:#1797A1;color:white;font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#138b94;}"
            "QPushButton:disabled{background:#a0c4c8;}")
        self._update_btn.clicked.connect(self._start_download)

        btn_row.addWidget(self._later_btn)
        btn_row.addSpacing(8)
        btn_row.addWidget(self._update_btn)
        layout.addLayout(btn_row)

    def _start_download(self):
        self._update_btn.setEnabled(False)
        self._later_btn.setEnabled(False)
        self._progress.show()
        self._status.show()
        self._dl_thread = UpdateDownloadThread(self._manifest, self)
        self._dl_thread.progress.connect(self._on_progress)
        self._dl_thread.finished_ok.connect(self._on_download_ok)
        self._dl_thread.finished_err.connect(self._on_download_err)
        self._dl_thread.start()

    def _on_progress(self, text, pct):
        self._status.setText(text)
        self._progress.setValue(pct)

    def _on_download_ok(self):
        self._progress.setValue(100)
        self._status.setText("Download complete. Preparing update…")

        # Write a flag file so that ANPR.exe (the launcher process) launches
        # updater.exe AFTER python.exe has fully exited.
        # Launching updater.exe from inside Python is unreliable — Windows Job
        # Objects or process-group inheritance can silently kill the child when
        # the Python process is terminated.  The launcher is the stable process
        # that should own this responsibility.
        flag_path = os.path.join(BASE_PATH, '_launch_updater.flag')
        try:
            with open(flag_path, 'w', encoding='utf-8') as _f:
                _f.write(BASE_PATH)
        except Exception as e:
            self._on_download_err(f"Could not write updater flag: {e}")
            return

        # Small pause to ensure the flag file is fully flushed to disk.
        import time as _time
        _time.sleep(0.2)

        # Exit using TerminateProcess (skips ALL DLL cleanup including PaddlePaddle/MKL
        # detach routines which cause the 0xC0000409 / 3221226505 crash on process exit).
        # Exit code 124 = launcher signal for "update in progress, stay silent".
        try:
            import ctypes
            ctypes.windll.kernel32.TerminateProcess(
                ctypes.windll.kernel32.GetCurrentProcess(), 124)
        except Exception:
            import os as _os
            _os._exit(124)

    def _on_download_err(self, msg):
        self._status.setText(f"Error: {msg}")
        self._status.setStyleSheet("font-size:11px;color:#ef4444;")
        self._update_btn.setEnabled(True)
        self._update_btn.setText("Retry")
        self._later_btn.setEnabled(True)

# ══════════════════════════════════════════════════════════════════════════════
# END OTA UPDATE SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

class ANPRWindow(QMainWindow):
    """PyQt5 main window for ANPR application"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ANPR System")

        # Set taskbar / window icon — set on QApplication in run_pyqt_window; inherited here

        # Create persistent WebEngine profile so cookies (e.g. Remember me) survive app restart
        # Store in %APPDATA%\ANPR — never inside the ANPR_DEPLOY folder
        self._web_profile = None
        self.browser = QWebEngineView()
        try:
            _appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
            storage_dir = os.path.join(_appdata, 'ANPR', 'webengine_data')
            os.makedirs(storage_dir, exist_ok=True)
            profile = QWebEngineProfile("ANPRProfile", self)
            profile.setPersistentStoragePath(os.path.join(storage_dir, 'storage'))
            profile.setCachePath(os.path.join(storage_dir, 'cache'))
            profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
            profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
            profile.setHttpUserAgent(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/118.0.5993.90 Safari/537.36")
            self._web_profile = profile
            # Inject CSS normalisation via stylesheet — avoids JS repaint flash on every load
            profile.setUserStyleSheet(
                "html,body{margin:0!important;padding:0!important;"
                "-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;}"
            )
            custom_page = ANPRWebEnginePage(profile, self.browser)
            self.browser.setPage(custom_page)
        except Exception as e:
            # Fallback: use default profile if persistent profile fails
            try:
                profile = self.browser.page().profile()
                profile.setHttpUserAgent(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/118.0.5993.90 Safari/537.36")
                custom_page = ANPRWebEnginePage(profile, self.browser)
                self.browser.setPage(custom_page)
            except Exception:
                pass

        # Track initial startup state: retry only while Flask isn't ready yet
        self._initial_loading = True

        # Set zoom once before first load — avoids re-render flash on every loadFinished
        self.browser.setZoomFactor(_compute_pyqt_zoom())

        # Set page background colour to white — prevents dark/white flash between page loads
        from PyQt5.QtGui import QColor
        self.browser.page().setBackgroundColor(QColor('#efefef'))

        # Load login immediately — Flask is confirmed up before PyQt5 window opens
        from PyQt5.QtCore import QTimer
        def _load_login():
            self.browser.setUrl(QUrl("http://localhost:5000/login?embedded=1"))
        QTimer.singleShot(0, _load_login)

        # Connect load finished to apply layout normalizations
        self.browser.loadFinished.connect(self.on_load_finished)

        # Set as central widget BEFORE showing the window to avoid black-frame flash
        self.setCentralWidget(self.browser)

        # Apply base styling — matches body background #efefef, prevents colour mismatch in header gap
        self.setStyleSheet("""
            QMainWindow { background-color: #efefef; }
        """)

        # Show maximized AFTER browser is in place — no empty black window flash
        self.showMaximized()

        # Start background update checker (fires after _UPDATE_CHECK_DELAY_S seconds)
        self._update_checker = UpdateCheckerThread(self)
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.start()

    def _on_update_available(self, manifest):
        """Called from UpdateCheckerThread signal — runs in main Qt thread."""
        dlg = UpdateDialog(manifest, self)
        dlg.exec_()

    def on_load_finished(self, ok: bool):
        """Ensure zoom, font rendering, and margin normalization after page load."""
        if not ok:
            # Only retry back to login during initial startup (Flask not ready yet).
            # Once login has loaded successfully (_initial_loading=False), never
            # redirect back to login — that would cancel the user's post-login navigation.
            if self._initial_loading:
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(400, lambda: self.browser.setUrl(QUrl("http://localhost:5000/login?embedded=1")))
            return
        # First successful load means Flask + login are ready — disable retry
        self._initial_loading = False
        # CSS normalisation is already applied via profile.setUserStyleSheet — no JS needed here
    
    def closeEvent(self, event):
        """
        Handle window close event.

        - If we're on the embedded homepage (/?embedded=1) or bare root, allow close.
        - If we're on the login page, also allow close so the user can exit without logging in.
        - For any other page, navigate back to the embedded homepage instead of closing.
        """
        url = self.browser.url()
        url_str = url.toString()
        path = url.path() if hasattr(url, "path") else ""
        query = url.query() if hasattr(url, "query") else ""

        is_home = 'embedded=1' in query or url_str.endswith('localhost:5000/') or path in ('', '/')
        is_login = path == '/login'

        if is_home or is_login:
            # Allow the window to close
            event.accept()
        else:
            # Navigate back to homepage instead of closing
            self.browser.setUrl(QUrl("http://localhost:5000/?embedded=1"))
            event.ignore()

def run_pyqt_window(_qt_app=None, _splash=None):
    """Run PyQt5 window in main thread"""
    # Suppress Chromium/WebEngine debug.log — must be set before QApplication is created
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = '--disable-logging --log-level=3 --log-file=NUL'
    os.environ['QTWEBENGINE_DISABLE_SANDBOX'] = os.environ.get('QTWEBENGINE_DISABLE_SANDBOX', '1')

    # Enable high DPI scaling for better text rendering
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    # Set Windows AppUserModelID BEFORE QApplication — makes taskbar show correct icon
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(u'ANPR.System.1.0')
    except Exception:
        pass

    # Reuse QApplication created in anpr_entry.py (splash screen) if available
    qt_app = _qt_app or QApplication.instance() or QApplication(sys.argv)
    qt_app.setApplicationName("ANPR System")

    # Set icon on QApplication — this controls the Windows taskbar icon
    try:
        from PyQt5.QtGui import QIcon
        if getattr(sys, 'frozen', False):
            qt_app.setWindowIcon(QIcon(sys.executable))
        else:
            _ico = os.path.join(BASE_PATH, 'truck2.ico')
            if os.path.exists(_ico):
                qt_app.setWindowIcon(QIcon(_ico))
    except Exception:
        pass

    from PyQt5.QtGui import QFont
    
    # Set application font for better rendering
    font = QFont("Segoe UI", 9)
    font.setStyleStrategy(QFont.PreferAntialias)
    qt_app.setFont(font)
    
    # Create main window — showMaximized() is called inside ANPRWindow.__init__
    window = ANPRWindow()

    # Close the splash screen now that the real window is ready
    if _splash is not None:
        try:
            _splash.finish(window)
        except Exception:
            pass

    # Run Qt event loop
    sys.exit(qt_app.exec_())

# ----- Main -----
def main(_qt_app=None, _splash=None):
    global web_shared_cam, MQTT_HANDLER_REF

    # Ensure save root exists
    os.makedirs(SAVE_ROOT, exist_ok=True)

    def background_init():
        """All heavy initialization runs in this background thread.
        Flask (and the login page) is available immediately while this runs.
        _system_status is updated in real-time so the frontend banner reflects progress."""
        global plate_model, web_shared_cam, MQTT_HANDLER_REF

        # ── Pre-flight: warn about unconfigured critical components ──────────
        if not RTSP_URL or not RTSP_URL.strip():
            print("")
            print("\u26a0  WARNING: RTSP stream is not configured.")
            print("   Go to System Configuration \u2192 set the Camera Stream URL.")
            print("   Live detection will be unavailable until configured.")
            print("")
        if not SQL_SERVER or not SQL_SERVER.strip():
            print("\u26a0  WARNING: Database is not configured.")
            print("   Go to System Configuration \u2192 set DB Server, DB Name, Username and Password.")
            print("   Recognition records will not be saved until configured.")
            print("")

        # ── PaddleOCR ──────────────────────────────────────────────────────
        _system_status['paddleocr'] = 'loading'
        _system_status['message'] = 'Initializing OCR engine...'
        logger.info("Initializing PaddleOCR...")
        try:
            initialize_paddleocr()
        except Exception as e:
            logger.error(f"Failed to initialize PaddleOCR: {e}", exc_info=True)
            _system_status['paddleocr'] = 'failed'
        finally:
            # PaddlePaddle wipes logging.root.handlers during its init.
            # Restore our handler immediately so every subsequent log line appears.
            _restore_log_handler()

        if ocr is not None:
            _system_status['paddleocr'] = 'ready'
            logger.info("✓ PaddleOCR initialized successfully")
        elif _system_status.get('paddleocr') != 'failed':
            _system_status['paddleocr'] = 'failed'
            logger.warning("PaddleOCR initialization failed - number plate detection will not work")

        # ── YOLO plate model (imports torch here — not at module level) ────
        _system_status['yolo'] = 'loading'
        _system_status['message'] = 'Loading plate detection model...'
        _import_yolo_lazy()  # imports ultralytics + configures torch threads
        logger.info("Checking YOLO weights:")
        logger.info(f"  Weights folder: {WEIGHTS_DIR} - Exists: {os.path.isdir(WEIGHTS_DIR)}")
        logger.info(f"  Plate model: {PLATE_MODEL_PATH} - Exists: {os.path.isfile(PLATE_MODEL_PATH)}")
        if os.path.isfile(PLATE_MODEL_PATH):
            try:
                plate_model = YOLO(PLATE_MODEL_PATH)
                logger.info(f"✓ Loaded YOLO plate model: {PLATE_MODEL_PATH}")
                _system_status['yolo'] = 'ready'
            except Exception as e:
                logger.warning(f"Failed to load plate model '{PLATE_MODEL_PATH}': {e}")
                _system_status['yolo'] = 'failed'
        else:
            logger.info("Plate model not found - upload via System Configuration to enable detection.")
            _system_status['yolo'] = 'failed'

        # ── Database ────────────────────────────────────────────────────────
        _system_status['database'] = 'loading'
        _system_status['message'] = 'Connecting to database...'
        if ENABLE_SQL_LOGGING and SQL_SERVER and SQL_SERVER.strip():
            logger.info("Initializing database...")
            try:
                if create_database_and_table():
                    logger.info("✓ Database ready for logging")
                    _system_status['database'] = 'ready'
                else:
                    logger.warning("Database initialization failed, will continue without SQL logging")
                    _system_status['database'] = 'failed'
            except Exception as e:
                logger.error(f"Database error: {e}")
                _system_status['database'] = 'failed'
        else:
            logger.info("Database not configured — skipping DB initialization")
            _system_status['database'] = 'skipped'

        # ── Camera ──────────────────────────────────────────────────────────
        _system_status['camera'] = 'loading'
        _system_status['message'] = 'Connecting to camera stream...'
        shared_cam = SharedCamera(RTSP_URL)
        if RTSP_URL and RTSP_URL.strip():
            shared_cam.start()
            web_shared_cam = shared_cam
            # Wait up to 5 s for the first frame — fail fast without blocking long
            _cam_deadline = time.time() + 5.0
            while time.time() < _cam_deadline:
                _f, _ft = shared_cam.get_frame()
                if _f is not None:
                    break
                time.sleep(0.25)
            _f, _ft = shared_cam.get_frame()
            if _f is None:
                logger.warning("Camera: RTSP stream did not deliver a frame within 5 s — marking as failed")
                _system_status['camera'] = 'failed'
            else:
                logger.info("✓ Camera stream connected and delivering frames")
                _system_status['camera'] = 'ready'
        else:
            # No RTSP URL configured — don't start capture thread; keep reference for later
            web_shared_cam = shared_cam
            logger.info("SharedCamera not started — RTSP URL not configured")
            _system_status['camera'] = 'skipped'

        # ── MQTT ────────────────────────────────────────────────────────────
        if ENABLE_MQTT and MQTT_BROKER and str(MQTT_BROKER).strip():
            logger.info("Starting MQTT handler...")
            mqtth = MQTTHandler(MQTT_BROKER, MQTT_PORT, MQTT_TRIGGER_TOPIC, MQTT_PUBLISH_TOPIC,
                                rtsp_url=None, shared_cam=shared_cam)
            MQTT_HANDLER_REF = mqtth
            try:
                mqtth.start()
                logger.info("MQTT handler started")
                _system_status['mqtt'] = 'connected'
            except ConnectionError as e:
                logger.warning(str(e))
                logger.warning("MQTT is not connected — skipping MQTT. App will continue without it.")
                MQTT_HANDLER_REF = None
                _system_status['mqtt'] = 'not_connected'
        else:
            logger.info("MQTT integration is DISABLED - detection available only via API")
            _system_status['mqtt'] = 'disabled'

        # ── Model warmup (deferred 1 s to let camera settle) ────────────────
        _system_status['message'] = 'Running model warmup — almost ready...'
        logger.info("Model warmup started in background - first detection will be faster")

        def _do_warmup():
            time.sleep(1)
            warmup_inference()
            # Only promote camera to ready if it wasn't already marked failed/skipped
            if _system_status.get('camera') not in ('failed', 'skipped'):
                _system_status['camera'] = 'ready'

        threading.Thread(target=_do_warmup, daemon=True).start()

        # ── Done ─────────────────────────────────────────────────────────────
        _system_status['overall'] = 'ready'
        _failed = [k for k in ('paddleocr', 'yolo', 'database', 'camera')
                   if _system_status.get(k) == 'failed']
        if _failed:
            _system_status['message'] = 'System ready with warnings — some components failed'
            logger.warning(f"System ready with failures: {', '.join(_failed)}")
        else:
            _system_status['message'] = 'System ready ✓'
            logger.info("✓ System initialization complete — all subsystems running")

    # Start Flask immediately so the login page is available in <2 s
    if ENABLE_LIVE_STREAM:
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        logger.info("Web server started at http://0.0.0.0:5000")

        # Heavy init (OCR, YOLO, DB, camera, MQTT, warmup) runs in background
        init_thread = threading.Thread(target=background_init, daemon=True)
        init_thread.start()
        logger.info("Starting PyQt5 desktop window...")

        # Run PyQt5 window (blocks until window is closed)
        try:
            run_pyqt_window(_qt_app=_qt_app, _splash=_splash)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            if MQTT_HANDLER_REF:
                MQTT_HANDLER_REF.stop()
            if web_shared_cam:
                try:
                    web_shared_cam.stop()
                except Exception:
                    pass
    else:
        logger.info("Live stream DISABLED - running in headless mode for lower CPU usage")
        background_init()  # run synchronously in headless mode
        if ENABLE_MQTT:
            logger.info("Simple MQTT+RTSP running. Press Ctrl+C to exit.")
        else:
            logger.info("API-only mode. Press Ctrl+C to exit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            if MQTT_HANDLER_REF:
                MQTT_HANDLER_REF.stop()
            if web_shared_cam:
                try:
                    web_shared_cam.stop()
                except Exception:
                    pass

if __name__ == "__main__":
    main()
