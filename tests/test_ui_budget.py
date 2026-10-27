import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coder390 as app


class UiBudgetChecks(unittest.TestCase):
    def test_browser_default_and_api_default_share_compact_budget(self):
        profile = app.choose_profile('compact32', 32 * 2**30, 17 * 2**30)
        command = app.command(Path('engine'), Path('ui'), {'path': 'local model.gguf'},
                              profile, mock.Mock(port=8793, mmproj=None))
        ui = json.loads(command[command.index('--ui-config') + 1])
        self.assertEqual(ui['max_tokens'], 2048)
        self.assertEqual(command[command.index('--n-predict') + 1], '2048')


if __name__ == '__main__':
    unittest.main()
