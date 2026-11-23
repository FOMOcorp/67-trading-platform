from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from market_data import candle_values, decimal_value
from app.exchanges.base import BaseExchange, ms_to_datetime, now_utc, raw_json, to_float


class BinancePublicExchange(BaseExchange):
    name = 'binance'
    market_type = 'spot'
    base_url = 'https://data-api.binance.vision'
    api = '/api/v3'

    def common(self, symbol):
        return dict(exchange=self.name, market_type=self.market_type, symbol=symbol)

    async def get_instruments(self):
        data = await self._get(self.api + '/exchangeInfo')
        rows = []
        for item in data['symbols']:
            filters = {f['filterType']: f for f in item.get('filters', [])}
            rows.append(dict(self.common(item['symbol']), base_asset=item.get('baseAsset', ''),
                             quote_asset=item.get('quoteAsset', ''), instrument_type='spot' if self.market_type == 'spot' else 'future',
                             contract_type=item.get('contractType', ''), status=item.get('status', ''),
                             tick_size=to_float(filters.get('PRICE_FILTER', {}).get('tickSize')),
                             step_size=to_float(filters.get('LOT_SIZE', {}).get('stepSize')),
                             raw=raw_json(item), updated_at=now_utc()))
        return rows

    def map_symbols(self, symbols, instruments=None):
        available = {r['symbol'] for r in instruments or [] if r.get('status') == 'TRADING'}
        missing = set(symbols)-available if instruments else set()
        if missing:
            raise ValueError('Symbols are not trading on this market: ' + ','.join(sorted(missing)))
        return symbols

    async def fetch_candles(self, symbol, timeframe):
        data = await self._get(self.api + '/klines', dict(symbol=symbol, interval=timeframe, limit=3))
        cutoff = int(datetime.now(timezone.utc).timestamp()*1000)
        rows = []
        for item in data:
            if int(item[6]) >= cutoff:
                continue
            v = candle_values(item)
            rows.append(dict(self.common(symbol), timeframe=timeframe, open_time=ms_to_datetime(item[0]),
                             close_time=ms_to_datetime(item[6]), available_at=ms_to_datetime(int(item[6])+1),
                             **{k:float(v[k]) for k in ('open','high','low','close')},
                             volume=float(v['volume_base']), quote_volume=float(v['volume_quote']),
                             trades=v['number_of_trades'],
                             **{k:to_float(v[k]) for k in ('taker_buy_base_volume','taker_buy_quote_volume',
                                 'taker_sell_base_volume','taker_sell_quote_volume','trade_imbalance')}, raw=raw_json(item)))
        return rows

    async def fetch_ticker(self, symbol):
        ticker, book = await asyncio.gather(
            self._get(self.api + '/ticker/24hr', dict(symbol=symbol)),
            self._get(self.api + '/ticker/bookTicker', dict(symbol=symbol)))
        premium = await self._get('/fapi/v1/premiumIndex', dict(symbol=symbol)) if self.market_type == 'usdt_futures' else {}
        return [dict(self.common(symbol), event_time=ms_to_datetime(ticker['closeTime']),
                     price=to_float(ticker['lastPrice']), mark_price=to_float(premium.get('markPrice')),
                     index_price=to_float(premium.get('indexPrice')), bid_price=to_float(book['bidPrice']),
                     ask_price=to_float(book['askPrice']), volume_24h=to_float(ticker.get('volume')),
                     quote_volume_24h=to_float(ticker.get('quoteVolume')),
                     raw=raw_json(dict(ticker=ticker, book=book, premium=premium)), received_at=now_utc())]

    async def fetch_funding_rate(self, symbol):
        return []

    async def fetch_open_interest(self, symbol):
        return []


class BinanceSpotExchange(BinancePublicExchange):
    pass


class BinanceFuturesExchange(BinancePublicExchange):
    market_type = 'usdt_futures'
    base_url = 'https://fapi.binance.com'
    api = '/fapi/v1'

    async def fetch_funding_rate(self, symbol):
        # Settlements have fundingTime; polling time must never replace it.
        items = await self._get('/fapi/v1/fundingRate', dict(symbol=symbol, limit=100))
        return [dict(self.common(symbol), funding_time=ms_to_datetime(item['fundingTime']),
                     funding_rate=to_float(item['fundingRate']), next_funding_time=None,
                     mark_price=to_float(item.get('markPrice')), index_price=None, raw=raw_json(item)) for item in items]

    async def fetch_funding_snapshot(self, symbol):
        item = await self._get('/fapi/v1/premiumIndex', dict(symbol=symbol))
        return [dict(self.common(symbol), event_time=ms_to_datetime(item['time']),
                     funding_rate_snapshot=to_float(item['lastFundingRate']),
                     next_funding_time=ms_to_datetime(item['nextFundingTime']),
                     mark_price=to_float(item['markPrice']), index_price=to_float(item['indexPrice']),
                     raw=raw_json(item), received_at=now_utc())]

    async def fetch_open_interest(self, symbol):
        item = await self._get('/fapi/v1/openInterest', dict(symbol=symbol))
        return [dict(self.common(symbol), event_time=ms_to_datetime(item['time']),
                     open_interest=to_float(item['openInterest']), open_interest_value=None,
                     single_open_interest=None, raw=raw_json(item))]

    async def fetch_basis(self, symbol):
        cutoff = int(datetime.now(timezone.utc).timestamp()*1000)//60_000*60_000
        params = dict(symbol=symbol, interval='1m', limit=3, endTime=cutoff-1)
        data = await asyncio.gather(
            self._get('https://data-api.binance.vision/api/v3/klines', params),
            self._get('/fapi/v1/klines', params),
            self._get('/fapi/v1/markPriceKlines', params),
            self._get('/fapi/v1/indexPriceKlines', {k:v for k,v in params.items() if k!='symbol'} | {'pair':symbol}),
            self._get('/fapi/v1/premiumIndexKlines', params))
        tables = [{int(r[0]):r for r in values if int(r[6]) < cutoff} for values in data]
        common = set.intersection(*(set(t) for t in tables))
        if not common:
            raise ValueError('No common closed minute across spot/futures/mark/index/premium')
        def gap(a,b):
            return float((decimal_value(a)/decimal_value(b)-1)*10_000)
        rows = []
        for stamp in sorted(common):
            spot, futures, mark, index, premium = [t[stamp] for t in tables]
            rows.append(dict(self.common(symbol), event_time=ms_to_datetime(stamp),
                             available_at=ms_to_datetime(stamp+60_000), spot_close=float(spot[4]),
                             futures_close=float(futures[4]), mark_price=float(mark[4]), index_price=float(index[4]),
                             premium_index=float(premium[4]), futures_spot_basis_bps=gap(futures[4],spot[4]),
                             mark_index_gap_bps=gap(mark[4],index[4]), raw=raw_json([t[stamp] for t in tables])))
        return rows
