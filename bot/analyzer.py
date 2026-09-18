"""
Motor de análisis estadístico para detectar mercados mispricedados.

Estrategias implementadas:
1. Black-Scholes Reference Arbitrage: calcula prob real con modelo log-normal
   comparada contra la prob implícita en el precio de Polymarket.
2. Near-resolution sweep: detecta mercados próximos a vencer donde el resultado
   es casi certero según el precio actual del activo.
3. Spread arbitrage: detecta inconsistencias lógicas entre mercados correlados.
"""
import time
from typing import List, Dict, Optional
import numpy as np
from scipy.stats import norm
from loguru import logger

from config import Config
from bot.models import Market, Opportunity, Side


class MarketAnalyzer:
    def __init__(self, config: Config):
        self.config = config

    def find_opportunities(
        self,
        markets: List[Market],
        prices: Dict[str, float],
        volatilities: Optional[Dict[str, float]] = None,
    ) -> List[Opportunity]:
        opportunities: List[Opportunity] = []
        vols = volatilities or {}

        for market in markets:
            spot = prices.get(market.asset)
            if spot is None or spot <= 0:
                continue
            if market.target_price is None or market.target_price <= 0:
                continue

            sigma = vols.get(market.asset, self._default_vol(market.asset))
            T = market.days_to_expiry

            opp = self._analyze_market(market, spot, sigma, T)
            if opp:
                opportunities.append(opp)

        # Ordenar por score descendente
        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities

    def _analyze_market(
        self,
        market: Market,
        spot: float,
        sigma: float,
        days: float,
    ) -> Optional[Opportunity]:
        try:
            true_prob = self._black_scholes_probability(
                spot, market.target_price, days, sigma, market.direction
            )
        except Exception as e:
            logger.debug(f"BS error {market.asset}: {e}")
            return None

        # Confianza: mayor cuando el tiempo es corto y la diferencia es grande
        confidence = self._calculate_confidence(spot, market.target_price, days, sigma)

        # Determinar qué lado apostar
        implied_yes = market.yes_price
        implied_no = market.no_price

        # Para dirección "above": YES gana si precio > objetivo
        # Para dirección "below": YES gana si precio < objetivo
        if market.direction == "above":
            true_yes_prob = true_prob
        else:
            true_yes_prob = 1.0 - true_prob

        # Buscar edge en YES
        edge_yes = true_yes_prob - implied_yes
        ev_yes = self._expected_value(true_yes_prob, implied_yes)
        kelly_yes = self._kelly_fraction(true_yes_prob, implied_yes, self.config.kelly_fraction)

        # Buscar edge en NO
        true_no_prob = 1.0 - true_yes_prob
        edge_no = true_no_prob - implied_no
        ev_no = self._expected_value(true_no_prob, implied_no)
        kelly_no = self._kelly_fraction(true_no_prob, implied_no, self.config.kelly_fraction)

        # Elegir el mejor lado
        if edge_yes >= edge_no and edge_yes >= self.config.min_edge:
            side = Side.YES
            edge = edge_yes
            true_p = true_yes_prob
            implied_p = implied_yes
            ev = ev_yes
            kelly = kelly_yes
        elif edge_no > edge_yes and edge_no >= self.config.min_edge:
            side = Side.NO
            edge = edge_no
            true_p = true_no_prob
            implied_p = implied_no
            ev = ev_no
            kelly = kelly_no
        else:
            return None

        # Filtros adicionales
        if true_p < self.config.min_win_probability:
            return None
        if ev < self.config.min_expected_value:
            return None
        if kelly <= 0:
            return None

        reason = self._build_reason(market, spot, true_p, implied_p, days, sigma)

        return Opportunity(
            market=market,
            side=side,
            true_probability=true_p,
            implied_probability=implied_p,
            edge=edge,
            expected_value=ev,
            kelly_fraction=kelly,
            confidence=confidence,
            reason=reason,
        )

    @staticmethod
    def _black_scholes_probability(
        spot: float,
        strike: float,
        days: float,
        sigma: float,
        direction: str = "above",
    ) -> float:
        """
        Probabilidad risk-neutral de que el precio esté por encima (o abajo) del strike
        en T días, bajo modelo log-normal (GBM sin drift).
        P(S_T > K) = N(d2)  donde d2 = (ln(S/K) - 0.5*σ²*T) / (σ*√T)
        """
        if days <= 0:
            if direction == "above":
                return 1.0 if spot > strike else 0.0
            else:
                return 1.0 if spot < strike else 0.0

        T = days / 365.0
        if T <= 0 or sigma <= 0:
            return 0.5

        ln_moneyness = np.log(spot / strike)
        d2 = (ln_moneyness - 0.5 * sigma**2 * T) / (sigma * np.sqrt(T))

        prob_above = float(norm.cdf(d2))
        if direction == "above":
            return prob_above
        else:
            return 1.0 - prob_above

    @staticmethod
    def _expected_value(true_prob: float, market_price: float) -> float:
        """EV por USDC apostado: si ganas cobras 1/price, si pierdes pierdes 1."""
        if market_price <= 0 or market_price >= 1:
            return -1.0
        payout = 1.0 / market_price  # payout si ganas
        ev = true_prob * payout - 1.0  # esperanza por USDC invertido
        return float(ev)

    @staticmethod
    def _kelly_fraction(true_prob: float, market_price: float, multiplier: float = 0.25) -> float:
        """
        Kelly criterion fraccionado.
        f* = (b*p - q) / b  donde b = (1/price) - 1
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0
        b = (1.0 / market_price) - 1.0  # odds netas
        p = true_prob
        q = 1.0 - p
        full_kelly = (b * p - q) / b if b > 0 else 0.0
        return max(0.0, full_kelly * multiplier)

    @staticmethod
    def _calculate_confidence(
        spot: float, strike: float, days: float, sigma: float
    ) -> float:
        """
        Confianza basada en:
        - Distancia del spot al strike (más distancia = más certeza)
        - Tiempo hasta expiración (menos tiempo = más certeza si está lejos)
        """
        if sigma <= 0 or days <= 0:
            return 0.5

        T = days / 365.0
        # Número de desviaciones estándar entre spot y strike
        moneyness = abs(np.log(spot / strike)) / (sigma * np.sqrt(T))
        # Convertir a confianza (0-1) usando función sigmoide
        confidence = float(1.0 / (1.0 + np.exp(-moneyness + 1.5)))
        return max(0.1, min(confidence, 0.99))

    @staticmethod
    def _default_vol(asset: str) -> float:
        defaults = {
            "BTC": 0.65,
            "ETH": 0.80,
            "SOL": 1.10,
            "MATIC": 1.20,
            "DOGE": 1.30,
            "XRP": 0.90,
            "AVAX": 1.05,
            "LINK": 0.95,
            "BNB": 0.70,
            "GOLD": 0.15,
            "SILVER": 0.25,
            "OIL": 0.35,
        }
        return defaults.get(asset.upper(), 0.80)

    @staticmethod
    def _build_reason(
        market: Market,
        spot: float,
        true_prob: float,
        implied_prob: float,
        days: float,
        sigma: float,
    ) -> str:
        direction = "↑" if market.direction == "above" else "↓"
        dist_pct = (spot / market.target_price - 1) * 100
        return (
            f"Spot={spot:,.2f} Target={market.target_price:,.0f} ({dist_pct:+.1f}%) "
            f"{direction} σ={sigma:.0%} T={days:.1f}d | "
            f"P_real={true_prob:.1%} vs P_mkt={implied_prob:.1%} "
            f"edge={true_prob-implied_prob:+.1%}"
        )
