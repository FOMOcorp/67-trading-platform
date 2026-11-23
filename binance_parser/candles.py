"""Export closed Binance spot or USD-M 1-minute candles with real taker volumes."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'collectors'))
from export_history import main

if __name__ == '__main__':
    raise SystemExit(main())
