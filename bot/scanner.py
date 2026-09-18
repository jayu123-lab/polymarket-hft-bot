"""
Scanner de mercados Polymarket.
Detecta oportunidades de spread arbitrage y market-making:
compra YES + NO cuando su suma es < 0.95 (ganancia garantizada).
"""
import json
import asyncio
import time
from datetime import datetime
from typing import List, Optional, Dict, Any
import aiohttp
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from config import Config
from bot.models import Market

GAMMA_API = "https://gamma-api.polymarket.com"

TAKER_FEE = 0.02  # 2% por lado → 4% total para ambos lados


def _parse_outcome_prices(raw) -> Optional[tuple[float, float]]:
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, list) or len(raw) < 2:
            return None
        return float(raw[0]), float(raw[1])
    except Exception:
        return None


def _parse_end_date(raw: Dict) -> Optional[datetime]:
    for field in ["endDateIso", "endDate"]:
        val = raw.get(field)
        if not val:
            continue
        try:
            s = val if "T" in val else val + "T23:59:59"
            return datetime.fromisoformat(s.replace("Z", ""))
        except ValueError:
            continue
    return None


def _detect_asset(question: str) -> str:
    q = question.lower()
    mapping = {
        "BTC": ["bitcoin", "btc"],
        "ETH": ["ethereum", "eth"],
        "SOL": ["solana", "sol "],
        "GOLD": ["gold", "xau"],
        "TRUMP": ["trump"],
        "ELECTION": ["election", "presidential"],
        "FED": ["federal reserve", "fed rate", "interest rate"],
        "OIL": [" oil ", "crude"],
    }
    for asset, kws in mapping.items():
        if any(k in q for k in kws):
            return asset
    return "OTHER"


class MarketScanner:
    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: List[Market] = []
        self._cache_ts: float = 0.0
        self._cache_ttl = 20.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "polymarket-hft-bot/2.0"},
            )
        return self._session

    async def get_active_markets(self) -> List[Market]:
        now = time.monotonic()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        raw_markets = await self._fetch_all_markets()
        markets: List[Market] = []
        counts = {"parse_err": 0, "spread_low": 0, "liq_low": 0, "expired": 0, "ok": 0}

        for raw in raw_markets:
            m = self._parse_market(raw, counts)
            if m:
                markets.append(m)

        # Ordenar por spread descendente (mejor oportunidad primero)
        markets.sort(key=lambda m: m.spread, reverse=True)

        self._cache = markets
        self._cache_ts = now
        logger.info(
            f"Scanner: {len(raw_markets)} totales → {len(markets)} con spread útil | {counts}"
        )
        return markets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=5))
    async def _fetch_all_markets(self) -> List[Dict]:
        session = await self._get_session()
        all_markets: List[Dict] = []
        limit = 100

        for offset in range(0, 1200, limit):
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            }
            async with session.get(f"{GAMMA_API}/markets", params=params) as resp:
                if resp.status == 422:
                    break
                resp.raise_for_status()
                page = await resp.json()

            if not page:
                break
            all_markets.extend(page)
            if len(page) < limit:
                break
            await asyncio.sleep(0.05)  # respetar rate limit

        return all_markets

    def _parse_market(self, raw: Dict, counts: dict) -> Optional[Market]:
        try:
            prices = _parse_outcome_prices(raw.get("outcomePrices"))
            if prices is None:
                counts["parse_err"] += 1
                return None
            yes_price, no_price = prices

            # CRITERIO PRINCIPAL: spread > umbral (YES + NO < 1 - umbral_spread)
            total = yes_price + no_price
            spread = 1.0 - total
            net_spread = spread - 2 * TAKER_FEE  # spread neto después de fees

            if net_spread < self.config.min_edge:
                counts["spread_low"] += 1
                return None

            # Liquidez mínima
            liq = float(raw.get("liquidityClob") or raw.get("liquidity") or 0)
            if liq < self.config.min_market_liquidity:
                counts["liq_low"] += 1
                return None

            # Fecha de vencimiento
            end_date = _parse_end_date(raw)
            if end_date is None:
                counts["parse_err"] += 1
                return None

            # Horas hasta expiración
            hours_left = max(0, (end_date - datetime.utcnow()).total_seconds() / 3600)
            if hours_left < self.config.min_hours_to_expiry:
                counts["expired"] += 1
                return None

            token_ids = raw.get("clobTokenIds") or []
            question = raw.get("question", "No question")
            asset = _detect_asset(question)

            counts["ok"] += 1
            return Market(
                id=str(raw.get("id", "")),
                question=question,
                condition_id=str(raw.get("conditionId", "")),
                token_id_yes=str(token_ids[0]) if len(token_ids) > 0 else "",
                token_id_no=str(token_ids[1]) if len(token_ids) > 1 else "",
                yes_price=yes_price,
                no_price=no_price,
                volume=float(raw.get("volumeClob") or raw.get("volume") or 0),
                liquidity=liq,
                end_date=end_date,
                category=asset.lower(),
                asset=asset,
                target_price=None,
                direction="above",
            )

        except Exception as e:
            logger.debug(f"Parse error: {e}")
            counts["parse_err"] += 1
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
