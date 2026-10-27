import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coder390 as app


class NativeCacheChecks(unittest.TestCase):
    def test_m2_binary_cache_is_not_accepted_as_m1_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'engine.json').write_text(json.dumps({'machine': {'chip': 'Apple M2 Ultra'}}))
            with mock.patch.object(app, 'DATA', Path(tmp)), \
                 mock.patch.object(app, 'hardware', return_value={'chip': 'Apple M1 Max'}):
                self.assertIsNone(app.engine_ready())

    def test_foreign_cache_is_preserved_instead_of_reconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / 'engine.json'
            original = json.dumps({'machine': {'chip': 'Apple M2 Ultra'}})
            receipt.write_text(original)
            with mock.patch.object(app, 'DATA', Path(tmp)), \
                 mock.patch.object(app, 'hardware', return_value={'chip': 'Apple M1 Max'}):
                with self.assertRaisesRegex(RuntimeError, '另一种芯片'):
                    app.setup(mock.Mock())
            self.assertEqual(receipt.read_text(), original)


if __name__ == '__main__':
    unittest.main()
