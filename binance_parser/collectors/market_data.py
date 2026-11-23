"""Exact Binance candle normalization shared by CSV and online collectors."""
from decimal import Decimal, InvalidOperation

CANDLE_FIELDS = [
    'timestamp', 'datetime_utc', 'open', 'high', 'low', 'close',
    'volume_base', 'volume_quote', 'last_trade_id', 'number_of_trades',
    'taker_buy_base_volume', 'taker_buy_quote_volume',
    'taker_sell_base_volume', 'taker_sell_quote_volume', 'trade_imbalance',
]


def milliseconds(value):
    value = int(value)
    return value // 1000 if value >= 100_000_000_000_000 else value


def decimal_value(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f'Invalid decimal: {value!r}') from exc
    if not number.is_finite():
        raise ValueError('Non-finite market value')
    return number


def candle_values(row):
    if len(row) < 11:
        raise ValueError('Incomplete candle: taker volumes and trade count are required')
    op, high, low, close, base, quote, buy_base, buy_quote = map(
        decimal_value, [row[1], row[2], row[3], row[4], row[5], row[7], row[9], row[10]])
    if min(op, high, low, close) <= 0 or low > min(op, close) or high < max(op, close) or low > high:
        raise ValueError('Invalid candle OHLC')
    if min(base, quote, buy_base, buy_quote) < 0 or buy_base > base or buy_quote > quote:
        raise ValueError('Invalid candle volumes: require 0 <= taker buy <= total')
    count = decimal_value(row[8])
    if count < 0 or count != count.to_integral_value():
        raise ValueError('Invalid number of trades')
    return dict(open=str(row[1]), high=str(row[2]), low=str(row[3]), close=str(row[4]),
                volume_base=str(row[5]), volume_quote=str(row[7]), last_trade_id='',
                number_of_trades=int(count), taker_buy_base_volume=str(row[9]),
                taker_buy_quote_volume=str(row[10]),
                taker_sell_base_volume=format(base-buy_base, 'f'),
                taker_sell_quote_volume=format(quote-buy_quote, 'f'),
                trade_imbalance=format((2*buy_base-base)/base, 'f') if base else '')
