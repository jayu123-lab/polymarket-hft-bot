"""
Analizador de oportunidades — Estrategia: Spread Arbitrage.

Cuando YES_price + NO_price < 1.00, comprar AMBOS lados es ganancia garantizada.
El spread neto (después de fees) es el beneficio por USDC invertido.

Ejemplo:
  YES = 0.43,  NO = 0.43  →  total = 0.86
  Spread bruto = 14%
  Fees Polymarket ≈ 2% × 2 = 4%
  Beneficio neto = 10% garantizado (sin importar cómo resuelve)
"""
from typing import List, Dict, Optional
from loguru import logger

from config import Config
from bot.models import Market, Opportunity, Side

TAKER_FEE = 0.02   # fee por lado


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

        for market in markets:
            opp = self._analyze_spread(market)
            if opp:
                opportunities.append(opp)

        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities

    def _analyze_spread(self, market: Market) -> Optional[Opportunity]:
        yes_p = market.yes_price
        no_p  = market.no_price
        total = yes_p + no_p

        if total >= 1.0:
            return None  # imposible ganar

        spread_gross = 1.0 - total
        spread_net   = spread_gross - 2 * TAKER_FEE  # fees de ambos lados

        if spread_net < self.config.min_edge:
            return None

        # Cuando compramos ambos lados, siempre cobraremos 1.00
        # Coste total = YES + NO
        # EV por USDC total invertido = spread_net / total
        ev_per_usdc = spread_net / total if total > 0 else 0

        if ev_per_usdc < self.config.min_expected_value:
            return None

        # Kelly: fracción óptima para un trade garantizado (EV positivo sin riesgo)
        # Como es "riskless" (siempre gana), Kelly = min(max_bet, EV-based sizing)
        kelly = min(0.50, ev_per_usdc * self.config.kelly_fraction * 5)

        # Reportamos la oportunidad como "YES" (representando ambos lados)
        reason = (
            f"SPREAD ARBI: YES={yes_p:.3f} + NO={no_p:.3f} = {total:.3f} "
            f"| spread bruto={spread_gross:.1%} neto={spread_net:.1%} "
            f"| EV={ev_per_usdc:.3f} por USDC"
        )

        return Opportunity(
            market=market,
            side=Side.YES,           # señal de "comprar ambos"
            true_probability=1.0,    # guaranteed win (siempre resuelve)
            implied_probability=total,
            edge=spread_net,
            expected_value=ev_per_usdc,
            kelly_fraction=kelly,
            confidence=0.99,         # garantizado matemáticamente
            reason=reason,
        )

    # Mantenemos compatibilidad con la firma original
    @staticmethod
    def _default_vol(asset: str) -> float:
        return 0.65
