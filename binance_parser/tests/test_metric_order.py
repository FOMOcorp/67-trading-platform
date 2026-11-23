from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'collectors'))
import download_history as history

HEADER=['create_time','symbol','sum_open_interest','sum_open_interest_value']


def row(time,oi='10'):
    return ['2026-05-18 '+time,'BTCUSDT',oi,'1000']


class MetricOrderTests(unittest.TestCase):
    def test_shuffled_archive_is_sorted_without_losing_records(self):
        rows=list(history.metric_rows(iter([HEADER,row('00:25:00'),row('00:20:00'),row('00:00:00')]),'BTCUSDT'))
        self.assertEqual(len(rows),3)
        stamps=[r['timestamp'] for r in rows]
        self.assertEqual(stamps,sorted(set(stamps)))
        self.assertTrue(rows[0]['datetime_utc'].endswith('00:00:00+00:00'))

    def test_identical_duplicate_is_kept_once(self):
        rows=list(history.metric_rows(iter([HEADER,row('00:00:00'),row('00:00:00')]),'BTCUSDT'))
        self.assertEqual(len(rows),1)

    def test_conflicting_duplicate_is_not_silently_overwritten(self):
        with self.assertRaisesRegex(ValueError,'Conflicting metrics'):
            list(history.metric_rows(iter([HEADER,row('00:00:00'),row('00:00:00','11')]),'BTCUSDT'))
