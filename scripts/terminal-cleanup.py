#!/usr/bin/env python3
"""Stop this checkout's services when its Start.bat owner disappears."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys


def wait_for_owner_exit(pids: list[int]) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForMultipleObjects.argtypes = [
        ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.c_bool, ctypes.c_ulong]
    kernel32.WaitForMultipleObjects.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handles = []
    try:
        for pid in dict.fromkeys(pid for pid in pids if pid > 4):
            handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if handle:
                handles.append(handle)
        if not handles:
            return
        values = (ctypes.c_void_p * len(handles))(*handles)
        kernel32.WaitForMultipleObjects(len(handles), values, False, 0xFFFFFFFF)
    finally:
        for handle in handles:
            kernel32.CloseHandle(handle)


def targets(root: Path) -> list[tuple[Path, int]]:
    found = []
    for state_path in (root / "logs").glob("*.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            executable = Path(state.get("exe", ""))
            if not executable.name.casefold().startswith("llama-kvmem-server"):
                continue
            argv = state.get("argv", [])
            index = argv.index("--port") if "--port" in argv else -1
            port = int(argv[index + 1]) if index >= 0 else 18200
            found.append((executable.parent.parent, port))
        except (OSError, ValueError, TypeError):
            continue
    return list(dict.fromkeys(found)) or [(root / "build-windows", 18200)]


def stop_services(root: Path) -> None:
    env = os.environ.copy()
    stop_server = root / "scripts" / "stop-iq4.ps1"
    for build_dir, port in targets(root):
        env["BUILD_DIR"] = str(build_dir)
        env["PORT"] = str(port)
        if stop_server.is_file():
            try:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(stop_server)], cwd=root, env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=30, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
    stop_webui = root / "scripts" / "stop-open-webui.ps1"
    if stop_webui.is_file():
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(stop_webui)], cwd=root, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--terminal-pid", type=int, required=True)
    args = parser.parse_args()
    wait_for_owner_exit([args.owner_pid, args.terminal_pid])
    stop_services(args.root.resolve())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Cleanup must never become a source of a visible terminal error.
        sys.exit(0)
