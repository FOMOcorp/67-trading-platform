import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'collectors'))
from app.config import load_settings


class ConfigTests(unittest.TestCase):
    def test_binance_only_defaults(self):
        with patch.dict(os.environ,{},clear=True):
            settings=load_settings()
        self.assertEqual(settings.symbols,('BTCUSDT',))
        self.assertEqual(settings.storage,'csv')

    def test_invalid_polling_interval_is_rejected(self):
        with patch.dict(os.environ,{'COLLECT_CANDLES_INTERVAL_SECONDS':'0'},clear=True):
            with self.assertRaisesRegex(ValueError,'positive'):
                load_settings()
