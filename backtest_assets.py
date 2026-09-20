"""
Backtest por activo y plazo (BTC, ETH, SOL, XRP, DOGE, BNB x 5m, 15m, 4h) sobre 96 h de ventanas ya resueltas.
Reparte los datos en 48 h de entrenamiento y 48 h de prueba y muestra el ROI de la estrategia (edge >= 8 %,
ask >= 0.15, regla de salida) con y sin bloqueo de beneficio del 30 %.

HYPE no se puede probar aqui: Binance no lo lista y no hay velas de 1 s. Se valida en vivo con calibration.py.

Uso:  python backtest_assets.py       (descarga y cachea en bt_cache_all.pkl; ~5 min la primera vez)
"""
import asyncio
import os
import time

import backtest as bt
import backtest_lab as L
from bot.updown import TF_SECONDS

L.CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bt_cache_all.pkl")
bt.ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")


def line(tr):
    if not tr:
        return "n=0"
    c = sum(t[0] for t in tr)
    return (f"n={len(tr):4d} ROI={100 * sum(t[2] for t in tr) / c:+6.1f}% "
            f"win={100 * sum(1 for t in tr if t[2] > 0) / len(tr):3.0f}%")


async def main():
    t = time.time()
    kl, wins, t0, now = await L.load(96)
    print(f"datos cargados en {time.time() - t:.0f}s: {len(wins)} ventanas", flush=True)
    wins = L.build(kl, wins)
    mid = t0 + (now - t0) // 2
    TR, TE = (t0, mid), (mid, now + 1)
    b = 1e-4
    ask15 = lambda m: m["ask"] >= 0.15
    print("\n== por plazo y activo | edge>=8%, ask>=0.15 | sin bloqueo || con bloqueo 30% ==")
    for tf in TF_SECONDS:
        for a in bt.ASSETS:
            f = lambda m, a=a, tf=tf: ask15(m) and m["asset"] == a and m["tf"] == tf
            n_w = sum(1 for w in wins if w["asset"] == a and w["tf"] == tf)
            print(f"{tf:>3} {a:5s} ventanas={n_w:4d} | "
                  f"TRAIN {line(L.simulate(wins, b, *TR, th=0.08, filt=f))} || TEST {line(L.simulate(wins, b, *TE, th=0.08, filt=f))} | "
                  f"lock30 TRAIN {line(L.simulate(wins, b, *TR, th=0.08, lock=0.30, filt=f))} || "
                  f"TEST {line(L.simulate(wins, b, *TE, th=0.08, lock=0.30, filt=f))}", flush=True)
    print("\n== agregado por plazo ==")
    for tf in TF_SECONDS:
        f = lambda m, tf=tf: ask15(m) and m["tf"] == tf
        print(f"{tf:>3} TRAIN {line(L.simulate(wins, b, *TR, th=0.08, filt=f))} || TEST {line(L.simulate(wins, b, *TE, th=0.08, filt=f))}")
    print("\n== Brier por activo (modelo vs mercado; menor = mejor) ==")
    for a in bt.ASSETS:
        sub = [w for w in wins if w["asset"] == a]
        tr, te = L.brier(sub, b, *TR), L.brier(sub, b, *TE)
        print(f"{a:5s} TRAIN modelo {tr[0]:.4f} mercado {tr[1]:.4f} || TEST modelo {te[0]:.4f} mercado {te[1]:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
