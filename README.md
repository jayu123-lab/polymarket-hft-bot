# Polymarket HFT Bot

Bot para [Polymarket](https://polymarket.com) que opera los mercados **Up/Down de 5 y 15 minutos** de BTC y ETH: compara una probabilidad justa calculada con el precio en vivo de Binance contra el precio real (ask) del libro de órdenes, descuenta la comisión de taker y entra sólo si queda edge. Cobra vendiendo cuando el mercado ya paga más que el valor justo, al ganar un porcentaje objetivo, al llenar la "cesta", a mano con una tecla, o al vencimiento (máximo 15 minutos).

> **Estado: experimental. Empieza siempre en modo `paper`.** Ver [Qué está y qué no está validado](#qué-está-y-qué-no-está-validado).

## Cómo funciona

```
Datos en tiempo real (websocket, con respaldo REST automático):
  · Binance  : bookTicker BTC/ETH  -> precio y media del último minuto
  · Polymarket CLOB : libro de órdenes de los tokens Up/Down (~400 eventos/s)
El bucle de decisión se despierta con cada dato nuevo (dirigido por eventos) y sólo lee memoria; las órdenes
y las liquidaciones salen como tareas en segundo plano, así que nunca espera a la red.

Medido en vivo (5.000 ciclos, tu propia máquina): dato -> decisión mediana 0.33 ms, p95 2.2 ms, p99 3.3 ms
(antes, con REST y ciclo de 100 ms: ~300 ms). Un RTT al servidor de Polymarket con la conexión abierta: ~64 ms.

Cada ciclo (dirigido por eventos):
  1. P(Up) = P(TWAP de los ÚLTIMOS 60 s >= precio inicial)   <- así resuelve Polymarket (Chainlink btc-usd-twap-60s)
  2. edge = P_modelo - (ask + comisión_taker)  ->  si edge >= FAST_MIN_EDGE: compra (Kelly fraccionado)
  3. Paper realista: la orden tarda PAPER_FILL_LATENCY_MS y sólo se llena si el ask no se alejó más de 1 tick
  4. Cobro: valor justo (+1c), bloqueo de beneficio por posición, cesta, teclas, o liquidación al vencimiento
     con el resultado REAL publicado por Polymarket
```

Comisión de estos mercados (`crypto_fees_v2`, sólo taker): `0.07 · p · (1 - p)` por share (~1.75c a p=0.50). Está incluida en el edge y en el P&L simulado.

## Teclas en vivo (Windows)

| Tecla | Acción |
|---|---|
| `C` | **Cobrar la cesta**: vende a mercado todas las posiciones que van en positivo (neto de comisión) |
| `X` | Cerrar todo (en `live` hay que pulsar dos veces en 3 s) |
| `P` | Pausar / reanudar nuevas entradas |
| `A` | Activar / desactivar el cobro automático |

Cobro automático (`AUTO_COLLECT=true`): vende una posición al ganar `LOCK_PROFIT_PCT` neto (30 %) y cobra toda la cesta en positivo cuando el beneficio neto conjunto llega a `BASKET_TARGET_PCT` del capital (3 %). La regla de la cesta **no está backtesteada**; la de bloqueo por posición sí (tabla F).

## Qué está y qué no está validado

`backtest_lab.py` reproduce la estrategia sobre 96 h de ventanas **ya resueltas** (48 h de entrenamiento y 48 h de prueba separadas; precios de mercado por minuto, velas de 1 s de Binance, resultado real; compra a precio medio + 1c, con comisión). Los parámetros se eligieron en entrenamiento y se comprobaron en prueba.

Regla de resolución (3.054 ventanas reales): "TWAP de los últimos 60 s >= precio inicial" acierta el **92,8 %** (97,9 % en movimientos claros); "TWAP de la ventana completa" sólo el 84,7 %. Una versión anterior del bot usaba esta última y estaba mal.

Combinaciones finales (ROI por trade sobre coste | % de aciertos | peor racha de pérdidas):

| Configuración | Entrenamiento | Prueba |
|---|---|---|
| edge ≥ 5 % | +15,7 % · 52 % · 10 | +13,7 % · 52 % · 10 |
| edge ≥ 8 % + ask ≥ 0,15 | +18,3 % · 51 % · 10 | +20,0 % · 53 % · 10 |
| … + bloqueo 80 % | +17,1 % · 56 % · 7 | +18,0 % · 56 % · 8 |
| … + bloqueo 50 % | +16,6 % · 59 % · 7 | +16,2 % · 57 % · 8 |
| **… + bloqueo 30 % (defecto)** | +13,8 % · 61 % · 6 | +15,7 % · 60 % · 8 |
| … + bloqueo 15 % | +12,6 % · 64 % · 6 | +14,8 % · 63 % · 8 |

Cobrar antes **cuesta rendimiento** (cada escalón hacia abajo pierde ~1-2 puntos de ROI) a cambio de más aciertos y rachas menos largas. Ajusta `LOCK_PROFIT_PCT` según prefieras.

Otros hallazgos del laboratorio: los contratos baratos (ask < 0,15) son loterías (11-17 % de aciertos, resultado inestable entre mitades) y se descartan; un ruido de base Binance/Chainlink de 1e-4 mejora algo el ajuste; a mayor edge exigido, más rinde en ambas mitades.

Lo que **no** demuestra:
- El ROI del backtest (+15-20 % por trade) está casi seguro inflado; espera bastante menos en real. Los precios históricos son una muestra por minuto (posiblemente desfasada), lo que puede crear edge falso, sobre todo en los edges más altos. **El paper trading con libros en vivo es la prueba real.**
- Hay mucha varianza: rachas de 8-10 pérdidas seguidas ocurren incluso en el backtest. No es dinero garantizado.
- El modelo por sí solo **no** predice mejor que el precio de mercado; en el último par de minutos el mercado (que ve el precio de Chainlink) es más preciso que el modelo. La ganancia sale de entrar selectivamente donde ambos discrepan.
- Sólo BTC y ETH, pocos días, un único régimen de mercado. SOL/XRP existen pero no están validados.
- El backtest no modela latencia ni competencia; el modo paper sí simula latencia (150 ms; el RTT medido a Polymarket es ~64 ms) y slippage de 1 tick, pero no la competencia de otros bots.
- Se resuelve con Chainlink; usamos Binance como aproximación.
- Modo `live` **no está probado** con dinero real (la orden de compra no es FOK y no hay redención automática).

Reproducir: `python backtest_lab.py` (descarga y cachea los datos; ~5 min la primera vez). `python backtest.py 48 0` da una versión simple.

## Instalación

```bash
git clone https://github.com/jayu123-lab/polymarket-hft-bot
cd polymarket-hft-bot
python -m venv venv
venv\Scripts\activate        # Windows  (source venv/bin/activate en Mac/Linux)
pip install -r requirements.txt
cp .env.example .env
```

## Uso

```bash
python main.py               # paper (simulación con precios y libros reales)
python main.py --cycles 100  # N ciclos y termina
python main.py --debug       # logs en pantalla
python main.py --live        # DINERO REAL - requiere claves en .env y no está validado
```

## Configuración (`.env`)

| Variable | Defecto | Descripción |
|---|---|---|
| `BOT_MODE` | paper | `paper` o `live` |
| `INITIAL_CAPITAL` | 1000 | Capital simulado (USDC) |
| `MAX_BET_PCT` | 0.05 | Máx. por operación (% del capital inicial) |
| `KELLY_FRACTION` | 0.25 | Kelly fraccionado |
| `MAX_OPEN_POSITIONS` | 8 | Posiciones simultáneas |
| `REACT_MIN_INTERVAL_MS` | 1 | Separación mínima entre ciclos (dirigidos por eventos); menos = más reacción y más CPU |
| `CYCLE_INTERVAL_MS` | 100 | Cadencia sólo sin websocket / mercados largos |
| `FAST_ASSETS` / `FAST_TIMEFRAMES` | BTC,ETH / 5m,15m | Activos y ventanas |
| `FAST_MIN_EDGE` | 0.08 | Edge neto mínimo (tras comisión) |
| `FAST_SIGMA_MULT` / `FAST_BASIS` | 1.0 / 0.0001 | Volatilidad del modelo / ruido de base Binance-Chainlink |
| `FAST_USE_WS` | true | Websockets (si fallan, cae solo a REST) |
| `PAPER_FILL_LATENCY_MS` / `PAPER_MAX_SLIPPAGE` | 150 / 0.01 | Latencia y tolerancia de precio del relleno simulado |
| `AUTO_COLLECT` | true | Cobro automático (tecla `A` lo alterna) |
| `LOCK_PROFIT_PCT` | 0.30 | Vende una posición al ganar +30 % neto |
| `BASKET_TARGET_PCT` | 0.03 | Cobra la cesta al sumar +3 % del capital (0 = desactivado) |
| `ENABLE_LONG_MARKETS` | false | Estrategia antigua sobre mercados que vencen en meses (sin validar) |

## Arquitectura

```
main.py                 # loop principal, teclas, dashboard
config.py               # configuración vía .env
backtest.py             # backtest simple
backtest_lab.py         # laboratorio train/test (filtros, salidas, cobro)
bot/
  streams.py            # websockets Binance + libro de Polymarket
  updown.py             # ventanas 5m/15m, modelo TWAP 60 s, comisión, feed en memoria, liquidación
  analyzer.py           # oportunidades (Up/Down y mercados largos)
  risk_manager.py       # Kelly + límites de cartera
  executor.py           # ejecución paper (latencia + slippage) / live
  position_manager.py   # venta anticipada, bloqueo de beneficio, cesta, teclas, liquidación
  keys.py               # teclas C / X / P / A
  dashboard.py          # panel Rich en vivo
  scanner.py, price_feed.py   # mercados largos (opcional)
tests/
```

## Aviso de riesgo

Herramienta experimental/educativa. Los mercados de predicción pueden hacerte perder todo el capital. Un buen resultado en paper o en backtest no garantiza resultados reales. No inviertas dinero que no puedas perder.

## Licencia

MIT
