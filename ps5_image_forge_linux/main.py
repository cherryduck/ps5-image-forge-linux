"""App initialization and entry point."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# Global root helper state
_root_helper_proc: subprocess.Popen | None = None
_root_helper_socket: str = ""


def _get_app_resource(name: str) -> Path | None:
    """Locate an app resource file (works in dev and PyInstaller modes)."""
    # Dev mode: alongside main.py
    base = Path(__file__).resolve().parent
    path = base / name
    if path.is_file():
        return path
    # PyInstaller mode
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        path = Path(meipass) / "ps5_image_forge_linux" / name
        if path.is_file():
            return path
    return None


def _log_startup(msg: str) -> None:
    """Write startup log to stderr (bypasses Qt stdout capture)."""
    sys.stderr.write(f"[ROOT-HELPER] {msg}\n")
    sys.stderr.flush()


def _start_root_helper() -> str | None:
    """Start the root helper daemon via pkexec.

    Returns the socket path on success, None if the helper could not be started.
    The main app will fall back to per-build pkexec calls if this fails.
    """
    global _root_helper_proc, _root_helper_socket
    import shutil

    # Find the root helper script
    helper_script = _get_app_resource("root_helper.py")
    if not helper_script or not helper_script.is_file():
        _log_startup(f"NOT FOUND: root_helper.py (searched: {_get_app_resource('root_helper.py')})")
        return None

    # Create socket path in XDG_RUNTIME_DIR (auto-cleaned on logout)
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    socket_path = os.path.join(xdg_runtime, f"ps5-forge-helper-{os.getuid()}.sock")

    # Remove stale socket
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    # Ready file: helper touches this when it's listening
    ready_fd, ready_file = tempfile.mkstemp(
        prefix=".ps5-forge-ready-", dir=xdg_runtime
    )
    os.close(ready_fd)

    try:
        # Determine python executable — use system python for pkexec
        # In PyInstaller mode, sys.executable is the bundled binary which can't
        # act as a Python interpreter for external scripts
        python = shutil.which("python3") or "python3"

        # Spawn helper via pkexec — no env wrapper to avoid polkit issues
        # The root helper script handles its own env var setup
        cmd = [
            "pkexec", python, str(helper_script),
            "--socket", socket_path,
            "--ready-file", ready_file,
        ]

        _log_startup(f"Starting: {' '.join(cmd)}")

        _root_helper_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait for ready file (with timeout)
        import time
        deadline = time.monotonic() + 15  # 15 second timeout
        while time.monotonic() < deadline:
            if os.path.exists(ready_file):
                _root_helper_socket = socket_path
                _log_startup(f"Started successfully (PID {_root_helper_proc.pid})")
                return socket_path
            # Check if process died
            if _root_helper_proc.poll() is not None:
                stdout, stderr = _root_helper_proc.communicate()
                _log_startup(f"Process exited with code {_root_helper_proc.returncode}")
                if stdout:
                    _log_startup(f"stdout: {stdout.decode()!r}")
                if stderr:
                    _log_startup(f"stderr: {stderr.decode()!r}")
                return None
            time.sleep(0.2)

        # Timeout - kill the process
        _log_startup("Startup timed out after 15s")
        _root_helper_proc.terminate()
        try:
            _root_helper_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _root_helper_proc.kill()
            _root_helper_proc.wait()
        return None

    except Exception as e:
        _log_startup(f"Startup failed: {e}")
        return None
    finally:
        # Clean up ready file (helper already signaled readiness by this point)
        try:
            os.unlink(ready_file)
        except OSError:
            pass


def _stop_root_helper() -> None:
    """Stop the root helper daemon."""
    global _root_helper_proc, _root_helper_socket

    socket_path = _root_helper_socket
    proc = _root_helper_proc

    if socket_path:
        # Send shutdown command
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(3.0)
            sock.connect(socket_path)
            sock.sendall(json.dumps({"cmd": "shutdown"}).encode())
            # Read response (ignore)
            try:
                sock.recv(4096)
            except Exception:
                pass
            sock.close()
        except Exception:
            pass

    if proc:
        # Wait briefly for graceful shutdown
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

    # Clean up socket
    if socket_path and os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass

    _root_helper_proc = None
    _root_helper_socket = ""


def _is_root_helper_running() -> bool:
    """Check if the root helper is available."""
    if not _root_helper_socket:
        _log_startup("Health check: _root_helper_socket is empty")
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(_root_helper_socket)
        sock.sendall(json.dumps({"cmd": "health"}).encode())
        data = sock.recv(4096)
        sock.close()
        result = json.loads(data).get("ok", False)
        _log_startup(f"Health check: socket={_root_helper_socket}, response={data.decode()!r}, ok={result}")
        return result
    except Exception as e:
        _log_startup(f"Health check failed: {e}")
        return False


def _root_helper_call(request: dict, timeout: float = 3600.0) -> dict:
    """Send a command to the root helper and get the response.

    Raises RuntimeError if the helper is not available or the command fails.
    """
    if not _root_helper_socket:
        raise RuntimeError("Root helper not available")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(_root_helper_socket)
        sock.sendall(json.dumps(request).encode())

        # Read response
        data = b""
        while True:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                try:
                    response = json.loads(data)
                    return response
                except json.JSONDecodeError:
                    continue
            except socket.timeout:
                continue

        raise RuntimeError("Root helper returned no response")
    finally:
        sock.close()


def _ensure_lazy_mkpfs() -> None:
    """Ensure lazy_mkpfs is importable (dev mode: add vendor path)."""
    # Try direct import first (works in PyInstaller bundle)
    try:
        import lazy_mkpfs.compression  # noqa: F401
        return
    except ImportError:
        pass

    # Dev mode: add vendor/lazy_mkpfs to sys.path
    base_path = Path(__file__).resolve().parent.parent
    vendor_path = base_path / "vendor" / "lazy_mkpfs"
    if vendor_path.is_dir() and str(vendor_path) not in sys.path:
        sys.path.insert(0, str(vendor_path))


# Set up lazy_mkpfs import path
_ensure_lazy_mkpfs()

try:
    from lazy_mkpfs.compression import set_zlib_backend
except ImportError:
    set_zlib_backend = None  # type: ignore[assignment]


def _detect_zlib_backend() -> str:
    """Detect the best available zlib backend."""
    if set_zlib_backend is None:
        return "zlib (library unavailable)"

    # Try isa-l first
    try:
        set_zlib_backend("isa-l")
        return "Intel ISA-L"
    except Exception:
        pass

    # Try zlib-ng
    try:
        set_zlib_backend("zlib-ng")
        return "zlib-ng"
    except Exception:
        pass

    # Fallback to standard zlib
    set_zlib_backend("zlib")
    return "standard zlib"


def main() -> None:
    """Initialize and run the PS5 Image Forge Linux application."""
    # Set up exception hook
    sys.excepthook = _global_exception_handler

    # Detect compression backend early
    backend_name = _detect_zlib_backend()

    # Start root helper (single credential prompt at startup)
    helper_socket = _start_root_helper()
    helper_available = helper_socket is not None

    # Set up Qt application
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon

    app = QApplication(sys.argv)
    app.setApplicationName("PS5 Image Forge Linux")
    app.setApplicationVersion("0.1.0")
    app.setStyle("Fusion")

    # Load icon
    icon_path = Path(__file__).resolve().parent / "resources" / "icon.svg"
    if not icon_path.exists():
        # PyInstaller mode: resources in _internal
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            icon_path = Path(meipass) / "ps5_image_forge_linux" / "resources" / "icon.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Import and show main window
    from ps5_image_forge_linux.gui import MainWindow

    window = MainWindow(backend_name=backend_name, helper_available=helper_available)
    window.show()

    result = app.exec()

    # Stop root helper on exit
    _stop_root_helper()

    sys.exit(result)


def _global_exception_handler(exc_type, exc_value, exc_tb) -> None:
    """Handle uncaught exceptions gracefully."""
    from PyQt6.QtWidgets import QMessageBox, QApplication

    traceback_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    message = f"An unexpected error occurred:\n\n{traceback_str[:1000]}"

    app = QApplication.instance()
    if app:
        QMessageBox.critical(None, "Fatal Error", message)
    else:
        print(message, file=sys.stderr)
