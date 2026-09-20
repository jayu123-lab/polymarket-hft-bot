# Polymarket HFT Bot

Bot para [Polymarket](https://polymarket.com) que opera los mercados **Up/Down de 5 min, 15 min y 4 h** de BTC, ETH, SOL, XRP, DOGE, BNB y HYPE. Compara una probabilidad justa contra el precio real (ask) del libro, descuenta la comisión de taker y entra sólo si queda edge. Cobra vendiendo cuando el mercado ya paga más que el valor justo, al ganar un porcentaje objetivo, al llenar la "cesta", a mano con una tecla, o al vencimiento.

> **Estado: experimental. Empieza siempre en modo `paper`.** Ver [Qué está y qué no está validado](#qué-está-y-qué-no-está-validado).

> ## ⚠️ Resultado de la auditoría en vivo (20-09-2026): la ventaja del backtest NO se confirma
>
> Con ~165 ventanas resueltas de verdad y los libros reales registrados por el bot:
> - **Paper trading**: 103 operaciones cerradas, **−91,95 USDC (≈ −14 % sobre lo apostado)**. Aciertos 59 % (como predecía el backtest), pero las ganancias se recortan a +30 % y las pérdidas son casi totales.
> - **Simulación con libros reales a 10 s** (88 entradas, referencia exacta, edge ≥ 8 %): **−27 % a −40 % de ROI** según la regla de salida; al vencimiento sólo se gana el 22 %.
> - **¿Aporta el modelo algo que el precio de mercado no tenga?** En 5 min, no: mezclando `mercado + λ·(modelo − mercado)` el λ óptimo sale **−0,28 ± 0,11** (n = 2.263). El Brier del modelo es peor que el del mercado en 6 de los 7 activos.
> - Lo que sí funciona: la regla de resolución con el precio de Chainlink acierta 95-100 % de las ventanas.
>
> Conclusión: **cuando el modelo discrepa del mercado, gana el mercado.** El +15-25 % del backtest fue un espejismo (probablemente por precios históricos de una muestra por minuto y salidas evaluadas una vez por minuto). **No uses este bot con dinero real.** `python calibration.py` reproduce estas cifras con tus propios datos.

## Mercados y fuentes de precio

| | Operados en paper (el backtest sugería ventaja; la auditoría en vivo NO la confirma) | Sólo observados (registran datos, no operan) |
|---|---|---|
| Activos | BTC, ETH, SOL, XRP, DOGE, BNB | HYPE (Binance no lo lista: sin histórico de 1 s para validar) |
| Plazos | 5 min, 15 min | 4 h (sólo ~24 ventanas por activo en 96 h: sin evidencia suficiente) |

Todos los mercados resuelven con **Chainlink** (TWAP de los últimos 60 s frente al precio al inicio). El bot usa:

- **Chainlink en vivo** (websocket RTDS de Polymarket, ~1 Hz, los 7 activos): referencia exacta de cada ventana y media real del último minuto.
- **6 exchanges** por websocket (Binance, Coinbase, Kraken, Bybit, OKX, Bitget): van por delante de Chainlink; su **mediana** (ningún exchange suelto puede distorsionarla) corregida por la base con Chainlink estima hacia dónde se moverá. Medido en vivo: la base es un desfase constante de −3 a −4 pb con variación de 0,1-0,9 pb.
- **Libro de Polymarket** por websocket (~400 eventos/s).

**Referencia exacta obligatoria** (`FAST_REQUIRE_EXACT_REF=true`): el bot sólo opera ventanas cuya referencia de Chainlink vio en directo, así que tras arrancar espera al siguiente inicio de ventana (hasta 5 o 15 min). La auditoría en vivo mostró que con la referencia aproximada (vela de 1 minuto de un exchange) aparecían falsos edges cuando el precio estaba a ±0,03 % de la referencia: el mercado conoce el precio exacto a batir y el modelo no.

Si un websocket falla, cae solo a REST. Un mercado pasa de "observar" a "operar" editando `FAST_TRADE_ASSETS` / `FAST_TRADE_TIMEFRAMES` cuando `calibration.py` muestre evidencia.

## Cómo funciona

```
El bucle de decisión se despierta con cada dato nuevo (dirigido por eventos) y sólo lee memoria; las órdenes
y las liquidaciones salen como tareas en segundo plano, así que nunca espera a la red.
Medido en vivo con 7 activos x 3 plazos + 6 exchanges + Chainlink: ~400 ciclos/s, dato -> decisión ~0.8 ms
de mediana (p99 ~5 ms). RTT medido al servidor de Polymarket: ~64 ms (el tramo de red no lo baja el código).

Cada ciclo:
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

Cobro automático (`AUTO_COLLECT=true`): vende una posición al ganar `LOCK_PROFIT_PCT` neto (30 %) y cobra toda la cesta en positivo cuando el beneficio neto conjunto llega a `BASKET_TARGET_PCT` del capital (3 %). La regla de la cesta **no está backtesteada**; la de bloqueo por posición sí.

Riesgo conjunto: `MAX_EXPOSURE_PCT` (25 %) limita el total en posiciones y órdenes en vuelo, porque los activos cripto se mueven a la vez.

## Qué está y qué no está validado

**Actualización:** lo que sigue describe el backtest, que la auditoría en vivo (ver arriba) ha contradicho. Se conserva como registro de cómo se llegó aquí y como aviso sobre lo fácil que es engañarse con precios históricos de baja frecuencia.

`backtest_assets.py` y `backtest_lab.py` reproducen la estrategia sobre 96 h de ventanas **ya resueltas** (48 h de entrenamiento y 48 h de prueba separadas; precios de mercado por minuto, velas de 1 s de Binance, resultado real; compra a precio medio + 1c, con comisión). Regla: edge ≥ 8 %, ask ≥ 0,15, con la regla de salida por valor justo.

ROI por operación (sobre coste) y número de operaciones:

| Plazo | Entrenamiento | Prueba | Activos individuales (entrenamiento / prueba) |
|---|---|---|---|
| 5 min | +23,3 % (n=1.911) | +24,7 % (n=2.019) | los 6 positivos en ambas mitades: +19,9 % a +28,5 % / +16,3 % a +31,2 % |
| 15 min | +12,3 % (n=656) | +24,2 % (n=742) | los 6 positivos en ambas mitades (algo más flojos en entrenamiento: +5,8 % a +23,9 %) |
| 4 h | −0,9 % (n=43) | +31,3 % (n=31) | sin significado estadístico: por eso sólo se observa |

Regla de resolución (3.054 ventanas reales): "TWAP de los últimos 60 s >= precio inicial" acierta el **92,8 %** con datos de exchange (97,9 % en movimientos claros); "TWAP de la ventana completa" sólo el 84,7 %. Con la referencia exacta de Chainlink el error debería bajar; `calibration.py` lo mide en vivo.

Bloqueo de beneficio (BTC+ETH, entrenamiento | prueba): sin bloqueo +18,3 % · 51 % | +20,0 % · 53 %; al +50 % +16,6 % · 59 % | +16,2 % · 57 %; **al +30 % (defecto)** +13,8 % · 61 % | +15,7 % · 60 %; al +15 % +12,6 % · 64 % | +14,8 % · 63 %. Cobrar antes **cuesta rendimiento** a cambio de más aciertos y menos rachas de pérdidas.

Otros hallazgos: los contratos baratos (ask < 0,15) son loterías (11-17 % de aciertos, resultado inestable) y se descartan; a mayor edge exigido, más rinde en ambas mitades.

Lo que **no** demuestra:
- Los ROI del backtest (+15-25 % por operación) están casi seguro inflados; espera bastante menos en real. Los precios históricos son una muestra por minuto (posiblemente desfasada), lo que puede crear edge falso. **El paper trading con libros en vivo es la prueba real.**
- La mejora con Chainlink en vivo (referencia exacta, media real del último minuto) no se puede reconstruir del pasado: se audita en vivo con `calibration.py`.
- Hay mucha varianza: rachas de 8-10 pérdidas seguidas ocurren incluso en el backtest. No es dinero garantizado.
- El modelo por sí solo no predice mucho mejor que el precio de mercado; la ganancia sale de entrar selectivamente donde ambos discrepan.
- Los activos comparten riesgo (se mueven juntos); un mercado adverso puede afectar a varias posiciones a la vez.
- El paper simula latencia (150 ms; RTT medido a Polymarket ~64 ms) y slippage de 1 tick, pero no la competencia de otros bots.
- Modo `live` **no está probado** con dinero real (la orden de compra no es FOK y no hay redención automática).
- No incluye los mercados diarios "Bitcoin above ___ on September 20" (tienen otro modelo: precio terminal frente a un strike); habría que validarlos aparte.

Reproducir: `python backtest_assets.py` (por activo y plazo) o `python backtest_lab.py` (filtros y cobro). Descargan y cachean datos (~5 min la primera vez).

## Auditoría en vivo

El bot escribe `logs/calibration.csv` (predicción de cada ventana cada ~10 s) y `logs/outcomes.csv` (resultado real de Polymarket). `python calibration.py` muestra por activo y plazo: acierto de la regla de resolución con el precio de Chainlink, Brier del modelo frente al del mercado y ROI simulado. Es lo que decide si HYPE o las ventanas de 4 h pueden pasar a operarse.

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
python calibration.py        # auditoría con los resultados reales registrados
```

## Configuración (`.env`)

| Variable | Defecto | Descripción |
|---|---|---|
| `BOT_MODE` | paper | `paper` o `live` |
| `INITIAL_CAPITAL` | 1000 | Capital simulado (USDC) |
| `MAX_BET_PCT` | 0.05 | Máx. por operación (% del capital inicial) |
| `MAX_EXPOSURE_PCT` | 0.25 | Tope conjunto en posiciones + órdenes en vuelo |
| `KELLY_FRACTION` | 0.25 | Kelly fraccionado |
| `MAX_OPEN_POSITIONS` | 8 | Posiciones simultáneas |
| `FAST_ASSETS` / `FAST_TIMEFRAMES` | 7 activos / 5m,15m,4h | Lo que se sigue y se registra en el diario |
| `FAST_TRADE_ASSETS` / `FAST_TRADE_TIMEFRAMES` | 6 activos (sin HYPE) / 5m,15m | Lo que se opera |
| `FAST_MIN_EDGE` | 0.08 | Edge neto mínimo (tras comisión) |
| `FAST_SIGMA_MULT` / `FAST_BASIS` | 1.0 / 0.0001 | Volatilidad del modelo / ruido residual respecto a Chainlink |
| `FAST_USE_WS` / `FAST_JOURNAL` | true / true | Websockets (cae solo a REST) / diario de calibración |
| `REACT_MIN_INTERVAL_MS` | 1 | Separación mínima entre ciclos; menos = más reacción y más CPU |
| `PAPER_FILL_LATENCY_MS` / `PAPER_MAX_SLIPPAGE` | 150 / 0.01 | Latencia y tolerancia de precio del relleno simulado |
| `AUTO_COLLECT` | true | Cobro automático (tecla `A` lo alterna) |
| `LOCK_PROFIT_PCT` | 0.30 | Vende una posición al ganar +30 % neto |
| `BASKET_TARGET_PCT` | 0.03 | Cobra la cesta al sumar +3 % del capital (0 = desactivado) |
| `ENABLE_LONG_MARKETS` | false | Estrategia antigua sobre mercados que vencen en meses (sin validar) |

## Arquitectura

```
main.py                 # loop principal, teclas, dashboard
config.py               # configuración vía .env
calibration.py          # auditoría en vivo (predicción vs resultado real)
backtest.py, backtest_lab.py, backtest_assets.py   # validación histórica (train/test)
bot/
  pricefeeds.py         # Chainlink (RTDS) + 6 exchanges por websocket; PriceHub (mediana, base, referencia, TWAP)
  streams.py            # libro de órdenes de Polymarket por websocket
  updown.py             # ventanas, modelo TWAP 60 s, comisión, feed en memoria, diario, liquidación
  analyzer.py           # oportunidades (Up/Down y mercados largos)
  risk_manager.py       # Kelly + límites de cartera y exposición
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
