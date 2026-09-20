"""
Gestion de posiciones abiertas.
- Mercados largos: TP/SL por precio.
- Up/Down 5m/15m:
    * venta anticipada cuando el bid (neto de comision) supera el valor justo del modelo,
    * bloqueo de beneficio: vende al ganar +LOCK_PROFIT_PCT neto (si AUTO_COLLECT),
    * cesta: al sumar +BASKET_TARGET_PCT del capital cobra todas las posiciones en positivo,
    * liquidacion al vencimiento con el resultado REAL publicado por Polymarket,
    * recogida manual (tecla C: cobrar lo que va en positivo, tecla X: cerrar todo).
"""
import asyncio
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from loguru import logger

from bot.models import Market, Position, BotStats, Side
from bot.executor import OrderExecutor
from bot.updown import taker_fee_per_share

SELL_MARGIN = 0.01          # vendemos si bid_neto >= valor justo + 1c
SETTLE_GRACE_S = 3
MIN_SELL_LEFT_S = 5         # no vender con menos de 5 s (mejor liquidar al vencimiento, sin comision)


class PositionManager:
    def __init__(self, config, executor: OrderExecutor):
        self.config = config
        self.executor = executor
        self.positions: List[Position] = []
        self.resolver: Optional[Callable[[Market], Awaitable[Optional[float]]]] = None
        self.last_reason: Dict[str, str] = {}
        self.auto_collect: bool = config.auto_collect
        self.lock_pct: float = config.lock_profit_pct
        self.basket_target: float = config.basket_target_pct * config.initial_capital

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

    # ── valoracion ─────────────────────────────────────────────────────────
    @staticmethod
    def sellable(p: Position) -> bool:
        """True si aun se puede vender en el libro (ventana viva y hay bid)."""
        m = p.market
        if m.kind == "updown":
            bid = m.yes_bid if p.side == Side.YES else m.no_bid
            return bid is not None and (m.start_ts + m.window_s - time.time()) > MIN_SELL_LEFT_S
        return True

    @staticmethod
    def net_value(p: Position) -> float:
        """Lo que cobrariamos si vendemos ahora al bid, ya descontada la comision."""
        bid = p.current_price
        fee = taker_fee_per_share(bid) if p.market.kind == "updown" else 0.0
        return p.shares * (bid - fee)

    def basket_pnl(self) -> float:
        """Beneficio neto no realizado de todas las posiciones vendibles."""
        return sum(self.net_value(p) - p.size_usdc for p in self.open_positions() if self.sellable(p))

    # ── ciclo de decision ──────────────────────────────────────────────────
    async def monitor(self, stats: BotStats) -> List[Position]:
        open_pos = self.open_positions()
        decisions = await asyncio.gather(*[self._decide(p) for p in open_pos])
        todo: Dict[str, Tuple[Position, str, Optional[float]]] = {
            p.id: (p, action, settle) for p, (action, settle) in zip(open_pos, decisions) if action
        }

        # cesta: al alcanzar el objetivo, cobra todo lo que va en positivo
        if self.auto_collect and self.basket_target > 0 and self.basket_pnl() >= self.basket_target:
            for p in open_pos:
                if p.id not in todo and self.sellable(p) and self.net_value(p) > p.size_usdc:
                    todo[p.id] = (p, "CESTA", None)

        return await self._close_many(list(todo.values()), stats)

    async def collect(self, stats: BotStats, everything: bool = False) -> List[Position]:
        """Tecla C (solo en positivo) / X (todo): venta a mercado inmediata de lo vendible."""
        items = []
        for p in self.open_positions():
            if not self.sellable(p):
                continue
            if everything:
                items.append((p, "CIERRE_MANUAL", None))
            elif self.net_value(p) > p.size_usdc:
                items.append((p, "COBRADO", None))
        return await self._close_many(items, stats)

    async def _close_many(self, items: List[Tuple[Position, str, Optional[float]]], stats: BotStats) -> List[Position]:
        if not items:
            return []
        results = await asyncio.gather(*[self.executor.close_position(p, settle) for p, _, settle in items])
        closed = []
        for (p, action, _), result in zip(items, results):
            if result.success and result.position:
                pnl = result.position.realized_pnl
                stats.total_pnl += pnl
                stats.current_capital += pnl
                stats.total_trades += 1
                if pnl > 0:
                    stats.winning_trades += 1
                self.last_reason[p.id] = action
                closed.append(p)
                logger.info(
                    f"Posicion cerrada [{action}] {p.market.asset} P&L={pnl:+.2f} USDC | "
                    f"WinRate={stats.win_rate:.1%} TotalP&L={stats.total_pnl:+.2f}"
                )
        return closed

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

        if m.p_model_yes is None or (end_ts - now) <= MIN_SELL_LEFT_S:
            return "", None
        bid = m.yes_bid if position.side == Side.YES else m.no_bid
        if bid is None:
            return "", None
        net_bid = bid - taker_fee_per_share(bid)

        if self.auto_collect and self.lock_pct > 0 and position.size_usdc > 0 \
                and net_bid * position.shares >= position.size_usdc * (1.0 + self.lock_pct):
            return "LOCK_PROFIT", None

        fair = m.p_model_yes if position.side == Side.YES else 1.0 - m.p_model_yes
        cost_ps = position.size_usdc / position.shares if position.shares else 0.0
        if net_bid >= fair + SELL_MARGIN:
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
