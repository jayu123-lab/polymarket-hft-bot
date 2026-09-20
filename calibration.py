"""
Auditoria en vivo: compara lo que el bot predijo con lo que Polymarket resolvio de verdad.

Lee logs/calibration.csv (predicciones y libro cada ~10 s por ventana) y logs/outcomes.csv (resultado real):
  1) Acierto de la regla de resolucion (TWAP Chainlink de los ultimos 60 s >= referencia vs resultado real).
  2) Brier del modelo frente al del mercado, por activo y plazo (menor = mejor).
  3) SIMULACION CON DATOS REALES: entra con las mismas reglas que el bot (referencia exacta, >=30 s desde el inicio,
     >=20 s restantes, ask entre 0.15 y 0.95, edge neto >= --min-edge) y prueba distintas reglas de salida usando los
     bid/ask reales registrados a 10 s. Es la evidencia mas fiable que hay para elegir LOCK_PROFIT_PCT y compararlo con
     mantener hasta el vencimiento.

Uso:  python calibration.py [--min-edge 0.08]
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

FEE_RATE = 0.07
TF_SECS = {"5m": 300, "15m": 900, "4h": 14400}


def fee(p):
    return FEE_RATE * p * (1 - p)


def load(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def fl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def simulate(series, outcome, min_edge, lock=None, rule=True, margin=0.01, slip=0.0, tfs=("5m", "15m"), assets=None):
    """Una entrada por ventana; devuelve [(coste, pnl, gana, motivo, activo, tf)]."""
    res = []
    for slug, rows in series.items():
        o = outcome.get(slug)
        if not o:
            continue
        tf = rows[0]["tf"]
        if tf not in tfs or (assets and rows[0]["asset"] not in assets):
            continue
        y_up = o["actual_up"] == "1"
        for i, r in enumerate(rows):
            if r["ref_kind"] != "chainlink":
                continue
            left = fl(r["left_s"])
            p, a_up, a_dn = fl(r["p_model"]), fl(r["ask_up"]), fl(r["ask_dn"])
            if None in (left, p, a_up, a_dn) or left < 20 or TF_SECS[tf] - left < 30:
                continue
            best = None
            for side, ask, pp in (("UP", a_up, p), ("DOWN", a_dn, 1 - p)):
                if 0.15 <= ask <= 0.95:
                    cost = ask + slip + fee(ask + slip)
                    e = (pp - 0.02) - cost
                    if best is None or e > best[0]:
                        best = (e, side, cost)
            if best is None or best[0] < min_edge:
                continue
            _, side, cost = best
            won = y_up if side == "UP" else not y_up
            pnl, why = (1.0 if won else 0.0) - cost, "vence"
            for r2 in rows[i + 1:]:
                l2, p2 = fl(r2["left_s"]), fl(r2["p_model"])
                bid = fl(r2["bid_up"] if side == "UP" else r2["bid_dn"])
                if l2 is None or p2 is None or bid is None or l2 < 5 or bid <= 0.01:
                    continue
                nb = bid - fee(bid)
                if lock is not None and nb >= cost * (1 + lock):
                    pnl, why = nb - cost, "bloqueo"
                    break
                fair = p2 if side == "UP" else 1 - p2
                if rule and nb >= fair + margin:
                    pnl, why = nb - cost, "valor justo"
                    break
            res.append((cost, pnl, pnl > 0, why, rows[0]["asset"], tf))
            break
    return res


def fmt(res):
    if not res:
        return "n=  0"
    c = sum(x[0] for x in res)
    return f"n={len(res):3d}  ROI={100 * sum(x[1] for x in res) / c:+7.1f}%  aciertos={100 * sum(x[2] for x in res) / len(res):3.0f}%  P&L/op={sum(x[1] for x in res) / len(res):+.3f}"


def main():
    min_edge = 0.08
    if "--min-edge" in sys.argv:
        min_edge = float(sys.argv[sys.argv.index("--min-edge") + 1])
    base = Path(__file__).parent / "logs"
    if not base.exists():
        base = Path.home() / "polymarket-logs"
    snaps, outs = load(base / "calibration.csv"), load(base / "outcomes.csv")
    outcome = {o["slug"]: o for o in outs}
    print(f"Ventanas con resultado real: {len(outcome)}   |   instantaneas: {len(snaps)}   ({base})")
    if not outcome:
        print("Aun no hay resultados: deja el bot corriendo (las ventanas de 5 min resuelven ~1 min despues de cerrar).")
        return

    print("\n== 1) Regla de resolucion: TWAP Chainlink 60 s >= referencia  vs  resultado real ==")
    grp = defaultdict(lambda: [0, 0])
    for o in outcome.values():
        if o["pred_up"] == "":
            continue
        k = (o["asset"], o["ref_kind"])
        grp[k][1] += 1
        grp[k][0] += int(o["pred_up"] == o["actual_up"])
    for (a, kind), (ok, n) in sorted(grp.items()):
        print(f"  {a:5s} ref={kind:9s} acierta {100 * ok / n:5.1f}%  (n={n})")

    series = defaultdict(list)
    seen = set()
    for r in snaps:
        key = (r["slug"], r["ts"])
        if key in seen:
            continue
        seen.add(key)
        series[r["slug"]].append(r)
    for rows in series.values():
        rows.sort(key=lambda r: float(r["ts"]))

    print("\n== 2) Brier del modelo vs mercado (solo referencia exacta; menor = mejor) ==")
    by = defaultdict(lambda: [0.0, 0.0, 0])
    for slug, rows in series.items():
        o = outcome.get(slug)
        if not o:
            continue
        y = float(o["actual_up"])
        for r in rows:
            p, au, bu = fl(r["p_model"]), fl(r["ask_up"]), fl(r["bid_up"])
            if r["ref_kind"] != "chainlink" or p is None or au is None:
                continue
            mid = (au + bu) / 2 if bu is not None else au
            g = by[(r["asset"], r["tf"])]
            g[0] += (p - y) ** 2
            g[1] += (mid - y) ** 2
            g[2] += 1
    print(f"  {'activo':6s} {'plazo':5s} {'puntos':>7s} {'modelo':>8s} {'mercado':>8s}")
    for (a, tf), g in sorted(by.items()):
        if g[2]:
            print(f"  {a:6s} {tf:5s} {g[2]:>7d} {g[0] / g[2]:>8.4f} {g[1] / g[2]:>8.4f}")

    print(f"\n== 3) Simulacion con libros reales a 10 s (edge >= {min_edge:.0%}, ref. exacta, misma logica de entrada que el bot) ==")
    variants = (
        ("mantener hasta el vencimiento", dict(lock=None, rule=False)),
        ("solo valor justo (sin bloqueo)", dict(lock=None, rule=True)),
        ("bloqueo +30% + valor justo (defecto)", dict(lock=0.30, rule=True)),
        ("bloqueo +50% + valor justo", dict(lock=0.50, rule=True)),
        ("bloqueo +100% + valor justo", dict(lock=1.00, rule=True)),
    )
    for tfs, title in ((("5m", "15m"), "5m + 15m"), (("5m",), "solo 5m"), (("15m",), "solo 15m")):
        print(f"  -- {title} --")
        for name, kw in variants:
            print(f"     {name:38s} {fmt(simulate(series, outcome, min_edge, tfs=tfs, **kw))}")
    print("  -- por activo (bloqueo +30% + valor justo | mantener) --")
    for a in sorted({rows[0]['asset'] for rows in series.values()}):
        d = simulate(series, outcome, min_edge, lock=0.30, rule=True, assets={a})
        h = simulate(series, outcome, min_edge, lock=None, rule=False, assets={a})
        print(f"     {a:5s} defecto: {fmt(d)}   |   mantener: {fmt(h)}")
    print("\nCriterio orientativo para operar un activo/plazo: >= 100 ventanas resueltas, ROI simulado positivo y estable.")


if __name__ == "__main__":
    main()
