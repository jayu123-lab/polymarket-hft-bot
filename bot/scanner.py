import re
import asyncio
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import aiohttp
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from config import Config
from bot.models import Market

GAMMA_API = "https://gamma-api.polymarket.com"

# Keywords para detectar activos en preguntas
ASSET_KEYWORDS: Dict[str, List[str]] = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth", "ether"],
    "SOL": ["solana", "sol"],
    "MATIC": ["polygon", "matic"],
    "DOGE": ["dogecoin", "doge"],
    "XRP": ["ripple", "xrp"],
    "BNB": ["bnb", "binance coin"],
    "ADA": ["cardano", "ada"],
    "AVAX": ["avalanche", "avax"],
    "LINK": ["chainlink", "link"],
    "GOLD": ["gold", "xau", "golden"],
    "SILVER": ["silver", "xag"],
    "OIL": ["oil", "crude", "wti", "brent"],
}

# Regex para extraer precio objetivo de la pregunta
PRICE_PATTERN = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(?:k|K)?",
    re.IGNORECASE,
)


def _extract_asset(question: str) -> Optional[str]:
    q = question.lower()
    for asset, keywords in ASSET_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return asset
    return None


def _extract_target_price(question: str) -> Optional[float]:
    """Extrae el precio objetivo de preguntas como 'BTC above $80,000?'"""
    matches = PRICE_PATTERN.findall(question)
    if not matches:
        return None
    try:
        raw = matches[0].replace(",", "")
        value = float(raw)
        # Si dice '80k' o '80K' en el texto cerca del número
        if re.search(r"\d+\s*[kK]", question):
            value *= 1000
        return value
    except ValueError:
        return None


def _extract_direction(question: str) -> str:
    q = question.lower()
    if any(w in q for w in ["above", "over", "higher", "exceed", "surpass", "reach", "hit"]):
        return "above"
    if any(w in q for w in ["below", "under", "lower", "drop", "fall", "decline"]):
        return "below"
    return "above"


def _is_target_category(question: str, tags: List[str]) -> bool:
    q = question.lower()
    tag_str = " ".join(tags).lower()
    crypto_terms = ["bitcoin", "ethereum", "crypto", "btc", "eth", "blockchain", "defi", "nft",
                    "solana", "matic", "polygon", "xrp", "cardano"]
    commodity_terms = ["gold", "silver", "oil", "crude", "xau", "xag", "metal", "commodity"]
    all_terms = crypto_terms + commodity_terms
    return any(t in q or t in tag_str for t in all_terms)


class MarketScanner:
    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: List[Market] = []
        self._cache_ts: float = 0.0
        self._cache_ttl = 10.0  # segundos

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def get_active_markets(self) -> List[Market]:
        now = time.monotonic()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        raw_markets = await self._fetch_markets()
        markets = []
        for raw in raw_markets:
            m = self._parse_market(raw)
            if m and self._passes_filter(m):
                markets.append(m)

        self._cache = markets
        self._cache_ts = now
        logger.debug(f"Scanner: {len(markets)} mercados activos encontrados")
        return markets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=5))
    async def _fetch_markets(self) -> List[Dict[str, Any]]:
        session = await self._get_session()
        all_markets: List[Dict] = []
        offset = 0
        limit = 200

        while True:
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            }
            async with session.get(f"{GAMMA_API}/markets", params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if not data:
                break
            all_markets.extend(data)
            if len(data) < limit:
                break
            offset += limit

        return all_markets

    def _parse_market(self, raw: Dict[str, Any]) -> Optional[Market]:
        try:
            question = raw.get("question", "")
            tags = [t.get("label", "") for t in raw.get("tags", [])]

            if not _is_target_category(question, tags):
                return None

            asset = _extract_asset(question)
            if not asset:
                return None

            # Precios YES/NO de los tokens
            tokens = raw.get("tokens", [])
            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)

            if not yes_token or not no_token:
                return None

            yes_price = float(yes_token.get("price", 0.5))
            no_price = float(no_token.get("price", 0.5))

            # Fecha de cierre
            end_date_str = raw.get("endDate") or raw.get("end_date_iso")
            if not end_date_str:
                return None
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            end_date_naive = end_date.replace(tzinfo=None)

            liquidity = float(raw.get("liquidity") or raw.get("outcomePrices", {}).get("liquidity", 0))
            volume = float(raw.get("volume") or 0)

            market = Market(
                id=str(raw.get("id", "")),
                question=question,
                condition_id=str(raw.get("conditionId", "")),
                token_id_yes=str(yes_token.get("token_id", "")),
                token_id_no=str(no_token.get("token_id", "")),
                yes_price=yes_price,
                no_price=no_price,
                volume=volume,
                liquidity=liquidity,
                end_date=end_date_naive,
                category="crypto" if asset not in ("GOLD", "SILVER", "OIL") else "commodities",
                asset=asset,
                target_price=_extract_target_price(question),
                direction=_extract_direction(question),
            )
            return market

        except Exception as e:
            logger.debug(f"Error parseando mercado: {e} | raw={raw.get('question', '')[:60]}")
            return None

    def _passes_filter(self, market: Market) -> bool:
        if market.hours_to_expiry < self.config.min_hours_to_expiry:
            return False
        if market.hours_to_expiry > self.config.max_hours_to_expiry:
            return False
        if market.liquidity < self.config.min_market_liquidity:
            return False
        if market.yes_price <= 0.01 or market.yes_price >= 0.99:
            return False
        return True

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
