"""
GestiÃ³n de riesgo: filtra oportunidades y calcula tamaÃ±os de posiciÃ³n.
Implementa Kelly fraccionado con lÃ­mites de cartera.
"""
from typing import List
from loguru import logger

from config import Config
from bot.models import Opportunity, Position, BotStats


class RiskManager:
    def __init__(self, config: Config):
        self.config = config
        self.pending_usdc = 0.0      # ordenes en vuelo (aun sin posicion)
        self.pending_count = 0

    def filter_opportunities(
        self,
        opportunities: List[Opportunity],
        open_positions: List[Position],
        stats: BotStats,
    ) -> List[Opportunity]:
        approved: List[Opportunity] = []
        open_market_ids = {p.market.id for p in open_positions if p.status == "open"}

        available_capital = self._available_capital(open_positions, stats)
        if available_capital < 1.0:
            logger.debug("Capital insuficiente, saltando ciclo")
            return []

        for opp in opportunities:
            # No duplicar posiciÃ³n en mismo mercado
            if opp.market.id in open_market_ids:
                continue

            # LÃ­mite de posiciones abiertas
            if len(open_positions) + self.pending_count + len(approved) >= self.config.max_open_positions:
                break

            # Validar umbrales
            if opp.edge < self.config.min_edge:
                continue
            # En Up/Down se compra por edge de precio (p. ej. p=48% a 0.40), no por alta probabilidad
            if opp.market.kind != "updown" and opp.true_probability < self.config.min_win_probability:
                continue
            if opp.expected_value < self.config.min_expected_value:
                continue

            # Calcular tamaÃ±o
            size = self.calculate_bet_size(opp, available_capital)
            if size < 1.0:
                continue

            opp._bet_size = size  # type: ignore[attr-defined]
            approved.append(opp)
            available_capital -= size

        return approved

    def calculate_bet_size(self, opp: Opportunity, available_capital: float) -> float:
        """
        TamaÃ±o de apuesta usando Kelly fraccionado con lÃ­mites de cartera.
        size = min(kelly_fraction * capital, max_bet_size)
        """
        kelly_size = opp.kelly_fraction * available_capital
        max_size = min(self.config.max_bet_size, available_capital * 0.10)
        size = min(kelly_size, max_size)
        # Redondear a 2 decimales y asegurar mÃ­nimo de $1
        return max(0.0, round(size, 2))

    def calculate_exits(self, opp: Opportunity) -> tuple[float, float]:
        """Calcula take-profit y stop-loss basado en el edge."""
        price = opp.bet_price
        # Take profit: cuando el mercado corrija hasta nuestra prob real estimada
        take_profit = min(0.97, price + opp.edge * 0.6)
        # Stop loss: si el precio cae 40% del edge hacia abajo
        stop_loss = max(0.03, price - opp.edge * 0.4)
        return round(take_profit, 4), round(stop_loss, 4)

    def _available_capital(self, open_positions: List[Position], stats: BotStats) -> float:
        invested = sum(p.size_usdc for p in open_positions if p.status == "open")
        return max(0.0, stats.current_capital - invested - self.pending_usdc)

    def portfolio_exposure(self, open_positions: List[Position], stats: BotStats) -> float:
        invested = sum(p.size_usdc for p in open_positions if p.status == "open")
        return invested / stats.current_capital if stats.current_capital > 0 else 0.0

    def log_risk_summary(self, open_positions: List[Position], stats: BotStats):
        exposure = self.portfolio_exposure(open_positions, stats)
        available = self._available_capital(open_positions, stats)
        logger.info(
            f"Risk | Capital={stats.current_capital:.2f} USDC "
            f"ExposiciÃ³n={exposure:.1%} Disponible={available:.2f} "
            f"Posiciones={len(open_positions)}/{self.config.max_open_positions}"
        )

