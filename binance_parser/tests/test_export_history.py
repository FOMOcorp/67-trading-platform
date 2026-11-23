import argparse
import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collectors'))
import export_history as history

START = 1735689600000


def candle(stamp, price="100.01000000"):
    return [str(stamp), price, "102", "99", "101", "2.50000000",
            str(stamp + 59999), "251.12500000", "42", "1", "100", "0"]


class HistoryTests(unittest.TestCase):
    def test_microseconds_dedup_order_and_exact_csv_columns(self):
        rows = [candle((START + 60000) * 1000), candle(START), candle(START)]
        result = history.normalize(rows, START, START + 120000)
        self.assertEqual(result[0], [START, "2025-01-01 00:00:00", "100.01000000",
                                   "102", "99", "101", "2.50000000", "251.12500000", "",
                                   42, '1', '100', '1.50000000', '151.12500000', '-0.2'])
        self.assertEqual(result[1][0], START + 60000)

    def test_missing_candles_stop_export(self):
        with self.assertRaisesRegex(ValueError, "Missing candles"):
            history.normalize([candle(START)], START, START + 120000)

    def test_conflicting_duplicates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            history.normalize([candle(START), candle(START, "100.02")], START, START + 60000)

    def test_archive_header_and_checksum(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("candles.csv", "open_time,open,high,low,close,volume,close_time,quote_volume,trades\n"
                             + ",".join(candle(START)) + "\n")
        payload = data.getvalue()
        checksum = history.hashlib.sha256(payload).hexdigest().encode() + b"  candles.zip"
        with patch.object(history, "get_bytes", side_effect=[payload, checksum]):
            self.assertEqual(history.archive_rows("futures", "BTCUSDT", "daily", "2025-01-01"),
                             [candle(START)])
        with patch.object(history, "get_bytes", side_effect=[payload, b"bad"]):
            with self.assertRaisesRegex(ValueError, "Checksum"):
                history.archive_rows("spot", "BTCUSDT", "daily", "2025-01-01")

    def test_api_pagination(self):
        with patch.object(history, "get_bytes", side_effect=[json.dumps([candle(START)]).encode(),
                          json.dumps([candle(START + 60000)]).encode()]) as request, \
                patch.object(history.time, "sleep"):
            rows = history.api_rows("spot", "BTCUSDT", START, START + 120000)
            self.assertEqual(len(rows), 2)
            self.assertIn(f"startTime={START + 60000}", request.call_args.args[0])

    def test_resume_rolls_back_uncommitted_bytes_and_preserves_range(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "btc.csv"
            args = argparse.Namespace(output=str(output), start="2025-01-01", end="2025-01-01T00:02",
                                      years=5, symbol="BTCUSDT", market="spot", allow_gaps=False)

            def interrupted(*unused):
                yield START, START + 60000, [candle(START)]
                raise OSError("Simulated interruption")

            with patch.object(history, "batches", interrupted):
                with self.assertRaisesRegex(OSError, "interruption"):
                    history.export(args)
            self.assertFalse(output.exists())
            partial = output.with_name("btc.csv.part")
            with partial.open("ab") as stream:
                stream.write(b"incomplete row from crash")
            args.start = args.end = None
            with patch.object(history, "batches", return_value=[
                    (START + 60000, START + 120000, [candle(START + 60000)])]) as batches:
                history.export(args)
            self.assertEqual(batches.call_args.args[2:], (START + 60000, START + 120000))
            with output.open(newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[0], history.FIELDS)
            self.assertEqual(len(rows), 3)
            self.assertFalse(partial.exists())
            self.assertFalse(output.with_name("btc.csv.progress.json").exists())
            with self.assertRaisesRegex(ValueError, "already exists"):
                history.export(args)

    def test_end_exclusive_and_leap_date(self):
        self.assertEqual(len(history.normalize([candle(START), candle(START + 60000)],
                                              START, START + 60000)), 1)
        self.assertEqual(history.parse_date("2024-02-29T03:00:00+03:00").hour, 0)
        with self.assertRaises(ValueError):
            history.parse_date("2025-01-01T00:00:01")

    def test_allowed_gaps_have_exact_report(self):
        rows = history.normalize([candle(START + 60000)], START, START + 180000, allow_gaps=True)
        gaps = history.missing_ranges(rows, START, START + 180000)
        self.assertEqual([gap["minutes"] for gap in gaps], [1, 1])
        self.assertEqual(gaps[0]["start"], "2025-01-01 00:00:00")


if __name__ == "__main__":
    unittest.main()
