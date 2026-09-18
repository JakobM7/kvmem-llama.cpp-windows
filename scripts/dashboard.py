#!/usr/bin/env python3
"""Small dependency-free local dashboard for the native Windows launcher."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent if _HERE.name.lower() == "scripts" else _HERE
HOST = "127.0.0.1"
PORT = int(os.environ.get("KVMEM_DASHBOARD_PORT", "18600"))
LAUNCHER = ROOT / "scripts" / "start-server.py"
if not LAUNCHER.is_file():
    LAUNCHER = ROOT / "start-server.py"
OPEN_WEBUI = ROOT / "scripts" / "start-open-webui.ps1"
if not OPEN_WEBUI.is_file():
    OPEN_WEBUI = ROOT / "start-open-webui.ps1"
STATE = {"job": None, "scan": (0.0, []), "lock": threading.Lock()}


def default_build_dir():
    """Use the first local build/package that actually contains the server binary."""
    configured = os.environ.get("BUILD_DIR")
    if configured:
        return configured
    binary = "llama-kvmem-server.exe" if os.name == "nt" else "llama-kvmem-server"
    # If a launcher-owned server is already running, keep dashboard actions on
    # that exact package instead of guessing between several extracted ZIPs.
    for state_path in sorted((ROOT / "logs").glob("*.json"),
                             key=lambda path: path.stat().st_mtime, reverse=True) if (ROOT / "logs").is_dir() else []:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            executable = Path(state.get("exe", ""))
            if executable.name.casefold() == binary.casefold() and executable.is_file():
                return str(executable.parent.parent)
        except (OSError, ValueError, TypeError):
            continue
    candidates = [ROOT / "build-windows", ROOT / "build", ROOT]
    if (ROOT / "dist").is_dir():
        candidates.extend(sorted((path for path in (ROOT / "dist").iterdir() if path.is_dir()),
                                 key=lambda path: path.name.casefold()))
    for candidate in candidates:
        if (candidate / "bin" / binary).is_file():
            return str(candidate)
    return str(candidates[0])


def model_dirs():
    raw = os.environ.get("KVMEM_MODEL_DIRS")
    values = raw.split(os.pathsep) if raw else [str(ROOT / "models"), r"I:\models"]
    result = []
    for value in values:
        path = Path(value).expanduser()
        if path.is_dir():
            result.append(path.resolve())
    return list(dict.fromkeys(result))


def scan_models(force=False):
    now = time.monotonic()
    with STATE["lock"]:
        if not force and now - STATE["scan"][0] < 5:
            return STATE["scan"][1]
    found = []
    for directory in model_dirs():
        for current, subdirs, files in os.walk(directory, followlinks=False):
            subdirs[:] = [name for name in subdirs if name not in {".git", ".cache"}]
            for name in files:
                if not name.lower().endswith(".gguf"):
                    continue
                path = (Path(current) / name).resolve()
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                found.append({"path": str(path), "name": name, "mmproj": "mmproj" in name.lower(), "size": size})
    found.sort(key=lambda item: (item["mmproj"], item["name"].lower(), item["path"].lower()))
    with STATE["lock"]:
        STATE["scan"] = (now, found)
    return found


def allowed_gguf(value):
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    else:
        path = path.resolve()
    if path.suffix.lower() != ".gguf" or not path.is_file():
        raise ValueError("Model and projector must be existing .gguf files")
    allowed = {Path(item["path"]).resolve() for item in scan_models()}
    if path not in allowed:
        raise ValueError("Select a GGUF from the configured model directories")
    return path


def default_models():
    entries = scan_models()
    models = [item for item in entries if not item["mmproj"]]
    projectors = [item for item in entries if item["mmproj"]]
    def prefer(items, words):
        return next((item["path"] for item in items if all(word in item["name"].lower() for word in words)), "")
    # Prefer the documented 27B UD-IQ4 MTP profile when it is installed.  A
    # random alphabetic GGUF (often a slower non-UD quant) is a poor default.
    model = (prefer(models, ("qwen3.8", "ud-iq4", "mtp")) or
             prefer(models, ("qwen3.8", "ud-iq4")) or
             prefer(models, ("qwen3.8", "iq3")) or
             prefer(models, ("iq3", "mtp")) or
             (models[0]["path"] if models else ""))
    model_path = Path(model)
    sibling_projectors = [item for item in projectors
                          if Path(item["path"]).parent == model_path.parent or
                          Path(item["path"]).parent in model_path.parents]
    projector_pool = sibling_projectors or projectors
    projector = (prefer(projector_pool, ("qwen3.8", "mmproj")) or
                 prefer(projector_pool, ("q8",)) or
                 prefer(projector_pool, ("f16",)) or
                 (projector_pool[0]["path"] if projector_pool else ""))
    return (model, projector)


def gpu_list():
    try:
        result = subprocess.run(
            [os.environ.get("NVIDIA_SMI", "nvidia-smi"), "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in result.stdout.splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) >= 3:
            rows.append({"index": fields[0], "name": fields[1], "memory_mb": fields[2],
                         "used_mb": fields[3] if len(fields) > 3 else "", "utilization": fields[4] if len(fields) > 4 else "",
                         "temperature": fields[5] if len(fields) > 5 else ""})
    return rows


def tail_logs(limit=12000):
    files = sorted((path for path in (ROOT / "logs").glob("*.log")
                    if "open-webui" not in path.name.lower() and "kvmem" in path.name.lower()),
                   key=lambda path: path.stat().st_mtime, reverse=True) if (ROOT / "logs").is_dir() else []
    chunks = []
    # Show only the newest KVMem log; older recipe logs can contain stale
    # requests and make a healthy current server look stuck.
    for path in files[:1]:
        try:
            chunks.append(f"--- {path.name} ---\n{path.read_text(encoding='utf-8', errors='replace')[-4000:]}")
        except OSError:
            pass
    return "\n".join(chunks)[-limit:]


def latest_server_state():
    """Read the launcher state without depending on a platform process API."""
    states = sorted((ROOT / "logs").glob("*_*.json"), key=lambda path: path.stat().st_mtime, reverse=True) if (ROOT / "logs").is_dir() else []
    for path in states:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("exe"):
                return value
        except (OSError, ValueError):
            continue
    return None


def system_memory():
    """Return host memory using only the standard library."""
    try:
        if os.name == "nt":
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                            ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                            ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                            ("extended", ctypes.c_ulonglong)]
            value = MemoryStatus(); value.length = ctypes.sizeof(value)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
                total = value.total / 2**30
                used = (value.total - value.available) / 2**30
                return {"used_gib": round(used, 1), "total_gib": round(total, 1),
                        "available_gib": round(value.available / 2**30, 1), "load_percent": int(value.memory_load)}
        pages = os.sysconf("SC_PHYS_PAGES")
        available = os.sysconf("SC_AVPHYS_PAGES")
        total = pages * os.sysconf("SC_PAGE_SIZE")
        return {"used_gib": round((total - available * os.sysconf("SC_PAGE_SIZE")) / 2**30, 1),
                "total_gib": round(total / 2**30, 1), "available_gib": round(available * os.sysconf("SC_PAGE_SIZE") / 2**30, 1)}
    except (AttributeError, OSError, ValueError):
        return None


def process_memory(pid):
    if not pid:
        return None
    try:
        if os.name == "nt":
            result = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                                    capture_output=True, text=True, timeout=2, check=False)
            match = re.search(r'"[^\"]+","\d+","([\d.,]+) K"', result.stdout)
            if match:
                return {"working_set_gib": round(float(re.sub(r"[.,]", "", match.group(1))) / 1024**2, 2)}
        status = Path(f"/proc/{int(pid)}/status")
        values = dict(re.findall(r"^(VmRSS|VmSize):\s+(\d+) kB$", status.read_text(), re.M))
        return {"working_set_gib": round(int(values["VmRSS"]) / 1024**2, 2), "virtual_gib": round(int(values["VmSize"]) / 1024**2, 2)} if values else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def nvme_snapshot(path):
    if not path:
        return None
    try:
        location = Path(path)
        usage = shutil.disk_usage(location)
        files = sum(item.stat().st_size for item in location.glob("*") if item.is_file()) if location.is_dir() else 0
        return {"path": str(location), "cache_gib": round(files / 2**30, 2),
                "free_gib": round(usage.free / 2**30, 1), "total_gib": round(usage.total / 2**30, 1)}
    except OSError:
        return {"path": str(path), "error": "Verzeichnis nicht erreichbar"}


def log_activity():
    files = sorted((path for path in (ROOT / "logs").glob("*.log") if "open-webui" not in path.name.lower()),
                   key=lambda path: path.stat().st_mtime, reverse=True) if (ROOT / "logs").is_dir() else []
    if not files:
        return {"stage": "unbekannt", "age_seconds": None, "event": "Noch kein Server-Log vorhanden."}
    path = files[0]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        event = lines[-1] if lines else "Log ist leer"
        lower = event.lower()
        if "error" in lower or "failed" in lower or "cancelled" in lower:
            stage = "Fehler / abgebrochen"
        elif "cache_commit" in lower or "chat_out" in lower or "chat_turn" in lower:
            stage = "Bereit (letzte Antwort fertig)"
        elif "gen_wall" in lower or "spec_verify" in lower or "decode" in lower:
            stage = "Antwort wird erzeugt (Decode)"
        elif "prefill" in lower or "multimodal_compute" in lower or "compute" in lower:
            stage = "Eingabe wird verarbeitet (Prefill)"
        elif "listening" in lower:
            stage = "Bereit und wartet auf eine Anfrage"
        elif "load" in lower or "backend" in lower or "model" in lower:
            stage = "Modell wird geladen / vorbereitet"
        else:
            stage = "Server arbeitet"
        metrics = {}
        for line in reversed(lines):
            match = re.search(r"KVMEM_GEN_WALL\s+n=(\d+)\s+ms=([\d.]+)\s+toks=([\d.]+)", line)
            if match:
                metrics = {"generated_tokens": int(match.group(1)), "decode_ms": float(match.group(2)), "decode_toks": float(match.group(3))}
                break
        return {"file": path.name, "stage": stage, "age_seconds": round(max(0, time.time() - path.stat().st_mtime), 1),
                "event": event[-300:], "metrics": metrics}
    except OSError:
        return {"stage": "Log nicht lesbar", "age_seconds": None, "event": str(path)}


def telemetry(config=None):
    state = latest_server_state()
    argv = state.get("argv", []) if state else []
    def arg_value(flag, default=None):
        try:
            return argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            return default
    port = (config or {}).get("port") or arg_value("--port", 18200)
    gpus = gpu_list()
    activity = log_activity()
    return {"activity": activity, "gpus": gpus, "memory": system_memory(),
            "process": process_memory(state.get("pid") if state else None),
            "nvme": nvme_snapshot((config or {}).get("nvme_dir") or arg_value("--kvmem-nvme-dir")),
            "server": {"pid": state.get("pid") if state else None, "port": int(port), "argv": argv}}


def server_json(port, endpoint):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}{endpoint}", timeout=1) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def open_webui_port():
    try:
        port = int(os.environ.get("OPEN_WEBUI_PORT", "3000"))
    except ValueError as exc:
        raise ValueError("OPEN_WEBUI_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("OPEN_WEBUI_PORT is out of range")
    return port


def open_webui_ready():
    port = open_webui_port()
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/", timeout=1) as response:
            return response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def base_config(data):
    defaults = default_models()
    recipe = str(data.get("recipe", "iq3"))
    if recipe not in {"iq3", "iq4"}:
        raise ValueError("recipe must be iq3 or iq4")
    model = allowed_gguf(data.get("model") or defaults[0])
    mmproj_value = data["mmproj"] if "mmproj" in data else defaults[1]
    mmproj = allowed_gguf(mmproj_value) if mmproj_value else None
    if mmproj is not None and mmproj.name.lower().find("mmproj") < 0:
        raise ValueError("Projector must be an mmproj GGUF")
    def integer(name, default, minimum=1):
        try:
            value = int(data.get(name, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        return value
    try:
        port = integer("port", 18200)
        nvme_gb = float(data.get("nvme_gb", 64))
    except (TypeError, ValueError) as exc:
        raise ValueError("port and nvme_gb must be numbers") from exc
    if port > 65535 or nvme_gb < 0:
        raise ValueError("port or nvme_gb is out of range")
    vision = str(data.get("vision", "cpu"))
    kv = str(data.get("kv", "q8_0" if recipe == "iq3" else "q5_0"))
    if vision not in {"cpu", "gpu"} or kv not in {"q8_0", "q5_0", "q4_0", "f16"}:
        raise ValueError("invalid vision or KV dtype")
    spec = str(data.get("mtp", "on"))
    if spec not in {"on", "off"}:
        raise ValueError("mtp must be on or off")
    replay = str(data.get("replay", "auto"))
    policy = str(data.get("policy", "user"))
    method = str(data.get("method", "retrieval"))
    if replay not in {"auto", "legacy"} or policy not in {"user", "legacy"} or method not in {"retrieval", "recency"}:
        raise ValueError("invalid replay, policy or method")
    thinking = str(data.get("thinking", "off")).strip().lower()
    if thinking not in {"off", "256", "1024", "4096"}:
        raise ValueError("thinking must be off, 256, 1024 or 4096")
    nvme_dir = str(data.get("nvme_dir", str(ROOT / "cache" / "nvme"))).strip()
    if not nvme_dir:
        nvme_dir = str(ROOT / "cache" / "nvme")
    nvme_path = Path(nvme_dir).expanduser()
    if not nvme_path.is_absolute():
        nvme_path = (ROOT / nvme_path).resolve()
    else:
        nvme_path = nvme_path.resolve()
    if nvme_path.exists() and not nvme_path.is_dir():
        raise ValueError("NVMe path must be a directory")
    nvme_path.mkdir(parents=True, exist_ok=True)
    gpu = str(data.get("gpu", "auto")).strip()
    if gpu != "auto" and not all(char.isalnum() or char in {"-", ":", ",", "_"} for char in gpu):
        raise ValueError("invalid GPU selector")
    budget_default, reserve_default = ((36864, 16384) if recipe == "iq3" else (32768, 12288))
    return {
        "recipe": recipe, "model": str(model), "mmproj": str(mmproj) if mmproj else "", "vision": vision, "kv": kv,
        "budget": integer("budget", budget_default), "reserve": integer("reserve", reserve_default),
        "block": integer("block", 128), "context": integer("context", 262144),
        "draft_max": integer("draft_max", 3), "mtp": spec, "replay": replay, "policy": policy,
        "method": method, "nvme_gb": nvme_gb, "nvme_dir": str(nvme_path), "gpu": gpu,
        "thinking": thinking,
        "port": port, "raw_k": bool(data.get("raw_k", True)), "kvmem": bool(data.get("kvmem", True)),
    }


def stop_config(data):
    """Stop needs only the recipe and port; models may not be installed yet."""
    try:
        port = int(data.get("port", 18200))
    except (TypeError, ValueError) as exc:
        raise ValueError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("port is out of range")
    recipe = str(data.get("recipe", "iq3"))
    if recipe not in {"iq3", "iq4"}:
        raise ValueError("recipe must be iq3 or iq4")
    return {"recipe": recipe, "model": "", "mmproj": "", "vision": "cpu", "kv": "q8_0",
            "budget": 1, "reserve": 1, "block": 128, "context": 1, "draft_max": 1,
            "mtp": "off", "replay": "auto", "policy": "user", "method": "retrieval",
            "nvme_gb": 0, "nvme_dir": str(ROOT / "cache" / "nvme"), "gpu": "auto",
            "thinking": "off", "port": port, "raw_k": False, "kvmem": False}


def command(config, action):
    if action == "openwebui":
        if not OPEN_WEBUI.is_file():
            raise ValueError(f"Open WebUI script missing: {OPEN_WEBUI}")
        openwebui_env = os.environ.copy()
        openwebui_env["KVMEM_OPENWEBUI_DEFAULT_MODEL"] = Path(config["model"]).name
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OPEN_WEBUI), "-NoOpen"], openwebui_env
    if action == "openwebui_stop":
        stop_script = OPEN_WEBUI.with_name("stop-open-webui.ps1")
        if not stop_script.is_file():
            raise ValueError(f"Open WebUI stop script missing: {stop_script}")
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(stop_script)], os.environ.copy()
    env = os.environ.copy()
    env.update({"MODEL": config["model"], "MMPROJ_DEVICE": config["vision"],
                "PORT": str(config["port"]), "BUILD_DIR": default_build_dir(),
                "KVMEM_CONTEXT": str(config["context"]), "SPEC_TYPE": "draft-mtp" if config["mtp"] == "on" else "none",
                "SPEC_DRAFT_N_MAX": str(config["draft_max"]), "KVMEM_QUERY_REPLAY": config["replay"],
                "KVMEM_QUERY_POLICY": config["policy"], "KVMEM_METHOD": config["method"],
                "KVMEM_NVME_GB": str(config["nvme_gb"]), "KVMEM_NVME_DIR": config["nvme_dir"],
                "KVMEM_RAW_K_NVME": "1" if config["raw_k"] else "0", "KVMEM_ENABLE": "1" if config["kvmem"] else "0",
                "KVMEM_THINKING": "off" if config["thinking"] == "off" else "on",
                "KVMEM_REASONING_BUDGET": "0" if config["thinking"] == "off" else config["thinking"]})
    if config["mmproj"]:
        env["MMPROJ"] = config["mmproj"]
    else:
        env.pop("MMPROJ", None)
    if config["gpu"] != "auto":
        env["CUDA_VISIBLE_DEVICES"] = config["gpu"]
    args = [str(LAUNCHER), "--recipe", config["recipe"], "--default-model", config["model"],
            "--default-mmproj", config["mmproj"], "--default-vision-device", config["vision"], "--kv", config["kv"],
            "--budget", str(config["budget"]), "--reserve", str(config["reserve"]),
            "--kvmem-block-tokens", str(config["block"])]
    if action == "restart":
        args.append("--restart")
    elif action == "stop":
        args.append("--stop")
    elif action == "preview":
        args.append("--dry-run")
    return [os.environ.get("PYTHON", "python"), *args], env


def run_job(config, action):
    proc = None
    try:
        argv, env = command(config, action)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
        # The launcher starts the actual server detached.  A stuck launcher must
        # not keep the dashboard's single-action slot occupied indefinitely.
        timeout = 900 if action == "openwebui" else 240
        output, _ = proc.communicate(timeout=timeout)
        result = {"action": action, "returncode": proc.returncode, "output": output[-8000:], "config": config}
    except subprocess.TimeoutExpired:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        result = {"action": action, "returncode": 1,
                  "output": f"Die Aktion wurde nach {timeout} Sekunden abgebrochen. "
                            "Prüfe den Server-Log und starte sie danach erneut.", "config": config}
    except Exception as exc:  # surface failures in the dashboard instead of taking down its server
        result = {"action": action, "returncode": 1, "output": str(exc), "config": config}
    with STATE["lock"]:
        STATE["job"] = {"running": False, **result}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def send_json(self, value, status=200):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        if self.path == "/":
            payload = HTML.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        if self.path == "/api/models":
            defaults = default_models()
            self.send_json({"models": scan_models(force=True), "dirs": [str(path) for path in model_dirs()],
                            "defaults": {"model": defaults[0], "mmproj": defaults[1]}}); return
        if self.path == "/api/gpus":
            self.send_json({"gpus": gpu_list()}); return
        if self.path == "/api/status":
            with STATE["lock"]:
                job = dict(STATE["job"]) if STATE["job"] else None
            port = 18200
            if job and job.get("config"): port = job["config"].get("port", port)
            self.send_json({"health": server_json(port, "/health"), "models": server_json(port, "/v1/models"),
                            "job": job, "logs": tail_logs(), "telemetry": telemetry(job.get("config") if job else None)}); return
        if self.path == "/api/openwebui":
            port = open_webui_port()
            self.send_json({"ready": open_webui_ready(), "url": f"http://{HOST}:{port}/"}); return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/action":
            self.send_json({"error": "not found"}, 404); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 256000: raise ValueError("request too large")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            action = data.pop("action", "start")
            if action not in {"start", "restart", "stop", "preview", "openwebui", "openwebui_stop"}: raise ValueError("invalid action")
            config = ({"port": open_webui_port()} if action in {"openwebui", "openwebui_stop"} else
                      stop_config(data) if action == "stop" else base_config(data))
            with STATE["lock"]:
                previous = STATE["job"]
                if previous and previous.get("running"):
                    name = previous.get("action", "unbekannt")
                    self.send_json({"error": f"Eine Aktion läuft bereits ({name}). "
                                           "Warte, bis sie beendet ist; die Eingabefelder bleiben änderbar."}, 409)
                    return
                STATE["job"] = {"running": True, "action": action, "config": config, "started": time.time()}
            threading.Thread(target=run_job, args=(config, action), daemon=True).start()
            self.send_json({"ok": True, "job": STATE["job"]})
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)


HTML = r'''<!doctype html><meta charset="utf-8"><title>KVMem Windows Dashboard</title>
<style>
body{font:14px system-ui,sans-serif;background:#10131a;color:#e8edf5;max-width:1100px;margin:24px auto;padding:0 18px}h1{font-size:24px}h2{font-size:16px;margin:0 0 10px}section{background:#191e28;border:1px solid #30394a;border-radius:10px;padding:16px;margin:12px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}.card{background:#0f141d;border:1px solid #30394a;border-radius:7px;padding:10px}.card b{display:block;font-size:17px;color:#e8edf5;margin-top:3px}label{display:flex;flex-direction:column;gap:4px;color:#aebbd0}input,select,button{font:inherit;border-radius:6px;border:1px solid #44516a;background:#0f141d;color:#edf2fa;padding:8px}button{cursor:pointer;background:#2869b2;border-color:#438bd8;margin:4px 4px 4px 0}button.warn{background:#84521e}button.stop{background:#8f3030}small{color:#91a0b7}pre{white-space:pre-wrap;max-height:330px;overflow:auto;background:#0c0f14;padding:10px;border-radius:6px}#state{font-weight:600;color:#8bd5a6}details{margin-top:10px;color:#aebbd0}summary{cursor:pointer;color:#cbd9ec}
</style><h1>KVMem Windows Dashboard</h1><p><span id=state>Verbinde …</span> <small>nur lokal: 127.0.0.1 · automatische Aktualisierung alle 2 s</small></p>
<section><h2>Modell und Laufzeitprofil</h2><p><small><b>Wichtig:</b> Das Modell ist die GGUF-Datei mit den quantisierten Gewichten (z. B. IQ4). Das <b>Rezept</b> ist nur ein Laufzeitprofil: Es wählt Speicherbudgets, KV-Cache, MTP und sichere Startwerte. Es muss nicht den Dateinamen kopieren, sollte aber zur Quantisierung passen: IQ3 spart mehr VRAM für KV, IQ4 braucht etwas weniger KV-Budget. Das Rezept ändert die Gewichte des Modells nicht. Wenn du unsicher bist, nimm das passende Profil (IQ4-Modell → IQ4).</small></p><p id=compat><small>Modell/Rezept werden geprüft …</small></p><div class=grid><label title="Laufzeitprofil, nicht die Quantisierung der GGUF-Datei">Rezept<select id=recipe><option value=iq3>IQ3 · mehr Speicher für KV/Retrieval</option><option value=iq4>IQ4 · weniger Gewichtsspeicher</option></select></label><label title="Die GGUF-Datei, die tatsächlich geladen wird">Modell<select id=model></select></label><label title="Nur für Bilder; bei Text-Chats leer lassen">Vision-Projektor<select id=mmproj></select></label><label title="CPU ist langsamer, lässt aber mehr VRAM für Modell und KV frei">Vision<select id=vision><option>cpu</option><option>gpu</option></select></label><label title="CUDA-Gerät; automatisch wählt die passende NVIDIA-GPU">GPU<select id=gpu><option value=auto>automatisch</option></select></label><label title="Datentyp des laufzeitgenerierten KV-Caches; nicht die Modellquantisierung">KV-Dtype<select id=kv><option>q8_0</option><option>q5_0</option><option>q4_0</option><option>f16</option></select></label><label title="Interne Denk-Tokens vor der sichtbaren Antwort. Aus liefert für erste Tests schneller sichtbaren Text; größere Budgets brauchen mehr Zeit.">Thinking/Reasoning<select id=thinking><option value=off>aus · schneller sichtbarer Text</option><option value=256>256 Tokens</option><option value=1024>1024 Tokens</option><option value=4096>4096 Tokens</option></select></label></div><details><summary>Was sollte ich auswählen?</summary><p>Für ein IQ4-Modell: Rezept IQ4, KV q5_0, Vision CPU. Für ein IQ3-Modell: Rezept IQ3, KV q8_0. Ein anderes KV-Dtype kann Speicher sparen, aber Geschwindigkeit oder Qualität beeinflussen. Thinking erzeugt interne Begründungstokens, bevor Text erscheint; für einen Funktionstest ist <b>aus</b> am schnellsten. Die Auswahl wird erst mit <b>Starten</b> angewendet.</p></details></section>
<section><h2>Kontext und KVMem</h2><div class=grid><label title="Maximale Promptlänge inklusive Historie">Kontext (Tokens)<input id=context type=number value=262144 min=1></label><label title="GPU-KV-Fenster für Retrieval; größer = mehr VRAM">Retrieval-Budget<input id=budget type=number value=36864 min=1></label><label title="Reservierte Slots für die Antwort; muss zur erwarteten Antwortlänge passen">Generierungsreserve<input id=reserve type=number value=16384 min=1></label><label title="Tokens je KVMem-Block; kleiner ist feiner, aber langsamer">Blockgröße<input id=block type=number value=128 min=1></label><label title="MTP erzeugt Entwürfe und kann Decode beschleunigen">MTP<select id=mtp><option value=on>an</option><option value=off>aus</option></select></label><label title="Anzahl vorgeschlagener Tokens pro MTP-Schritt">MTP-Draft-Länge<input id=draft_max type=number value=3 min=1 max=5></label><label title="retrieval holt relevante Blöcke, recency bevorzugt die jüngsten">Retrieval-Methode<select id=method><option>retrieval</option><option>recency</option></select></label><label title="Automatisch optimiert Wiederverwendung; legacy ist Kompatibilitätsmodus">Query-Replay<select id=replay><option>auto</option><option>legacy</option></select></label><label title="user nutzt die letzte Nutzerfrage als Query">Query-Policy<select id=policy><option>user</option><option>legacy</option></select></label></div></section>
<section><h2>NVMe und Server</h2><div class=grid><label>NVMe-Budget (GiB)<input id=nvme_gb type=number value=64 min=0 step=0.5></label><label>NVMe-Verzeichnis<input id=nvme_dir></label><label>Port<input id=port type=number value=18200 min=1 max=65535></label></div><label style="display:block;margin-top:10px"><input id=raw_k type=checkbox checked> K/V auf NVMe auslagern</label><label style="display:block"><input id=kvmem type=checkbox checked> KVMem aktivieren</label><p><small><b>Vorschau (nur anzeigen)</b> zeigt den aufgelösten Startbefehl und ändert keinen Server. <b>Starten</b> fährt den Server hoch, <b>Neu starten</b> ersetzt einen laufenden KVMem-Server, <b>Stoppen</b> beendet ihn.</small></p><button data-action=preview onclick="act('preview')">Vorschau (nur anzeigen)</button><button data-action=start onclick="act('start')">Starten</button><button data-action=restart onclick="act('restart')" class=warn>Neu starten</button><button data-action=stop onclick="act('stop')" class=stop>Stoppen</button></section>
<section><h2>Chat</h2><p><small>Open WebUI wird lokal mit Python eingerichtet und auf Port 3000 gestartet. Beim ersten Start kann die Installation einige Minuten dauern.</small></p><button data-action=openwebui onclick="act('openwebui')">Open WebUI einrichten und öffnen</button><button data-action=openwebui_stop class=stop onclick="act('openwebui_stop')">Open WebUI stoppen</button></section>
<section><h2>Live-Status</h2><div class=cards><div class=card>Phase<b id=phase>–</b><small id=activity>–</small></div><div class=card>Decode<b id=toks>–</b><small>tok/s aus Server-Log</small></div><div class=card>GPU<b id=vram>–</b><small id=gpuutil>–</small></div><div class=card>RAM<b id=ram>–</b><small id=processmem>Prozess: –</small></div><div class=card>NVMe<b id=nvme>–</b><small id=nvmefree>–</small></div></div><details open><summary>Letztes Serverereignis</summary><pre id=event>–</pre></details><details><summary>Technische Details</summary><pre id=details>–</pre></details></section>
<section><h2>Server-Log / Terminal-Ausgabe</h2><p><small>Hier erscheint laufend, ob das Modell lädt, Prefill läuft, dekodiert wird oder ein Fehler aufgetreten ist. GPU-Speicher kann beim Laden bereits voll sein; entscheidend ist, dass die Phase und das letzte Ereignis weiterlaufen.</small></p><pre id=output>–</pre></section>
<script>
const $=id=>document.getElementById(id), q=async u=>(await fetch(u)).json();
let actionRunning=false, statusTimer=0;
let catalog=[];
function compatibility(){let n=$('model').selectedOptions[0]?.textContent.toLowerCase()||'',r=$('recipe').value;if((n.includes('iq4')&&r==='iq3')||(n.includes('iq3')&&r==='iq4'))$('compat').innerHTML='<small>Hinweis: Modell und Rezept haben unterschiedliche IQ-Stufen. Das ist technisch möglich, aber das passende Profil ist als Ausgangspunkt empfohlen.</small>';else $('compat').innerHTML='<small>Modell und Rezept passen als Laufzeitprofil zusammen.</small>'}
function fill(defaults){let m=catalog.filter(x=>!x.mmproj),p=catalog.filter(x=>x.mmproj),e=x=>x.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'); $('model').innerHTML=m.map(x=>`<option value="${e(x.path)}">${e(x.name)}</option>`).join(''); $('mmproj').innerHTML='<option value="">Text-only (kein Projektor)</option>'+p.map(x=>`<option value="${e(x.path)}">${e(x.name)}</option>`).join(''); if(defaults?.model)$('model').value=defaults.model;if(defaults?.mmproj)$('mmproj').value=defaults.mmproj;let n=$('model').selectedOptions[0]?.textContent.toLowerCase()||'';if(n.includes('iq4')||n.includes('ud-iq4')){$('recipe').value='iq4';$('kv').value='q5_0';$('budget').value='32768';$('reserve').value='12288'}$('model').onchange=compatibility;$('recipe').onchange=compatibility;compatibility()}
async function load(){let x=await q('/api/models');catalog=x.models;fill(x.defaults); let g=await q('/api/gpus'); $('gpu').innerHTML='<option value="auto">automatisch</option>'+g.gpus.map(x=>`<option value="${x.index}">${x.index}: ${x.name} (${x.memory_mb} MB)</option>`).join(''); $('nvme_dir').value='cache/nvme'; status()}
function config(){return {action:'',recipe:$('recipe').value,model:$('model').value,mmproj:$('mmproj').value,vision:$('vision').value,gpu:$('gpu').value,kv:$('kv').value,thinking:$('thinking').value,context:$('context').value,budget:$('budget').value,reserve:$('reserve').value,block:$('block').value,draft_max:$('draft_max').value,mtp:$('mtp').value,method:$('method').value,replay:$('replay').value,policy:$('policy').value,nvme_gb:$('nvme_gb').value,nvme_dir:$('nvme_dir').value,port:$('port').value,raw_k:$('raw_k').checked,kvmem:$('kvmem').checked}}
async function openWebUI(chat){for(let i=0;i<900;i++){await new Promise(resolve=>setTimeout(resolve,1000));let x=await q('/api/status');if(x.job&&x.job.action==='openwebui'&&!x.job.running){if(x.job.returncode===0){let w=await q('/api/openwebui');if(w.ready){chat.location.href=w.url;return}}chat?.close();if(x.job.returncode!==0)alert('Open WebUI konnte nicht gestartet werden:\n'+(x.job.output||'Unbekannter Fehler'));return}}chat?.close();alert('Open WebUI braucht ungewöhnlich lange. Details stehen im Status/Log.')}
async function waitJob(action){for(let i=0;i<480;i++){await new Promise(resolve=>setTimeout(resolve,500));let x=await q('/api/status');if(x.job&&x.job.action===action&&!x.job.running)return x.job}return null}
async function act(action){if(actionRunning)return;let c=config();c.action=action;let chat=action==='openwebui'?window.open('about:blank','_blank'):null;actionRunning=true;document.querySelectorAll('[data-action]').forEach(x=>x.disabled=true);$('state').textContent=action==='preview'?'Vorschau wird erstellt …':'Aktion läuft: '+action+' …';try{let r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)});let x=await r.json();if(!r.ok){chat?.close();alert(x.error)}else if(action==='openwebui')await openWebUI(chat);else await waitJob(action);}catch(e){chat?.close();alert('Dashboard-Verbindung fehlgeschlagen: '+e)}finally{actionRunning=false;document.querySelectorAll('[data-action]').forEach(x=>x.disabled=false);status()}}
function fmt(v,suffix=''){return v===null||v===undefined||v===''?'–':v+suffix}
async function status(){try{let x=await q('/api/status'),h=x.health,j=x.job,t=x.telemetry||{},a=t.activity||{},g=(t.gpus||[])[0],m=t.memory,p=t.process,n=t.nvme;if(j&&j.running)$('state').textContent='Aktion läuft: '+j.action+' …';else if(h)$('state').textContent='Server läuft';else $('state').textContent='Server nicht aktiv';$('phase').textContent=a.stage||'–';$('activity').textContent=a.age_seconds===null?'–':'Letztes Log vor '+a.age_seconds+' s';let metric=a.metrics||{};$('toks').textContent=metric.decode_toks?metric.decode_toks.toFixed(2):'–';$('vram').textContent=g&&g.used_mb?g.used_mb+' / '+g.memory_mb+' MB':'–';$('gpuutil').textContent=g&&g.utilization?('GPU-Auslastung: '+g.utilization+' %'):'–';$('ram').textContent=m?m.used_gib+' / '+m.total_gib+' GiB':'–';$('processmem').textContent=p&&p.working_set_gib?('Server: '+p.working_set_gib+' GiB Working Set'):'Prozess: –';$('nvme').textContent=n&&n.cache_gib!==undefined?n.cache_gib+' GiB Cache':'–';$('nvmefree').textContent=n&&n.free_gib!==undefined?n.free_gib+' GiB frei':'–';$('event').textContent=a.event||'–';$('details').textContent=JSON.stringify({health:x.health,models:x.models,server:t.server,job:j},null,2);$('output').textContent=(x.logs||'')||'Noch keine Log-Ausgabe.'}catch(e){$('state').textContent='Dashboard wartet auf den Server';$('event').textContent=String(e);$('output').textContent=String(e)}finally{clearTimeout(statusTimer);statusTimer=setTimeout(status,2000)}}
load();
</script>'''


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"KVMem dashboard: {url}", flush=True)
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
