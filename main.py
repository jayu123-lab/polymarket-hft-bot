"""
Polymarket HFT Bot - Dashboard visual
======================================
Uso:
    python main.py              # Modo paper (simulacion)
    python main.py --live       # Modo live (requiere .env configurado)
    python main.py --cycles 50  # N ciclos y termina
    python main.py --debug      # Logs detallados en pantalla

Teclas en vivo (Windows):  C = cobrar la cesta (lo que va en positivo)   X = cerrar todo
                           P = pausar/reanudar entradas                   A = recogida automatica on/off
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
from bot.dashboard import Dashboard, side_label, MINT, RED, AMBER, DIM
from bot.models import BotStats
from bot.updown import UpDownFeed
from bot.keys import KeyListener

LOG_DIR = Path(__file__).parent / "logs"
FAIL_COOLDOWN_S = 3.0


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


async def _empty_list():
    return []


async def _fetch_static(scanner, price_feed):
    if not scanner:
        return [], {}
    return await asyncio.gather(scanner.get_active_markets(), price_feed.get_prices())


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
        executor.quote_fn = feed.quote
    stats = BotStats(current_capital=config.initial_capital)
    dash  = Dashboard(config.mode, config.initial_capital, config.max_open_positions)
    dash.min_edge = config.fast_min_edge
    keys = KeyListener()
    keys.start()

    dash.log.add("»", f"Bot iniciado - Capital {config.initial_capital:.0f} USDC | modo {config.mode.upper()} | "
                      f"Kelly x{config.kelly_fraction}", AMBER)
    if feed:
        dash.log.add("◆", f"Up/Down {','.join(feed.tfs)} en {','.join(feed.assets)} | edge neto >= "
                          f"{config.fast_min_edge:.0%} tras comision", AMBER)

    traded_ids: set = set()
    seen_opps: set = set()
    fail_until: dict = {}
    paused = False
    x_armed_until = 0.0

    def log_closed(closed):
        for p in closed:
            dash.log_close(p.market.asset, p.realized_pnl, position_manager.last_reason.get(p.id, "CLOSE"))

    with dash.make_live() as live:
        try:
            while True:
                t0 = time.monotonic()
                stats.cycles += 1

                try:
                    # 0. Teclas
                    for k in keys.poll():
                        if k == "c":
                            closed = await position_manager.collect(stats)
                            log_closed(closed)
                            total = sum(p.realized_pnl for p in closed)
                            dash.log.add("$", f"CESTA COBRADA: {len(closed)} posiciones  {total:+.2f} USDC" if closed
                                         else "CESTA: nada en positivo que cobrar", MINT if closed else DIM)
                        elif k == "x":
                            if config.is_live and time.time() > x_armed_until:
                                x_armed_until = time.time() + 3
                                dash.log.add("!", "LIVE: pulsa X otra vez en 3 s para cerrar TODO", RED)
                            else:
                                closed = await position_manager.collect(stats, everything=True)
                                log_closed(closed)
                                dash.log.add("$", f"CIERRE TOTAL: {len(closed)} posiciones "
                                                  f"{sum(p.realized_pnl for p in closed):+.2f} USDC", AMBER)
                        elif k == "p":
                            paused = not paused
                            dash.log.add("‖" if paused else "»", "entradas PAUSADAS" if paused else "entradas reanudadas", AMBER)
                        elif k == "a":
                            position_manager.auto_collect = not position_manager.auto_collect
                            dash.log.add("»", f"recogida automatica {'ACTIVADA' if position_manager.auto_collect else 'DESACTIVADA'}", AMBER)

                    # 1. Datos (el feed lee memoria alimentada por websocket)
                    (static_markets, prices), fast_markets = await asyncio.gather(
                        _fetch_static(scanner, price_feed),
                        feed.snapshot() if feed else _empty_list(),
                    )
                    markets = static_markets + fast_markets

                    position_manager.refresh_prices(markets)
                    dash.windows = fast_markets
                    if feed:
                        dash.feed_mode = feed.mode_label()
                        dash.data_lag_ms = feed.data_lag_ms()
                        if feed.last_error:
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

                    now = time.time()
                    opportunities = [o for o in opportunities
                                     if not (o.market.kind == "updown" and
                                             (o.market.id in traded_ids or fail_until.get(o.market.id, 0) > now))]
                    for opp in opportunities:
                        key = (opp.market.id, opp.side)
                        if key not in seen_opps:
                            seen_opps.add(key)
                            dash.log_opportunity(opp)
                    stats.opportunities_found = len(seen_opps)

                    # 3. Riesgo y ejecucion (ordenes concurrentes, con latencia simulada en paper)
                    approved = [] if paused else risk_manager.filter_opportunities(
                        opportunities, position_manager.open_positions(), stats)
                    if approved:
                        sizes = [getattr(o, "_bet_size", risk_manager.calculate_bet_size(o, stats.current_capital))
                                 for o in approved]
                        for o in approved:
                            traded_ids.add(o.market.id) if o.market.kind == "updown" else None
                        results = await asyncio.gather(*[executor.execute(o, s) for o, s in zip(approved, sizes)])
                        for opp, size, result in zip(approved, sizes, results):
                            if result.success and result.position:
                                position_manager.add_position(result.position)
                                dash.log_trade(side_label(opp.market, opp.side), opp.market.asset,
                                               result.position.size_usdc, result.position.entry_price)
                                if config.is_live:
                                    stats.current_capital -= result.position.size_usdc
                            else:
                                traded_ids.discard(opp.market.id)
                                fail_until[opp.market.id] = time.time() + FAIL_COOLDOWN_S
                                dash.log.add("·", f"no llenada {opp.market.asset}: {result.error or 'sin fill'}", DIM)

                    # 4. Monitorizar / cerrar posiciones
                    log_closed(await position_manager.monitor(stats))

                    stats.open_positions = len(position_manager.open_positions())
                    dash.paused = paused
                    dash.auto_collect = position_manager.auto_collect
                    dash.basket_pnl = position_manager.basket_pnl()
                    dash.basket_target = position_manager.basket_target
                    dash.lock_pct = position_manager.lock_pct
                    dash.update(stats, len(markets), opportunities, position_manager.positions)

                except Exception as e:
                    dash.log_error(str(e)[:80])
                    logger.exception(f"Error ciclo {stats.cycles}: {e}")

                dash.cycle_ms = (time.monotonic() - t0) * 1000
                live.update(dash.render())

                elapsed_ms = (time.monotonic() - t0) * 1000
                await asyncio.sleep(max(0, config.cycle_interval_ms - elapsed_ms) / 1000)

                if max_cycles and stats.cycles >= max_cycles:
                    dash.log.add("■", f"Fin: {max_cycles} ciclos completados", AMBER)
                    live.update(dash.render())
                    await asyncio.sleep(2)
                    break

        except (KeyboardInterrupt, asyncio.CancelledError):
            dash.log.add("■", "Bot detenido por el usuario", AMBER)
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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
