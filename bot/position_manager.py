"""
Gestión de posiciones abiertas: monitorea P&L y ejecuta cierres automáticos.
"""
from typing import List
from loguru import logger

from bot.models import Position, BotStats
from bot.executor import OrderExecutor


class PositionManager:
    def __init__(self, config, executor: OrderExecutor):
        self.config = config
        self.executor = executor
        self.positions: List[Position] = []

    def add_position(self, position: Position):
        self.positions.append(position)

    def open_positions(self) -> List[Position]:
        return [p for p in self.positions if p.status == "open"]

    async def monitor(self, stats: BotStats):
        """Revisa posiciones abiertas y cierra según TP/SL."""
        for position in self.open_positions():
            action = self._should_close(position)
            if action:
                result = await self.executor.close_position(position)
                if result.success and result.position:
                    pnl = result.position.realized_pnl
                    stats.total_pnl += pnl
                    stats.current_capital += pnl
                    stats.total_trades += 1
                    if pnl > 0:
                        stats.winning_trades += 1
                    logger.info(
                        f"Posición cerrada [{action}] {position.market.asset} "
                        f"P&L={pnl:+.2f} USDC | "
                        f"WinRate={stats.win_rate:.1%} TotalP&L={stats.total_pnl:+.2f}"
                    )

    def _should_close(self, position: Position) -> str:
        """Retorna la razón de cierre o '' si debe mantenerse."""
        if position.should_take_profit:
            return "TAKE_PROFIT"
        if position.should_stop_loss:
            return "STOP_LOSS"
        # Cerrar si el mercado ya expiró
        if position.market.hours_to_expiry <= 0:
            return "EXPIRED"
        return ""

    def summary(self) -> str:
        open_pos = self.open_positions()
        total_invested = sum(p.size_usdc for p in open_pos)
        total_unrealized = sum(p.unrealized_pnl for p in open_pos)
        lines = [
            f"Posiciones abiertas: {len(open_pos)} | "
            f"Invertido: {total_invested:.2f} USDC | "
            f"P&L no realizado: {total_unrealized:+.2f} USDC"
        ]
        for p in open_pos:
            lines.append(f"  {p}")
        return "\n".join(lines)
