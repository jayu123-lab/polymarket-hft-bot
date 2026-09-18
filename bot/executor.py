"""
Ejecutor de órdenes — Spread Arbitrage.
Compra YES + NO simultáneamente para capturar el spread garantizado.
"""
import uuid
from datetime import datetime
from loguru import logger

from config import Config
from bot.models import Opportunity, Position, TradeResult, Side

TAKER_FEE = 0.02


class OrderExecutor:
    def __init__(self, config: Config):
        self.config = config
        self._client = None

    def _get_clob_client(self):
        if self._client:
            return self._client
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=self.config.poly_api_key,
                api_secret=self.config.poly_api_secret,
                api_passphrase=self.config.poly_api_passphrase,
            )
            self._client = ClobClient(
                "https://clob.polymarket.com",
                key=self.config.poly_private_key,
                chain_id=self.config.poly_chain_id,
                creds=creds,
            )
        except ImportError:
            raise RuntimeError("py-clob-client no instalado")
        return self._client

    async def execute(self, opp: Opportunity, size_usdc: float) -> TradeResult:
        if self.config.mode == "paper":
            return self._paper_execute(opp, size_usdc)
        return await self._live_execute(opp, size_usdc)

    def _paper_execute(self, opp: Opportunity, size_usdc: float) -> TradeResult:
        """
        Spread arbitrage: divide el capital y compra YES + NO.
        half_size → YES,  half_size → NO
        Payout garantizado = size_usdc + spread_neto * size_usdc
        """
        half = size_usdc / 2
        yes_p = opp.market.yes_price
        no_p  = opp.market.no_price
        total_cost = yes_p + no_p
        spread_gross = 1.0 - total_cost
        spread_net   = spread_gross - 2 * TAKER_FEE
        guaranteed_pnl = spread_net * size_usdc

        tp = min(0.99, max(yes_p, no_p) + spread_net * 0.5)
        sl = max(0.01, min(yes_p, no_p) - spread_net * 0.3)

        position = Position(
            id=f"arb_{uuid.uuid4().hex[:8]}",
            market=opp.market,
            side=Side.YES,         # representa "ambos lados"
            size_usdc=size_usdc,
            entry_price=total_cost,
            entry_time=datetime.utcnow(),
            shares=size_usdc / total_cost if total_cost > 0 else 0,
            target_exit_price=tp,
            stop_loss_price=sl,
        )
        logger.info(
            f"[PAPER ARBI] {opp.market.asset} | "
            f"YES={yes_p:.3f} + NO={no_p:.3f} = {total_cost:.3f} | "
            f"size={size_usdc:.2f} USDC | "
            f"P&L garantizado={guaranteed_pnl:+.2f} USDC ({spread_net:.1%})"
        )
        return TradeResult(success=True, order_id=position.id, position=position)

    async def _live_execute(self, opp: Opportunity, size_usdc: float) -> TradeResult:
        """
        Modo live: envía orden de compra YES + orden de compra NO simultáneamente.
        """
        try:
            client = self._get_clob_client()
            half = round(size_usdc / 2, 2)

            from py_clob_client.clob_types import OrderArgs

            results = []
            for token_id, price, label in [
                (opp.market.token_id_yes, opp.market.yes_price, "YES"),
                (opp.market.token_id_no,  opp.market.no_price,  "NO"),
            ]:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=round(half / price, 2),
                    side="BUY",
                )
                resp = client.create_and_post_order(order_args)
                if resp and not resp.get("errorCode"):
                    results.append(resp.get("orderID", "?"))
                    logger.info(f"[LIVE] ORDEN {label} enviada id={resp.get('orderID')}")
                else:
                    logger.error(f"[LIVE] Error orden {label}: {resp}")
                    return TradeResult(success=False, order_id=None, position=None, error=str(resp))

            total_cost = opp.market.yes_price + opp.market.no_price
            position = Position(
                id="-".join(results),
                market=opp.market,
                side=Side.YES,
                size_usdc=size_usdc,
                entry_price=total_cost,
                entry_time=datetime.utcnow(),
                shares=size_usdc / total_cost if total_cost > 0 else 0,
                target_exit_price=0.97,
                stop_loss_price=0.03,
            )
            return TradeResult(success=True, order_id=position.id, position=position)

        except Exception as e:
            logger.error(f"Error ejecutando arbi live: {e}")
            return TradeResult(success=False, order_id=None, position=None, error=str(e))

    async def close_position(self, position: Position) -> TradeResult:
        if self.config.mode == "paper":
            return self._paper_close(position)
        return TradeResult(success=False, order_id=None, position=position, error="live close not impl")

    def _paper_close(self, position: Position) -> TradeResult:
        # En spread arbi, el P&L es fijo: el spread neto capturado al entrar
        entry_total   = position.entry_price          # YES + NO al entrar
        spread_net    = (1.0 - entry_total) - 2 * TAKER_FEE
        pnl           = spread_net * position.size_usdc

        position.exit_price  = 1.0
        position.exit_time   = datetime.utcnow()
        position.status      = "closed"
        position.realized_pnl = pnl
        logger.info(
            f"[PAPER ARBI] CIERRE {position.market.asset} | "
            f"P&L={pnl:+.2f} USDC ({spread_net:.1%})"
        )
        return TradeResult(success=True, order_id=position.id, position=position)
