import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'collectors'))
import file_io


class FileCommitTests(unittest.TestCase):
    def test_transient_permission_error_retries_without_changing_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'coverage.json'
            path.write_text('{"old":true}')
            original=file_io.os.replace
            calls=[]
            def locked_twice(source,destination):
                calls.append(source)
                if len(calls)<3:
                    raise PermissionError('WinError 5 simulated')
                original(source,destination)
            with patch.object(file_io.os,'replace',side_effect=locked_twice),patch.object(file_io.time,'sleep'):
                file_io.save_json(path,{'entries':{'a':{'status':'complete'}}})
            self.assertEqual(len(calls),3)
            self.assertEqual(json.loads(path.read_text())['entries']['a']['status'],'complete')
            self.assertEqual(len(list(Path(folder).glob('*.tmp'))),0)

    def test_persistent_lock_preserves_original_and_new_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'coverage.json'
            path.write_text('{"old":true}')
            with patch.object(file_io.os,'replace',side_effect=PermissionError('locked')),patch.object(file_io.time,'sleep'):
                with self.assertRaises(PermissionError):
                    file_io.save_json(path,{'new':True})
            self.assertEqual(json.loads(path.read_text()),{'old':True})
            temp=next(Path(folder).glob('*.tmp'))
            self.assertEqual(json.loads(temp.read_text()),{'new':True})

    def test_stale_fixed_temp_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'coverage.json'
            stale=Path(folder)/'coverage.json.tmp'
            stale.write_text('retained previous checkpoint')
            file_io.save_json(path,{'new':True})
            self.assertEqual(stale.read_text(),'retained previous checkpoint')
