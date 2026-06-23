#!/usr/bin/env python3
"""Root helper daemon for PS5 Image Forge Linux.

Run via pkexec at app startup. Listens on a Unix socket for privileged
operations (losetup, mount, umount). Shuts down when the app exits.

Usage: pkexec python3 root_helper.py <socket_path> <ready_file>

  socket_path: Path to the Unix socket to listen on.
  ready_file: Path to a file that will be touched when the helper is ready.
              The main app waits for this file before sending commands.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


class RootHelper:
    """Handle privileged filesystem operations on behalf of the main app."""

    def __init__(self, socket_path: str, ready_file: str | None = None) -> None:
        self.socket_path = socket_path
        self.ready_file = ready_file
        self.sock: socket.socket | None = None
        self._running = True
        # Track active mounts for cleanup on shutdown
        self._active_mounts: list[str] = []
        self._active_loops: list[str] = []

    def start(self) -> None:
        """Start listening on the Unix socket."""
        # Clean up stale socket
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.socket_path)
        self.sock.listen(5)
        self.sock.settimeout(1.0)  # Allow periodic check for shutdown

        # Set restrictive permissions on the socket, but allow the calling user
        # to connect. pkexec/sudo set PKEXEC_UID / SUDO_UID env vars.
        calling_uid = int(os.environ.get("PKEXEC_UID", os.environ.get("SUDO_UID", os.getuid())))
        try:
            import pwd
            calling_gid = pwd.getpwuid(calling_uid).pw_gid
        except KeyError:
            calling_gid = os.getgid()
        os.chown(self.socket_path, calling_uid, calling_gid)
        os.chmod(self.socket_path, 0o600)

        # Signal readiness AFTER socket is fully set up and accessible
        if self.ready_file:
            # Small delay to ensure VFS directory entry is flushed
            time.sleep(0.1)
            Path(self.ready_file).touch()

        while self._running:
            try:
                conn, _ = self.sock.accept()
                # Handle each connection in a thread
                t = threading.Thread(target=self._handle_connection, args=(conn,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    raise
                break

        self._cleanup()

    def _handle_connection(self, conn: socket.socket) -> None:
        """Handle a single client connection."""
        try:
            # Read the request
            data = b""
            conn.settimeout(3.0)
            while True:
                try:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    # Check if we have a complete JSON object
                    try:
                        request = json.loads(data)
                        break
                    except json.JSONDecodeError:
                        continue
                except socket.timeout:
                    # Timeout waiting for more data - if we have invalid JSON,
                    # respond with an error rather than hanging
                    if data:
                        break
                    continue

            if not data:
                return  # Client disconnected

            try:
                request = json.loads(data)
            except json.JSONDecodeError as e:
                try:
                    conn.sendall(json.dumps({"ok": False, "error": f"Invalid JSON: {e}"}).encode())
                except OSError:
                    pass
                return

            cmd = request.get("cmd", "")

            # Dispatch to handler
            if cmd == "health":
                response = {"ok": True}
            elif cmd == "shutdown":
                response = {"ok": True}
            elif cmd == "mount_exfat":
                response = self._handle_mount_exfat(request)
            elif cmd == "batch_mount_exfat":
                response = self._handle_batch_mount_exfat(request)
            elif cmd == "unmount":
                response = self._handle_unmount(request)
            else:
                response = {"ok": False, "error": f"Unknown command: {cmd}"}

            # Send response
            conn.sendall(json.dumps(response).encode())

            # For shutdown, close connection and stop after sending response
            if cmd == "shutdown":
                conn.close()
                self._shutdown()
                return
        except json.JSONDecodeError as e:
            try:
                conn.sendall(json.dumps({"ok": False, "error": f"Invalid JSON: {e}"}).encode())
            except OSError:
                pass
        except Exception as e:
            try:
                conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode())
            except OSError:
                pass
        finally:
            conn.close()

    def _handle_mount_exfat(self, request: dict) -> dict:
        """Mount an exFAT image and copy files from source."""
        image = request["image"]
        source = request["source"]
        uid = request.get("uid", os.getuid())
        gid = request.get("gid", os.getgid())
        progress_file = request.get("progress_file", "")

        mount_point = tempfile.mkdtemp(prefix="exfat_mount_")
        loop_dev = ""

        try:
            # 1. Attach loop device
            result = subprocess.run(
                ["losetup", "--find", "--show", image],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return {"ok": False, "error": f"losetup failed: {result.stderr}"}
            loop_dev = result.stdout.strip()
            self._active_loops.append(loop_dev)

            # 2. Mount
            result = subprocess.run(
                ["mount", "-o", f"uid={uid},gid={gid}", loop_dev, mount_point],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return {"ok": False, "error": f"mount failed: {result.stderr}"}
            self._active_mounts.append(mount_point)

            # 3. Copy files with progress tracking
            total_bytes = _get_dir_size(source)

            progress_thread = None
            if progress_file:
                progress_thread = threading.Thread(
                    target=_write_progress,
                    args=(mount_point, progress_file, total_bytes),
                    daemon=True,
                )
                progress_thread.start()

            result = subprocess.run(
                ["rsync", "-a", "--info=progress2", f"{source}/", f"{mount_point}/"],
                capture_output=True, text=True, timeout=3600,
            )

            if progress_thread:
                progress_thread.join(timeout=2)
                if progress_file:
                    with open(progress_file, "w") as f:
                        f.write("100")

            if result.returncode != 0:
                return {"ok": False, "error": f"rsync failed: {result.stderr}"}

            # 4. Sync
            subprocess.run(["sync"], capture_output=True)

            return {"ok": True}

        finally:
            # Cleanup
            _safe_unmount(mount_point)
            if loop_dev:
                _safe_losetup_detach(loop_dev)
                if loop_dev in self._active_loops:
                    self._active_loops.remove(loop_dev)
            if mount_point in self._active_mounts:
                self._active_mounts.remove(mount_point)
            _safe_rmdir(mount_point)

    def _handle_batch_mount_exfat(self, request: dict) -> dict:
        """Mount multiple exFAT images and copy files, one pkexec session."""
        items = request["items"]
        results_file = request.get("results_file", "")

        results: list[dict] = []

        for i, item in enumerate(items):
            image = item["image"]
            source = item["source"]
            uid = item.get("uid", os.getuid())
            gid = item.get("gid", os.getgid())
            progress_file = item.get("progress_file", "")

            result = self._mount_single_item(image, source, uid, gid, progress_file)
            results.append({
                "index": i,
                "image": image,
                "ok": result["ok"],
                "error": result.get("error"),
            })

        # Write results to file if requested
        if results_file:
            with open(results_file, "w") as f:
                json.dump(results, f)

        # Check if any failed
        all_ok = all(r["ok"] for r in results)
        return {"ok": all_ok, "results": results}

    def _mount_single_item(
        self, image: str, source: str, uid: int, gid: int, progress_file: str
    ) -> dict:
        """Mount a single exFAT image and copy files."""
        mount_point = tempfile.mkdtemp(prefix="exfat_batch_mount_")
        loop_dev = ""

        try:
            # 1. Attach loop device
            result = subprocess.run(
                ["losetup", "--find", "--show", image],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return {"ok": False, "error": f"losetup failed: {result.stderr}"}
            loop_dev = result.stdout.strip()
            self._active_loops.append(loop_dev)

            # 2. Mount
            result = subprocess.run(
                ["mount", "-o", f"uid={uid},gid={gid}", loop_dev, mount_point],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return {"ok": False, "error": f"mount failed: {result.stderr}"}
            self._active_mounts.append(mount_point)

            # 3. Copy files
            total_bytes = _get_dir_size(source)

            progress_thread = None
            if progress_file:
                progress_thread = threading.Thread(
                    target=_write_progress,
                    args=(mount_point, progress_file, total_bytes),
                    daemon=True,
                )
                progress_thread.start()

            result = subprocess.run(
                ["rsync", "-a", "--info=progress2", f"{source}/", f"{mount_point}/"],
                capture_output=True, text=True, timeout=3600,
            )

            if progress_thread:
                progress_thread.join(timeout=2)
                if progress_file:
                    with open(progress_file, "w") as f:
                        f.write("100")

            if result.returncode != 0:
                return {"ok": False, "error": f"rsync failed: {result.stderr}"}

            # 4. Sync
            subprocess.run(["sync"], capture_output=True)

            return {"ok": True}

        finally:
            _safe_unmount(mount_point)
            if loop_dev:
                _safe_losetup_detach(loop_dev)
                if loop_dev in self._active_loops:
                    self._active_loops.remove(loop_dev)
            if mount_point in self._active_mounts:
                self._active_mounts.remove(mount_point)
            _safe_rmdir(mount_point)

    def _handle_unmount(self, request: dict) -> dict:
        """Unmount a mount point."""
        mount_point = request.get("mount_point", "")
        if mount_point:
            _safe_unmount(mount_point)
        return {"ok": True}

    def _shutdown(self) -> None:
        """Signal the helper to shut down."""
        self._running = False

    def _cleanup(self) -> None:
        """Clean up all active mounts and the socket."""
        # Unmount any remaining mounts
        for mount_point in self._active_mounts[:]:
            _safe_unmount(mount_point)
        self._active_mounts.clear()

        # Detach any remaining loop devices
        for loop_dev in self._active_loops[:]:
            _safe_losetup_detach(loop_dev)
        self._active_loops.clear()

        # Remove socket
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        # Close socket
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def _get_dir_size(path: str) -> int:
    """Get total size of a directory in bytes."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total


def _write_progress(mount_point: str, progress_file: str, total_bytes: int) -> None:
    """Background thread: write progress percentage to file."""
    time.sleep(0.5)
    try:
        baseline = _get_mount_used(mount_point)
    except Exception:
        baseline = 0

    while True:
        time.sleep(0.5)
        try:
            used = _get_mount_used(mount_point)
            copied = (used - baseline) * 1024  # Convert 1K blocks to bytes
            if total_bytes > 0:
                pct = min(int(copied * 100 / total_bytes), 100)
                with open(progress_file, "w") as f:
                    f.write(str(pct))
        except Exception:
            pass


def _get_mount_used(mount_point: str) -> int:
    """Get used space on a mount point in 1K blocks."""
    result = subprocess.run(
        ["df", "--output=used", mount_point],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0:
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            return int(lines[-1].strip())
    return 0


def _safe_unmount(mount_point: str) -> None:
    """Safely unmount, with lazy fallback."""
    subprocess.run(["umount", mount_point], capture_output=True, timeout=10)
    if _is_mounted(mount_point):
        subprocess.run(["umount", "-l", mount_point], capture_output=True, timeout=10)


def _is_mounted(mount_point: str) -> bool:
    """Check if a path is currently mounted."""
    result = subprocess.run(["findmnt", "-n", "-o", "TARGET", mount_point],
                           capture_output=True, text=True, timeout=5)
    return mount_point in result.stdout


def _safe_losetup_detach(loop_dev: str) -> None:
    """Safely detach a loop device."""
    subprocess.run(["losetup", "-d", loop_dev], capture_output=True, timeout=10)


def _safe_rmdir(path: str) -> None:
    """Safely remove a directory."""
    subprocess.run(["rm", "-rf", path], capture_output=True, timeout=10)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Root helper daemon for PS5 Image Forge")
    parser.add_argument("--socket", required=True, help="Path to Unix socket")
    parser.add_argument("--ready-file", required=True, help="Path to ready signal file")
    args = parser.parse_args()

    socket_path = args.socket
    ready_file = args.ready_file

    # Set up Python env
    os.environ["PYTHONUNBUFFERED"] = "1"

    helper = RootHelper(socket_path, ready_file=ready_file)

    # Set up signal handlers
    def _signal_handler(signum: int, frame: object) -> None:
        helper._shutdown()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Start helper in a thread so we can signal readiness
    helper_thread = threading.Thread(target=helper.start, daemon=True)
    helper_thread.start()

    # Ready file is touched inside helper.start() once the socket is bound and accessible

    # Wait for helper thread
    helper_thread.join()


if __name__ == "__main__":
    main()