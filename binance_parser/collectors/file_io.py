"""Atomic file commits with bounded retries for transient Windows file locks."""
import json
import logging
import os
import tempfile
import time

LOG = logging.getLogger('binance.files')


def replace_file(source, destination, attempts=12):
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts-1:
                raise
            if attempt == 0:
                LOG.warning('File is temporarily locked; retrying commit: %s', destination)
            time.sleep(min(.1 * 2**attempt, 1.0))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique names prevent accidental collisions with a stale temporary file.
    # On failure retain the complete temporary file for recovery.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.name+'.', suffix='.tmp', delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    replace_file(temporary, path)
