# Polymarket HFT Bot 🤖📈

Bot de alta frecuencia para [Polymarket](https://polymarket.com) que detecta mercados mispricedados en cripto y oro usando arbitraje estadístico con modelo Black-Scholes.

## ¿Cómo funciona?

```
Cada 200ms (ciclo):
  1. Escanea mercados activos de cripto y oro en Polymarket
  2. Obtiene precios en tiempo real (Binance + Yahoo Finance)
  3. Calcula la probabilidad REAL usando Black-Scholes (modelo log-normal)
  4. Compara con la probabilidad IMPLÍCITA en los precios del mercado
  5. Si hay edge > 3%: calcula Kelly fraction y apuesta
  6. Monitorea posiciones → cierra en Take-Profit o Stop-Loss
```

### Estrategia: Reference Price Arbitrage

El bot obtiene el precio spot de Binance (ej. BTC = $65,000) y calcula la probabilidad real de que llegue a un objetivo en el tiempo que queda hasta la resolución del mercado. Si Polymarket lo tiene mispricedado, entra a la apuesta.

**Ejemplo:**
```
Mercado: "¿BTC estará por encima de $60,000 el 20 Sep?"
Precio actual BTC: $65,000
Tiempo hasta expiración: 6 horas
Volatilidad histórica: 65% anual

Probabilidad real (BS): 96.2%
Precio en Polymarket (YES): 0.88 ($0.88)

Edge: 96.2% - 88% = +8.2% ✅ → COMPRA YES
EV por USDC: +0.09 USDC
Kelly fraccionado: 2.1% del capital
```

## Instalación

```bash
# Clonar repositorio
git clone https://github.com/jayu123-lab/polymarket-hft-bot
cd polymarket-hft-bot

# Crear entorno virtual
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Mac/Linux

# Instalar dependencias
pip install -r requirements.txt
```

## Configuración

```bash
# Copiar template de configuración
cp .env.example .env
```

Editar `.env`:

```env
# EMPIEZA EN MODO PAPER (simulación sin dinero real)
BOT_MODE=paper

# Capital inicial para simulación
INITIAL_CAPITAL=1000.0

# Para trading real, necesitas:
# POLY_API_KEY=...         (desde polymarket.com)
# POLY_API_SECRET=...
# POLY_API_PASSPHRASE=...
# POLY_PRIVATE_KEY=...     (wallet Polygon con USDC)
```

## Uso

```bash
# Modo simulación (recomendado para empezar)
python main.py

# Con debug
python main.py --debug

# Ejecutar N ciclos y terminar
python main.py --cycles 100

# Modo live (DINERO REAL - requiere .env configurado)
python main.py --live
```

## Tests

```bash
pip install pytest
pytest tests/ -v
```

## Arquitectura

```
polymarket-hft-bot/
├── main.py                  # Loop principal de trading
├── config.py                # Configuración vía .env
├── bot/
│   ├── models.py            # Tipos: Market, Opportunity, Position
│   ├── scanner.py           # Escaneo de mercados (Gamma API)
│   ├── price_feed.py        # Precios en tiempo real (Binance + yfinance)
│   ├── analyzer.py          # Motor estadístico (Black-Scholes)
│   ├── risk_manager.py      # Kelly criterion + límites de cartera
│   ├── executor.py          # Ejecución de órdenes (paper/live)
│   └── position_manager.py  # Monitoreo de posiciones
└── tests/
    └── test_analyzer.py     # Tests del motor estadístico
```

## Parámetros de riesgo

| Parámetro | Por defecto | Descripción |
|-----------|-------------|-------------|
| `MIN_EDGE` | 3% | Edge mínimo para entrar a la apuesta |
| `MIN_WIN_PROBABILITY` | 60% | Probabilidad mínima calculada |
| `MAX_BET_PCT` | 5% | Máximo del capital por apuesta |
| `KELLY_FRACTION` | 0.25x | Kelly fraccionado (conservador) |
| `MAX_OPEN_POSITIONS` | 8 | Máximo de posiciones simultáneas |
| `MAX_HOURS_TO_EXPIRY` | 48h | Mercados dentro de 48 horas |

## Activos soportados

**Cripto:** BTC, ETH, SOL, MATIC, DOGE, XRP, BNB, ADA, AVAX, LINK

**Commodities:** GOLD (XAU), SILVER, OIL

## ⚠️ Aviso de riesgo

Este bot es una herramienta educativa/experimental. Los mercados de predicción conllevan riesgo de pérdida total del capital. Empieza siempre en modo **paper** para entender el comportamiento antes de usar dinero real. El rendimiento pasado no garantiza resultados futuros.

## Licencia

MIT
