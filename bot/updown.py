"""
Mercados Up/Down de 5 y 15 minutos (BTC/ETH/SOL/XRP) de Polymarket.

Resuelven "Up" si el TWAP de Chainlink de la ventana es >= al precio al inicio de la ventana.
Comparamos una probabilidad justa calculada con el precio en vivo de Binance contra el
precio (ask) real del libro de ordenes, descontando la comision de taker.
"""
import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import aiohttp
from loguru import logger

from bot.models import Market, Side

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://api.binance.com/api/v3"

SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
SLUG_PREFIX = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp"}
TF_SECONDS = {"5m": 300, "15m": 900}

FEE_RATE = 0.07          # crypto_fees_v2: fee por share = 0.07 * p * (1 - p), solo taker
MIN_ELAPSED_S = 30       # no entrar en los primeros segundos (TWAP aun sin informacion)
MIN_LEFT_S = 20          # no entrar con menos de 20s (riesgo de ejecucion / resolucion)
MODEL_HAIRCUT = 0.02     # margen de seguridad restado a la probabilidad del modelo
MIN_ASK, MAX_ASK = 0.05, 0.95
MIN_SHARES = 5


def taker_fee_per_share(price: float) -> float:
    return FEE_RATE * price * (1.0 - price)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_up(ref: float, spot: float, avg_so_far: float, elapsed: float, total: float, sigma_s: float) -> float:
    """
    P(TWAP de la ventana >= ref).
    TWAP final = f*media_ya_observada + (1-f)*media_del_tramo_restante.
    La media de un browniano sobre tau segundos tiene varianza sigma^2 * tau / 3.
    """
    tau = total - elapsed
    if tau <= 0:
        return 1.0 if avg_so_far >= ref else 0.0
    f = max(0.0, min(1.0, elapsed / total))
    mean_final = f * avg_so_far + (1.0 - f) * spot
    std = (1.0 - f) * spot * sigma_s * math.sqrt(tau / 3.0)
    if std <= 0:
        return 1.0 if mean_final >= ref else 0.0
    return norm_cdf((mean_final - ref) / std)


def side_edges(market: Market, now: Optional[float] = None) -> List[Tuple[Side, float, float, float, float]]:
    """[(lado, p_modelo, ask, coste_con_fee, edge_neto)] para un mercado up/down."""
    if market.p_model_yes is None:
        return []
    out = []
    for side, ask, p in (
        (Side.YES, market.yes_price, market.p_model_yes),
        (Side.NO, market.no_price, 1.0 - market.p_model_yes),
    ):
        if not (0.0 < ask < 1.0):
            continue
        cost = ask + taker_fee_per_share(ask)
        p_eff = max(0.0, p - MODEL_HAIRCUT)
        out.append((side, p_eff, ask, cost, p_eff - cost))
    return out


def entry_window_ok(market: Market, now: Optional[float] = None) -> bool:
    now = now or time.time()
    elapsed = now - market.start_ts
    left = market.start_ts + market.window_s - now
    return elapsed >= MIN_ELAPSED_S and left >= MIN_LEFT_S


@dataclass
class Window:
    asset: str
    tf: str
    slug: str
    market_id: str
    condition_id: str
    start_ts: int
    end_ts: int
    token_up: str
    token_dn: str
    title: str
    liquidity: float

    @property
    def total(self) -> int:
        return self.end_ts - self.start_ts


class UpDownFeed:
    def __init__(self, config):
        self.config = config
        self.assets = [a.strip().upper() for a in config.fast_assets.split(",") if a.strip().upper() in SYMBOLS]
        self.tfs = [t.strip() for t in config.fast_timeframes.split(",") if t.strip() in TF_SECONDS]
        self._session: Optional[aiohttp.ClientSession] = None
        self._windows: Dict[str, Optional[Window]] = {}
        self._neg_until: Dict[str, float] = {}
        self._klines: Dict[str, Tuple[float, List[list]]] = {}
        self._spot: Dict[str, float] = {}
        self._last_poll: Dict[str, float] = {}
        self.last_error: str = ""

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8),
                headers={"User-Agent": "polymarket-hft-bot/3.0"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── descubrimiento de ventanas ────────────────────────────────────────
    async def _fetch_window(self, asset: str, tf: str, start_ts: int) -> Optional[Window]:
        slug = f"{SLUG_PREFIX[asset]}-updown-{tf}-{start_ts}"
        if slug in self._windows and self._windows[slug] is not None:
            return self._windows[slug]
        if self._neg_until.get(slug, 0) > time.time():
            return None
        try:
            s = await self._sess()
            async with s.get(f"{GAMMA}/events", params={"slug": slug}) as r:
                r.raise_for_status()
                data = await r.json()
            if not data or not data[0].get("markets"):
                self._neg_until[slug] = time.time() + 10
                return None
            e = data[0]
            m = e["markets"][0]
            toks = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
            w = Window(
                asset=asset, tf=tf, slug=slug, market_id=str(m["id"]),
                condition_id=str(m.get("conditionId", "")), start_ts=start_ts,
                end_ts=start_ts + TF_SECONDS[tf], token_up=str(toks[0]), token_dn=str(toks[1]),
                title=e.get("title", slug), liquidity=float(m.get("liquidityClob") or m.get("liquidity") or 0),
            )
            self._windows[slug] = w
            return w
        except Exception as ex:
            self.last_error = f"gamma {slug}: {ex}"
            self._neg_until[slug] = time.time() + 5
            return None

    async def _current_windows(self, now: float) -> List[Window]:
        tasks = []
        for asset in self.assets:
            for tf in self.tfs:
                step = TF_SECONDS[tf]
                start = int(now) - int(now) % step
                tasks.append(self._fetch_window(asset, tf, start))
                # pre-carga de la siguiente para no perder segundos al cambiar de ventana
                if now - start > step - 90:
                    tasks.append(self._fetch_window(asset, tf, start + step))
        res = await asyncio.gather(*tasks)
        return [w for w in res if w is not None and w.start_ts <= now < w.end_ts]

    # ── Binance ────────────────────────────────────────────────────────────
    async def _refresh_spot(self):
        try:
            s = await self._sess()
            syms = json.dumps([SYMBOLS[a] for a in self.assets], separators=(",", ":"))
            async with s.get(f"{BINANCE}/ticker/price", params={"symbols": syms}) as r:
                r.raise_for_status()
                for row in await r.json():
                    for a, sym in SYMBOLS.items():
                        if sym == row["symbol"]:
                            self._spot[a] = float(row["price"])
        except Exception as ex:
            self.last_error = f"binance spot: {ex}"

    async def _klines_1m(self, asset: str, force: bool = False) -> List[list]:
        ts, data = self._klines.get(asset, (0.0, []))
        if not force and data and time.time() - ts < 8:
            return data
        try:
            s = await self._sess()
            async with s.get(f"{BINANCE}/klines", params={"symbol": SYMBOLS[asset], "interval": "1m", "limit": 90}) as r:
                r.raise_for_status()
                data = await r.json()
            self._klines[asset] = (time.time(), data)
        except Exception as ex:
            self.last_error = f"binance klines {asset}: {ex}"
        return data

    @staticmethod
    def sigma_per_sqrt_s(klines: List[list]) -> float:
        closes = [float(k[4]) for k in klines[:-1]][-60:]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) < 10:
            return 6e-5
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return max(math.sqrt(var) / math.sqrt(60.0), 2e-5)

    @staticmethod
    def ref_and_avg(klines: List[list], start_ts: int, spot: float) -> Optional[Tuple[float, float]]:
        """(precio al inicio de ventana, media temporal aproximada hasta ahora)."""
        start_ms = start_ts * 1000
        pts, ref = [], None
        for k in klines:
            if k[0] < start_ms:
                continue
            if ref is None and k[0] == start_ms:
                ref = float(k[1])
            o = float(k[1])
            c = float(k[4]) if k[6] < time.time() * 1000 else spot
            pts.append((o + c) / 2.0)
        if ref is None or not pts:
            return None
        return ref, sum(pts) / len(pts)

    # ── libro de ordenes ───────────────────────────────────────────────────
    async def _books(self, tokens: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        try:
            s = await self._sess()
            async with s.post(f"{CLOB}/books", json=[{"token_id": t} for t in tokens]) as r:
                r.raise_for_status()
                data = await r.json()
            for b in data:
                asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
                bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
                best_ask = min(asks) if asks else None
                best_bid = max(bids) if bids else None
                out[str(b["asset_id"])] = {
                    "ask": best_ask[0] if best_ask else None,
                    "ask_size": best_ask[1] if best_ask else 0.0,
                    "bid": best_bid[0] if best_bid else None,
                }
        except Exception as ex:
            self.last_error = f"clob books: {ex}"
        return out

    # ── snapshot principal ─────────────────────────────────────────────────
    async def snapshot(self) -> List[Market]:
        now = time.time()
        wins, _ = await asyncio.gather(self._current_windows(now), self._refresh_spot())
        if not wins:
            return []
        tokens = [t for w in wins for t in (w.token_up, w.token_dn)]
        books, *kl = await asyncio.gather(self._books(tokens), *[self._klines_1m(a) for a in self.assets])
        klines = dict(zip(self.assets, kl))

        markets: List[Market] = []
        now = time.time()
        for w in wins:
            spot = self._spot.get(w.asset)
            k = klines.get(w.asset) or []
            bu, bd = books.get(w.token_up), books.get(w.token_dn)
            if not spot or not k or not bu or not bd:
                continue
            ra = self.ref_and_avg(k, w.start_ts, spot)
            if ra is None:
                continue
            ref, avg = ra
            sigma = self.sigma_per_sqrt_s(k) * self.config.fast_sigma_mult
            elapsed = now - w.start_ts
            p_up = prob_up(ref, spot, avg, elapsed, w.total, sigma)
            ask_u, ask_d = bu["ask"], bd["ask"]
            mk = Market(
                id=w.market_id, question=f"{w.asset} {w.tf} Up/Down {datetime.utcfromtimestamp(w.start_ts):%H:%M}Z",
                condition_id=w.condition_id, token_id_yes=w.token_up, token_id_no=w.token_dn,
                yes_price=ask_u if ask_u is not None else 1.0,
                no_price=ask_d if ask_d is not None else 1.0,
                volume=0.0, liquidity=w.liquidity,
                end_date=datetime.utcfromtimestamp(w.end_ts),
                category=f"updown-{w.tf}", asset=w.asset,
                target_price=ref, direction="above",
                kind="updown", ref_price=ref, spot=spot, p_model_yes=p_up,
                yes_bid=bu["bid"], no_bid=bd["bid"],
                yes_ask_size=bu["ask_size"], no_ask_size=bd["ask_size"],
                start_ts=w.start_ts, window_s=w.total, slug=w.slug,
            )
            markets.append(mk)
        return markets

    # ── resolucion (liquidacion de posiciones paper) ───────────────────────
    async def resolution(self, market: Market) -> Optional[float]:
        """Valor final del lado YES (Up): 1.0, 0.0 o None si aun no resolvio."""
        key = market.slug
        if time.time() - self._last_poll.get(key, 0) < 4:
            return None
        self._last_poll[key] = time.time()
        try:
            s = await self._sess()
            async with s.get(f"{GAMMA}/events", params={"slug": market.slug}) as r:
                r.raise_for_status()
                data = await r.json()
            m = data[0]["markets"][0]
            if m.get("closed"):
                prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m["outcomePrices"]
                up = float(prices[0])
                if up >= 0.99:
                    return 1.0
                if up <= 0.01:
                    return 0.0
        except Exception as ex:
            self.last_error = f"resolution {key}: {ex}"
        # Respaldo: si Polymarket tarda mas de 10 min, estimamos con Binance
        if time.time() > market.start_ts + market.window_s + 600:
            return await self.proxy_resolution(market)
        return None

    async def proxy_resolution(self, market: Market) -> Optional[float]:
        k = await self._klines_1m(market.asset, force=True)
        end_ms = (market.start_ts + market.window_s) * 1000
        vals, ref = [], None
        for row in k:
            if row[0] < market.start_ts * 1000 or row[0] >= end_ms:
                continue
            if ref is None and row[0] == market.start_ts * 1000:
                ref = float(row[1])
            vals.append((float(row[1]) + float(row[4])) / 2.0)
        if ref is None or not vals:
            return None
        return 1.0 if sum(vals) / len(vals) >= ref else 0.0
