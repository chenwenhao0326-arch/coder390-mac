"""Local UI supervisor, bounded model restart, and streaming loopback proxy."""
import contextlib
import copy
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from .util import digest, write

VERSION = '0.2.0'
PREFIX = '/_coder390/'
MAX_BODY = 32 * 2**20
CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'authorization',
               'accept-encoding', 'access-control-allow-origin', 'access-control-allow-credentials'}


class Rejected(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def projector_check(path, data, contract):
    if not isinstance(path, str) or not path.strip():
        raise Rejected('开启看图需要填写本地 mmproj 文件路径')
    candidate = Path(path).expanduser().resolve(strict=True)
    if not candidate.is_file() or candidate.suffix.lower() != '.gguf':
        raise Rejected('视觉投影必须是本地 .gguf 文件')
    info = candidate.stat()
    if info.st_size != contract['size']:
        raise Rejected('视觉投影与本版本不匹配，请使用 ' + contract['filename'])
    receipt_path = data / 'projector-verified.json'
    stamp = {'path': str(candidate), 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
             'sha256': contract['sha256']}
    try:
        receipt = json.loads(receipt_path.read_text())
    except (FileNotFoundError, ValueError):
        receipt = None
    if receipt != stamp:
        if digest(candidate) != contract['sha256']:
            raise Rejected('视觉投影 SHA256 校验失败，原模型未改变')
        write(receipt_path, stamp)
    return str(candidate)


def discover_projector(model_path, contract):
    candidate = Path(model_path).parent / contract['filename']
    return str(candidate) if candidate.is_file() else ''


def context_options(profile):
    return [4096, 8192, 16384] if profile['name'] == 'compact32' else [4096, 8192, 16384, 32768, 65536, 131072, 262144]


def memory_estimate(model, profile, machine, settings, contract):
    # Planning estimate, not an allocator guarantee or measured GPU footprint.
    ram = min(machine['memory_bytes'], 32 * 2**30) if profile['name'] == 'compact32' else machine['memory_bytes']
    budget = ram - (6 if ram <= 36 * 2**30 else 8) * 2**30
    overhead = (4 if profile['name'] == 'compact32' else 8) * 2**30
    kv = settings['context'] * 17 * 4 * 256 * 2 * 2
    vision = contract['size'] + 4 * 2**30 if settings['vision_enabled'] else 0
    return {'estimated_bytes': model['size'] + overhead + kv + vision, 'budget_bytes': budget}


class Supervisor:
    def __init__(self, root, data, binary, ui, model, profile, machine, args, make_command, clean_environment):
        self.root, self.data, self.binary, self.ui = root, data, binary, ui
        self.model, self.profile, self.machine, self.args = model, profile, machine, args
        self.make_command, self.clean_environment = make_command, clean_environment
        self.contract = json.loads((root / 'projector-contract.json').read_text())
        self.token = secrets.token_urlsafe(32)
        self.api_key = secrets.token_urlsafe(32)
        self.key_file = data / 'backend-api-key'
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(self.api_key)
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self.closed = threading.Event()
        self.proc = None
        self.backend_port = None
        self.active_requests = 0
        self.version = 0
        self.phase = 'loading'
        self.error = ''
        self.peak_rss = 0
        self.last_rss = 0
        self.operation = None
        self.worker = None
        self.log = None
        self.config = self.initial_settings()
        self.monitor = threading.Thread(target=self._watch, daemon=True)
        self.monitor.start()

    def initial_settings(self):
        path = discover_projector(self.model['path'], self.contract)
        settings = {'context': self.profile['context'], 'vision_enabled': bool(path), 'mmproj_path': path}
        try:
            saved = json.loads((self.data / 'user-settings.json').read_text())
            if saved.get('model_path') == self.model['path'] and saved.get('profile') == self.profile['name']:
                candidate = saved['settings']
                maximum = 16384 if self.profile['name'] == 'compact32' else 262144
                if type(candidate['context']) is int and 4096 <= candidate['context'] <= maximum:
                    settings = {k: candidate[k] for k in settings}
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            pass
        if self.args.context is not None:
            settings['context'] = self.args.context
        if self.args.mmproj:
            settings.update(mmproj_path=str(Path(self.args.mmproj).expanduser()), vision_enabled=True)
        if self.args.no_vision:
            settings['vision_enabled'] = False
        return settings

    def validate(self, candidate):
        if not isinstance(candidate, dict) or set(candidate) != {'context', 'vision_enabled', 'mmproj_path'}:
            raise Rejected('设置字段不完整或包含未知字段')
        maximum = 16384 if self.profile['name'] == 'compact32' else 262144
        if type(candidate['context']) is not int or not 4096 <= candidate['context'] <= maximum:
            raise Rejected('该配置不支持所选上下文容量')
        if type(candidate['vision_enabled']) is not bool or not isinstance(candidate['mmproj_path'], str):
            raise Rejected('看图开关或路径格式不正确')
        settings = copy.deepcopy(candidate)
        if len(settings['mmproj_path']) > 4096:
            raise Rejected('路径过长')
        if settings['vision_enabled']:
            try:
                settings['mmproj_path'] = projector_check(settings['mmproj_path'], self.data, self.contract)
            except (OSError, ValueError) as exc:
                raise Rejected('无法使用视觉投影：' + str(exc)) from exc
        estimate = memory_estimate(self.model, self.profile, self.machine, settings, self.contract)
        if estimate['estimated_bytes'] > estimate['budget_bytes']:
            raise Rejected('所选配置超出保守内存预算，请减小上下文或关闭看图')
        return settings

    def snapshot(self, include_token=False):
        with self.lock:
            result = {'phase': self.phase, 'error': self.error, 'version': self.version,
                      'operation': self.operation, 'settings': copy.deepcopy(self.config),
                      'profile': self.profile['name'], 'context_options': sorted(set(context_options(self.profile) + [self.config['context']])),
                      'model_name': Path(self.model['path']).name, 'memory_gib': self.machine['memory_bytes'] / 2**30,
                      'active_requests': self.active_requests, 'peak_rss_gib': self.peak_rss / 2**30,
                      'image_max_tokens': 1024 if self.profile['name'] == 'compact32' else 4096,
                      'projector_filename': self.contract['filename'],
                      'model_running': self.proc is not None and self.proc.poll() is None}
            result['memory_plan'] = memory_estimate(self.model, self.profile, self.machine, self.config, self.contract)
            if include_token:
                result['token'] = self.token
            return result

    def _save(self):
        write(self.data / 'user-settings.json', {'model_path': self.model['path'], 'profile': self.profile['name'],
                                               'settings': self.config, 'saved_at': time.time()})

    def _stop(self):
        with self.lifecycle_lock:
            self._stop_owned()

    def _stop_owned(self):
        with self.lock:
            proc = self.proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        with self.lock:
            if self.proc is proc:
                self.proc = None
                self.backend_port = None
                self.last_rss = 0
        if self.log:
            self.log.close()
            self.log = None

    def _start(self, settings):
        with self.lifecycle_lock:
            self._start_owned(settings)

    def _start_owned(self, settings):
        if self.closed.is_set():
            raise RuntimeError('管理服务正在退出')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        args = copy.copy(self.args)
        args.port = port
        args.mmproj = settings['mmproj_path'] if settings['vision_enabled'] else None
        profile = {**self.profile, 'context': settings['context']}
        command = self.make_command(self.binary, self.ui, self.model, profile, args)
        command += ['--api-key-file', str(self.key_file)]
        if settings['vision_enabled']:
            command += ['--image-max-tokens', '1024' if self.profile['name'] == 'compact32' else '4096']
        self.log = (self.data / 'server.log').open('a', buffering=1)
        self.log.write('\n=== Model configuration restart ===\n')
        proc = subprocess.Popen(command, env=self.clean_environment(), stdin=subprocess.DEVNULL,
                                stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        with self.lock:
            self.proc, self.backend_port = proc, port
        write(self.data / 'active.json', {'pid': proc.pid, 'public_port': self.args.port, 'backend_port': port,
                                         'binary': str(self.binary), 'supervisor_pid': os.getpid()})
        write(self.data / 'last-run.json', {'command': command, 'profile': profile, 'model': self.model,
                                          'hardware': self.machine, 'settings': settings})
        budget = memory_estimate(self.model, self.profile, self.machine, settings, self.contract)['budget_bytes']
        for _ in range(180):
            if self.closed.is_set() or proc.poll() is not None:
                raise RuntimeError('模型启动失败或被停止，详细原因见 .coder390/server.log')
            rss = self._rss(proc)
            with self.lock:
                self.last_rss = rss
                self.peak_rss = max(self.peak_rss, rss)
            if rss > budget:
                raise RuntimeError('加载期间超过进程内存预算，请减小上下文')
            try:
                request = urllib.request.Request(f'http://127.0.0.1:{port}/health',
                                                 headers={'Authorization': 'Bearer ' + self.api_key})
                with CLIENT.open(request, timeout=1) as response:
                    if json.load(response).get('status') == 'ok':
                        owner = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(proc.pid),
                                                f'-iTCP:{port}', '-sTCP:LISTEN'], capture_output=True)
                        if owner.returncode == 0:
                            return
            except (OSError, ValueError):
                pass
            self.closed.wait(1)
        raise RuntimeError('模型加载超过180秒，请检查日志')

    @staticmethod
    def _rss(proc):
        result = subprocess.run(['ps', '-p', str(proc.pid), '-o', 'rss='], capture_output=True, text=True)
        return int(result.stdout.strip() or '0') * 1024

    def _watch(self):
        while not self.closed.wait(2):
            with self.lock:
                proc, phase = self.proc, self.phase
                settings = copy.deepcopy(self.config)
            if proc is None or phase != 'ready':
                continue
            if proc.poll() is not None:
                with self.lock:
                    if self.proc is proc and self.phase == 'ready':
                        self.phase, self.error = 'error', '模型进程已退出，请检查日志后重新应用设置'
                continue
            rss = self._rss(proc)
            with self.lock:
                self.last_rss = rss
                self.peak_rss = max(self.peak_rss, rss)
                exceeded = rss > memory_estimate(self.model, self.profile, self.machine, settings, self.contract)['budget_bytes']
                if exceeded and self.proc is proc and self.phase == 'ready':
                    self.phase, self.error = 'error', '进程内存超过预算，已停止模型；请减小上下文或图片尺寸后应用'
                else:
                    exceeded = False
            if exceeded:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)

    def boot(self):
        self.worker = threading.Thread(target=self._initial_load, daemon=True)
        self.worker.start()

    def _initial_load(self):
        try:
            settings = self.validate(self.config)
            self._start(settings)
            with self.lock:
                self.config = settings
                self.phase, self.error = 'ready', ''
                self._save()
        except Exception as exc:
            self._stop()
            with self.lock:
                self.phase, self.error = 'error', str(exc)

    def apply(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'version', 'settings'}:
            raise Rejected('请求格式不正确')
        with self.lock:
            if self.closed.is_set() or self.phase in ('loading', 'restarting') or self.active_requests:
                raise Rejected('模型正在加载或生成，请完成当前请求后再应用', 409)
            if type(payload['version']) is not int or payload['version'] != self.version:
                raise Rejected('设置已被另一个页面更新，请重新打开面板', 409)
        candidate = self.validate(payload['settings'])
        with self.lock:
            if self.phase in ('loading', 'restarting') or self.active_requests or payload['version'] != self.version:
                raise Rejected('模型状态已变化，请稍后重试', 409)
            if candidate == self.config and self.phase == 'ready':
                return {'changed': False, 'operation': self.operation}
            previous = copy.deepcopy(self.config)
            had_working = self.phase == 'ready' and self.proc is not None and self.proc.poll() is None
            self.phase, self.error = 'restarting', ''
            self.operation = secrets.token_hex(8)
            self.worker = threading.Thread(target=self._restart, args=(candidate, previous, had_working), daemon=True)
            self.worker.start()
            return {'changed': True, 'operation': self.operation}

    def _restart(self, candidate, previous, had_working):
        self._stop()
        try:
            self._start(candidate)
            with self.lock:
                self.config = candidate
                self._save()
                self.version += 1
                self.phase, self.error = 'ready', ''
        except Exception as exc:
            self._stop()
            message = str(exc)
            if had_working and not self.closed.is_set():
                try:
                    self._start(previous)
                    with self.lock:
                        self.config = previous
                        self.phase = 'ready'
                        self.error = '新设置未生效，已恢复原配置：' + message
                    return
                except Exception as rollback:
                    self._stop()
                    message += '；恢复原配置也失败：' + str(rollback)
            with self.lock:
                self.phase, self.error = 'error', message

    def begin_request(self, track=True):
        with self.lock:
            if self.phase != 'ready' or self.proc is None or self.proc.poll() is not None:
                raise Rejected('模型正在重启或尚未就绪，请稍后重试', 503)
            if track:
                self.active_requests += 1
            return self.backend_port

    def end_request(self):
        with self.lock:
            self.active_requests -= 1

    def close(self):
        self.closed.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=5)
        self._stop()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=25)
        self.monitor.join(timeout=3)
        self._stop()
        self.key_file.unlink(missing_ok=True)
        (self.data / 'active.json').unlink(missing_ok=True)


def handler_for(manager):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            self.connection.settimeout(60)

        def log_message(self, *args):
            pass

        def local_request(self):
            hosts = self.headers.get_all('Host', [])
            allowed = {f'127.0.0.1:{manager.args.port}', f'localhost:{manager.args.port}'}
            if len(hosts) != 1 or hosts[0] not in allowed:
                raise Rejected('Invalid Host', 403)
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + hosts[0]:
                raise Rejected('Cross-origin request rejected', 403)
            user_navigation = (self.command in ('GET', 'HEAD') and
                               self.headers.get('Sec-Fetch-Mode') == 'navigate' and
                               self.headers.get('Sec-Fetch-Dest') == 'document' and
                               self.headers.get('Sec-Fetch-User') == '?1')
            if self.headers.get('Sec-Fetch-Site') == 'cross-site' and not user_navigation:
                raise Rejected('Cross-site request rejected', 403)
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme or parsed.netloc or not self.path.startswith('/'):
                raise Rejected('Invalid request target')
            return parsed.path

        def send(self, status, body, content_type='application/json; charset=utf-8', cache='no-store'):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', cache)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            if self.command != 'HEAD':
                self.wfile.write(body)

        def read_body(self, maximum):
            if self.headers.get('Transfer-Encoding'):
                raise Rejected('Chunked request bodies are not supported', 411)
            values = self.headers.get_all('Content-Length', [])
            if len(values) != 1 or not values[0].isdigit():
                raise Rejected('Content-Length required', 411)
            length = int(values[0])
            if length > maximum:
                raise Rejected('请求太大，请缩小图片后重试', 413)
            body = self.rfile.read(length)
            if len(body) != length:
                raise Rejected('Truncated request body')
            return body

        def handle_local(self):
            try:
                path = self.local_request()
                if path == PREFIX + 'status' and self.command == 'GET':
                    self.send(200, manager.snapshot(include_token=True))
                    return
                if path == PREFIX + 'apply' and self.command == 'POST':
                    token = self.headers.get('X-Coder390-Token', '')
                    if not hmac.compare_digest(token, manager.token):
                        raise Rejected('Invalid control token', 403)
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise Rejected('application/json required', 415)
                    payload = json.loads(self.read_body(16384))
                    self.send(202, manager.apply(payload))
                    return
                if path == PREFIX + 'settings.js' and self.command in ('GET', 'HEAD'):
                    self.send(200, (manager.root / 'ui-controls/settings.js').read_bytes(), 'text/javascript; charset=utf-8')
                    return
                if path.startswith(PREFIX):
                    raise Rejected('Not found', 404)
                if path == '/sw.js' and self.command in ('GET', 'HEAD'):
                    self.send(200, (manager.root / 'ui-controls/sw.js').read_bytes(), 'text/javascript; charset=utf-8')
                    return
                if self.command in ('GET', 'HEAD'):
                    relative = urllib.parse.unquote(path).lstrip('/') or 'index.html'
                    target = (manager.ui / relative).resolve()
                    if manager.ui.resolve() not in target.parents:
                        raise Rejected('Not found', 404)
                    if manager.ui.resolve() in target.parents and target.is_file():
                        body = target.read_bytes()
                        if target.name == 'index.html':
                            body = body.replace(b'</body>', b'<script src="/_coder390/settings.js" defer></script></body>')
                        self.send(200, body, mimetypes.guess_type(str(target))[0] or 'application/octet-stream',
                                  'no-store' if target.suffix in ('.html', '.json') else 'max-age=3600')
                        return
                self.forward()
            except Rejected as exc:
                self.send(exc.status, {'error': str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                self.send(400, {'error': 'Invalid request: ' + str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def forward(self):
            body = self.read_body(MAX_BODY) if self.command in ('POST', 'PUT', 'PATCH') else None
            tracked = self.command in ('POST', 'PUT', 'PATCH', 'DELETE')
            port = manager.begin_request(track=tracked)
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=600)
            sent = False
            try:
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
                headers.update(Authorization='Bearer ' + manager.api_key, Connection='close',
                               Host=f'127.0.0.1:{port}', **{'Accept-Encoding': 'identity'})
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in HOP_HEADERS:
                        self.send_header(key, value)
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                sent = True
                if self.command != 'HEAD':
                    while True:
                        chunk = response.read1(16384)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (OSError, http.client.HTTPException):
                if not sent:
                    self.send(502, {'error': '模型连接中断，请查看模型设置或服务日志'})
            finally:
                connection.close()
                if tracked:
                    manager.end_request()

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = handle_local

        def do_OPTIONS(self):
            self.send(403, {'error': 'Cross-origin access is disabled'})

    return Handler


def serve(manager, open_browser=True):
    server = ThreadingHTTPServer(('127.0.0.1', manager.args.port), handler_for(manager))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .25}, daemon=True)
    thread.start()
    manager.boot()
    url = f'http://127.0.0.1:{manager.args.port}/?c390-managed={VERSION}'
    print('管理页已启动，模型正在加载：' + url + ' （Ctrl+C关闭）', flush=True)
    if open_browser:
        subprocess.run(['/usr/bin/open', url], check=False)
    try:
        while thread.is_alive():
            thread.join(timeout=.5)
    finally:
        manager.closed.set()
        server.shutdown()
        server.server_close()
        manager.close()
