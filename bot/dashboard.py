"""
Dashboard visual en tiempo real usando Rich.
Muestra scanner, oportunidades, posiciones y log de actividad.
"""
import time
from collections import deque
from datetime import datetime
from typing import List, Optional

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich import box

from bot.models import BotStats, Market, Opportunity, Position, Side
from bot.updown import entry_window_ok, side_edges

# Paleta de colores
C_GOLD   = "bold yellow"
C_GREEN  = "bold green"
C_RED    = "bold red"
C_CYAN   = "bold cyan"
C_DIM    = "dim white"
C_WHITE  = "bold white"
C_MAGENTA = "bold magenta"

ASSETS_CYCLE = ["BTC", "ETH", "SOL", "GOLD", "XRP", "DOGE", "AVAX", "LINK", "MATIC", "SILVER"]
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
COIN_FRAMES    = ["🪙", "💰", "💎", "🎯", "⚡", "🔥"]

HEADER = """[bold cyan]
 ██████╗  ██████╗ ██╗  ██╗   ██╗███╗   ███╗ █████╗ ██████╗ ██╗  ██╗███████╗████████╗
 ██╔══██╗██╔═══██╗██║  ╚██╗ ██╔╝████╗ ████║██╔══██╗██╔══██╗██║ ██╔╝██╔════╝╚══██╔══╝
 ██████╔╝██║   ██║██║   ╚████╔╝ ██╔████╔██║███████║██████╔╝█████╔╝ █████╗     ██║
 ██╔═══╝ ██║   ██║██║    ╚██╔╝  ██║╚██╔╝██║██╔══██║██╔══██╗██╔═██╗ ██╔══╝     ██║
 ██║     ╚██████╔╝███████╗██║   ██║ ╚═╝ ██║██║  ██║██║  ██║██║  ██╗███████╗   ██║
 ╚═╝      ╚═════╝ ╚══════╝╚═╝   ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝   ╚═╝
[/bold cyan]"""


def side_label(market: Market, side: Side) -> str:
    if market.kind == "updown":
        return "UP" if side == Side.YES else "DOWN"
    return side.value


class ActivityLog:
    def __init__(self, maxlen: int = 9):
        self._log: deque = deque(maxlen=maxlen)

    def add(self, icon: str, msg: str, color: str = "white"):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        self._log.appendleft((ts, icon, msg, color))

    def render(self) -> Table:
        t = Table(box=None, show_header=False, padding=(0, 1))
        t.add_column("ts",   style="dim cyan", width=10)
        t.add_column("icon", width=3)
        t.add_column("msg",  style="white")
        for ts, icon, msg, color in self._log:
            t.add_row(ts, icon, f"[{color}]{msg}[/]")
        return t


class Dashboard:
    def __init__(self, mode: str, initial_capital: float, max_positions: int = 8):
        self.mode = mode
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.min_edge = 0.05
        self.windows: List[Market] = []
        self.log = ActivityLog()
        self._frame = 0
        self._scan_asset_idx = 0
        self._scan_progress: dict[str, float] = {}   # asset -> 0..1
        self._last_opps: List[Opportunity] = []
        self._last_positions: List[Position] = []
        self._stats: Optional[BotStats] = None
        self._markets_count = 0
        self._last_trade_flash = 0.0
        self._console = Console()

    # ── API pública ──────────────────────────────────────────────

    def update(
        self,
        stats: BotStats,
        markets_count: int,
        opportunities: List[Opportunity],
        positions: List[Position],
    ):
        self._stats = stats
        self._markets_count = markets_count
        self._last_opps = opportunities
        self._last_positions = positions
        self._frame += 1
        # Avanzar animación de scanner
        self._scan_asset_idx = self._frame % len(ASSETS_CYCLE)
        asset_now = ASSETS_CYCLE[self._scan_asset_idx]
        self._scan_progress[asset_now] = (self._frame % 20) / 20.0

    def log_scan(self, count: int, assets: List[str]):
        assets_str = ", ".join(assets[:6])
        self.log.add("🔍", f"Escaneando {count} mercados  [{assets_str}…]", C_DIM)

    def log_opportunity(self, opp: Opportunity):
        self.log.add(
            "⚡",
            f"OPORTUNIDAD {opp.side} {opp.market.asset} | edge={opp.edge:.1%} EV={opp.expected_value:.3f}",
            C_GOLD,
        )

    def log_trade(self, side: str, asset: str, size: float, price: float):
        coin = COIN_FRAMES[self._frame % len(COIN_FRAMES)]
        self.log.add(
            coin,
            f"COMPRA {side} {asset}  ${size:.2f} USDC @ {price:.4f}",
            C_GREEN,
        )
        self._last_trade_flash = time.monotonic()

    def log_close(self, asset: str, pnl: float, reason: str):
        icon = "💸" if pnl >= 0 else "🩸"
        color = C_GREEN if pnl >= 0 else C_RED
        self.log.add(icon, f"CIERRE {asset} [{reason}]  P&L {pnl:+.2f} USDC", color)

    def log_error(self, msg: str):
        self.log.add("⚠️", msg, C_RED)

    # ── Render ────────────────────────────────────────────────────

    def render(self) -> Group:
        return Group(
            self._render_header(),
            Columns([self._render_portfolio(), self._render_stats()], equal=True, expand=True),
            self._render_windows() if self.windows else self._render_scanner(),
            self._render_opportunities(),
            self._render_positions(),
            self._render_log(),
        )

    def _render_header(self) -> Panel:
        sp = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
        mode_color = "red" if self.mode == "live" else "yellow"
        runtime = ""
        if self._stats:
            s = int(self._stats.runtime_seconds)
            runtime = f"⏱ {s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"
        mode_label = Text("🔴 LIVE" if self.mode == "live" else "🟡 PAPER", style=f"bold {mode_color}")
        cycles = self._stats.cycles if self._stats else 0
        subtitle = Text.assemble(
            (f" {sp} ", "bold cyan"),
            mode_label,
            ("  │  ", "dim white"),
            (runtime, "cyan"),
            ("  │  CIC: ", "dim white"),
            (str(cycles), "bold white"),
        )
        return Panel(subtitle, border_style="cyan", padding=(0, 2))

    def _render_portfolio(self) -> Panel:
        s = self._stats
        if not s:
            return Panel("Iniciando…", title="💼 PORTFOLIO", border_style="green")

        pnl_color = C_GREEN if s.total_pnl >= 0 else C_RED
        pnl_arrow = "▲" if s.total_pnl >= 0 else "▼"
        cap = s.current_capital

        t = Table(box=None, show_header=False, padding=(0, 1))
        t.add_column("k", style="dim white", width=16)
        t.add_column("v")
        t.add_row("Capital:",    f"[bold green]{cap:,.2f} USDC[/]")
        t.add_row("P&L total:",  f"[{pnl_color}]{pnl_arrow} {s.total_pnl:+,.2f} USDC[/]")
        t.add_row("ROI:",        f"[{pnl_color}]{s.roi:+.2%}[/]")
        t.add_row("Posiciones:", f"[bold white]{s.open_positions}[/][dim] / {self.max_positions}[/]")

        # Mini barra de capital
        bar_pct = min(1.0, cap / self.initial_capital)
        filled = int(bar_pct * 20)
        bar = f"[green]{'█' * filled}[/][dim]{'░' * (20 - filled)}[/]"
        t.add_row("Capital:", bar)

        return Panel(t, title="[bold green]💼  PORTFOLIO[/]", border_style="green", padding=(0, 1))

    def _render_stats(self) -> Panel:
        s = self._stats
        if not s:
            return Panel("Iniciando…", title="📊 ESTADÍSTICAS", border_style="blue")

        wr_color = C_GREEN if s.win_rate > 0.5 else (C_RED if s.win_rate > 0 else C_DIM)
        t = Table(box=None, show_header=False, padding=(0, 1))
        t.add_column("k", style="dim white", width=16)
        t.add_column("v")
        t.add_row("Trades:",      f"[bold]{s.total_trades}[/]  [green]✓{s.winning_trades}[/] [red]✗{s.total_trades - s.winning_trades}[/]")
        t.add_row("WinRate:",     f"[{wr_color}]{s.win_rate:.1%}[/]" if s.total_trades else "[dim]--[/]")
        t.add_row("Oport. vistas:", f"[yellow]{s.opportunities_found}[/]")
        t.add_row("Mercados act.:", f"[cyan]{self._markets_count}[/]")
        t.add_row("Ciclos/seg:",  f"[dim]{max(0, s.cycles / max(1, s.runtime_seconds)):.1f}[/]")

        return Panel(t, title="[bold blue]📊  ESTADÍSTICAS[/]", border_style="blue", padding=(0, 1))

    def _render_scanner(self) -> Panel:
        sp = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
        asset_now = ASSETS_CYCLE[self._scan_asset_idx]

        rows = []
        for i, asset in enumerate(ASSETS_CYCLE):
            prog = self._scan_progress.get(asset, 0.0)
            filled = int(prog * 10)
            bar = f"[cyan]{'█' * filled}[/][dim]{'░' * (10 - filled)}[/]"
            done = prog >= 0.95
            if asset == asset_now and not done:
                label = f"[bold yellow]{sp} {asset}[/]"
            elif done:
                label = f"[green]✓ {asset}[/]"
            else:
                label = f"[dim]· {asset}[/]"
            rows.append(f"{label} {bar}")

        # 2 columnas de assets
        half = len(rows) // 2
        cols_text = "   ".join(
            f"{rows[i]}   {rows[i + half]}" for i in range(half)
        )
        body = Text.from_markup(
            f"  [dim]Analizando {self._markets_count} mercados activos…[/]\n\n  " + cols_text
        )
        return Panel(body, title=f"[bold cyan]🔍  SCANNER  [dim]{sp}[/][/]", border_style="cyan", padding=(0, 1))

    def _render_windows(self) -> Panel:
        now = time.time()
        held = {p.market.id for p in self._last_positions if p.status == "open"}
        t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan")
        t.add_column("Ventana", width=9)
        t.add_column("Resta", width=6)
        t.add_column("Precio vs ref", width=22)
        t.add_column("P(Up) modelo", width=12)
        t.add_column("Up ask", width=7)
        t.add_column("Down ask", width=8)
        t.add_column("Edge neto", width=10)
        t.add_column("Estado", width=20)
        for m in sorted(self.windows, key=lambda x: (x.asset, x.window_s)):
            left = max(0, int(m.start_ts + m.window_s - now))
            mov = (m.spot / m.ref_price - 1.0) if (m.spot and m.ref_price) else 0.0
            mcol = C_GREEN if mov >= 0 else C_RED
            edges = side_edges(m)
            best = max(edges, key=lambda e: e[4]) if edges else None
            ok = entry_window_ok(m, now)
            if best and best[4] >= self.min_edge and ok:
                estado = f"[bold green]ENTRADA {'UP' if best[0] == Side.YES else 'DOWN'}[/]"
            elif not ok:
                estado = "[dim]esperando datos[/]" if now - m.start_ts < 30 else "[dim]cierre de ventana[/]"
            else:
                estado = "[dim]sin ventaja[/]"
            if m.id in held:
                estado = "[bold yellow]* posicion abierta[/]"
            edge_txt = f"[{C_GREEN if best and best[4] >= self.min_edge else C_DIM}]{best[4]:+.1%}[/]" if best else "--"
            t.add_row(
                f"[bold]{m.asset}[/] {m.category.split('-')[1]}",
                f"{left // 60}:{left % 60:02d}",
                f"[{mcol}]{m.spot:,.2f} ({mov:+.3%})[/]" if m.spot else "--",
                f"{m.p_model_yes:.1%}" if m.p_model_yes is not None else "--",
                f"{m.yes_price:.2f}", f"{m.no_price:.2f}", edge_txt, estado,
            )
        return Panel(t, title="[bold cyan]VENTANAS EN VIVO (Up/Down)[/]", border_style="cyan", padding=(0, 1))

    def _render_opportunities(self) -> Panel:
        opps = self._last_opps
        if not opps:
            msg = Text.from_markup(
                "[dim]  Sin oportunidades en este ciclo — edge mínimo 3% no alcanzado[/]\n"
                "[dim]  El bot sigue buscando…  🔍[/]"
            )
            return Panel(msg, title="[bold yellow]⚡  OPORTUNIDADES (0)[/]", border_style="yellow", padding=(0, 1))

        t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan")
        t.add_column("Asset",  width=7)
        t.add_column("Pregunta",  width=40)
        t.add_column("Lado", width=5)
        t.add_column("P_real",  width=8)
        t.add_column("Edge",    width=8)
        t.add_column("EV",      width=8)
        t.add_column("Kelly",   width=8)

        for opp in opps[:6]:
            edge_col = C_GREEN if opp.edge > 0.05 else C_GOLD
            t.add_row(
                f"[bold]{opp.market.asset}[/]",
                opp.market.question[:40],
                f"[{'green' if opp.side == Side.YES else 'red'}]{side_label(opp.market, opp.side)}[/]",
                f"{opp.true_probability:.1%}",
                f"[{edge_col}]{opp.edge:+.1%}[/]",
                f"[green]{opp.expected_value:.3f}[/]",
                f"{opp.kelly_fraction:.3f}",
            )

        return Panel(t, title=f"[bold yellow]⚡  OPORTUNIDADES ({len(opps)})[/]", border_style="yellow", padding=(0, 1))

    def _render_positions(self) -> Panel:
        positions = [p for p in self._last_positions if p.status == "open"]
        if not positions:
            msg = Text.from_markup("[dim]  Sin posiciones abiertas[/]")
            return Panel(msg, title="[bold magenta]📈  POSICIONES ABIERTAS (0)[/]", border_style="magenta", padding=(0, 1))

        t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta")
        t.add_column("Asset",   width=7)
        t.add_column("Lado",    width=5)
        t.add_column("Entrada", width=8)
        t.add_column("Actual",  width=8)
        t.add_column("P&L",     width=12)
        t.add_column("TP/Justo", width=9)
        t.add_column("SL/Resta", width=9)

        for p in positions:
            pnl = p.unrealized_pnl
            pnl_col = C_GREEN if pnl >= 0 else C_RED
            arrow = "▲" if pnl >= 0 else "▼"
            if p.market.kind == "updown":
                pm = p.market.p_model_yes
                fair = None if pm is None else (pm if p.side == Side.YES else 1.0 - pm)
                left = max(0, int(p.market.start_ts + p.market.window_s - time.time()))
                col_a = f"[cyan]{fair:.2f}[/]" if fair is not None else "--"
                col_b = f"[dim]{left // 60}:{left % 60:02d}[/]" if left > 0 else "[yellow]liquidando[/]"
            else:
                col_a = f"[dim green]{p.target_exit_price:.3f}[/]"
                col_b = f"[dim red]{p.stop_loss_price:.3f}[/]"
            t.add_row(
                f"[bold]{p.market.asset}[/]",
                f"[{'green' if p.side == Side.YES else 'red'}]{side_label(p.market, p.side)}[/]",
                f"{p.entry_price:.4f}",
                f"{p.current_price:.4f}",
                f"[{pnl_col}]{arrow} {pnl:+.2f}[/]",
                col_a,
                col_b,
            )

        return Panel(t, title=f"[bold magenta]📈  POSICIONES ({len(positions)})[/]", border_style="magenta", padding=(0, 1))

    def _render_log(self) -> Panel:
        return Panel(
            self.log.render(),
            title="[bold white]📋  ACTIVIDAD RECIENTE[/]",
            border_style="white",
            padding=(0, 1),
        )

    # ── Context manager ───────────────────────────────────────────

    def make_live(self) -> Live:
        return Live(
            self.render(),
            refresh_per_second=5,
            screen=True,
            console=self._console,
        )
