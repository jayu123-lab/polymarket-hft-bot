"""
Polymarket HFT Bot - Dashboard visual
======================================
Uso:
    python main.py              # Modo paper (simulacion)
    python main.py --live       # Modo live (requiere .env configurado)
    python main.py --cycles 50  # N ciclos y termina
    python main.py --debug      # Logs detallados en pantalla
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from loguru import logger

from config import Config
from bot.scanner import MarketScanner
from bot.price_feed import PriceFeed
from bot.analyzer import MarketAnalyzer
from bot.risk_manager import RiskManager
from bot.executor import OrderExecutor
from bot.position_manager import PositionManager
from bot.dashboard import Dashboard, side_label
from bot.models import BotStats
from bot.updown import UpDownFeed

LOG_DIR = Path(__file__).parent / "logs"


def setup_logger():
    logger.remove()
    try:
        LOG_DIR.mkdir(exist_ok=True)
        logger.add(str(LOG_DIR / "bot_{time:YYYY-MM-DD}.log"), rotation="1 day",
                   retention="7 days", level="DEBUG", enqueue=True)
    except OSError:
        fallback = Path.home() / "polymarket-logs"
        fallback.mkdir(exist_ok=True)
        logger.add(str(fallback / "bot_{time:YYYY-MM-DD}.log"), rotation="1 day",
                   retention="7 days", level="DEBUG", enqueue=True)
    if "--debug" in sys.argv:
        logger.add(sys.stderr, level="DEBUG", format="{time:HH:mm:ss} | {level:<7} | {message}")


async def _none():
    return None


async def main():
    if "--live" in sys.argv:
        os.environ["BOT_MODE"] = "live"

    config = Config()
    max_cycles = None
    if "--cycles" in sys.argv:
        idx = sys.argv.index("--cycles")
        if idx + 1 < len(sys.argv):
            max_cycles = int(sys.argv[idx + 1])

    setup_logger()

    scanner          = MarketScanner(config) if config.enable_long_markets else None
    price_feed       = PriceFeed(config) if config.enable_long_markets else None
    feed             = UpDownFeed(config) if config.enable_fast else None
    analyzer         = MarketAnalyzer(config)
    risk_manager     = RiskManager(config)
    executor         = OrderExecutor(config)
    position_manager = PositionManager(config, executor)
    if feed:
        position_manager.resolver = feed.resolution
    stats = BotStats(current_capital=config.initial_capital)
    dash  = Dashboard(config.mode, config.initial_capital, config.max_open_positions)
    dash.min_edge = config.fast_min_edge

    dash.log.add("🚀", f"Bot iniciado - Capital {config.initial_capital:.0f} USDC | modo {config.mode.upper()} | "
                       f"Kelly x{config.kelly_fraction}", "cyan")
    if feed:
        dash.log.add("🎯", f"Up/Down {','.join(feed.tfs)} en {','.join(feed.assets)} | edge neto >= "
                           f"{config.fast_min_edge:.0%} tras comision", "cyan")

    traded_ids: set = set()
    seen_opps: set = set()

    with dash.make_live() as live:
        try:
            while True:
                t0 = time.monotonic()
                stats.cycles += 1

                try:
                    # 1. Datos en paralelo
                    (static_markets, prices), fast_markets = await asyncio.gather(
                        _fetch_static(scanner, price_feed),
                        feed.snapshot() if feed else _empty_list(),
                    )
                    markets = static_markets + fast_markets

                    position_manager.refresh_prices(markets)
                    dash.windows = fast_markets
                    if feed and feed.last_error:
                        logger.debug(feed.last_error)
                        feed.last_error = ""

                    # 2. Analisis
                    volatilities = {}
                    if static_markets and price_feed:
                        assets = list({m.asset for m in static_markets})
                        vols = await asyncio.gather(*[price_feed.get_historical_volatility(a) for a in assets],
                                                    return_exceptions=True)
                        volatilities = {a: (v if isinstance(v, float) else 0.8) for a, v in zip(assets, vols)}
                    opportunities = analyzer.find_opportunities(markets, prices, volatilities)

                    # una sola entrada por ventana Up/Down (evita comprar-vender-comprar pagando comisiones)
                    opportunities = [o for o in opportunities
                                     if not (o.market.kind == "updown" and o.market.id in traded_ids)]
                    for opp in opportunities:
                        key = (opp.market.id, opp.side)
                        if key not in seen_opps:
                            seen_opps.add(key)
                            dash.log_opportunity(opp)
                    stats.opportunities_found = len(seen_opps)

                    # 3. Riesgo y ejecucion
                    approved = risk_manager.filter_opportunities(
                        opportunities, position_manager.open_positions(), stats)
                    for opp in approved:
                        size = getattr(opp, "_bet_size", risk_manager.calculate_bet_size(opp, stats.current_capital))
                        result = await executor.execute(opp, size)
                        if result.success and result.position:
                            position_manager.add_position(result.position)
                            traded_ids.add(opp.market.id)
                            dash.log_trade(side_label(opp.market, opp.side), opp.market.asset,
                                           result.position.size_usdc, opp.bet_price)
                            if config.is_live:
                                stats.current_capital -= result.position.size_usdc

                    # 4. Monitorizar / cerrar posiciones
                    before = {p.id for p in position_manager.positions if p.status != "open"}
                    await position_manager.monitor(stats)
                    for p in position_manager.positions:
                        if p.status == "closed" and p.id not in before:
                            dash.log_close(p.market.asset, p.realized_pnl,
                                           position_manager.last_reason.get(p.id, "CLOSE"))

                    stats.open_positions = len(position_manager.open_positions())
                    dash.update(stats, len(markets), opportunities, position_manager.positions)

                except Exception as e:
                    dash.log_error(str(e)[:80])
                    logger.exception(f"Error ciclo {stats.cycles}: {e}")

                live.update(dash.render())

                elapsed_ms = (time.monotonic() - t0) * 1000
                await asyncio.sleep(max(0, config.cycle_interval_ms - elapsed_ms) / 1000)

                if max_cycles and stats.cycles >= max_cycles:
                    dash.log.add("🏁", f"Fin: {max_cycles} ciclos completados", "yellow")
                    live.update(dash.render())
                    await asyncio.sleep(2)
                    break

        except (KeyboardInterrupt, asyncio.CancelledError):
            dash.log.add("🛑", "Bot detenido por el usuario", "yellow")
            live.update(dash.render())
            await asyncio.sleep(1)

        finally:
            for c in (scanner, price_feed, feed):
                if c:
                    await c.close()

    open_left = len(position_manager.open_positions())
    print(f"\n{'-'*55}")
    print(f"  RESUMEN FINAL")
    print(f"  Ciclos : {stats.cycles}")
    print(f"  Trades : {stats.total_trades}  (W:{stats.winning_trades} / L:{stats.total_trades - stats.winning_trades})")
    print(f"  WinRate: {stats.win_rate:.1%}")
    print(f"  P&L    : {stats.total_pnl:+.2f} USDC   (posiciones aun abiertas: {open_left})")
    print(f"  ROI    : {stats.roi:+.2%}")
    print(f"{'-'*55}\n")


async def _empty_list():
    return []


async def _fetch_static(scanner, price_feed):
    if not scanner:
        return [], {}
    return await asyncio.gather(scanner.get_active_markets(), price_feed.get_prices())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
