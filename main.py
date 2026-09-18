"""
Polymarket HFT Bot — Dashboard visual
======================================
Uso:
    python main.py              # Modo paper (simulación)
    python main.py --live       # Modo live (requiere .env configurado)
    python main.py --cycles 50  # N ciclos y termina
    python main.py --debug      # Logs detallados en archivo
"""
import asyncio
import sys
import time
from loguru import logger

from config import Config
from bot.scanner import MarketScanner
from bot.price_feed import PriceFeed
from bot.analyzer import MarketAnalyzer
from bot.risk_manager import RiskManager
from bot.executor import OrderExecutor
from bot.position_manager import PositionManager
from bot.dashboard import Dashboard
from bot.models import BotStats


def setup_logger():
    logger.remove()
    # Solo al archivo; la pantalla la maneja Rich
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="7 days",
        level="DEBUG",
        enqueue=True,
    )
    if "--debug" in sys.argv:
        logger.add(sys.stderr, level="DEBUG", format="{time:HH:mm:ss} | {level:<7} | {message}")


async def main():
    if "--live" in sys.argv:
        import os; os.environ["BOT_MODE"] = "live"

    config = Config()
    max_cycles = None
    if "--cycles" in sys.argv:
        idx = sys.argv.index("--cycles")
        if idx + 1 < len(sys.argv):
            max_cycles = int(sys.argv[idx + 1])

    setup_logger()

    # Componentes del bot
    scanner          = MarketScanner(config)
    price_feed       = PriceFeed(config)
    analyzer         = MarketAnalyzer(config)
    risk_manager     = RiskManager(config)
    executor         = OrderExecutor(config)
    position_manager = PositionManager(config, executor)
    stats            = BotStats(current_capital=config.initial_capital)
    dash             = Dashboard(config.mode, config.initial_capital)

    dash.log.add("🚀", f"Bot iniciado — Capital: {config.initial_capital:.0f} USDC  Edge≥{config.min_edge:.0%}  Kelly×{config.kelly_fraction}", "cyan")
    dash.log.add("🔧", f"Filtros: liq≥{config.min_market_liquidity:.0f}  t∈[{config.min_hours_to_expiry:.0f}h, {config.max_hours_to_expiry:.0f}h]", "dim white")

    with dash.make_live() as live:
        try:
            while True:
                t0 = time.monotonic()
                stats.cycles += 1

                try:
                    # ── 1. Datos en paralelo ──────────────────────────
                    markets, prices = await asyncio.gather(
                        scanner.get_active_markets(),
                        price_feed.get_prices(),
                    )

                    assets_found = list({m.asset for m in markets})
                    dash.update(stats, len(markets), dash._last_opps, position_manager.positions)
                    if stats.cycles % 15 == 1:
                        dash.log_scan(len(markets), assets_found)

                    opportunities = []
                    if markets:
                        # ── 2. Volatilidades ─────────────────────────
                        vol_values = await asyncio.gather(
                            *[price_feed.get_historical_volatility(a) for a in assets_found],
                            return_exceptions=True,
                        )
                        volatilities = {
                            a: (v if isinstance(v, float) else 0.8)
                            for a, v in zip(assets_found, vol_values)
                        }

                        # ── 3. Análisis ───────────────────────────────
                        opportunities = analyzer.find_opportunities(markets, prices, volatilities)
                        stats.opportunities_found += len(opportunities)

                        if opportunities:
                            for opp in opportunities[:3]:
                                dash.log_opportunity(opp)

                        # ── 4. Filtrar por riesgo ─────────────────────
                        open_pos = position_manager.open_positions()
                        approved = risk_manager.filter_opportunities(opportunities, open_pos, stats)

                        # ── 5. Ejecutar ───────────────────────────────
                        for opp in approved:
                            size = getattr(opp, "_bet_size", risk_manager.calculate_bet_size(opp, stats.current_capital))
                            result = await executor.execute(opp, size)
                            if result.success and result.position:
                                position_manager.add_position(result.position)
                                dash.log_trade(str(opp.side), opp.market.asset, size, opp.bet_price)
                                if config.is_live:
                                    stats.current_capital -= size

                    # ── 6. Monitorear posiciones ──────────────────────
                    prev_closed = [p for p in position_manager.positions if p.status != "open"]
                    await position_manager.monitor(stats)
                    for p in position_manager.positions:
                        if p.status == "closed" and p not in prev_closed:
                            dash.log_close(p.market.asset, p.realized_pnl, "TP/SL")

                    stats.open_positions = len(position_manager.open_positions())
                    dash.update(stats, len(markets), opportunities, position_manager.positions)

                except Exception as e:
                    dash.log_error(str(e)[:80])
                    logger.exception(f"Error ciclo {stats.cycles}: {e}")

                # ── Refresca el dashboard ─────────────────────────────
                live.update(dash.render())

                elapsed_ms = (time.monotonic() - t0) * 1000
                sleep_ms   = max(0, config.cycle_interval_ms - elapsed_ms)
                await asyncio.sleep(sleep_ms / 1000)

                if max_cycles and stats.cycles >= max_cycles:
                    dash.log.add("🏁", f"Fin: {max_cycles} ciclos completados", "yellow")
                    live.update(dash.render())
                    await asyncio.sleep(2)
                    break

        except KeyboardInterrupt:
            dash.log.add("🛑", "Bot detenido por el usuario", "yellow")
            live.update(dash.render())
            await asyncio.sleep(1)

        finally:
            await scanner.close()
            await price_feed.close()

    # Resumen final en consola normal
    print(f"\n{'─'*55}")
    print(f"  RESUMEN FINAL")
    print(f"  Ciclos : {stats.cycles}")
    print(f"  Trades : {stats.total_trades}  (W:{stats.winning_trades} / L:{stats.total_trades - stats.winning_trades})")
    print(f"  WinRate: {stats.win_rate:.1%}")
    print(f"  P&L    : {stats.total_pnl:+.2f} USDC")
    print(f"  ROI    : {stats.roi:+.2%}")
    print(f"{'─'*55}\n")


if __name__ == "__main__":
    asyncio.run(main())
