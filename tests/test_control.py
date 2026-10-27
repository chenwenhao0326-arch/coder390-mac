import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import control


class Process:
    def poll(self):
        return None


class FakeSupervisor(control.Supervisor):
    def _start(self, settings):
        self.started.append(copy.deepcopy(settings))
        if settings['context'] in self.fail_contexts:
            raise RuntimeError('controlled load failure')
        self.proc = Process()
        self.backend_port = 1

    def _stop(self):
        if self.proc:
            self.stopped += 1
        self.proc, self.backend_port = None, None

    def _watch(self):
        self.closed.wait()


class ControlChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data, self.ui = self.root / 'data', self.root / 'ui'
        self.data.mkdir()
        self.ui.mkdir()
        (self.ui / 'index.html').write_text('<html><body>chat</body></html>')
        (self.root / 'secret').write_text('outside')
        self.mmproj = self.root / 'mmproj-test.gguf'
        self.mmproj.write_bytes(b'projector fixture')
        self.contract = {'filename': self.mmproj.name, 'size': self.mmproj.stat().st_size,
                         'sha256': hashlib.sha256(self.mmproj.read_bytes()).hexdigest()}
        (self.root / 'projector-contract.json').write_text(json.dumps(self.contract))
        self.model = {'path': str(self.root / 'model.gguf'), 'size': 16 * 2**30}
        self.profile = {'name': 'compact32', 'context': 8192}
        self.machine = {'memory_bytes': 32 * 2**30}
        self.args = types.SimpleNamespace(context=None, mmproj=None, no_vision=True, port=0)
        self.manager = self.new_manager()
        self.manager.boot()
        self.wait_done()

    def new_manager(self):
        manager = FakeSupervisor(self.root, self.data, Path('engine'), self.ui, self.model,
                                 self.profile, self.machine, self.args, lambda *a: [], lambda: {})
        manager.started, manager.stopped, manager.fail_contexts = [], 0, set()
        return manager

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def wait_done(self):
        self.manager.worker.join(timeout=2)
        self.assertFalse(self.manager.worker.is_alive())

    def request(self, **changes):
        return {'version': self.manager.version, 'settings': {**self.manager.config, **changes}}

    def test_32gb_vision_and_context_change_persist(self):
        self.manager.apply(self.request(context=16384, vision_enabled=True, mmproj_path=str(self.mmproj)))
        self.wait_done()
        self.assertEqual(self.manager.phase, 'ready')
        self.assertTrue(self.manager.config['vision_enabled'])
        self.assertEqual(self.manager.version, 1)
        self.manager.close()
        self.args.no_vision = False
        self.manager = self.new_manager()
        self.manager.boot()
        self.wait_done()
        self.assertEqual(self.manager.config['context'], 16384)
        self.assertTrue(self.manager.config['vision_enabled'])

    def test_invalid_projector_does_not_stop_current_model(self):
        old = copy.deepcopy(self.manager.config)
        with self.assertRaises(control.Rejected):
            self.manager.apply(self.request(vision_enabled=True, mmproj_path='missing.gguf'))
        self.assertEqual(self.manager.stopped, 0)
        self.assertEqual(self.manager.config, old)
        self.assertEqual(self.manager.phase, 'ready')

    def test_checksum_failure_keeps_model_running(self):
        self.mmproj.write_bytes(b'x' * self.contract['size'])
        with self.assertRaisesRegex(control.Rejected, 'SHA256'):
            self.manager.apply(self.request(vision_enabled=True, mmproj_path=str(self.mmproj)))
        self.assertEqual(self.manager.stopped, 0)

    def test_verified_projector_is_not_rehashed_unchanged(self):
        control.projector_check(str(self.mmproj), self.data, self.contract)
        with mock.patch.object(control, 'digest', side_effect=AssertionError('unnecessary rehash')):
            self.assertEqual(control.projector_check(str(self.mmproj), self.data, self.contract), str(self.mmproj.resolve()))

    def test_busy_request_blocks_reconfiguration(self):
        self.manager.active_requests = 1
        with self.assertRaises(control.Rejected) as caught:
            self.manager.apply(self.request(context=16384))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.manager.stopped, 0)

    def test_stale_version_cannot_overwrite_new_settings(self):
        stale = self.request(context=4096)
        self.manager.apply(self.request(context=16384))
        self.wait_done()
        with self.assertRaises(control.Rejected) as caught:
            self.manager.apply(stale)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.manager.config['context'], 16384)

    def test_failed_load_restores_last_working_configuration(self):
        self.manager.fail_contexts = {16384}
        self.manager.apply(self.request(context=16384))
        self.wait_done()
        self.assertEqual(self.manager.phase, 'ready')
        self.assertEqual(self.manager.config['context'], 8192)
        self.assertEqual(self.manager.version, 0)
        self.assertIn('已恢复', self.manager.error)
        self.assertEqual([s['context'] for s in self.manager.started], [8192, 16384, 8192])

    def test_memory_estimate_rejects_before_stopping(self):
        self.manager.model = {**self.model, 'size': 24 * 2**30}
        with self.assertRaisesRegex(control.Rejected, '内存预算'):
            self.manager.apply(self.request(context=16384))
        self.assertEqual(self.manager.stopped, 0)

    def test_identical_config_does_not_restart(self):
        result = self.manager.apply(self.request())
        self.assertFalse(result['changed'])
        self.assertEqual(self.manager.stopped, 0)

    def test_boolean_is_not_accepted_as_context(self):
        with self.assertRaises(control.Rejected):
            self.manager.apply(self.request(context=True))


class HttpChecks(ControlChecks):
    # Only HTTP cases in this class; do not rerun the inherited lifecycle cases.
    def setUp(self):
        super().setUp()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), control.handler_for(self.manager))
        self.manager.args.port = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        super().tearDown()

    def http_request(self, path, body=None, headers=None, method=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        connection.request(method or ('POST' if body is not None else 'GET'), path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_http_cross_origin_and_invalid_token_rejected(self):
        status, _, _ = self.http_request('/_coder390/status', headers={'Origin': 'https://untrusted.example'})
        self.assertEqual(status, 403)
        status, _, _ = self.http_request('/_coder390/apply', '{}', {'Content-Type': 'application/json', 'X-Coder390-Token': 'wrong'})
        self.assertEqual(status, 403)
        self.assertEqual(self.manager.stopped, 0)

    def test_http_foreign_host_rejected(self):
        status, _, _ = self.http_request('/_coder390/status', headers={'Host': 'untrusted.example'})
        self.assertEqual(status, 403)

    def test_http_user_navigation_allowed_but_foreign_iframe_rejected(self):
        headers = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
                   'Sec-Fetch-Dest': 'document', 'Sec-Fetch-User': '?1'}
        status, body, _ = self.http_request('/', headers=headers)
        self.assertEqual(status, 200)
        self.assertIn(b'/_coder390/settings.js', body)
        headers['Sec-Fetch-Dest'] = 'iframe'
        status, _, _ = self.http_request('/', headers=headers)
        self.assertEqual(status, 403)

    def test_http_static_traversal_rejected(self):
        status, body, _ = self.http_request('/%2e%2e/secret')
        self.assertEqual(status, 404)
        self.assertNotIn(b'outside', body)

    def test_http_native_index_contains_settings_button_script(self):
        status, body, _ = self.http_request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'/_coder390/settings.js', body)

    def test_http_oversized_body_rejected_without_reading_it(self):
        status, _, _ = self.http_request('/v1/chat/completions', b'', {'Content-Length': str(control.MAX_BODY + 1)})
        self.assertEqual(status, 413)

    def test_http_streaming_is_not_buffered_until_completion_and_busy_blocks_apply(self):
        first_sent, release, received = threading.Event(), threading.Event(), []
        manager = self.manager
        class Backend(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *a):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                received.append((body, self.headers.get('Authorization')))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Transfer-Encoding', 'chunked')
                self.send_header('Connection', 'close')
                self.end_headers()
                chunk = b'data: first\n\n'
                self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
                self.wfile.flush()
                first_sent.set()
                release.wait(3)
                chunk = b'data: [DONE]\n\n'
                self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n0\r\n\r\n')
                self.wfile.flush()
                self.close_connection = True
        backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        backend_thread = threading.Thread(target=backend.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        backend_thread.start()
        self.manager.backend_port = backend.server_port
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=2)
        try:
            body = json.dumps({'image': 'non-sensitive-fixture', 'stream': True}).encode()
            connection.request('POST', '/v1/chat/completions', body=body, headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.readline(), b'data: first\n')
            self.assertFalse(release.is_set())
            status, _, _ = self.http_request('/_coder390/apply', json.dumps(self.request(context=16384)),
                {'Content-Type': 'application/json', 'X-Coder390-Token': self.manager.token})
            self.assertEqual(status, 409)
            release.set()
            self.assertIn(b'[DONE]', response.read())
            self.assertEqual(received, [(body, 'Bearer ' + manager.api_key)])
        finally:
            release.set()
            connection.close()
            backend.shutdown()
            backend.server_close()
            backend_thread.join()


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(ControlChecks))
    for name in sorted(n for n in HttpChecks.__dict__ if n.startswith('test_http_')):
        suite.addTest(HttpChecks(name))
    return suite


if __name__ == '__main__':
    unittest.main()
