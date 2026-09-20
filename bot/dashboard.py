"""
Dashboard en terminal (Rich): fondo oscuro, acento ambar, semaforo menta/ambar/rojo,
tarjetas redondeadas con etiquetas en mayusculas, cifras grandes, curva de P&L y barras.
"""
import time
from collections import deque
from datetime import datetime
from typing import Deque, List, Optional

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bot.models import BotStats, Market, Opportunity, Position, Side
from bot.updown import entry_window_ok, side_edges

AMBER = "#f5a623"
MINT = "#4ade80"
RED = "#f87171"
TXT = "#e5e7eb"
DIM = "#6b7280"
LINE = "#33343c"
EMPTY = "#2b2c33"

ROBOT = [
    " ▄▄▄▄▄▄▄ ",
    " █ ▄ ▄ █ ",
    " █▀▀▀▀▀█ ",
    " ▀█▀ ▀█▀ ",
]
BLOCKS = " ▁▂▃▄▅▆▇█"
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def side_label(market: Market, side: Side) -> str:
    if market.kind == "updown":
        return "UP" if side == Side.YES else "DOWN"
    return side.value


def _tag(text: str, color: str = AMBER) -> Text:
    return Text(f" {text.upper()} ", style=f"bold {color}")


def _card(content, title: str, color: str = AMBER, border: str = LINE, height: Optional[int] = None,
          padding=(0, 1)) -> Panel:
    return Panel(content, title=_tag(title, color), title_align="left", border_style=border,
                 box=box.ROUNDED, padding=padding, height=height)


def _bar(frac: float, width: int, color: str) -> Text:
    frac = max(0.0, min(1.0, frac))
    n = round(frac * width)
    t = Text("█" * n, style=color)
    t.append("░" * (width - n), style=EMPTY)
    return t


def _pill(text: str, fg: str, bg: str) -> Text:
    fg = "#0b0b0e" if fg == "black" else fg
    return Text(f" {text} ", style=f"bold {fg} on {bg}")


def _money(x: float, sign: bool = False) -> str:
    return f"{x:+,.2f}" if sign else f"{x:,.2f}"


class ActivityLog:
    def __init__(self, maxlen: int = 9):
        self._log: Deque = deque(maxlen=maxlen)

    def add(self, icon: str, msg: str, color: str = TXT):
        self._log.appendleft((datetime.now().strftime("%H:%M:%S"), icon, msg, color))

    def render(self, rows: int = 9) -> Table:
        t = Table.grid(padding=(0, 1), expand=True)
        t.add_column(width=8, no_wrap=True)
        t.add_column(width=2, no_wrap=True)
        t.add_column(no_wrap=True, overflow="ellipsis")
        items = list(self._log)[:rows]
        if not items:
            t.add_row("", "", Text("esperando actividad...", style=DIM))
        for ts, icon, msg, color in items:
            t.add_row(Text(ts, style=DIM), Text(icon, style=f"bold {color}"), Text(msg, style=color))
        return t


class Dashboard:
    def __init__(self, mode: str, initial_capital: float, max_positions: int = 8):
        self.mode = mode
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.min_edge = 0.05
        self.windows: List[Market] = []
        self.latency_ms = 0.0
        self.feed_mode = "--"
        self.data_lag_ms = 0.0
        self.cycle_ms = 0.0
        self.react_ms = 0.0
        self.paused = False
        self.auto_collect = True
        self.basket_pnl = 0.0
        self.basket_target = 0.0
        self.lock_pct = 0.0
        self.log = ActivityLog()
        self._frame = 0
        self._stats: Optional[BotStats] = None
        self._markets_count = 0
        self._last_opps: List[Opportunity] = []
        self._last_positions: List[Position] = []
        self._eq: Deque = deque(maxlen=4000)
        self._eq_t = 0.0
        self._console = Console()

    # ── API publica (compatible con main.py) ───────────────────────────────
    def update(self, stats: BotStats, markets_count: int, opportunities: List[Opportunity],
               positions: List[Position]):
        self._stats = stats
        self._markets_count = markets_count
        self._last_opps = opportunities
        self._last_positions = positions
        self._frame += 1
        now = time.time()
        if not self._eq or now - self._eq_t >= 1.0:
            self._eq.append(self._equity())
            self._eq_t = now

    def log_scan(self, count: int, assets: List[str]):
        self.log.add("·", f"escaneando {count} mercados", DIM)

    def log_opportunity(self, opp: Opportunity):
        self.log.add("◆", f"SEÑAL {side_label(opp.market, opp.side)} {opp.market.asset}  "
                          f"edge {opp.edge:+.1%}  P {opp.true_probability:.0%} vs ask {opp.implied_probability:.2f}", AMBER)

    def log_trade(self, side: str, asset: str, size: float, price: float):
        self.log.add("»", f"COMPRA {side} {asset}  ${size:,.2f} @ {price:.2f}", TXT)

    def log_close(self, asset: str, pnl: float, reason: str):
        bal = self._stats.current_capital if self._stats else 0.0
        ok = pnl >= 0
        self.log.add("✓" if ok else "✗", f"{reason} {asset}  {pnl:+.2f}  · saldo ${bal:,.2f}", MINT if ok else RED)

    def log_error(self, msg: str):
        self.log.add("!", msg, RED)

    # ── calculos ────────────────────────────────────────────────────────────
    def _open(self) -> List[Position]:
        return [p for p in self._last_positions if p.status == "open"]

    def _equity(self) -> float:
        s = self._stats
        base = s.current_capital if s else self.initial_capital
        return base + sum(p.unrealized_pnl for p in self._open())

    # ── render ──────────────────────────────────────────────────────────────
    def render(self) -> Group:
        h = self._console.size.height
        tall = h >= 44
        return Group(
            self._header(),
            self._kpis(),
            self._curve(5 if tall else 3),
            self._windows_card(),
            self._bottom(rows=8 if tall else 5),
            self._footer(),
        )

    def _header(self) -> Table:
        s = self._stats
        runtime = int(s.runtime_seconds) if s else 0
        sp = SPINNER[self._frame % len(SPINNER)]
        live = self.mode == "live"
        mode_pill = _pill("LIVE", "white", "#dc2626") if live else _pill("PAPER", "black", AMBER)

        robot = Text("\n".join(ROBOT), style=f"bold {AMBER}")
        title = Text()
        title.append("POLYMARKET HFT BOT\n", style=f"bold {AMBER}")
        title.append("mercados Up/Down 5m · 15m  ·  probabilidad justa vs libro real, neta de comision\n", style=DIM)
        title.append_text(mode_pill)
        title.append(f"  {sp} escaneando", style=DIM)

        right = Text(justify="right")
        right.append("TIEMPO ", style=DIM)
        right.append(f"{runtime // 3600:02d}:{(runtime % 3600) // 60:02d}:{runtime % 60:02d}\n", style=f"bold {TXT}")
        right.append("CICLOS ", style=DIM)
        right.append(f"{s.cycles if s else 0}\n", style=f"bold {TXT}")
        right.append("DATOS ", style=DIM)
        right.append(f"{self.data_lag_ms:.0f} ms ", style=f"bold {MINT if self.data_lag_ms < 500 else AMBER}")
        right.append(self.feed_mode, style=f"bold {MINT if self.feed_mode == 'WS' else AMBER}")
        right.append("\nREACCION ", style=DIM)
        right.append(f"{self.react_ms:.2f} ms", style=f"bold {MINT if self.react_ms < 20 else AMBER}")

        g = Table.grid(expand=True, padding=(0, 2))
        g.add_column(width=10)
        g.add_column(ratio=1)
        g.add_column(justify="right", width=22)
        g.add_row(robot, title, right)
        return g

    def _kpis(self) -> Table:
        s = self._stats or BotStats(current_capital=self.initial_capital)
        openp = self._open()
        invested = sum(p.size_usdc for p in openp)
        unreal = sum(p.unrealized_pnl for p in openp)
        pcol = MINT if s.total_pnl >= 0 else RED
        arrow = "▲" if s.total_pnl >= 0 else "▼"

        bal = Text()
        bal.append(f"${_money(s.current_capital)}\n", style=f"bold {TXT}")
        bal.append(f"en posiciones ${_money(invested)}", style=DIM)

        pnl = Text()
        pnl.append(f"{arrow} {_money(s.total_pnl, True)}\n", style=f"bold {pcol}")
        pnl.append(f"ROI {s.roi:+.2%}  ·  no realizado ", style=DIM)
        pnl.append(_money(unreal, True), style=MINT if unreal >= 0 else RED)

        tr = Text()
        tr.append(f"{s.total_trades}\n", style=f"bold {TXT}")
        tr.append(f"✓ {s.winning_trades}", style=MINT)
        tr.append(f"  ✗ {s.total_trades - s.winning_trades}", style=RED)
        tr.append(f"  ·  abiertas {len(openp)}/{self.max_positions}", style=DIM)

        wr = Text()
        if s.total_trades:
            wcol = MINT if s.win_rate >= 0.5 else (AMBER if s.win_rate >= 0.4 else RED)
            wr.append(f"{s.win_rate:.1%}\n", style=f"bold {wcol}")
            wr.append_text(_bar(s.win_rate, 14, wcol))
        else:
            wr.append("--\n", style=f"bold {DIM}")
            wr.append("sin operaciones cerradas", style=DIM)
        wr.append(f"  señales {s.opportunities_found}", style=DIM)

        cesta = Text()
        bp = self.basket_pnl
        ccol = MINT if bp > 0 else (RED if bp < 0 else TXT)
        cesta.append(f"{bp:+,.2f}\n", style=f"bold {ccol}")
        if self.basket_target > 0 and self.auto_collect:
            cesta.append_text(_bar(max(0.0, bp) / self.basket_target, 12, MINT))
            cesta.append(f" meta +{self.basket_target:,.0f}", style=DIM)
        else:
            cesta.append("[C] para cobrar ahora", style=DIM)

        g = Table.grid(expand=True, padding=(0, 0))
        for _ in range(5):
            g.add_column(ratio=1)
        kp = (1, 2)
        g.add_row(_card(bal, "balance", padding=kp), _card(pnl, "p&l realizado", padding=kp),
                  _card(tr, "trades", padding=kp), _card(wr, "win rate", padding=kp),
                  _card(cesta, "cesta no realizada", padding=kp))
        return g

    def _curve(self, height: int) -> Panel:
        vals = list(self._eq) or [self.initial_capital]
        width = max(20, self._console.size.width - 6)
        base = self.initial_capital
        lo, hi = min(min(vals), base), max(max(vals), base)
        pad = max((hi - lo) * 0.1, base * 0.0005)
        lo, hi = lo - pad, hi + pad
        span = hi - lo

        cols = []
        n = len(vals)
        for x in range(width):
            v = vals[min(n - 1, int(x * n / width))]
            cols.append((v, (v - lo) / span * height * 8))

        lines = []
        for r in range(height):
            level = height - 1 - r
            line = Text()
            for v, eighths in cols:
                cell = int(max(0, min(8, eighths - level * 8)))
                line.append(BLOCKS[cell], style=MINT if v >= base else RED)
            lines.append(line)

        cur = vals[-1]
        d = cur - base
        head = Text()
        head.append(f"inicio ${_money(base)}", style=DIM)
        head.append("   equity ", style=DIM)
        head.append(f"${_money(cur)}", style=f"bold {TXT}")
        head.append(f"  ({d:+,.2f})", style=f"bold {MINT if d >= 0 else RED}")
        head.append(f"   min {_money(min(vals))}  max {_money(max(vals))}", style=DIM)
        return _card(Group(head, *lines), "live p&l curve")

    def _windows_card(self) -> Panel:
        if not self.windows:
            return _card(Text("Buscando ventanas activas...", style=DIM), "ventanas en vivo")
        now = time.time()
        held = {p.market.id for p in self._open()}
        t = Table(box=None, expand=True, show_header=True, header_style=f"bold {DIM}", padding=(0, 1))
        t.add_column("VENTANA", no_wrap=True)
        t.add_column("TIEMPO", no_wrap=True)
        t.add_column("PRECIO vs REF", no_wrap=True, justify="right")
        t.add_column("P(UP) MODELO", no_wrap=True)
        t.add_column("P(UP) MERCADO", no_wrap=True)
        t.add_column("EDGE NETO", no_wrap=True, justify="center")
        t.add_column("ESTADO", no_wrap=True)

        for m in sorted(self.windows, key=lambda x: (x.asset, x.window_s)):
            elapsed = now - m.start_ts
            left = max(0, int(m.start_ts + m.window_s - now))
            frac = elapsed / m.window_s if m.window_s else 0
            mov = (m.spot / m.ref_price - 1.0) if (m.spot and m.ref_price) else 0.0
            mcol = MINT if mov >= 0 else RED
            edges = side_edges(m)
            best = max(edges, key=lambda e: e[4]) if edges else None
            ok = entry_window_ok(m, now)
            hot = bool(best and best[4] >= self.min_edge and ok)

            if m.id in held:
                estado = Text("● posicion abierta", style=f"bold {AMBER}")
            elif hot:
                estado = Text(f"ENTRADA {'UP' if best[0] == Side.YES else 'DOWN'}", style=f"bold {MINT}")
            elif not ok:
                estado = Text("esperando datos" if elapsed < 30 else "cierre de ventana", style=DIM)
            else:
                estado = Text("sin ventaja", style=DIM)

            edge_txt = Text("--", style=DIM)
            if best:
                edge_txt = _pill(f"{best[4]:+.1%}", "black", MINT) if hot else Text(f"{best[4]:+.1%}", style=DIM)

            pu = m.p_model_yes if m.p_model_yes is not None else 0.5
            tf = m.category.split("-")[1] if "-" in m.category else ""
            price = f"{m.spot:,.2f}" if m.spot and m.spot > 1000 else (f"{m.spot:.4f}" if m.spot else "--")

            timec = Text()
            timec.append_text(_bar(frac, 8, AMBER))
            timec.append(f" {left // 60}:{left % 60:02d}", style=TXT)
            model = Text()
            model.append_text(_bar(pu, 10, AMBER))
            model.append(f" {pu:.0%}", style=TXT)
            market = Text()
            market.append_text(_bar(m.yes_price, 10, "#60a5fa"))
            market.append(f" {m.yes_price:.0%}", style=TXT)

            t.add_row(
                Text.assemble((m.asset, f"bold {TXT}"), (f" {tf}", DIM)),
                timec,
                Text.assemble((price, TXT), (f"  {'▲' if mov >= 0 else '▼'}{abs(mov):.3%}", mcol)),
                model, market, edge_txt, estado,
            )
        return _card(t, "ventanas en vivo  ·  modelo vs mercado")

    def _bottom(self, rows: int) -> Table:
        pos = self._open()
        pt = Table(box=None, expand=True, show_header=True, header_style=f"bold {DIM}", padding=(0, 1))
        for name, just in (("ACTIVO", "left"), ("LADO", "left"), ("ENTRADA", "right"), ("BID", "right"),
                           ("P&L", "right"), ("JUSTO", "right"), ("RESTA", "right")):
            pt.add_column(name, no_wrap=True, justify=just)
        if not pos:
            pt.add_row(Text("sin posiciones abiertas", style=DIM), "", "", "", "", "", "")
        for p in pos[:rows]:
            m = p.market
            pnl = p.unrealized_pnl
            if m.kind == "updown":
                pm = m.p_model_yes
                fair = None if pm is None else (pm if p.side == Side.YES else 1.0 - pm)
                left = max(0, int(m.start_ts + m.window_s - time.time()))
                justo = f"{fair:.2f}" if fair is not None else "--"
                resta = f"{left // 60}:{left % 60:02d}" if left > 0 else "liquidando"
                tf = m.category.split("-")[1]
                name = Text.assemble((m.asset, f"bold {TXT}"), (f" {tf}", DIM))
            else:
                justo, resta = f"{p.target_exit_price:.2f}", f"{p.stop_loss_price:.2f}"
                name = Text(m.asset, style=f"bold {TXT}")
            lado = _pill(side_label(m, p.side), "black", MINT if p.side == Side.YES else RED)
            pt.add_row(name, lado, f"{p.entry_price:.3f}", f"{p.current_price:.3f}",
                       Text(f"{pnl:+.2f}", style=f"bold {MINT if pnl >= 0 else RED}"),
                       Text(justo, style=AMBER), Text(resta, style=DIM))
        if len(pos) > rows:
            pt.add_row(Text(f"+{len(pos) - rows} mas", style=DIM), "", "", "", "", "", "")

        g = Table.grid(expand=True, padding=(0, 0))
        g.add_column(ratio=11)
        g.add_column(ratio=9)
        feed_n = min(len(self.log._log), rows + 1)
        pos_n = 1 + max(1, min(len(pos), rows) + (1 if len(pos) > rows else 0))
        h = max(pos_n, feed_n, 4) + 2
        g.add_row(_card(pt, f"posiciones ({len(pos)})", height=h), _card(self.log.render(rows + 1), "trade feed", height=h))
        return g

    def _footer(self) -> Text:
        t = Text(justify="center")
        keyc = f"bold black on {AMBER}"
        t.append(" C ", style=keyc)
        t.append(" cobrar cesta   ", style=TXT)
        t.append(" X ", style=keyc)
        t.append(" cerrar todo   ", style=TXT)
        t.append(" P ", style=keyc)
        t.append(" pausar" + (" (PAUSADO)" if self.paused else "") + "   ", style=f"bold {RED}" if self.paused else TXT)
        t.append(" A ", style=keyc)
        if self.auto_collect:
            t.append(f" auto-cobro ON (+{self.lock_pct:.0%} por posicion", style=MINT)
            t.append(f", cesta +{self.basket_target:,.0f})" if self.basket_target > 0 else ")", style=MINT)
        else:
            t.append(" auto-cobro OFF", style=DIM)
        t.append("\n")
        if self.mode == "live":
            t.append("MODO LIVE: DINERO REAL", style=f"bold {RED}")
        else:
            t.append("PAPER: simulacion con libros y precios reales, sin dinero en juego", style=AMBER)
        t.append("  ·  comisiones incluidas  ·  Ctrl+C para salir", style=DIM)
        return t

    # ── contexto ────────────────────────────────────────────────────────────
    def make_live(self) -> Live:
        return Live(self.render(), refresh_per_second=8, screen=True, console=self._console)
