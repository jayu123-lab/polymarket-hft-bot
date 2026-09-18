"""
Polymarket HFT Bot
==================
Bot de alta frecuencia para mercados de predicción Polymarket.
Detecta mispricings en mercados de cripto y oro usando modelos estadísticos,
ejecuta y cobra en milisegundos.

Uso:
    python main.py              # Modo paper (simulación)
    python main.py --live       # Modo live (requiere .env configurado)
    python main.py --cycles 50  # Ejecuta N ciclos y termina
"""
import asyncio
import sys
import time
from typing import Optional

from loguru import logger
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich import box

from config import Config
from bot.scanner import MarketScanner
from bot.price_feed import PriceFeed
from bot.analyzer import MarketAnalyzer
from bot.risk_manager import RiskManager
from bot.executor import OrderExecutor
from bot.position_manager import PositionManager
from bot.models import BotStats

console = Console()


def setup_logger(config: Config):
    logger.remove()
    level = "DEBUG" if "--debug" in sys.argv else "INFO"
    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level:<8}</level> | {message}")
    logger.add("logs/bot_{time:YYYY-MM-DD}.log", rotation="1 day", retention="7 days", level="DEBUG", enqueue=True)


def render_dashboard(stats: BotStats, opportunities_found: int, mode: str) -> Panel:
    mode_color = "red" if mode == "live" else "yellow"
    mode_label = f"[bold {mode_color}]{mode.upper()}[/]"

    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column()
    grid.add_row(
        f"[cyan]Modo:[/] {mode_label}",
        f"[cyan]Ciclos:[/] {stats.cycles}",
    )
    grid.add_row(
        f"[cyan]Capital:[/] [bold green]{stats.current_capital:.2f} USDC[/]",
        f"[cyan]P&L Total:[/] [bold {'green' if stats.total_pnl >= 0 else 'red'}]{stats.total_pnl:+.2f} USDC[/]",
    )
    grid.add_row(
        f"[cyan]Trades:[/] {stats.total_trades} ([green]{stats.winning_trades}W[/]/[red]{stats.total_trades - stats.winning_trades}L[/])",
        f"[cyan]WinRate:[/] [bold]{stats.win_rate:.1%}[/]",
    )
    grid.add_row(
        f"[cyan]Posiciones abiertas:[/] {stats.open_positions}",
        f"[cyan]Oportunidades detectadas:[/] {stats.opportunities_found}",
    )
    grid.add_row(
        f"[cyan]ROI:[/] [bold {'green' if stats.roi >= 0 else 'red'}]{stats.roi:+.2%}[/]",
        f"[cyan]Runtime:[/] {stats.runtime_seconds:.0f}s",
    )
    return Panel(grid, title="[bold white]Polymarket HFT Bot[/]", border_style="blue")


async def main():
    # Configuración
    if "--live" in sys.argv:
        import os; os.environ["BOT_MODE"] = "live"
    config = Config()

    max_cycles: Optional[int] = None
    if "--cycles" in sys.argv:
        idx = sys.argv.index("--cycles")
        if idx + 1 < len(sys.argv):
            max_cycles = int(sys.argv[idx + 1])

    setup_logger(config)

    # Advertencia modo live
    if config.is_live:
        logger.warning("="*60)
        logger.warning("MODO LIVE ACTIVADO - Se usará dinero real")
        logger.warning("Asegúrate de tener .env configurado correctamente")
        logger.warning("="*60)
        await asyncio.sleep(3)

    logger.info(f"Iniciando bot en modo: {config.mode.upper()}")
    logger.info(f"Capital inicial: {config.initial_capital:.2f} USDC")
    logger.info(f"Max apuesta: {config.max_bet_size:.2f} USDC ({config.max_bet_pct:.0%})")
    logger.info(f"Edge mínimo: {config.min_edge:.1%} | EV mínimo: {config.min_expected_value:.3f}")
    logger.info(f"Intervalo ciclo: {config.cycle_interval_ms}ms")

    # Inicializar componentes
    scanner = MarketScanner(config)
    price_feed = PriceFeed(config)
    analyzer = MarketAnalyzer(config)
    risk_manager = RiskManager(config)
    executor = OrderExecutor(config)
    position_manager = PositionManager(config, executor)

    stats = BotStats(current_capital=config.initial_capital)

    try:
        while True:
            cycle_start = time.monotonic()
            stats.cycles += 1

            try:
                # Paso 1: datos en paralelo
                markets, prices = await asyncio.gather(
                    scanner.get_active_markets(),
                    price_feed.get_prices(),
                )

                if not markets:
                    logger.debug("Sin mercados activos en este ciclo")
                else:
                    # Paso 2: volatilidades (en paralelo para los assets encontrados)
                    unique_assets = list({m.asset for m in markets})
                    vol_tasks = [price_feed.get_historical_volatility(a) for a in unique_assets]
                    vol_values = await asyncio.gather(*vol_tasks, return_exceptions=True)
                    volatilities = {
                        asset: (v if isinstance(v, float) else 0.8)
                        for asset, v in zip(unique_assets, vol_values)
                    }

                    # Paso 3: analizar oportunidades
                    opportunities = analyzer.find_opportunities(markets, prices, volatilities)
                    stats.opportunities_found += len(opportunities)

                    # Paso 4: filtrar por riesgo
                    open_pos = position_manager.open_positions()
                    approved = risk_manager.filter_opportunities(opportunities, open_pos, stats)

                    # Paso 5: ejecutar
                    for opp in approved:
                        size = getattr(opp, "_bet_size", risk_manager.calculate_bet_size(opp, stats.current_capital))
                        result = await executor.execute(opp, size)
                        if result.success and result.position:
                            position_manager.add_position(result.position)
                            if config.is_live:
                                stats.current_capital -= size

                # Paso 6: monitorear posiciones
                await position_manager.monitor(stats)
                stats.open_positions = len(position_manager.open_positions())

                # Log cada 10 ciclos
                if stats.cycles % 10 == 0:
                    logger.info(
                        f"Ciclo {stats.cycles} | Mercados={len(markets) if markets else 0} "
                        f"Opors={stats.opportunities_found} P&L={stats.total_pnl:+.2f} USDC"
                    )
                    if position_manager.open_positions():
                        logger.info(position_manager.summary())

            except Exception as e:
                logger.error(f"Error en ciclo {stats.cycles}: {e}")
                if "--debug" in sys.argv:
                    import traceback; traceback.print_exc()

            # Control de tiempo del ciclo
            elapsed_ms = (time.monotonic() - cycle_start) * 1000
            sleep_ms = max(0, config.cycle_interval_ms - elapsed_ms)
            await asyncio.sleep(sleep_ms / 1000)

            if max_cycles and stats.cycles >= max_cycles:
                logger.info(f"Alcanzados {max_cycles} ciclos. Deteniendo bot.")
                break

    except KeyboardInterrupt:
        logger.info("Bot detenido por el usuario")
    finally:
        await scanner.close()
        await price_feed.close()
        logger.info(
            f"\n{'='*50}\n"
            f"RESUMEN FINAL\n"
            f"Ciclos: {stats.cycles}\n"
            f"Trades: {stats.total_trades} (W:{stats.winning_trades} / L:{stats.total_trades - stats.winning_trades})\n"
            f"WinRate: {stats.win_rate:.1%}\n"
            f"P&L Total: {stats.total_pnl:+.2f} USDC\n"
            f"ROI: {stats.roi:+.2%}\n"
            f"{'='*50}"
        )


if __name__ == "__main__":
    asyncio.run(main())
