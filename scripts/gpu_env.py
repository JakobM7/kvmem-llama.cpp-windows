"""Select a CUDA device for the canaries.

Windows machines are discovered through ``nvidia-smi`` instead of relying on
UUIDs from the developer's old Linux host.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

UUID_5050 = "GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29"
UUID_5090 = "GPU-58a7c28b-e698-307f-c149-24d4ecd88bf4"

ROOT = Path(__file__).resolve().parents[1]


def find_binary(name: str) -> Path | None:
    """Find a project executable in common Linux, Windows, or package layouts."""
    if os.name == "nt" and not name.lower().endswith(".exe"):
        name += ".exe"
    bases = []
    if os.name == "nt":
        if os.environ.get("KVMEM_BUILD_DIR"):
            build = Path(os.environ["KVMEM_BUILD_DIR"])
            bases.extend((build / "bin", build))
        bases.extend((ROOT / "build-windows" / "bin", ROOT / "build-windows",
                      ROOT / "bin", ROOT / "build" / "bin", ROOT / "build"))
    else:
        bases.extend((ROOT / "build" / "bin", ROOT / "build", ROOT / "bin"))
    for base in bases:
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def apply_gpu(env: dict, which: str = "small") -> dict:
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    if os.name == "nt":
        rows = _gpu_rows(env)
        if not rows:
            raise RuntimeError("nvidia-smi returned no CUDA devices")
        selected = _select_windows_gpu(rows, which, env.get("CUDA_VISIBLE_DEVICES", ""))
        env["CUDA_VISIBLE_DEVICES"] = selected["uuid"] or selected["index"]
        env["KVMEM_GPU_NAME"] = selected["name"]
        _add_windows_cuda_paths(env)
        return env
    if which in ("small", "5050", "lt27b"):
        env["CUDA_VISIBLE_DEVICES"] = UUID_5050
        env["KVMEM_GPU_NAME"] = "RTX 5050"
    elif which in ("27b", "5090"):
        env["CUDA_VISIBLE_DEVICES"] = UUID_5090
        env["KVMEM_GPU_NAME"] = "RTX 5090"
    else:
        raise ValueError(f"unknown gpu selector {which}")
    paths = [str(ROOT / "build" / "bin")]
    for key in ("CUDA_HOME", "CUDA_PATH"):
        if env.get(key):
            root = Path(env[key])
            paths.extend((str(root / "lib"), str(root / "lib64")))
    existing = env.get("LD_LIBRARY_PATH", "").split(":")
    env["LD_LIBRARY_PATH"] = ":".join(dict.fromkeys(path for path in paths + existing if path))
    return env


def _gpu_rows(env: dict) -> list[dict]:
    command = env.get("NVIDIA_SMI", "nvidia-smi.exe" if os.name == "nt" else "nvidia-smi")
    try:
        result = subprocess.run(
            [command, "--query-gpu=index,uuid,name,memory.total", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", 3)]
        if len(parts) == 4:
            try:
                memory = float(parts[3].split()[0])
            except ValueError:
                memory = 0
            rows.append({"index": parts[0], "uuid": parts[1], "name": parts[2], "memory": memory})
    return rows


def _select_windows_gpu(rows: list[dict], which: str, requested: str = "") -> dict:
    # Selector names remain compatible with the Linux canaries; on Windows
    # they mean "preferred local GPU", then the largest available one.
    requested = requested.strip().split(",", 1)[0]
    if requested and requested != "-1":
        match = next((row for row in rows if requested in (row["index"], row["uuid"])), None)
        if match:
            return match
        raise RuntimeError(f"CUDA_VISIBLE_DEVICES={requested!r} is not present in nvidia-smi")
    for preferred in ("RTX 5070 Ti", "RTX 5060 Ti"):
        match = next((row for row in rows if preferred.casefold() in row["name"].casefold()), None)
        if match:
            return match
    return max(rows, key=lambda row: row["memory"])


def _add_windows_cuda_paths(env: dict) -> None:
    build_dir = Path(env["KVMEM_BUILD_DIR"]) if env.get("KVMEM_BUILD_DIR") else ROOT / "build-windows"
    paths = [str(build_dir / "bin"), str(ROOT / "bin")]
    for key in ("CUDA_PATH", "CUDA_HOME"):
        if env.get(key):
            root = Path(env[key])
            paths.extend((str(root / "bin"), str(root / "bin" / "x64")))
    existing = env.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(dict.fromkeys(path for path in paths + existing if path))


def require_device(stderr: str, expect: str = "RTX 5050") -> None:
    if os.name == "nt" and expect and expect not in stderr:
        # Legacy canaries still pass their Linux alias (5050/5090). On a
        # single-GPU Windows host, validate the actual discovered model name.
        rows = _gpu_rows(os.environ)
        if len(rows) == 1 and rows[0]["name"] in stderr:
            expect = rows[0]["name"]
    if expect not in stderr:
        raise SystemExit(
            f"refusing to continue: expected CUDA device {expect} in llama logs, got:\n"
            + "\n".join(
                ln for ln in stderr.splitlines()
                if "CUDA" in ln or "Device" in ln or "offload" in ln
            )[:2000]
        )
    if "offloaded" not in stderr or "to GPU" not in stderr:
        raise SystemExit("refusing to continue: layers were not offloaded to GPU")
