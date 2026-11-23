"""Download the free Binance research datasets, with resumable coverage reports."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'collectors'))
from download_history import main

if __name__ == '__main__':
    raise SystemExit(main())
