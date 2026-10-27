import io
import json
from pathlib import Path
import socket
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coder390 as app
from lib import direct_transport


class PortableChecks(unittest.TestCase):
    def test_32gb_profile_and_large_model_guard(self):
        profile = app.choose_profile('auto', 32 * 2**30, 17 * 2**30)
        self.assertEqual(profile['name'], 'compact32')
        self.assertEqual(profile['context'], 8192)
        with self.assertRaises(ValueError):
            app.choose_profile('auto', 32 * 2**30, 27 * 2**30)
        with self.assertRaises(ValueError):
            app.choose_profile('compact32', 32 * 2**30, 17 * 2**30, 262144)

    def test_wrong_tokenizer_is_rejected_before_inference(self):
        metadata = {'general.architecture': 'qwen35', 'qwen35.nextn_predict_layers': 1,
                    'tokenizer.ggml.tokens': ['wrong']}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'model.gguf'
            p.write_bytes(b'GGUF')
            with mock.patch.object(app, 'read_metadata', return_value=(metadata, {}, 4)):
                with self.assertRaisesRegex(ValueError, 'tokenizer_sha256'):
                    app.model_check(p)

    def test_truncated_gguf_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'broken.gguf'
            p.write_bytes(b'GGUF\x03\x00\x00\x00')
            with self.assertRaises(ValueError):
                app.model_check(p)

    def test_archive_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                item = tarfile.TarInfo('../outside')
                item.size = 1
                tar.addfile(item, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                app.unpack_tar(archive, Path(tmp) / 'out')
            self.assertFalse((Path(tmp) / 'outside').exists())

    def test_archive_external_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                item = tarfile.TarInfo('link')
                item.type = tarfile.SYMTYPE
                item.linkname = '../../outside'
                tar.addfile(item)
            with self.assertRaises(ValueError):
                app.unpack_tar(archive, Path(tmp) / 'out')

    def test_physical_interface_is_configurable(self):
        old = direct_transport.DIRECT_INTERFACE
        try:
            self.assertEqual(direct_transport.configure('en0'), 'en0')
            self.assertEqual(direct_transport.configure('en2'), 'en2')
            with self.assertRaises(ValueError):
                direct_transport.configure('utun3')
        finally:
            direct_transport.DIRECT_INTERFACE = old

    def test_tunnel_route_never_falls_back_to_proxy(self):
        old = direct_transport.DIRECT_INTERFACE
        try:
            direct_transport.configure('en0')
            response = mock.Mock(returncode=0, stdout='interface: utun3\ngateway: 1.1.1.1\n')
            with mock.patch.object(direct_transport.subprocess, 'run', return_value=response):
                with self.assertRaises(direct_transport.DirectRouteError):
                    direct_transport.direct_route('1.1.1.1')
        finally:
            direct_transport.DIRECT_INTERFACE = old

    def test_busy_port_does_not_start_or_stop_another_service(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen()
            args = mock.Mock(model='existing.gguf', profile='compact32', context=None,
                             mmproj=None, port=busy.getsockname()[1])
            with mock.patch.object(app, 'DATA', Path(tmp)), \
                 mock.patch.object(app, 'hardware', return_value={'memory_bytes': 32 * 2**30}), \
                 mock.patch.object(app, 'model_check', return_value={'path': 'existing.gguf', 'size': 17 * 2**30}), \
                 mock.patch.object(app, 'setup') as setup:
                with self.assertRaises(OSError):
                    app.run(args)
                setup.assert_not_called()
            with socket.create_connection(busy.getsockname(), timeout=1):
                pass


if __name__ == '__main__':
    unittest.main()
