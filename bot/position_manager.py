"""
Gestion de posiciones abiertas.
- Mercados largos: TP/SL por precio.
- Up/Down 5m/15m: venta anticipada cuando el bid (neto de comision) supera el valor justo del
  modelo, y liquidacion al vencimiento con el resultado REAL publicado por Polymarket.
"""
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from loguru import logger

from bot.models import Market, Position, BotStats, Side
from bot.executor import OrderExecutor
from bot.updown import taker_fee_per_share

SELL_MARGIN = 0.01          # vendemos si bid_neto >= valor justo + 1c
SETTLE_GRACE_S = 3


class PositionManager:
    def __init__(self, config, executor: OrderExecutor):
        self.config = config
        self.executor = executor
        self.positions: List[Position] = []
        self.resolver: Optional[Callable[[Market], Awaitable[Optional[float]]]] = None
        self.last_reason: Dict[str, str] = {}

    def add_position(self, position: Position):
        self.positions.append(position)

    def open_positions(self) -> List[Position]:
        return [p for p in self.positions if p.status == "open"]

    def refresh_prices(self, markets: List[Market]):
        """Sustituye el mercado guardado por la ultima foto (mark-to-market)."""
        fresh: Dict[str, Market] = {m.id: m for m in markets}
        for p in self.open_positions():
            m = fresh.get(p.market.id)
            if m is not None:
                p.market = m

    async def monitor(self, stats: BotStats):
        for position in self.open_positions():
            action, settle = await self._decide(position)
            if not action:
                continue
            result = await self.executor.close_position(position, settle)
            if result.success and result.position:
                pnl = result.position.realized_pnl
                stats.total_pnl += pnl
                stats.current_capital += pnl
                stats.total_trades += 1
                if pnl > 0:
                    stats.winning_trades += 1
                self.last_reason[position.id] = action
                logger.info(
                    f"Posicion cerrada [{action}] {position.market.asset} "
                    f"P&L={pnl:+.2f} USDC | WinRate={stats.win_rate:.1%} TotalP&L={stats.total_pnl:+.2f}"
                )

    async def _decide(self, position: Position) -> Tuple[str, Optional[float]]:
        if position.market.kind == "updown":
            return await self._decide_updown(position)
        return self._should_close(position), None

    async def _decide_updown(self, position: Position) -> Tuple[str, Optional[float]]:
        m = position.market
        now = time.time()
        end_ts = m.start_ts + m.window_s

        if now >= end_ts:
            if now < end_ts + SETTLE_GRACE_S or self.resolver is None:
                return "", None
            up = await self.resolver(m)
            if up is None:
                return "", None
            value = up if position.side == Side.YES else 1.0 - up
            return ("SETTLED_WIN" if value >= 0.5 else "SETTLED_LOSS"), value

        if m.p_model_yes is None:
            return "", None
        bid = m.yes_bid if position.side == Side.YES else m.no_bid
        if bid is None:
            return "", None
        fair = m.p_model_yes if position.side == Side.YES else 1.0 - m.p_model_yes
        net_bid = bid - taker_fee_per_share(bid)
        cost_ps = position.size_usdc / position.shares if position.shares else 0.0
        if net_bid >= fair + SELL_MARGIN and (end_ts - now) > 5:
            return ("TAKE_PROFIT" if net_bid > cost_ps else "CUT_LOSS"), None
        return "", None

    def _should_close(self, position: Position) -> str:
        if position.should_take_profit:
            return "TAKE_PROFIT"
        if position.should_stop_loss:
            return "STOP_LOSS"
        if position.market.hours_to_expiry <= 0:
            return "EXPIRED"
        return ""

    def summary(self) -> str:
        open_pos = self.open_positions()
        total_invested = sum(p.size_usdc for p in open_pos)
        total_unrealized = sum(p.unrealized_pnl for p in open_pos)
        return (
            f"Posiciones abiertas: {len(open_pos)} | Invertido: {total_invested:.2f} USDC | "
            f"P&L no realizado: {total_unrealized:+.2f} USDC"
        )
