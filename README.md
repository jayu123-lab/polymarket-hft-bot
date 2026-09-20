# Polymarket HFT Bot

Bot para [Polymarket](https://polymarket.com) que opera los mercados **Up/Down de 5 y 15 minutos** de BTC y ETH: compara una probabilidad justa calculada con el precio en vivo de Binance contra el precio real (ask) del libro de órdenes, descuenta la comisión de taker y entra sólo si queda edge. Cobra vendiendo cuando el mercado ya paga más que el valor justo, o al vencimiento (máximo 15 minutos).

> **Estado: experimental. Empieza siempre en modo `paper`.** Ver [Qué está y qué no está validado](#qué-está-y-qué-no-está-validado).

## Cómo funciona

```
Cada ciclo (~0.3 s):
  1. Descubre las ventanas activas (slug btc-updown-15m-<ts>, eth-updown-5m-<ts>...)
  2. Precio spot (Binance) + precio de referencia al inicio de la ventana + volatilidad reciente
  3. P(Up) = P(TWAP de la ventana >= precio inicial)   <- así resuelve Polymarket (Chainlink TWAP)
  4. Lee el libro real (CLOB /books): ask, bid y profundidad
  5. edge = P_modelo - (ask + comisión_taker)  ->  si edge >= FAST_MIN_EDGE: compra (Kelly fraccionado)
  6. Vende si el bid neto supera el valor justo (+1c); si no, liquida al vencimiento
     con el resultado REAL publicado por Polymarket
```

Comisión de estos mercados (`crypto_fees_v2`, sólo taker): `0.07 · p · (1 - p)` por share (~1.75c a p=0.50). Está incluida en el edge y en el P&L simulado.

## Qué está y qué no está validado

`backtest.py` reproduce la estrategia sobre ventanas **ya resueltas** de Polymarket (precios de mercado por minuto, velas de 1 s de Binance, resultado real; compra a precio medio + 1c, con comisión):

| Muestra | Ventanas | Trades | ROI por trade (sobre coste) |
|---|---|---|---|
| Parámetros elegidos en 24 h | ~700 | ~600 | +7 % a +9 % |
| Fuera de muestra (72 h anteriores) | 2.276 | ~1.800 | +4 % a +6 % |
| 96 h con reglas de salida | 3.044 | 2.411 | +6 % mantener / **+8.6 % regla del bot** |

Lo que **no** demuestra:
- Es una ventaja pequeña (unos pocos % por trade) con mucha varianza (win rate ~48 %). No es dinero garantizado.
- El modelo por sí solo **no** predice mejor que el precio de mercado (Brier 0.169 vs 0.141); la ganancia sale de entrar selectivamente donde ambos discrepan.
- Sólo BTC y ETH, pocos días, un único régimen de mercado. SOL/XRP existen pero no están validados.
- El backtest no modela latencia de órdenes, competencia de otros bots ni que el ask desaparezca antes de llenar. El rendimiento real será peor que el backtest.
- Se resuelve con Chainlink TWAP; usamos Binance como aproximación.
- Modo `live` **no está probado** con dinero real (la orden de compra no es FOK y no hay redención automática).

Reproducir: `python backtest.py 96 0` (horas, desplazamiento en horas).

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
| `FAST_ASSETS` | BTC,ETH | Activos Up/Down |
| `FAST_TIMEFRAMES` | 5m,15m | Ventanas |
| `FAST_MIN_EDGE` | 0.05 | Edge neto mínimo (tras comisión) |
| `FAST_SIGMA_MULT` | 2.0 | Multiplicador de volatilidad (calibrado fuera de muestra) |
| `ENABLE_LONG_MARKETS` | false | Estrategia antigua sobre mercados que vencen en meses (sin validar, bloquea capital) |

## Arquitectura

```
main.py                 # loop principal + dashboard
config.py               # configuración vía .env
backtest.py             # validación histórica
bot/
  updown.py             # ventanas 5m/15m: descubrimiento, modelo TWAP, libro, comisión, liquidación
  analyzer.py           # oportunidades (Up/Down y mercados largos)
  risk_manager.py       # Kelly + límites de cartera
  executor.py           # ejecución paper/live (con comisión)
  position_manager.py   # venta anticipada y liquidación
  scanner.py, price_feed.py   # mercados largos (opcional)
  dashboard.py          # panel Rich en vivo
tests/
```

## Aviso de riesgo

Herramienta experimental/educativa. Los mercados de predicción pueden hacerte perder todo el capital. Un buen resultado en paper o en backtest no garantiza resultados reales. No inviertas dinero que no puedas perder.

## Licencia

MIT
