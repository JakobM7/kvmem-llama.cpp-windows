#!/usr/bin/env python3
"""Small dependency-free local dashboard for the native Windows launcher."""
from __future__ import annotations

import json
import os
from pathlib import Path
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
    return (prefer(models, ("qwen3.8", "iq3")) or prefer(models, ("iq3", "mtp")) or (models[0]["path"] if models else ""),
            prefer(projectors, ("q8",)) or (projectors[0]["path"] if projectors else ""))


def gpu_list():
    try:
        result = subprocess.run(
            [os.environ.get("NVIDIA_SMI", "nvidia-smi"), "--query-gpu=index,name,memory.total",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in result.stdout.splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) == 3:
            rows.append({"index": fields[0], "name": fields[1], "memory_mb": fields[2]})
    return rows


def tail_logs(limit=12000):
    files = sorted((ROOT / "logs").glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True) if (ROOT / "logs").is_dir() else []
    chunks = []
    for path in files[:4]:
        try:
            chunks.append(f"--- {path.name} ---\n{path.read_text(encoding='utf-8', errors='replace')[-4000:]}")
        except OSError:
            pass
    return "\n".join(chunks)[-limit:]


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
            "port": port, "raw_k": False, "kvmem": False}


def command(config, action):
    if action == "openwebui":
        if not OPEN_WEBUI.is_file():
            raise ValueError(f"Open WebUI script missing: {OPEN_WEBUI}")
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OPEN_WEBUI), "-NoOpen"], os.environ.copy()
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
                "KVMEM_RAW_K_NVME": "1" if config["raw_k"] else "0", "KVMEM_ENABLE": "1" if config["kvmem"] else "0"})
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
            self.send_json({"models": scan_models(force=True), "dirs": [str(path) for path in model_dirs()]}); return
        if self.path == "/api/gpus":
            self.send_json({"gpus": gpu_list()}); return
        if self.path == "/api/status":
            with STATE["lock"]:
                job = dict(STATE["job"]) if STATE["job"] else None
            port = 18200
            if job and job.get("config"): port = job["config"].get("port", port)
            self.send_json({"health": server_json(port, "/health"), "models": server_json(port, "/v1/models"),
                            "job": job, "logs": tail_logs()}); return
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
body{font:14px system-ui,sans-serif;background:#10131a;color:#e8edf5;max-width:1100px;margin:24px auto;padding:0 18px}h1{font-size:24px}h2{font-size:16px;margin:0 0 10px}section{background:#191e28;border:1px solid #30394a;border-radius:10px;padding:16px;margin:12px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}label{display:flex;flex-direction:column;gap:4px;color:#aebbd0}input,select,button{font:inherit;border-radius:6px;border:1px solid #44516a;background:#0f141d;color:#edf2fa;padding:8px}button{cursor:pointer;background:#2869b2;border-color:#438bd8;margin:4px 4px 4px 0}button.warn{background:#84521e}button.stop{background:#8f3030}small{color:#91a0b7}pre{white-space:pre-wrap;max-height:330px;overflow:auto;background:#0c0f14;padding:10px;border-radius:6px}#state{font-weight:600;color:#8bd5a6}
</style><h1>KVMem Windows Dashboard</h1><p><span id=state>Verbinde …</span> <small>nur lokal: 127.0.0.1</small></p>
<section><h2>Modell und Rezept</h2><div class=grid><label>Rezept<select id=recipe><option value=iq3>IQ3 · mehr KV-Budget</option><option value=iq4>IQ4 · weniger Speicher</option></select></label><label>Modell<select id=model></select></label><label>Vision-Projektor<select id=mmproj></select></label><label>Vision<select id=vision><option>cpu</option><option>gpu</option></select></label><label>GPU<select id=gpu><option value=auto>automatisch</option></select></label><label>KV-Dtype<select id=kv><option>q8_0</option><option>q5_0</option><option>q4_0</option><option>f16</option></select></label></div></section>
<section><h2>Kontext und KVMem</h2><div class=grid><label>Kontext (Tokens)<input id=context type=number value=262144 min=1></label><label>Retrieval-Budget<input id=budget type=number value=36864 min=1></label><label>Generierungsreserve<input id=reserve type=number value=16384 min=1></label><label>Blockgröße<input id=block type=number value=128 min=1></label><label>MTP<select id=mtp><option value=on>an</option><option value=off>aus</option></select></label><label>MTP-Draft-Länge<input id=draft_max type=number value=3 min=1 max=5></label><label>Retrieval-Methode<select id=method><option>retrieval</option><option>recency</option></select></label><label>Query-Replay<select id=replay><option>auto</option><option>legacy</option></select></label><label>Query-Policy<select id=policy><option>user</option><option>legacy</option></select></label></div></section>
<section><h2>NVMe und Server</h2><div class=grid><label>NVMe-Budget (GiB)<input id=nvme_gb type=number value=64 min=0 step=0.5></label><label>NVMe-Verzeichnis<input id=nvme_dir></label><label>Port<input id=port type=number value=18200 min=1 max=65535></label></div><label style="display:block;margin-top:10px"><input id=raw_k type=checkbox checked> K/V auf NVMe auslagern</label><label style="display:block"><input id=kvmem type=checkbox checked> KVMem aktivieren</label><p><small><b>Vorschau (nur anzeigen)</b> zeigt den aufgelösten Startbefehl und ändert keinen Server. <b>Starten</b> fährt den Server hoch, <b>Neu starten</b> ersetzt einen laufenden KVMem-Server, <b>Stoppen</b> beendet ihn.</small></p><button data-action=preview onclick="act('preview')">Vorschau (nur anzeigen)</button><button data-action=start onclick="act('start')">Starten</button><button data-action=restart onclick="act('restart')" class=warn>Neu starten</button><button data-action=stop onclick="act('stop')" class=stop>Stoppen</button></section>
<section><h2>Chat</h2><p><small>Open WebUI wird lokal mit Python eingerichtet und auf Port 3000 gestartet. Beim ersten Start kann die Installation einige Minuten dauern.</small></p><button data-action=openwebui onclick="act('openwebui')">Open WebUI einrichten und öffnen</button><button data-action=openwebui_stop class=stop onclick="act('openwebui_stop')">Open WebUI stoppen</button></section>
<section><h2>Status und Logs</h2><pre id=output>–</pre></section>
<script>
const $=id=>document.getElementById(id), q=async u=>(await fetch(u)).json();
let actionRunning=false, statusTimer=0;
let catalog=[];
function fill(){let m=catalog.filter(x=>!x.mmproj),p=catalog.filter(x=>x.mmproj),e=x=>x.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'); $('model').innerHTML=m.map(x=>`<option value="${e(x.path)}">${e(x.name)}</option>`).join(''); $('mmproj').innerHTML='<option value="">Text-only (kein Projektor)</option>'+p.map(x=>`<option value="${e(x.path)}">${e(x.name)}</option>`).join('')}
async function load(){let x=await q('/api/models');catalog=x.models;fill(); let g=await q('/api/gpus'); $('gpu').innerHTML='<option value="auto">automatisch</option>'+g.gpus.map(x=>`<option value="${x.index}">${x.index}: ${x.name} (${x.memory_mb} MB)</option>`).join(''); $('nvme_dir').value='cache/nvme'; status()}
function config(){return {action:'',recipe:$('recipe').value,model:$('model').value,mmproj:$('mmproj').value,vision:$('vision').value,gpu:$('gpu').value,kv:$('kv').value,context:$('context').value,budget:$('budget').value,reserve:$('reserve').value,block:$('block').value,draft_max:$('draft_max').value,mtp:$('mtp').value,method:$('method').value,replay:$('replay').value,policy:$('policy').value,nvme_gb:$('nvme_gb').value,nvme_dir:$('nvme_dir').value,port:$('port').value,raw_k:$('raw_k').checked,kvmem:$('kvmem').checked}}
async function openWebUI(chat){for(let i=0;i<900;i++){await new Promise(resolve=>setTimeout(resolve,1000));let x=await q('/api/status');if(x.job&&x.job.action==='openwebui'&&!x.job.running){if(x.job.returncode===0){let w=await q('/api/openwebui');if(w.ready){chat.location.href=w.url;return}}chat?.close();if(x.job.returncode!==0)alert('Open WebUI konnte nicht gestartet werden:\n'+(x.job.output||'Unbekannter Fehler'));return}}chat?.close();alert('Open WebUI braucht ungewöhnlich lange. Details stehen im Status/Log.')}
async function waitJob(action){for(let i=0;i<480;i++){await new Promise(resolve=>setTimeout(resolve,500));let x=await q('/api/status');if(x.job&&x.job.action===action&&!x.job.running)return x.job}return null}
async function act(action){if(actionRunning)return;let c=config();c.action=action;let chat=action==='openwebui'?window.open('about:blank','_blank'):null;actionRunning=true;document.querySelectorAll('[data-action]').forEach(x=>x.disabled=true);$('state').textContent=action==='preview'?'Vorschau wird erstellt …':'Aktion läuft: '+action+' …';try{let r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)});let x=await r.json();if(!r.ok){chat?.close();alert(x.error)}else if(action==='openwebui')await openWebUI(chat);else await waitJob(action);}catch(e){chat?.close();alert('Dashboard-Verbindung fehlgeschlagen: '+e)}finally{actionRunning=false;document.querySelectorAll('[data-action]').forEach(x=>x.disabled=false);status()}}
async function status(){try{let x=await q('/api/status');let h=x.health,j=x.job;if(j&&j.running)$('state').textContent='Aktion läuft: '+j.action+' …';else if(h)$('state').textContent='Server läuft';else $('state').textContent='Server nicht aktiv';$('output').textContent=(j?JSON.stringify(j,null,2)+'\n':'')+(x.models?JSON.stringify(x.models,null,2)+'\n':'')+(x.logs||'')}catch(e){$('state').textContent='Dashboard wartet auf den Server';$('output').textContent=String(e)}finally{clearTimeout(statusTimer);statusTimer=setTimeout(status,2000)}}
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
