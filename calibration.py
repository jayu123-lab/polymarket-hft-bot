"""
Auditoria en vivo: compara lo que el bot predijo con lo que Polymarket resolvio de verdad.

Lee logs/calibration.csv (predicciones cada ~10 s por ventana) y logs/outcomes.csv (resultado real) y muestra,
por activo y plazo:
  - Acierto de la regla de resolucion (TWAP Chainlink de los ultimos 60 s >= referencia vs resultado real).
  - Brier del modelo frente al del mercado (menor = mejor) en varios momentos de la ventana.
  - ROI simulado de entrar con edge >= FAST_MIN_EDGE al ask real (comision incluida, mantener hasta el final).
Sirve para decidir con datos si un activo o plazo en "observacion" (p. ej. HYPE o 4h) puede pasar a operarse.

Uso:  python calibration.py [--min-edge 0.08]
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

FEE_RATE = 0.07


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


def main():
    min_edge = 0.08
    if "--min-edge" in sys.argv:
        min_edge = float(sys.argv[sys.argv.index("--min-edge") + 1])
    base = Path(__file__).parent / "logs"
    if not base.exists():
        base = Path.home() / "polymarket-logs"
    snaps, outs = load(base / "calibration.csv"), load(base / "outcomes.csv")
    outcome = {}
    for o in outs:
        outcome[o["slug"]] = o
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

    print("\n== 2) Brier (modelo vs mercado) y ROI simulado por activo / plazo ==")
    print(f"     edge >= {min_edge:.0%}, primera entrada por ventana, compra al ask real + comision, mantener al final")
    by = defaultdict(lambda: {"bm": 0.0, "bk": 0.0, "n": 0, "trades": 0, "pnl": 0.0, "cost": 0.0, "win": 0})
    seen_entry = set()
    seen_snap = set()
    for r in snaps:
        slug = r["slug"]
        o = outcome.get(slug)
        if not o:
            continue
        key = (r["ts"], slug)
        if key in seen_snap:
            continue
        seen_snap.add(key)
        p, up = fl(r["p_model"]), fl(r["ask_up"])
        dn, bid_up = fl(r["ask_dn"]), fl(r["bid_up"])
        left = fl(r["left_s"])
        if p is None or up is None or dn is None or left is None:
            continue
        y = float(o["actual_up"])
        g = by[(r["asset"], r["tf"])]
        mid = (up + bid_up) / 2 if bid_up is not None else up
        g["bm"] += (p - y) ** 2
        g["bk"] += (mid - y) ** 2
        g["n"] += 1
        if slug in seen_entry or left < 20 or float(r["ts"]) - 0 < 0:
            continue
        cands = []
        for side, ask, pp, won in (("UP", up, p, y == 1.0), ("DOWN", dn, 1 - p, y == 0.0)):
            if 0.15 <= ask <= 0.95:
                cost = ask + fee(ask)
                cands.append(((pp - 0.02) - cost, cost, won))
        if not cands:
            continue
        edge, cost, won = max(cands)
        if edge >= min_edge:
            seen_entry.add(slug)
            g["trades"] += 1
            g["cost"] += cost
            g["pnl"] += (1.0 if won else 0.0) - cost
            g["win"] += int(won)
    print(f"  {'activo':6s} {'plazo':5s} {'puntos':>7s} {'Brier modelo':>13s} {'Brier mercado':>14s} {'trades':>7s} {'win%':>5s} {'ROI':>8s}")
    for (a, tf), g in sorted(by.items()):
        if g["n"] == 0:
            continue
        roi = f"{100 * g['pnl'] / g['cost']:+.1f}%" if g["cost"] else "--"
        win = f"{100 * g['win'] / g['trades']:.0f}%" if g["trades"] else "--"
        print(f"  {a:6s} {tf:5s} {g['n']:>7d} {g['bm'] / g['n']:>13.4f} {g['bk'] / g['n']:>14.4f} {g['trades']:>7d} {win:>5s} {roi:>8s}")
    print("\nCriterio orientativo para pasar un activo/plazo de 'observar' a 'operar': >= 100 ventanas resueltas,"
          "\nBrier del modelo <= Brier del mercado y ROI simulado positivo de forma estable.")


if __name__ == "__main__":
    main()
