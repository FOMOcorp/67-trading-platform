import csv
import gzip
from pathlib import Path
import sys
import tempfile
import unittest
import json
import argparse
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'collectors'))
import download_history as history
from build_dataset import build

START=1735689600000


class DatasetTests(unittest.TestCase):
    def test_default_is_spot_and_old_run_can_drop_futures_without_redownload(self):
        self.assertEqual(history.DEFAULT,('spot','metrics','funding'))
        self.assertNotIn('futures',history.DEFAULT)
        self.assertNotIn('basis',history.DEFAULT)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            previous=dict(start='2025-01-01',end='2025-01-02',symbols=['BTCUSDT'],
                datasets=['spot','futures','basis'],schema_version=2)
            (root/'coverage.json').write_text(json.dumps(dict(config=previous,entries={})))
            args=argparse.Namespace(output=folder,start='2025-01-01',end='2025-01-02',
                symbols=['BTCUSDT'],datasets=['spot'],years=5,retry_missing=False,no_csv=True)
            with patch.object(history,'archive',return_value=None) as archive,patch.object(history,'api_rows',return_value=iter([])):
                history.run(args)
            self.assertEqual(archive.call_count,1)
            self.assertEqual(archive.call_args.args[1],'spot')
            self.assertEqual(json.loads((root/'coverage.json').read_text())['config']['datasets'],['spot'])

    def test_previous_reference_download_run_migrates_to_three_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            previous=dict(start='2025-01-01',end='2025-01-02',symbols=['BTCUSDT'],
                datasets=['spot','metrics','funding','mark','index','premium'],schema_version=2)
            (root/'coverage.json').write_text(json.dumps(dict(config=previous,entries={
                'BTCUSDT/mark/2025-01-01':{'status':'missing'}})))
            args=argparse.Namespace(output=folder,start='2025-01-01',end='2025-01-02',
                symbols=['BTCUSDT'],datasets=list(history.DEFAULT),years=5,retry_missing=False,no_csv=True)
            with patch.object(history,'archive',return_value=None) as archive,patch.object(history,'api_rows',return_value=iter([])):
                history.run(args)
            self.assertEqual({c.args[1] for c in archive.call_args_list},{'spot','metrics'})
            state=json.loads((root/'coverage.json').read_text())
            self.assertEqual(state['config']['datasets'],list(history.DEFAULT))
            self.assertIn('BTCUSDT/mark/2025-01-01',state['retired_entries'])
    def write(self,root,kind,rows):
        path=root/'BTCUSDT'/kind/'2025-01-01.csv.gz'
        path.parent.mkdir(parents=True,exist_ok=True)
        with gzip.open(path,'wt',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_wide_csv_uses_no_future_prices_or_future_funding(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.write(root,'spot',[dict(timestamp=START,datetime_utc='2025-01-01',close='100'),
                dict(timestamp=START+60000,datetime_utc='2025-01-01',close='101')])
            self.write(root,'futures',[dict(timestamp=START+60000,close='102')])
            self.write(root,'mark',[dict(timestamp=START,close='999')])
            self.write(root,'metrics',[dict(timestamp=START,open_interest_base='10',
                open_interest_quote='1000',global_long_short_ratio='2')])
            self.write(root,'funding',[dict(timestamp=START+60015,funding_rate='.001')])
            build(root,'BTCUSDT',{'entries':{}})
            with (root/'BTCUSDT_dataset_1m.csv').open(newline='') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),2)
            self.assertNotIn('futures_close',rows[0])
            self.assertEqual(rows[0]['funding_rate'],'')
            self.assertFalse(any(k.startswith('futures_') for k in rows[1]))
            self.assertEqual(rows[1]['funding_rate'],'.001')
            self.assertGreater(float(rows[1]['funding_age_seconds']),0)
            self.assertFalse(any(k.startswith(('mark_','index_','premium_','global_long')) for k in rows[1]))
            self.assertEqual(rows[0]['open_interest_base'],'10')
            self.assertIn('taker_buy_base_volume',rows[0])
            self.assertIn('taker_sell_quote_volume',rows[0])
            self.assertIn('number_of_trades',rows[0])

    def test_stale_oi_expires(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.write(root,'spot',[dict(timestamp=START+900000,close='100')])
            self.write(root,'metrics',[dict(timestamp=START,open_interest_base='10')])
            report=build(root,'BTCUSDT',{'entries':{}})
            self.assertEqual(report['missing_rows']['metrics'],1)
            with (root/'BTCUSDT_dataset_1m.csv').open(newline='') as stream:
                row=next(csv.DictReader(stream))
            self.assertEqual(row['open_interest_base'],'')
            self.assertEqual(float(row['oi_age_seconds']),960)
            build(root,'BTCUSDT',{'entries':{}},oi_max_age_minutes=0)
            with (root/'BTCUSDT_dataset_1m.csv').open(newline='') as stream:
                self.assertEqual(next(csv.DictReader(stream))['open_interest_base'],'10')

    def test_oi_from_future_cannot_fill_previous_minute(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.write(root,'spot',[dict(timestamp=START,close='100')])
            self.write(root,'metrics',[dict(timestamp=START+60001,open_interest_base='10')])
            build(root,'BTCUSDT',{'entries':{}})
            with (root/'BTCUSDT_dataset_1m.csv').open(newline='') as stream:
                row=next(csv.DictReader(stream))
            self.assertEqual(row['open_interest_base'],'')
            self.assertEqual(row['oi_timestamp'],'')
