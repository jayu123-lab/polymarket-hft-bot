"""
Ejecutor de órdenes para Polymarket CLOB API.
Soporta modo paper (simulación) y modo live (trading real).
"""
import asyncio
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Callable, Optional, Tuple
from loguru import logger

from config import Config
from bot.models import Opportunity, Position, TradeResult, Side
from bot.updown import MIN_SHARES, taker_fee_per_share


class OrderExecutor:
    def __init__(self, config: Config):
        self.config = config
        self._client = None  # py_clob_client instance (solo live)
        # quote_fn(market, side) -> (ask, tamano_ask, bid) en tiempo real; lo pone main.py
        self.quote_fn: Optional[Callable] = None

    def _get_clob_client(self):
        """Inicializa el cliente CLOB de Polymarket (lazy, solo en modo live)."""
        if self._client is not None:
            return self._client
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            host = "https://clob.polymarket.com"
            creds = ApiCreds(
                api_key=self.config.poly_api_key,
                api_secret=self.config.poly_api_secret,
                api_passphrase=self.config.poly_api_passphrase,
            )
            self._client = ClobClient(
                host,
                key=self.config.poly_private_key,
                chain_id=self.config.poly_chain_id,
                creds=creds,
            )
            logger.info("CLOB client inicializado para modo LIVE")
        except ImportError:
            logger.error("py-clob-client no instalado. Instala con: pip install py-clob-client")
            raise
        return self._client

    async def execute(self, opp: Opportunity, size_usdc: float) -> TradeResult:
        if self.config.mode == "paper":
            if opp.market.kind == "updown":
                # Relleno simulado con latencia: la orden llega al libro mas tarde y puede no llenarse
                fresh = await self._after_latency(opp)
                if fresh is None:
                    return TradeResult(success=False, order_id=None, position=None,
                                       error="el precio se movio antes de llenar")
                opp = fresh
            return self._paper_execute(opp, size_usdc)
        else:
            return await self._live_execute(opp, size_usdc)

    async def _after_latency(self, opp: Opportunity) -> Optional[Opportunity]:
        """Espera la latencia simulada y relee el libro; orden limite = ask de decision + slippage."""
        lat = self.config.paper_fill_latency_ms / 1000.0
        if lat > 0:
            await asyncio.sleep(lat)
        q = self.quote_fn(opp.market, opp.side) if self.quote_fn else None
        if q is None:
            return opp
        ask, depth, bid = q
        if ask is None or ask > opp.bet_price + self.config.paper_max_slippage + 1e-9:
            return None
        m = opp.market
        if opp.side == Side.YES:
            m2 = replace(m, yes_price=ask, yes_ask_size=depth, yes_bid=bid)
        else:
            m2 = replace(m, no_price=ask, no_ask_size=depth, no_bid=bid)
        return replace(opp, market=m2)

    def _paper_execute(self, opp: Opportunity, size_usdc: float) -> TradeResult:
        """Simula una orden sin dinero real (compra al ask del libro, con comision de taker)."""
        entry_price = opp.bet_price
        fast = opp.market.kind == "updown"
        fee_ps = taker_fee_per_share(entry_price) if fast else 0.0
        shares = size_usdc / (entry_price + fee_ps) if entry_price > 0 else 0

        if fast:
            depth = opp.market.yes_ask_size if opp.side == Side.YES else opp.market.no_ask_size
            shares = min(shares, depth)
            if shares < MIN_SHARES:
                return TradeResult(success=False, order_id=None, position=None, error="sin profundidad")
            size_usdc = round(shares * (entry_price + fee_ps), 2)
            tp, sl = 0.99, -1.0
        else:
            tp, sl = _calculate_exit_levels(opp)
        position = Position(
            id=f"paper_{uuid.uuid4().hex[:8]}",
            market=opp.market,
            side=opp.side,
            size_usdc=size_usdc,
            entry_price=entry_price,
            entry_time=datetime.utcnow(),
            shares=shares,
            target_exit_price=tp,
            stop_loss_price=sl,
            fee_paid=shares * fee_ps,
        )
        logger.info(
            f"[PAPER] COMPRA {opp.side} {opp.market.asset} | "
            f"precio={entry_price:.4f} size={size_usdc:.2f} USDC "
            f"shares={shares:.2f} | TP={tp:.3f} SL={sl:.3f}"
        )
        return TradeResult(success=True, order_id=position.id, position=position)

    async def _live_execute(self, opp: Opportunity, size_usdc: float) -> TradeResult:
        """Ejecuta una orden real vía Polymarket CLOB API."""
        try:
            client = self._get_clob_client()
            token_id = opp.market.token_id_yes if opp.side == Side.YES else opp.market.token_id_no
            price = opp.bet_price
            size = round(size_usdc / price, 2)  # shares a comprar

            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
            resp = client.create_and_post_order(order_args)

            if not resp or resp.get("errorCode"):
                error_msg = str(resp.get("errorMsg", "Unknown error"))
                logger.error(f"Error en orden: {error_msg}")
                return TradeResult(success=False, order_id=None, position=None, error=error_msg)

            order_id = resp.get("orderID") or resp.get("id", "unknown")
            tp, sl = _calculate_exit_levels(opp)
            position = Position(
                id=order_id,
                market=opp.market,
                side=opp.side,
                size_usdc=size_usdc,
                entry_price=price,
                entry_time=datetime.utcnow(),
                shares=size,
                target_exit_price=tp,
                stop_loss_price=sl,
            )
            logger.info(
                f"[LIVE] ORDEN ENVIADA {opp.side} {opp.market.asset} | "
                f"order_id={order_id} precio={price:.4f} size={size_usdc:.2f} USDC"
            )
            return TradeResult(success=True, order_id=order_id, position=position)

        except Exception as e:
            logger.error(f"Error ejecutando orden live: {e}")
            return TradeResult(success=False, order_id=None, position=None, error=str(e))

    async def close_position(self, position: Position, settle_value: Optional[float] = None) -> TradeResult:
        if self.config.mode == "paper":
            sell_price = None
            if settle_value is None and position.market.kind == "updown":
                # venta a mercado: tambien tarda; se llena al bid del momento
                lat = self.config.paper_fill_latency_ms / 1000.0
                if lat > 0:
                    await asyncio.sleep(lat)
                q = self.quote_fn(position.market, position.side) if self.quote_fn else None
                if q is not None and q[2] is not None:
                    sell_price = q[2]
            return self._paper_close(position, settle_value, sell_price)
        else:
            return await self._live_close(position)

    def _paper_close(self, position: Position, settle_value: Optional[float] = None,
                     sell_price: Optional[float] = None) -> TradeResult:
        """settle_value: 1.0/0.0 = liquidacion al vencimiento (sin comision); None = venta al bid."""
        if settle_value is not None:
            exit_price, exit_fee = settle_value, 0.0
        else:
            exit_price = sell_price if sell_price is not None else position.current_price
            exit_fee = position.shares * taker_fee_per_share(exit_price) if position.market.kind == "updown" else 0.0
        pnl = position.shares * exit_price - exit_fee - position.size_usdc
        position.exit_price = exit_price
        position.exit_time = datetime.utcnow()
        position.status = "closed"
        position.realized_pnl = pnl
        pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
        logger.info(
            f"[PAPER] CIERRE {position.side} {position.market.asset} | "
            f"entrada={position.entry_price:.4f} salida={exit_price:.4f} "
            f"P&L={pnl_str} USDC"
        )
        return TradeResult(success=True, order_id=position.id, position=position)

    async def _live_close(self, position: Position) -> TradeResult:
        try:
            client = self._get_clob_client()
            token_id = (
                position.market.token_id_yes
                if position.side == Side.YES
                else position.market.token_id_no
            )
            price = position.current_price

            from py_clob_client.clob_types import OrderArgs

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=round(position.shares, 2),
                side="SELL",
            )
            resp = client.create_and_post_order(order_args)

            if not resp or resp.get("errorCode"):
                error_msg = str(resp.get("errorMsg", "Unknown"))
                return TradeResult(success=False, order_id=None, position=position, error=error_msg)

            pnl = (price - position.entry_price) * position.shares
            position.exit_price = price
            position.exit_time = datetime.utcnow()
            position.status = "closed"
            position.realized_pnl = pnl
            return TradeResult(success=True, order_id=resp.get("orderID"), position=position)

        except Exception as e:
            logger.error(f"Error cerrando posición live: {e}")
            return TradeResult(success=False, order_id=None, position=position, error=str(e))


def _calculate_exit_levels(opp: Opportunity) -> tuple[float, float]:
    """Calcula take-profit y stop-loss basados en el edge."""
    price = opp.bet_price
    take_profit = min(0.97, price + opp.edge * 0.6)
    stop_loss = max(0.03, price - opp.edge * 0.4)
    return round(take_profit, 4), round(stop_loss, 4)
