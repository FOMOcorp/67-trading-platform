import argparse
import asyncio
import csv
import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collectors'))
import download_history as history
from market_data import candle_values
from app.config import Settings
from app.csv_db import CSVDB
from app.exchanges.binance import BinanceFuturesExchange, BinanceSpotExchange
from test_export_history import START, candle


class NormalizationTests(unittest.TestCase):
    def test_exact_sell_volume_and_imbalance(self):
        values = candle_values(candle(START))
        self.assertEqual(values['taker_sell_base_volume'], '1.50000000')
        self.assertEqual(values['trade_imbalance'], '-0.2')
        self.assertEqual(values['number_of_trades'], 42)

    def test_zero_volume_has_no_invented_imbalance(self):
        row = candle(START)
        row[5] = row[7] = row[9] = row[10] = '0'
        self.assertEqual(candle_values(row)['trade_imbalance'], '')

    def test_reject_impossible_or_nonfinite_volumes(self):
        for value in ['3', '-1', 'NaN', 'Infinity']:
            row = candle(START)
            row[9] = value
            with self.assertRaises(ValueError):
                candle_values(row)

    def test_taker_side_is_opposite_maker(self):
        rows = list(history.trade_rows([
            ['1','100','2','1','1',str(START),'true'],
            ['2','100','3','2','2',str(START),'false']]))
        self.assertEqual([r['taker_side'] for r in rows], ['SELL','BUY'])
        self.assertEqual(rows[1]['quote_quantity'], '300')

    def test_spot_trade_microseconds(self):
        row = list(history.trade_rows([['1','100','2','1','1',str(START*1000),'false']]))[0]
        self.assertEqual(row['timestamp'], START)

    def test_funding_keeps_millisecond_settlement(self):
        row = next(history.funding_rows([dict(symbol='BTCUSDT',fundingTime=START+15,fundingRate='0.0001')], 'BTCUSDT'))
        self.assertEqual(row['timestamp'], START+15)

    def test_negative_premium_is_valid_and_not_available_at_open(self):
        row = candle(START)
        row[1:5] = ['-0.001','0','-0.002','-0.0005']
        result = next(history.price_rows([row], 'premium'))
        self.assertEqual(result['available_at'], START+60000)

    def test_metrics_preserve_native_time_and_all_ratios(self):
        header = ['create_time','symbol',*history.METRICS]
        values = ['2025-01-01 00:05:00','BTCUSDT','100','10000','1.1','1.2','1.3','0.9']
        row = next(history.metric_rows(iter([header,values]), 'BTCUSDT'))
        self.assertEqual(row['timestamp'], START+300000)
        self.assertEqual(row['taker_long_short_volume_ratio'], '0.9')


class PartitionTests(unittest.TestCase):
    def test_partial_partition_and_exact_missing_times(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'spot.csv.gz'
            details = history.save_rows(path, 'spot', history.price_rows([candle(START)],'spot'), START, START+120000)
            self.assertEqual(details['missing_timestamps'], [START+60000])
            self.assertEqual(details['rows'], 1)

    def test_basis_matches_same_minute_without_fill(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for kind in history.PRICE_KINDS:
                source = candle(START)
                source[1:5] = ['100','102','99','101']
                if kind == 'futures':
                    source[4] = '102'
                if kind == 'premium':
                    source[1:5] = ['0','0','0','0']
                path = root/'BTCUSDT'/kind/'2025-01-01.csv.gz'
                history.save_rows(path,kind,history.price_rows([source],kind),START,START+60000)
            row = next(history.basis_rows(root,'BTCUSDT',history.parse_date('2025-01-01')))
            self.assertAlmostEqual(float(row['futures_spot_basis_bps']), (102/101-1)*10000)
            self.assertEqual(row['available_at'],START+60000)
            # A later futures candle cannot fill a missing spot minute.
            path = root/'BTCUSDT'/'futures'/'2025-01-01.csv.gz'
            history.save_rows(path,'futures',history.price_rows([candle(START+60000)],'futures'),START,START+120000)
            rows = list(history.basis_rows(root,'BTCUSDT',history.parse_date('2025-01-01')))
            self.assertTrue(all(r['futures_spot_basis_bps']=='' for r in rows))

    def test_resume_checks_hash_and_does_not_redownload(self):
        with tempfile.TemporaryDirectory() as folder:
            args=argparse.Namespace(output=folder,start='2025-01-01',end='2025-01-02',
                years=5,symbols=['BTCUSDT'],datasets=['spot'],retry_missing=False)
            rows=[candle(START+i*60000) for i in range(1440)]
            with patch.object(history,'archive',return_value=None),patch.object(history,'api_rows',return_value=iter(rows)):
                self.assertEqual(history.run(args),0)
            with patch.object(history,'archive',side_effect=AssertionError('Unexpected download')):
                self.assertEqual(history.run(args),0)
            path=Path(folder)/'BTCUSDT/spot/2025-01-01.csv.gz'
            with path.open('ab') as stream:
                stream.write(b'corrupted')
            with self.assertRaisesRegex(ValueError,'corrupted'):
                history.run(args)

    def test_missing_oi_archive_returns_incomplete_status(self):
        with tempfile.TemporaryDirectory() as folder:
            args=argparse.Namespace(output=folder,start='2025-01-01',end='2025-01-02',
                years=5,symbols=['BTCUSDT'],datasets=['metrics'],retry_missing=False)
            with patch.object(history,'archive',return_value=None):
                self.assertEqual(history.run(args),2)
            state=json.loads((Path(folder)/'coverage.json').read_text())
            self.assertEqual(state['summary']['statuses'], {'missing':1})


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_closed_candles_and_taker_values(self):
        exchange=BinanceSpotExchange(Settings())
        try:
            future=9_000_000_000_000
            exchange._get=AsyncMock(return_value=[candle(START),candle(future)])
            rows=await exchange.fetch_candles('BTCUSDT','1m')
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['taker_sell_base_volume'],1.5)
        finally:
            await exchange.close()

    async def test_live_settlement_not_polling_timestamp(self):
        exchange=BinanceFuturesExchange(Settings())
        try:
            exchange._get=AsyncMock(return_value=[dict(symbol='BTCUSDT',fundingTime=START+15,fundingRate='0.0001')])
            rows=await exchange.fetch_funding_rate('BTCUSDT')
            self.assertEqual(round(rows[0]['funding_time'].timestamp()*1000),START+15)
        finally:
            await exchange.close()

    async def test_csv_restart_deduplicates_funding(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(output_dir=folder)
            row=dict(exchange='binance',market_type='usdt_futures',symbol='BTCUSDT',
                funding_time='2025-01-01T00:00:00+00:00',funding_rate=.0001)
            first=CSVDB(settings)
            await first.connect()
            await first.insert_funding_rates([row,row])
            second=CSVDB(settings)
            await second.insert_funding_rates([row])
            file=next(Path(folder).rglob('*.csv'))
            with file.open(newline='') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))),1)

    async def test_live_basis_joins_only_identical_closed_minutes(self):
        exchange=BinanceFuturesExchange(Settings())
        try:
            exchange._get=AsyncMock(side_effect=[[candle(START)],[candle(START+60000)],
                [candle(START)],[candle(START)],[candle(START)]])
            with self.assertRaisesRegex(ValueError,'common closed minute'):
                await exchange.fetch_basis('BTCUSDT')
        finally:
            await exchange.close()


if __name__=='__main__':
    unittest.main()
