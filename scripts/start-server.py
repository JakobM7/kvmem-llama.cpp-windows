#!/usr/bin/env python3
"""Shared cross-platform launcher for the IQ3/IQ4 recipes.

The launcher uses Linux procfs/pidfds when available and small Win32/netstat
adapters on native Windows. It deliberately has no third-party dependencies.
"""
import argparse
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

if os.name == 'nt':
    import ctypes
    import msvcrt
else:
    import fcntl

ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def choose_gpu(env):
    if 'CUDA_VISIBLE_DEVICES' in env:
        if not env['CUDA_VISIBLE_DEVICES'].strip() or env['CUDA_VISIBLE_DEVICES'].strip() == '-1':
            raise ValueError('CUDA_VISIBLE_DEVICES disables GPUs; select a GPU to run this recipe')
        # Explicit selection (indices, UUIDs or lists) takes precedence unchanged.
        return env['CUDA_VISIBLE_DEVICES']
    result = subprocess.run([env.get('NVIDIA_SMI', 'nvidia-smi'),
                             '--query-gpu=uuid,name', '--format=csv,noheader'],
                            capture_output=True, text=True, check=True)
    devices = [tuple(s.strip() for s in line.split(',', 1))
               for line in result.stdout.splitlines() if ',' in line]
    preferred_names = ('RTX 5090', 'RTX 5070 Ti', 'RTX 5060 Ti')
    preferred = [uuid for preferred_name in preferred_names
                 for uuid, name in devices if preferred_name in name]
    if len(preferred) == 1:
        return preferred[0]
    if len(devices) == 1:
        return devices[0][0]
    choices = '\n'.join(f'  {uuid}: {name}' for uuid, name in devices)
    raise ValueError('Cannot choose a GPU automatically. Set CUDA_VISIBLE_DEVICES to an index or UUID.\n' + choices)


def library_path(binary, env):
    # Honor caller settings and use the toolkit recorded by this build, without
    # embedding the developer's CUDA installation path in a portable launcher.
    directories = [str(binary.parent)]
    for bundled in (binary.parent.parent / 'lib', binary.parent.parent.parent / 'lib'):
        if bundled.is_dir():
            directories.append(str(bundled))
    path_key = 'PATH' if os.name == 'nt' else 'LD_LIBRARY_PATH'
    directories += [p for p in env.get(path_key, '').split(os.pathsep) if p]
    roots = [Path(env[k]) for k in ('CUDA_HOME', 'CUDA_PATH') if env.get(k)]
    cache = binary.parent.parent / 'CMakeCache.txt'
    if cache.is_file():
        for key, value in re.findall(r'^(CMAKE_CUDA_COMPILER|CUDAToolkit_NVCC_EXECUTABLE):[^=]+=(.+)$',
                                     cache.read_text(), re.M):
            compiler = Path(value)
            if compiler.is_file():
                roots.append(compiler.parent.parent)
    nvcc = shutil.which('nvcc')
    if nvcc:
        roots.append(Path(nvcc).resolve().parent.parent)
    for root in roots:
        suffixes = ('bin', 'bin/x64', 'lib', 'lib/x64') if os.name == 'nt' else (
            'lib64', 'lib', 'targets/x86_64-linux/lib')
        for suffix in suffixes:
            directory = root / suffix
            if directory.is_dir():
                directories.append(str(directory))
    return os.pathsep.join(dict.fromkeys(directories))


def resolve_binary(build_dir, name):
    """Handle single- and multi-config CMake output layouts."""
    root = Path(build_dir)
    candidates = [root / 'bin' / name]
    for config in (os.environ.get('CMAKE_BUILD_TYPE', ''), 'Release', 'RelWithDebInfo', 'Debug'):
        if config:
            candidates.extend((root / 'bin' / config / name,
                               root / config / name))
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()),
                candidates[0].resolve())


def _windows_process_query(pid):
    """Return process metadata using built-in PowerShell CIM on Windows."""
    script = (
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
        "if ($null -eq $p) {{ exit 2 }}; "
        "$o=Invoke-CimMethod -InputObject $p -MethodName GetOwner; "
        "[pscustomobject]@{{ExecutablePath=$p.ExecutablePath;"
        "CommandLine=$p.CommandLine;CreationDate=$p.CreationDate;"
        "Domain=$o.Domain;User=$o.User}} | ConvertTo-Json -Compress"
    ).format(pid=int(pid))
    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, timeout=5, check=True)
        raw = result.stdout.strip()
        return json.loads(raw) if raw else None
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _windows_current_user():
    domain = os.environ.get('USERDOMAIN', '')
    user = os.environ.get('USERNAME', '') or os.environ.get('USER', '')
    return f'{domain}\\{user}'.casefold() if domain else user.casefold()


def process_info(pid):
    if os.name == 'nt':
        data = _windows_process_query(pid)
        if not data:
            return None
        path = data.get('ExecutablePath')
        if not path:
            return None
        owner = data.get('User') or ''
        domain = data.get('Domain') or ''
        owner = f'{domain}\\{owner}' if domain else owner
        command_line = data.get('CommandLine') or ''
        # Windows command-line parsing is intentionally delegated to the
        # standard CommandLineToArgvW implementation when available.
        argv = _windows_argv(command_line)
        return dict(pid=int(pid), start=str(data.get('CreationDate') or ''),
                    uid=owner.casefold(), exe=str(Path(path).resolve()), argv=argv)
    try:
        proc = Path('/proc') / str(pid)
        stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            return None
        return dict(pid=pid, start=stat[19], uid=proc.stat().st_uid,
                    exe=os.readlink(proc / 'exe'),
                    argv=[os.fsdecode(x) for x in (proc / 'cmdline').read_bytes().split(b'\0') if x])
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def _windows_argv(command_line):
    if not command_line:
        return []
    try:
        shell32 = ctypes.windll.shell32
        shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        argc = ctypes.c_int()
        values = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
        if not values:
            return []
        try:
            return [values[index] for index in range(argc.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(values)
    except (AttributeError, OSError):
        # PowerShell already returns a trustworthy command line; this fallback
        # is only for unusual Windows Python builds without shell32.
        return [command_line]


def owned(info, binary):
    if not info:
        return False
    executable = info['exe'].removesuffix(' (deleted)')
    expected = str(binary.resolve())
    if os.name == 'nt':
        return info.get('uid', '').casefold() == _windows_current_user() and executable.casefold() == expected.casefold()
    return info['uid'] == os.getuid() and executable == expected


def same_process(info):
    now = process_info(info['pid'])
    same_exe = (now and now['exe'].casefold() == info['exe'].casefold()) if os.name == 'nt' else (now and now['exe'] == info['exe'])
    return bool(now and now['start'] == info['start'] and same_exe)


def stop_owned(info, binary):
    if os.name == 'nt':
        if not owned(info, binary) or not same_process(info):
            return
        handle = ctypes.windll.kernel32.OpenProcess(0x0001 | 0x0400 | 0x1000, False, info['pid'])
        if not handle:
            return
        try:
            current = process_info(info['pid'])
            if not current or current['start'] != info['start'] or not owned(current, binary):
                return
            print(f"stopping pid={info['pid']}", flush=True)
            if not ctypes.windll.kernel32.TerminateProcess(handle, 1):
                raise OSError(ctypes.get_last_error(), 'TerminateProcess failed')
            ctypes.windll.kernel32.WaitForSingleObject(handle, 10000)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    # A pidfd ties signals to the inspected process, even if a PID is reused.
    if not owned(info, binary):
        raise ValueError('Refusing to stop a process outside this project')
    try:
        fd = os.pidfd_open(info['pid'])
    except ProcessLookupError:
        return
    try:
        if not same_process(info):
            return
        print(f"stopping pid={info['pid']}", flush=True)
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if not select.select([fd], [], [], 10)[0]:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            if not select.select([fd], [], [], 10)[0]:
                raise RuntimeError(f"process {info['pid']} did not exit")
    except ProcessLookupError:
        # The inspected process may exit before either signal is sent.
        return
    finally:
        os.close(fd)


def listener(port):
    if os.name == 'nt':
        # Get-NetTCPConnection exposes the state as a stable enum, unlike
        # netstat whose text is localized (ABHÖREN on German Windows).
        query = (f"Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
                 "-ErrorAction SilentlyContinue | "
                 "Select-Object -ExpandProperty OwningProcess")
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                     '-Command', query], capture_output=True, text=True,
                                    timeout=5, check=False)
            pids = {int(line.strip()) for line in result.stdout.splitlines()
                    if line.strip().isdigit()}
            if pids:
                return True, pids
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        result = subprocess.run(['netstat', '-ano', '-p', 'tcp'],
                                capture_output=True, text=True, check=True)
        pids = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            state = fields[3].upper() if len(fields) >= 4 else ''
            if (len(fields) >= 5 and fields[0].upper() == 'TCP' and
                    state in {'LISTENING', 'ABHÖREN'}):
                local = fields[1].rsplit(':', 1)
                if len(local) == 2 and local[1] == str(port):
                    try:
                        pids.add(int(fields[4]))
                    except ValueError:
                        pass
        return bool(pids), pids
    output = subprocess.run(['ss', '-H', '-ltnp', f'sport = :{port}'],
                            capture_output=True, text=True, check=True).stdout
    if not output.strip():
        return False, set()
    return True, {int(pid) for pid in re.findall(r'pid=(\d+)', output)}


def healthy(port):
    try:
        with OPENER.open(f'http://127.0.0.1:{port}/health', timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def stop_server(binary, port):
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    # Use the launcher's port lock so stopping cannot race startup/restart.
    with port_lock(logs / f'kvmem_{port}.lock'):
        busy, pids = listener(port)
        targets = {}
        if busy:
            if len(pids) != 1:
                raise ValueError(f'port {port} is busy; cannot identify one owned service, leaving it running')
            info = process_info(next(iter(pids)))
            if not owned(info, binary):
                raise ValueError(f'port {port} belongs to another service; leaving it running')
            targets[info['pid']] = info
        # An interrupted launcher may leave a server loading without a listener.
        # A PID file alone is not sufficient proof of ownership.
        pidfiles = [logs / f'{recipe}_{port}.pid' for recipe in ('iq3', 'iq4')]
        for path in pidfiles:
            try:
                info = process_info(int(path.read_text().strip()))
            except (FileNotFoundError, ValueError):
                continue
            if owned(info, binary) and '--port' in info['argv']:
                index = info['argv'].index('--port')
                if info['argv'][index + 1:index + 2] == [str(port)]:
                    targets[info['pid']] = info
        for info in targets.values():
            stop_owned(info, binary)
        if listener(port)[0]:
            raise ValueError(f'port {port} is still busy; leaving PID files for inspection')
        for path in pidfiles:
            path.unlink(missing_ok=True)
            path.with_suffix('.json').unlink(missing_ok=True)
        print(f'stopped server on port {port}' if targets else f'no owned server running on port {port}')


def same_config(info, argv, env, state_path=None):
    if info['exe'].endswith(' (deleted)') or info['argv'] != argv:
        return False
    if os.name == 'nt':
        if state_path is None or not state_path.is_file():
            return False
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
            return (state.get('pid') == info['pid'] and
                    (not state.get('start') or state.get('start') == info['start']) and
                    state.get('exe', '').casefold() == info['exe'].casefold() and
                    state.get('argv') == argv and
                    all(state.get('env', {}).get(key) == env.get(key)
                        for key in ('CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER')))
        except (OSError, ValueError, TypeError):
            return False
    try:
        raw = (Path('/proc') / str(info['pid']) / 'environ').read_bytes()
        values = dict(part.split(b'=', 1) for part in raw.split(b'\0') if b'=' in part)
        return all(values.get(key.encode()) == env[key].encode()
                   for key in ('CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER'))
    except (FileNotFoundError, PermissionError):
        return False


class port_lock:
    """Small cross-platform non-blocking lock for one launcher port."""
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.file = self.path.open('a+b')
        if os.name == 'nt':
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                self.file.close()
                raise BlockingIOError from exc
        else:
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.file.close()
                raise
        return self.file

    def __exit__(self, *_):
        if self.file is None:
            return
        if os.name == 'nt':
            try:
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.file.close()
        else:
            try:
                fcntl.flock(self.file, fcntl.LOCK_UN)
            finally:
                self.file.close()


def write_state(path, proc, argv, env, info=None):
    """Atomically persist process identity/config for Windows reuse checks."""
    info = info or process_info(proc.pid) or {}
    state = dict(pid=proc.pid, start=info.get('start', ''), exe=str(Path(argv[0]).resolve()),
                 argv=argv, env={key: env.get(key) for key in ('CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER')})
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def launch(args, binary, argv, env, port):
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    pidfile = logs / f'{args.recipe}_{port}.pid'
    statefile = logs / f'{args.recipe}_{port}.json'
    # Serialize IQ3/IQ4 decisions on this port, including startup and restart.
    with port_lock(logs / f'kvmem_{port}.lock'):
        busy, pids = listener(port)
        if busy:
            if len(pids) != 1:
                raise ValueError(f'port {port} is busy; cannot identify one owned service, leaving it running')
            info = process_info(next(iter(pids)))
            if not owned(info, binary):
                raise ValueError(f'port {port} belongs to another service; leaving it running, even with --restart')
            if not args.restart:
                if same_config(info, argv, env, statefile) and healthy(port):
                    pidfile.write_text(str(info['pid']) + '\n')
                    print(f"already up recipe={args.recipe} pid={info['pid']} http://127.0.0.1:{port}/health")
                    return
                raise ValueError(f'port {port} has a different configuration or an unhealthy service; use --restart to switch')
            stop_owned(info, binary)
        # Also catch a previous launcher interrupted while its model was loading.
        # Stale PID files for unrelated processes are ignored, never signalled.
        for name in ('iq3', 'iq4'):
            previous = logs / f'{name}_{port}.pid'
            if previous.is_file():
                try:
                    info = process_info(int(previous.read_text().strip()))
                except ValueError:
                    info = None
                if owned(info, binary) and '--port' in info['argv']:
                    index = info['argv'].index('--port')
                    if info['argv'][index + 1:index + 2] == [str(port)]:
                        if not args.restart:
                            raise ValueError(f'owned server pid={info["pid"]} is still starting; wait or use --restart')
                        stop_owned(info, binary)
        if listener(port)[0]:
            raise ValueError(f'port {port} is still busy')
        port_suffix = '' if port == 18200 else f'_{port}'
        stdout = logs / f'opencode_kvmem_{args.recipe}_256k{port_suffix}.stdout.log'
        stderr = logs / f'opencode_kvmem_{args.recipe}_256k{port_suffix}.stderr.log'
        for path in (stdout, stderr):
            if path.exists():
                path.rename(path.with_name(path.name + f'.{time.time_ns()}'))
        with stdout.open('wb') as out, stderr.open('wb') as err:
            creationflags = 0
            if os.name == 'nt':
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, start_new_session=True,
                                    creationflags=creationflags)
        try:
            pidfile.write_text(str(proc.pid) + '\n')
            if os.name == 'nt':
                write_state(statefile, proc, argv, env)
            deadline = time.monotonic() + args.startup_timeout
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f'server exited with status {proc.returncode}')
                busy, pids = listener(port)
                if busy and pids == {proc.pid} and healthy(port):
                    print(f'started recipe={args.recipe} pid={proc.pid} gpu={env["CUDA_VISIBLE_DEVICES"]} '
                          f'vision={env["KVMEM_VISION_DEVICE"]} model={argv[2]} '
                          f'http://127.0.0.1:{port}/health')
                    return
                time.sleep(.5)
            raise RuntimeError('server startup timed out')
        except BaseException:
            # Only this child is cleaned up on startup failure or interruption.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            pidfile.unlink(missing_ok=True)
            statefile.unlink(missing_ok=True)
            print(stderr.read_text(errors='replace')[-4000:], file=sys.stderr)
            raise


def main():
    ap = argparse.ArgumentParser(description=__doc__, epilog=
        'Overrides: MODEL, MMPROJ, MMPROJ_DEVICE, CUDA_VISIBLE_DEVICES, PORT, BUILD_DIR, '
        'IMAGE_MAX_TOKENS, SPEC_KV_DTYPE, SPEC_DRAFT_N_MAX, KVMEM_MTP_STATE, '
        'KVMEM_QUERY_REPLAY, KVMEM_QUERY_POLICY, KVMEM_CONTEXT, KVMEM_ENABLE, '
        'KVMEM_METHOD, KVMEM_RAW_K_NVME, KVMEM_NVME_GB, KVMEM_NVME_DIR, SPEC_TYPE, '
        'MTP_ENABLE, CUDA_HOME, CUDA_PATH, LD_LIBRARY_PATH, NVIDIA_SMI.')
    ap.add_argument('--recipe', choices=('iq3', 'iq4'), required=True)
    ap.add_argument('--default-model', required=True)
    ap.add_argument('--default-mmproj', required=True)
    ap.add_argument('--default-vision-device', choices=('cpu', 'gpu'), required=True)
    ap.add_argument('--kv', required=True)
    ap.add_argument('--budget', type=int, required=True)
    ap.add_argument('--reserve', type=int, required=True)
    ap.add_argument('--kvmem-block-tokens', type=int, help='override retrieval block size (server default 128)')
    templates = ap.add_mutually_exclusive_group()
    templates.add_argument('--chat-template', help='Jinja template text')
    templates.add_argument('--chat-template-file', type=Path, help='custom Jinja template file')
    ap.add_argument('--chat-template-kwargs', help='default template arguments as a JSON object')
    ap.add_argument('--reasoning-effort', help='template effort (GSQ 27B: low, medium, xhigh); default or none')
    ap.add_argument('--ui-dir', type=Path, help='static chat UI directory')
    ap.add_argument('--no-ui', action='store_true', help='disable chat UI')
    ap.add_argument('--jinja', action='store_true', help='native Jinja rendering is always enabled')
    action = ap.add_mutually_exclusive_group()
    action.add_argument('--restart', action='store_true')
    action.add_argument('--stop', action='store_true', help="stop this project's server on PORT, without loading models")
    action.add_argument('--dry-run', action='store_true', help='print resolved argv/environment without starting or stopping anything')
    ap.add_argument('--startup-timeout', type=float, default=180)
    args = ap.parse_args()
    env = os.environ.copy()
    binary_name = 'llama-kvmem-server.exe' if os.name == 'nt' else 'llama-kvmem-server'
    binary = resolve_binary(env.get('BUILD_DIR', str(ROOT / 'build')), binary_name)
    port = int(env.get('PORT', '18200'))
    if not 1 <= port <= 65535:
        raise ValueError('invalid PORT')
    if args.stop:
        stop_server(binary, port)
        return
    if args.kvmem_block_tokens is not None and not 1 <= args.kvmem_block_tokens <= 2147483647:
        raise ValueError('--kvmem-block-tokens must be a positive int32')
    model = Path(env.get('MODEL', args.default_model)).resolve()
    mmproj_value = env.get('MMPROJ', args.default_mmproj)
    mmproj = Path(mmproj_value).resolve() if mmproj_value else None
    for path in (binary, model):
        if not path.is_file():
            raise ValueError(f'file not found: {path}')
    if mmproj is not None and not mmproj.is_file():
        raise ValueError(f'file not found: {mmproj}')
    if os.name != 'nt' and not os.access(binary, os.X_OK):
        raise ValueError(f'binary is not executable: {binary}')
    vision = env.get('MMPROJ_DEVICE', args.default_vision_device)
    replay = env.get('KVMEM_QUERY_REPLAY', 'auto')
    policy = env.get('KVMEM_QUERY_POLICY', 'user')
    draft_kv = env.get('SPEC_KV_DTYPE', 'f16')
    spec_type_explicit = 'SPEC_TYPE' in env or 'MTP_ENABLE' in env
    spec_type = env.get('SPEC_TYPE', 'draft-mtp')
    if env.get('MTP_ENABLE', '').strip().lower() in ('0', 'false', 'no', 'off'):
        spec_type = 'none'
    draft_max = int(env.get('SPEC_DRAFT_N_MAX', '3'))
    mtp_state = env.get('KVMEM_MTP_STATE', 'replay')
    kvmem_enabled = env.get('KVMEM_ENABLE', '1').strip().lower() not in ('0', 'false', 'no', 'off')
    raw_k_nvme = env.get('KVMEM_RAW_K_NVME', '1').strip().lower() not in ('0', 'false', 'no', 'off')
    kvmem_method = env.get('KVMEM_METHOD', 'retrieval')
    if vision not in ('cpu', 'gpu') or replay not in ('auto', 'legacy') or policy not in ('user', 'legacy'):
        raise ValueError('invalid MMPROJ_DEVICE, KVMEM_QUERY_REPLAY or KVMEM_QUERY_POLICY')
    if draft_kv not in ('f16', 'q8_0', 'q5_0', 'q4_0') or spec_type not in ('none', 'draft-mtp'):
        raise ValueError('SPEC_KV_DTYPE must be f16, q8_0, q5_0 or q4_0')
    if kvmem_method not in ('recency', 'retrieval'):
        raise ValueError('KVMEM_METHOD must be recency or retrieval')
    if draft_max < 1 or mtp_state not in ('snapshots', 'auto', 'replay'):
        raise ValueError('invalid SPEC_DRAFT_N_MAX or KVMEM_MTP_STATE')
    if mtp_state == 'replay' and draft_max > 5:
        raise ValueError('ReplaySSM supports SPEC_DRAFT_N_MAX from 1 to 5')
    image_tokens = int(env.get('IMAGE_MAX_TOKENS', '512'))
    if not 1 <= port <= 65535 or image_tokens <= 0 or not 0 < args.startup_timeout <= 3600:
        raise ValueError('invalid PORT, IMAGE_MAX_TOKENS or startup timeout')
    env['CUDA_VISIBLE_DEVICES'] = choose_gpu(env)
    env.setdefault('CUDA_DEVICE_ORDER', 'PCI_BUS_ID')
    library_key = 'PATH' if os.name == 'nt' else 'LD_LIBRARY_PATH'
    env[library_key] = library_path(binary, env)
    env['KVMEM_VISION_DEVICE'] = vision
    try:
        context = int(env.get('KVMEM_CONTEXT', env.get('CONTEXT_SIZE', '262144')))
    except ValueError as exc:
        raise ValueError('KVMEM_CONTEXT must be a positive integer') from exc
    if context < 1:
        raise ValueError('KVMEM_CONTEXT must be a positive integer')
    argv = [str(binary), '-m', str(model)]
    if mmproj is not None:
        argv += ['--mmproj', str(mmproj)]
    argv += [
            '--mmproj-offload' if vision == 'gpu' else '--no-mmproj-offload',
            '--image-max-tokens', str(image_tokens), '--host', '127.0.0.1', '--port', str(port),
            '-c', str(context), '-n', str(args.reserve), '--enable-thinking', '--reasoning-budget', '4096']
    if kvmem_enabled:
        if 'KVMEM_ENABLE' in env:
            argv += ['--kvmem']
        argv += ['--kvmem-budget', str(args.budget), '--kvmem-gen-reserve', str(args.reserve),
                 '--kv-dtype', args.kv]
        if 'KVMEM_METHOD' in env:
            argv += ['--kvmem-method', kvmem_method]
    else:
        argv += ['--no-kvmem']
    if spec_type_explicit:
        argv += ['--spec-type', spec_type]
    # Native Windows uses the project cache on the selected NVMe by default;
    # Linux keeps its historical RAM-only launcher unless explicitly enabled.
    nvme_gb = env.get('KVMEM_NVME_GB', '64' if os.name == 'nt' else '0')
    try:
        nvme_value = float(nvme_gb)
    except ValueError as exc:
        raise ValueError('KVMEM_NVME_GB must be a non-negative number') from exc
    if nvme_value < 0:
        raise ValueError('KVMEM_NVME_GB must be a non-negative number')
    if nvme_value > 0 and kvmem_enabled:
        nvme_dir = env.get('KVMEM_NVME_DIR')
        if not nvme_dir and os.name == 'nt':
            nvme_dir = str(ROOT / 'cache' / 'nvme')
        argv += ['--kvmem-nvme-gb', nvme_gb]
        if raw_k_nvme:
            argv += ['--kvmem-raw-k-nvme']
        if nvme_dir:
            argv += ['--kvmem-nvme-dir', nvme_dir]
    if args.ui_dir is not None:
        ui = args.ui_dir.resolve()
        if not (ui / 'index.html').is_file():
            raise ValueError(f'UI directory has no index.html: {ui}')
        argv += ['--ui-dir', str(ui)]
    elif args.no_ui or os.name == 'nt':
        # Open WebUI is the supported browser front end. Do not accidentally
        # serve a stale generated static page from share/kvmem/ui.
        argv += ['--no-ui']
    if args.no_ui and args.ui_dir is not None:
        argv += ['--no-ui']
    # These match server defaults; only emit caller overrides.
    if args.kvmem_block_tokens is not None:
        argv += ['--kvmem-block-tokens', str(args.kvmem_block_tokens)]
    for key, flag, value in (
        ('KVMEM_MTP_STATE', '--kvmem-mtp-state', mtp_state),
        ('KVMEM_QUERY_REPLAY', '--kvmem-query-replay', replay),
        ('KVMEM_QUERY_POLICY', '--kvmem-query-policy', policy),
        ('SPEC_DRAFT_N_MAX', '--spec-draft-n-max', str(draft_max)),
        ('SPEC_KV_DTYPE', '--spec-kv-dtype', draft_kv),
    ):
        if key in env:
            argv += [flag, value]
    if args.chat_template_file is not None:
        template = args.chat_template_file.resolve()
        if not template.is_file() or not template.read_text().strip():
            raise ValueError(f'chat template file missing or empty: {template}')
        argv += ['--chat-template-file', str(template)]
    if args.chat_template is not None:
        if not args.chat_template.strip():
            raise ValueError('chat template must not be empty')
        argv += ['--chat-template', args.chat_template]
    if args.chat_template_kwargs is not None:
        kwargs = json.loads(args.chat_template_kwargs)
        if not isinstance(kwargs, dict):
            raise ValueError('--chat-template-kwargs requires a JSON object')
        argv += ['--chat-template-kwargs', json.dumps(kwargs, ensure_ascii=False)]
    if args.reasoning_effort is not None:
        if not args.reasoning_effort.strip():
            raise ValueError('--reasoning-effort must not be empty')
        argv += ['--reasoning-effort', args.reasoning_effort]
    if args.jinja:
        argv += ['--jinja']
    if args.dry_run:
        print(json.dumps(dict(argv=argv, environment={k: env[k] for k in
                         ('CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER', library_key)}), indent=2))
        return
    # Resolve shared-library failures before stopping an existing working service.
    subprocess.run([str(binary), '--help'], env=env, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, check=True, timeout=30)
    launch(args, binary, argv, env, port)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'launch failed: {exc}', file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(os.fsdecode(exc.stderr), file=sys.stderr)
        sys.exit(1)
