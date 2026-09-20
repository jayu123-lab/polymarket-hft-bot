"""
Mercados Up/Down de 5 y 15 minutos (BTC/ETH/SOL/XRP) de Polymarket.

Resuelven "Up" si el TWAP de Chainlink de los ULTIMOS 60 s es >= al precio al inicio de la ventana.
Comparamos una probabilidad justa calculada con el precio en vivo de Binance contra el
precio (ask) real del libro de ordenes, descontando la comision de taker.

Datos en tiempo real por websocket (Binance + CLOB de Polymarket) con respaldo REST; el ciclo
caliente (snapshot) solo lee memoria, sin esperar a la red.
"""
import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import aiohttp

from bot.models import Market, Side
from bot.streams import BinanceStream, PolyBookStream

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://api.binance.com/api/v3"

SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
SLUG_PREFIX = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp"}
TF_SECONDS = {"5m": 300, "15m": 900}

FEE_RATE = 0.07          # crypto_fees_v2: fee por share = 0.07 * p * (1 - p), solo taker
MIN_ELAPSED_S = 30       # no entrar en los primeros segundos (sin referencia fiable)
MIN_LEFT_S = 20          # no entrar con menos de 20s (riesgo de ejecucion / resolucion)
MODEL_HAIRCUT = 0.02     # margen de seguridad restado a la probabilidad del modelo
MIN_ASK, MAX_ASK = 0.15, 0.95   # bajo 0.15 son loterias (11-17% de aciertos, resultado inestable)
MIN_SHARES = 5
TWAP_S = 60              # ventana del TWAP con el que resuelve Polymarket (ultimos 60 s)


def taker_fee_per_share(price: float) -> float:
    return FEE_RATE * price * (1.0 - price)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_up_twap(ref: float, spot: float, avg_obs: float, secs_left: float, sigma_s: float,
                 window: float = TWAP_S, basis: float = 0.0) -> float:
    """
    P(TWAP de los ultimos `window` segundos >= ref). Asi resuelve Polymarket (Chainlink btc-usd-twap-60s):
    verificado contra 3.054 ventanas reales (92.8% de acierto vs 84.7% del TWAP de ventana completa).
      - Con mas de `window` s por delante: media = spot, varianza sigma^2 * (tau - 2*window/3).
      - Dentro del ultimo tramo: parte ya observada (avg_obs) + parte futura (varianza sigma^2 * tau/3).
      - `basis`: ruido fraccional entre el precio de Binance y el de Chainlink (evita certezas falsas
        cuando el precio esta pegado a la referencia).
    """
    tau = secs_left
    if tau <= 0:
        mean, std = avg_obs, 0.0
    elif tau > window:
        mean = spot
        std = spot * sigma_s * math.sqrt(tau - 2.0 * window / 3.0)
    else:
        w = (window - tau) / window
        mean = w * avg_obs + (1.0 - w) * spot
        std = (1.0 - w) * spot * sigma_s * math.sqrt(tau / 3.0)
    std = math.sqrt(std * std + (basis * spot) ** 2)
    if std <= 0:
        return 1.0 if mean >= ref else 0.0
    return norm_cdf((mean - ref) / std)


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
        self._active: List[Window] = []
        self._klines: Dict[str, List[list]] = {}
        self._kl_ts: Dict[str, float] = {}
        self._sigma: Dict[str, float] = {}
        self._refs: Dict[str, float] = {}
        self._spot: Dict[str, float] = {}
        self._spot_ts: Dict[str, float] = {}
        self._samples: Dict[str, deque] = {a: deque() for a in SYMBOLS}
        self._rest_books: Dict[str, dict] = {}
        self._last_poll: Dict[str, float] = {}
        self.last_error: str = ""

        use_ws = getattr(config, "fast_use_ws", True)
        self._binance = BinanceStream({a: SYMBOLS[a] for a in self.assets}, self._on_tick) if use_ws else None
        self._poly = PolyBookStream(on_message=self._mark) if use_ws else None
        self._tasks: List[asyncio.Task] = []
        self._ready = asyncio.Event()
        self._evt = asyncio.Event()                  # se activa con cada dato nuevo (tick o libro)
        self.last_arrival = time.perf_counter()      # llegada del ultimo dato
        self.snap_arrival = self.last_arrival        # llegada del dato usado por el ultimo snapshot

    # ── ciclo de vida ──────────────────────────────────────────────────────
    async def start(self):
        if self._tasks:
            return
        self._tasks.append(asyncio.create_task(self._maintenance()))
        if self._binance:
            self._tasks.append(asyncio.create_task(self._binance.run()))
        if self._poly:
            self._tasks.append(asyncio.create_task(self._poly.run()))
        try:
            await asyncio.wait_for(self._ready.wait(), 10)
        except asyncio.TimeoutError:
            pass

    async def close(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._session and not self._session.closed:
            await self._session.close()

    def data_lag_ms(self) -> float:
        """Antiguedad (ms) del ultimo dato recibido por los websockets."""
        now = time.time()
        ages = [now - x.last_msg for x in (self._binance, self._poly) if x is not None and x.last_msg]
        return max(ages) * 1000.0 if ages else 0.0

    def mode_label(self) -> str:
        ws = bool(self._binance and self._poly and self._binance.healthy and self._poly.healthy)
        return "WS" if ws else "REST"

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8),
                headers={"User-Agent": "polymarket-hft-bot/3.0"},
            )
        return self._session

    # ── datos en tiempo real ───────────────────────────────────────────────
    def _mark(self):
        self.last_arrival = time.perf_counter()
        self._evt.set()

    async def wait_update(self, timeout: float = 0.25):
        """Espera al siguiente dato nuevo (dirigido por eventos) o al timeout."""
        try:
            await asyncio.wait_for(self._evt.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._evt.clear()

    def _on_tick(self, asset: str, price: float, now: float):
        self._mark()
        self._spot[asset] = price
        self._spot_ts[asset] = now
        q = self._samples[asset]
        if not q or now - q[-1][0] >= 0.1:
            q.append((now, price))
        while q and q[0][0] < now - 150:
            q.popleft()

    def _top(self, token: str) -> Optional[dict]:
        if self._poly and self._poly.healthy:
            t = self._poly.top(token)
            if t and (t["ask"] is not None or t["bid"] is not None):
                return t
        t = self._rest_books.get(token)
        if t and time.time() - t["ts"] < 5:
            return t
        return None

    def quote(self, market: Market, side: Side) -> Optional[Tuple[Optional[float], float, Optional[float]]]:
        """(ask, tamano_ask, bid) actuales del token del lado indicado, o None."""
        t = self._top(market.token_id_yes if side == Side.YES else market.token_id_no)
        return None if not t else (t["ask"], t["ask_size"], t["bid"])

    # ── mantenimiento en segundo plano (red) ───────────────────────────────
    async def _maintenance(self):
        while True:
            try:
                now = time.time()
                self._active = await self._current_windows(now)
                tokens = {t for w in self._active for t in (w.token_up, w.token_dn)}
                jobs = [self._refresh_klines(a) for a in self.assets if now - self._kl_ts.get(a, 0) > 8]
                if not (self._binance and self._binance.healthy):
                    jobs.append(self._refresh_spot_rest())
                missing = [t for t in tokens if not (self._poly and self._poly.healthy and self._poly.top(t))]
                if missing:
                    jobs.append(self._refresh_books_rest(missing))
                if self._poly:
                    self._poly.want(tokens)
                    jobs.append(self._poly.sync())
                if jobs:
                    await asyncio.gather(*jobs, return_exceptions=True)
                self._ready.set()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self.last_error = f"mantenimiento: {ex}"
            await asyncio.sleep(0.3)

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
                if now - start > step - 90:      # pre-carga de la siguiente ventana
                    tasks.append(self._fetch_window(asset, tf, start + step))
        res = await asyncio.gather(*tasks)
        return [w for w in res if w is not None and w.start_ts <= now < w.end_ts]

    async def _refresh_spot_rest(self):
        try:
            s = await self._sess()
            syms = json.dumps([SYMBOLS[a] for a in self.assets], separators=(",", ":"))
            async with s.get(f"{BINANCE}/ticker/price", params={"symbols": syms}) as r:
                r.raise_for_status()
                for row in await r.json():
                    for a, sym in SYMBOLS.items():
                        if sym == row["symbol"]:
                            self._on_tick(a, float(row["price"]), time.time())
        except Exception as ex:
            self.last_error = f"binance spot: {ex}"

    async def _fetch_klines(self, asset: str) -> List[list]:
        s = await self._sess()
        async with s.get(f"{BINANCE}/klines", params={"symbol": SYMBOLS[asset], "interval": "1m", "limit": 90}) as r:
            r.raise_for_status()
            return await r.json()

    async def _refresh_klines(self, asset: str):
        try:
            data = await self._fetch_klines(asset)
            self._klines[asset] = data
            self._kl_ts[asset] = time.time()
            self._sigma[asset] = self.sigma_per_sqrt_s(data)
        except Exception as ex:
            self.last_error = f"binance klines {asset}: {ex}"

    async def _refresh_books_rest(self, tokens: List[str]):
        try:
            s = await self._sess()
            async with s.post(f"{CLOB}/books", json=[{"token_id": t} for t in tokens]) as r:
                r.raise_for_status()
                data = await r.json()
            now = time.time()
            for b in data:
                asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
                bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
                ba = min(asks) if asks else None
                bb = max(bids) if bids else None
                self._rest_books[str(b["asset_id"])] = {
                    "ask": ba[0] if ba else None, "ask_size": ba[1] if ba else 0.0,
                    "bid": bb[0] if bb else None, "ts": now,
                }
        except Exception as ex:
            self.last_error = f"clob books: {ex}"

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
    def ref_price(klines: List[list], start_ts: int) -> Optional[float]:
        """Precio de referencia: apertura de la vela de 1m que empieza con la ventana."""
        for k in klines:
            if k[0] == start_ts * 1000:
                return float(k[1])
        return None

    def _ref(self, w: Window) -> Optional[float]:
        if w.slug not in self._refs:
            ref = self.ref_price(self._klines.get(w.asset, []), w.start_ts)
            if ref is None:
                return None
            self._refs[w.slug] = ref
        return self._refs[w.slug]

    def _avg_last_window(self, asset: str, end_ts: int, spot: float) -> float:
        """Media de los ticks observados dentro del ultimo minuto de la ventana (o spot si aun no hay)."""
        pts = [p for t, p in self._samples[asset] if end_ts - TWAP_S <= t <= end_ts]
        return sum(pts) / len(pts) if pts else spot

    # ── snapshot (ciclo caliente: solo memoria) ────────────────────────────
    async def snapshot(self) -> List[Market]:
        await self.start()
        self.snap_arrival = self.last_arrival
        now = time.time()
        basis = getattr(self.config, "fast_basis", 0.0)
        markets: List[Market] = []
        for w in self._active:
            if not (w.start_ts <= now < w.end_ts):
                continue
            spot = self._spot.get(w.asset)
            if not spot or now - self._spot_ts.get(w.asset, 0) > 5:
                continue
            ref, sigma = self._ref(w), self._sigma.get(w.asset)
            if ref is None or sigma is None:
                continue
            bu, bd = self._top(w.token_up), self._top(w.token_dn)
            if not bu or not bd:
                continue
            p_up = prob_up_twap(ref, spot, self._avg_last_window(w.asset, w.end_ts, spot),
                                w.end_ts - now, sigma * self.config.fast_sigma_mult, basis=basis)
            markets.append(Market(
                id=w.market_id, question=f"{w.asset} {w.tf} Up/Down {datetime.utcfromtimestamp(w.start_ts):%H:%M}Z",
                condition_id=w.condition_id, token_id_yes=w.token_up, token_id_no=w.token_dn,
                yes_price=bu["ask"] if bu["ask"] is not None else 1.0,
                no_price=bd["ask"] if bd["ask"] is not None else 1.0,
                volume=0.0, liquidity=w.liquidity,
                end_date=datetime.utcfromtimestamp(w.end_ts),
                category=f"updown-{w.tf}", asset=w.asset,
                target_price=ref, direction="above",
                kind="updown", ref_price=ref, spot=spot, p_model_yes=p_up,
                yes_bid=bu["bid"], no_bid=bd["bid"],
                yes_ask_size=bu["ask_size"], no_ask_size=bd["ask_size"],
                start_ts=w.start_ts, window_s=w.total, slug=w.slug,
            ))
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
        if time.time() > market.start_ts + market.window_s + 600:     # respaldo si Polymarket tarda
            return await self.proxy_resolution(market)
        return None

    async def proxy_resolution(self, market: Market) -> Optional[float]:
        """Respaldo: TWAP de los ultimos 60 s (velas de 1m) >= referencia."""
        try:
            k = await self._fetch_klines(market.asset)
        except Exception:
            return None
        end_ms = (market.start_ts + market.window_s) * 1000
        ref = self.ref_price(k, market.start_ts)
        last = [(float(r[1]) + float(r[4])) / 2.0 for r in k if end_ms - 60_000 <= r[0] < end_ms]
        if ref is None or not last:
            return None
        return 1.0 if sum(last) / len(last) >= ref else 0.0
