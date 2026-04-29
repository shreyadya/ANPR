"""
ANPR WebServer Launcher
This small launcher activates the virtual environment and runs app.py
"""
import os
import sys
import subprocess
import ctypes
import socket
import threading

def show_error(message, title=""):
    """Show error message box"""
    ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)

def show_info(message, title=""):
    """Show info message box"""
    ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)

def main():
    # Set Windows AppUserModelID so taskbar shows correct truck icon
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(u'ANPR.System.1.0')
    except Exception:
        pass

    # Get the directory where this launcher is located
    if getattr(sys, 'frozen', False):
        # Running as compiled exe
        app_dir = os.path.dirname(sys.executable)
    else:
        # Running as script
        app_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Paths
    venv_python = os.path.join(app_dir, 'venv', 'Scripts', 'python.exe')

    # Fallback: if venv not found in app_dir, check parent directory.
    # This covers the case where ANPR_DEPLOY is a subfolder of the dev directory
    # and the venv lives at ../venv/ relative to the exe.
    if not os.path.exists(venv_python):
        parent_dir = os.path.dirname(app_dir)
        parent_venv = os.path.join(parent_dir, 'venv', 'Scripts', 'python.exe')
        if os.path.exists(parent_venv):
            venv_python = parent_venv
    
    # Check for different build types (in order of preference):
    # 1. app.py (development)
    # 2. _internal_server.pyd (Cython compiled - most secure)
    # 3. _internal_server.py (PyArmor obfuscated)
    # 4. _internal_server.pyc (compiled bytecode)
    
    app_script = os.path.join(app_dir, 'app.py')
    pyd_module = os.path.join(app_dir, '_internal_server.pyd')
    obfuscated_script = os.path.join(app_dir, '_internal_server.py')
    pyc_script = os.path.join(app_dir, '_internal_server.pyc')
    cython_wrapper = os.path.join(app_dir, 'run_cython_module.py')
    
    # Check for PyArmor runtime
    runtime_dir = os.path.join(app_dir, 'pyarmor_runtime')
    
    # Set environment variable to indicate production mode (define before use)
    env = os.environ.copy()
    env['ANPR_PRODUCTION_MODE'] = '1'
    env['ANPR_EXE_DIR'] = app_dir
    env['ANPR_RESOURCE_DIR'] = app_dir  # Where .enc files, config.json, weights etc. live
    
    # Disable PaddlePaddle OneDNN/MKL-DNN to avoid PIR errors
    env['FLAGS_use_mkldnn'] = '0'
    env['FLAGS_use_onednn'] = '0'
    env['FLAGS_enable_pir_api'] = '0'
    env['FLAGS_pir_apply_inplace_pass'] = '0'
    env['MKLDNN_DISABLE'] = '1'
    env['DNNL_VERBOSE'] = '0'
    env['FLAGS_use_cuda'] = '0'
    env['FLAGS_use_tensorrt'] = '0'
    # Skip PaddleX network connectivity check — models are cached locally, no internet needed
    env['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    env['KMP_DUPLICATE_LIB_OK'] = 'TRUE'  # Prevent crash when torch+paddle both load OpenMP
    # Force UTF-8 for stdout/stderr so Unicode chars in app.py don't crash on cp1252 systems
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    
    # Initialize python_args (used only for .pyd files)
    python_args = None
    
    if os.path.exists(app_script):
        script_to_run = app_script
    elif os.path.exists(pyd_module):
        # Cython compiled - run directly by importing the module
        # Use Python -c to import and call main() without needing wrapper script
        import_code = f"import sys; sys.path.insert(0, r'{app_dir}'); import _internal_server; _internal_server.main()"
        # Run Python with -c flag to execute the import code directly
        script_to_run = None  # Will use -c flag instead
        python_args = ['-c', import_code]
    elif os.path.exists(obfuscated_script):
        script_to_run = obfuscated_script
        # Add PyArmor runtime to PYTHONPATH if it exists
        if os.path.exists(runtime_dir):
            env['PYTHONPATH'] = runtime_dir + os.pathsep + env.get('PYTHONPATH', '')
    elif os.path.exists(pyc_script):
        script_to_run = pyc_script
    else:
        show_error(f"Application not found!\n\nExpected:\n{app_script}\nor: {pyd_module}\nor: {obfuscated_script}\nor: {pyc_script}")
        sys.exit(1)
    
    # Check if venv exists
    if not os.path.exists(venv_python):
        show_error(
            f"Virtual environment not found!\n\n"
            f"Please run AUTO_SETUP.bat first to install dependencies.\n\n"
            f"Expected: {venv_python}",
            "ANPR WebServer - Setup Required"
        )
        sys.exit(1)
    
    def write_started_banner(log_path):
        """Write the APP STARTED banner as the very first entry for this session."""
        import datetime
        now = datetime.datetime.now().strftime('%Y-%m-%d  %H:%M:%S')
        header = f'= APP STARTED  {now}'
        line = header.ljust(79) + '='
        with open(log_path, 'a', encoding='utf-8') as bf:
            bf.write('\n' + '=' * 80 + '\n')
            bf.write(line + '\n')
            bf.write('=' * 80 + '\n')
            bf.write('-' * 80 + '\n')
            bf.flush()

    def launch_process():
        log_path = os.path.join(app_dir, 'system.log')
        write_started_banner(log_path)          # banner BEFORE any subprocess output
        log_file = open(log_path, 'a', encoding='utf-8', errors='replace')
        creation_flags = subprocess.CREATE_NO_WINDOW
        if script_to_run is None and python_args is not None:
            return subprocess.Popen(
                [venv_python] + python_args,
                cwd=app_dir, env=env,
                stdout=log_file, stderr=log_file,
                creationflags=creation_flags
            )
        else:
            return subprocess.Popen(
                [venv_python, script_to_run],
                cwd=app_dir, env=env,
                stdout=log_file, stderr=log_file,
                creationflags=creation_flags
            )

    def show_splash(process):
        """Show a beautiful tkinter splash screen that closes automatically when Flask is ready."""
        try:
            import tkinter as tk
            import math, random
        except ImportError:
            return

        W, H = 600, 260
        BG        = '#050f1e'
        CYAN      = '#06b6d4'
        CYAN_DIM  = '#0e4d5e'
        WHITE     = '#e2e8f0'
        MUTED     = '#64a8c0'
        BAR_BG    = '#0a2535'

        root = tk.Tk()
        root.overrideredirect(True)
        root.configure(bg=BG)
        root.attributes('-topmost', True)
        root.attributes('-alpha', 0.0)   # start invisible — fade in
        sx = (root.winfo_screenwidth()  - W) // 2
        sy = (root.winfo_screenheight() - H) // 2
        root.geometry(f'{W}x{H}+{sx}+{sy}')

        cv = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0)
        cv.pack(fill='both', expand=True)

        # ── Background: left-dark-to-right-slightly-lighter gradient strips ──
        for i in range(W):
            t   = i / W
            r_c = int(5  + t * 8)
            g_c = int(15 + t * 25)
            b_c = int(30 + t * 60)
            cv.create_line(i, 0, i, H, fill=f'#{r_c:02x}{g_c:02x}{b_c:02x}')

        # ── Network nodes + edges ─────────────────────────────────────────────
        random.seed(7)
        nodes = [(random.randint(20, W-20), random.randint(20, H-20)) for _ in range(38)]
        for i, (x1, y1) in enumerate(nodes):
            for x2, y2 in nodes[i+1:]:
                d = math.hypot(x2-x1, y2-y1)
                if d < 120:
                    a = int(40 * (1 - d/120))
                    col = f'#{0:02x}{max(0,60+a):02x}{max(0,110+a):02x}'
                    cv.create_line(x1, y1, x2, y2, fill=col, width=1)
        for (nx, ny) in nodes:
            r2 = random.randint(1, 3)
            cv.create_oval(nx-r2, ny-r2, nx+r2, ny+r2, fill=CYAN_DIM, outline='')

        # ── Glowing camera icon (left panel) ─────────────────────────────────
        ix, iy = 108, H // 2 - 4
        # outer glow rings
        for gr, ga in [(38, '#051c2a'), (28, '#072335'), (20, '#093040')]:
            cv.create_oval(ix-gr, iy-gr, ix+gr, iy+gr, fill=ga, outline='')
        # lens body
        cv.create_oval(ix-16, iy-16, ix+16, iy+16, fill='#0a3a4a', outline=CYAN, width=2)
        # inner lens
        cv.create_oval(ix-9, iy-9, ix+9, iy+9, fill='#0d4f64', outline=CYAN_DIM, width=1)
        # centre dot
        cv.create_oval(ix-3, iy-3, ix+3, iy+3, fill=CYAN, outline='')
        # top notch
        cv.create_rectangle(ix-6, iy-22, ix+6, iy-17, fill='#0a3a4a', outline=CYAN, width=1)
        # lens glint
        cv.create_oval(ix-8, iy-13, ix-2, iy-7, fill='#39d4f0', outline='', stipple='gray50')

        # ── Vertical separator line ───────────────────────────────────────────
        cv.create_line(190, 40, 190, H-40, fill=CYAN_DIM, width=1)

        # ── Title + subtitle ─────────────────────────────────────────────────
        # subtle shadow
        cv.create_text(W//2 + 36, H//2 - 36, text='ANPR System',
                       font=('Segoe UI', 26, 'bold'), fill='#0a1a2e', anchor='center')
        cv.create_text(W//2 + 35, H//2 - 37, text='ANPR System',
                       font=('Segoe UI', 26, 'bold'), fill=WHITE, anchor='center')
        cv.create_text(W//2 + 35, H//2 - 5, text='Automatic Number Plate Recognition',
                       font=('Segoe UI', 9), fill=MUTED, anchor='center')

        # ── Progress bar track ───────────────────────────────────────────────
        bar_x1, bar_y1 = 210, H - 58
        bar_x2, bar_y2 = W - 30, H - 45
        bar_w = bar_x2 - bar_x1
        cv.create_rectangle(bar_x1, bar_y1, bar_x2, bar_y2,
                            fill=BAR_BG, outline=CYAN_DIM, width=1)

        # animated fill — grows left to right then restarts
        bar_fill = cv.create_rectangle(bar_x1+1, bar_y1+1, bar_x1+1, bar_y2-1,
                                        fill=CYAN, outline='')

        # ── Status text ───────────────────────────────────────────────────────
        status_id = cv.create_text(W//2 + 35, H - 30,
                                   text='Starting, please wait...',
                                   font=('Segoe UI', 9), fill=MUTED, anchor='center')

        # ─────────────────────────────────────────────────────────────────────
        _state   = {'dots': 0, 'bar': 0, 'alpha': 0.0, 'ready': False}
        _dot_txt = ['', '.', '..', '...']

        def _fade_in():
            a = _state['alpha']
            if a < 0.97:
                a = min(0.97, a + 0.08)
                _state['alpha'] = a
                root.attributes('-alpha', a)
                root.after(30, _fade_in)

        def _animate():
            if _state['ready']:
                return
            # pulse bar: sweep 0→bar_w then reset
            bv = _state['bar']
            bv = (bv + 6) % (bar_w + 1)
            _state['bar'] = bv
            cv.coords(bar_fill, bar_x1+1, bar_y1+1, bar_x1+1+bv, bar_y2-1)
            root.after(18, _animate)

        def _poll():
            if process.poll() is not None:
                root.destroy(); return
            # Primary signal: PyQt5 window is visible — close splash immediately
            try:
                import ctypes as _ct
                hwnd = _ct.windll.user32.FindWindowW(None, "ANPR System")
                if hwnd:
                    _state['ready'] = True
                    root.destroy()
                    return
            except Exception:
                pass
            # Fallback: Flask /login endpoint responded (PyQt5 window about to appear)
            try:
                conn = socket.create_connection(('localhost', 5000), timeout=0.3)
                conn.close()
                try:
                    import urllib.request
                    req = urllib.request.urlopen('http://localhost:5000/login', timeout=1)
                    if req.status == 200:
                        req.close()
                        _state['ready'] = True
                        cv.coords(bar_fill, bar_x1+1, bar_y1+1, bar_x2-1, bar_y2-1)
                        cv.itemconfig(status_id, text='Ready', fill=CYAN)
                        root.destroy()
                        return
                    req.close()
                except Exception:
                    pass
            except Exception:
                pass
            _state['dots'] = (_state['dots'] + 1) % 4
            cv.itemconfig(status_id,
                          text='Starting, please wait' + _dot_txt[_state['dots']])
            root.after(100, _poll)

        _fade_in()
        _animate()
        root.after(500, _poll)
        root.mainloop()

    # Run the script using venv python (no console window — all output goes to system.log)
    try:
        process = launch_process()

        # Show splash on the MAIN thread (required by tkinter on Windows — GUI
        # must run on the thread that created the Tk root, and on Windows that
        # must be the main thread; running mainloop() in a daemon thread causes
        # it to silently fail to render).
        # show_splash blocks until Flask is ready (or process dies), then returns.
        show_splash(process)

        # Wait for the process (already running; splash already closed)
        process.wait()

        # Exit code 123 = app requested restart (e.g. after Save Configuration); re-run
        while process.returncode == 123:
            process = launch_process()
            show_splash(process)
            process.wait()

        # ── Update trigger ────────────────────────────────────────────────────
        # Triggered by TWO possible paths:
        #  (A) New pyd: writes '_launch_updater.flag' then exits with code 124
        #  (B) Old pyd: never writes the flag — just downloads files to
        #      '_pending_update/' and exits with code 124.
        # We handle BOTH so the update works even on the very first run where
        # the client still has the old pyd installed.
        flag_path     = os.path.join(app_dir, '_launch_updater.flag')
        manifest_path = os.path.join(app_dir, '_update_manifest.json')
        pending_dir   = os.path.join(app_dir, '_pending_update')

        update_triggered = (
            os.path.exists(flag_path) or
            (os.path.exists(manifest_path) and os.path.exists(pending_dir))
        )

        if update_triggered:
            try:
                os.remove(flag_path)
            except Exception:
                pass
            new_version = _do_update_in_launcher(app_dir)
            # Restart app with newly replaced files
            process = launch_process()
            show_splash(process)
            # show "✓ Updated" toast while app is running
            if new_version:
                _show_update_toast(new_version)
            process.wait()
            while process.returncode == 123:
                process = launch_process()
                show_splash(process)
                process.wait()

        # Check if process exited cleanly
        normal_exit_codes = {0, 124, 3221225786, 0xC000013A, 3221226505, 0xC0000409}
        update_pending = (
            os.path.exists(os.path.join(app_dir, '_pending_update')) or
            os.path.exists(os.path.join(app_dir, '_launch_updater.bat')) or
            os.path.exists(os.path.join(app_dir, '_launch_updater.flag'))
        )
        if process.returncode in normal_exit_codes or update_pending:
            pass  # Normal exit or update in progress — stay silent
        elif process.returncode != 0:
            show_error(f"Application exited with error code: {process.returncode}")

    except Exception as e:
        show_error(f"Failed to start application:\n\n{str(e)}")
        sys.exit(1)


def _do_update_in_launcher(app_dir):
    """Replace files in-process: read manifest, copy from _pending_update/, return new version.
    No child process is spawned — the launcher does everything directly.
    Returns the new version string on success, or None.
    """
    import json   as _json
    import shutil as _sh
    import time   as _t
    import threading as _th
    from datetime import datetime as _dt

    manifest_path = os.path.join(app_dir, '_update_manifest.json')
    pending_dir   = os.path.join(app_dir, '_pending_update')
    log_path      = os.path.join(app_dir, '_updater.log')

    def log(msg):
        try:
            ts = _dt.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(log_path, 'a', encoding='utf-8') as lf:
                lf.write(f'[{ts}] [launcher] {msg}\n')
        except Exception:
            pass

    log('=== in-process update started ===')
    log(f'app_dir       : {app_dir}')
    log(f'manifest      : {os.path.exists(manifest_path)}')
    log(f'pending_dir   : {os.path.exists(pending_dir)}')

    _done        = [False]
    _new_version = [None]

    def _do_work():
        try:
            # Give python.exe 2 s to fully release file handles
            _t.sleep(2)

            if not os.path.exists(manifest_path):
                log('ERROR: manifest not found — aborting')
                return

            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    manifest = _json.load(f)
                log(f'version={manifest.get("version")}  files={[x.get("name") for x in manifest.get("files", [])]}')
            except Exception as e:
                log(f'ERROR reading manifest: {e}')
                return

            errors = []
            for item in manifest.get('files', []):
                name     = item.get('name', '')
                dest_rel = item.get('dest', name)
                src      = os.path.join(pending_dir, name)
                dst      = os.path.join(app_dir, dest_rel)
                log(f'  replacing {name}  src_exists={os.path.exists(src)}')

                if not os.path.exists(src):
                    errors.append(name)
                    log(f'  ERROR: source file missing: {src}')
                    continue

                d = os.path.dirname(dst)
                if d:
                    os.makedirs(d, exist_ok=True)

                bak = dst + '.bak'
                if os.path.exists(dst):
                    try:
                        _sh.copy2(dst, bak)
                    except Exception:
                        pass

                try:
                    _sh.copy2(src, dst)
                    if os.path.exists(bak):
                        try: os.remove(bak)
                        except Exception: pass
                    log(f'  OK: {name} replaced')
                except Exception as e:
                    errors.append(name)
                    log(f'  ERROR replacing {name}: {e}')
                    if os.path.exists(bak):
                        try: _sh.copy2(bak, dst)
                        except Exception: pass

            # Update version.json
            new_ver = manifest.get('version', '')
            if not errors:
                vp = os.path.join(app_dir, 'version.json')
                try:
                    ev = {}
                    if os.path.exists(vp):
                        with open(vp, 'r', encoding='utf-8') as f:
                            ev = _json.load(f)
                    ev['version']      = new_ver
                    ev['install_date'] = _dt.now().strftime('%Y-%m-%d %H:%M:%S')
                    with open(vp, 'w', encoding='utf-8') as f:
                        _json.dump(ev, f, indent=2)
                    log(f'version.json updated to {new_ver}')
                    _new_version[0] = new_ver
                except Exception as e:
                    log(f'ERROR updating version.json: {e}')
            else:
                log(f'errors occurred — skipping version.json: {errors}')

            # Cleanup
            try: _sh.rmtree(pending_dir, ignore_errors=True)
            except Exception: pass
            try: os.remove(manifest_path)
            except Exception: pass
            log('=== update complete ===')
        finally:
            _done[0] = True

    _th.Thread(target=_do_work, daemon=True).start()

    # Show "Installing" UI on main thread; poll until work thread finishes
    try:
        import tkinter as _tk
        root = _tk.Tk()
        root.title('ANPR Update')
        root.overrideredirect(True)
        W, H = 460, 120
        sx = (root.winfo_screenwidth()  - W) // 2
        sy = (root.winfo_screenheight() - H) // 2
        root.geometry(f'{W}x{H}+{sx}+{sy}')
        root.configure(bg='#050f1e')
        root.attributes('-topmost', True)
        _tk.Label(root, text='\u2193  Installing update, please wait\u2026',
                  bg='#050f1e', fg='#06b6d4',
                  font=('Segoe UI', 14, 'bold')).pack(pady=(28, 6))
        _tk.Label(root, text='The application will restart automatically.',
                  bg='#050f1e', fg='#64a8c0',
                  font=('Segoe UI', 9)).pack()
        def _check():
            if _done[0]:
                root.destroy()
            else:
                root.after(300, _check)
        root.after(300, _check)
        root.mainloop()
    except Exception:
        import time as _t2
        while not _done[0]:
            _t2.sleep(0.5)

    return _new_version[0]


def _show_update_toast(version):
    """Show bottom-right toast 'Updated to vX.X.X' for 4 seconds."""
    try:
        import tkinter as _tk
        root = _tk.Tk()
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.attributes('-alpha', 0.93)
        W, H = 320, 56
        sx = root.winfo_screenwidth()  - W - 24
        sy = root.winfo_screenheight() - H - 60
        root.geometry(f'{W}x{H}+{sx}+{sy}')
        root.configure(bg='#065f46')
        _tk.Label(root, text=f'\u2713  Updated to v{version}  \u2014  Restarting\u2026',
                  bg='#065f46', fg='#d1fae5',
                  font=('Segoe UI', 10, 'bold')).pack(expand=True)
        root.after(4000, root.destroy)
        root.mainloop()
    except Exception:
        pass


if __name__ == '__main__':
    main()
