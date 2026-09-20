from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List


class Config(BaseSettings):
    # Modo
    bot_mode: str = Field("paper", alias="BOT_MODE")

    # Polymarket
    poly_api_key: str = Field("", alias="POLY_API_KEY")
    poly_api_secret: str = Field("", alias="POLY_API_SECRET")
    poly_api_passphrase: str = Field("", alias="POLY_API_PASSPHRASE")
    poly_private_key: str = Field("", alias="POLY_PRIVATE_KEY")
    poly_chain_id: int = Field(137, alias="POLY_CHAIN_ID")

    # Capital
    initial_capital: float = Field(1000.0, alias="INITIAL_CAPITAL")
    max_bet_pct: float = Field(0.05, alias="MAX_BET_PCT")

    # Thresholds
    min_edge: float = Field(0.03, alias="MIN_EDGE")
    min_win_probability: float = Field(0.60, alias="MIN_WIN_PROBABILITY")
    min_expected_value: float = Field(0.02, alias="MIN_EXPECTED_VALUE")
    max_open_positions: int = Field(8, alias="MAX_OPEN_POSITIONS")
    kelly_fraction: float = Field(0.25, alias="KELLY_FRACTION")

    # Timing
    cycle_interval_ms: int = Field(200, alias="CYCLE_INTERVAL_MS")

    # APIs
    alpha_vantage_key: str = Field("", alias="ALPHA_VANTAGE_KEY")
    binance_testnet: bool = Field(False, alias="BINANCE_TESTNET")

    # Filtros
    target_categories: str = Field("crypto,commodities", alias="TARGET_CATEGORIES")
    min_market_liquidity: float = Field(500.0, alias="MIN_MARKET_LIQUIDITY")
    min_hours_to_expiry: float = Field(0.5, alias="MIN_HOURS_TO_EXPIRY")
    max_hours_to_expiry: float = Field(48.0, alias="MAX_HOURS_TO_EXPIRY")

    # Estrategia rapida: mercados Up/Down de 5m/15m (validados en backtest solo para BTC y ETH)
    enable_fast: bool = Field(True, alias="ENABLE_FAST")
    fast_assets: str = Field("BTC,ETH", alias="FAST_ASSETS")
    fast_timeframes: str = Field("5m,15m", alias="FAST_TIMEFRAMES")
    fast_min_edge: float = Field(0.05, alias="FAST_MIN_EDGE")
    fast_sigma_mult: float = Field(1.0, alias="FAST_SIGMA_MULT")
    # Mercados largos (vencen en meses): capital bloqueado, sin validar
    enable_long_markets: bool = Field(False, alias="ENABLE_LONG_MARKETS")

    @property
    def mode(self) -> str:
        return self.bot_mode

    @property
    def is_live(self) -> bool:
        return self.bot_mode == "live"

    @property
    def categories(self) -> List[str]:
        return [c.strip() for c in self.target_categories.split(",")]

    @property
    def max_bet_size(self) -> float:
        return self.initial_capital * self.max_bet_pct

    model_config = {"env_file": ".env", "populate_by_name": True}
