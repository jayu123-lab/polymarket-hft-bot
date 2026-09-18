"""Tests del motor de análisis estadístico."""
import pytest
from bot.analyzer import MarketAnalyzer


def test_black_scholes_above_strike():
    """Precio muy por encima del strike → probabilidad alta."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=70_000, strike=60_000, days=1, sigma=0.65, direction="above"
    )
    assert p > 0.85, f"Esperaba p>0.85 pero obtuve {p:.4f}"


def test_black_scholes_below_strike():
    """Precio muy por debajo del strike → probabilidad baja."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=50_000, strike=80_000, days=1, sigma=0.65, direction="above"
    )
    assert p < 0.15, f"Esperaba p<0.15 pero obtuve {p:.4f}"


def test_black_scholes_at_expiry_above():
    """Con T=0, si spot > strike debe retornar 1.0."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=100, strike=90, days=0, sigma=0.5, direction="above"
    )
    assert p == 1.0


def test_black_scholes_at_expiry_below():
    """Con T=0, si spot < strike debe retornar 0.0."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=80, strike=90, days=0, sigma=0.5, direction="above"
    )
    assert p == 0.0


def test_direction_below():
    """Dirección 'below': prob alta cuando spot < strike."""
    p = MarketAnalyzer._black_scholes_probability(
        spot=1800, strike=2000, days=1, sigma=0.80, direction="below"
    )
    assert p > 0.80


def test_expected_value_positive():
    """EV positivo cuando la prob real > precio de mercado."""
    ev = MarketAnalyzer._expected_value(true_prob=0.80, market_price=0.60)
    assert ev > 0, f"EV debería ser positivo: {ev:.4f}"


def test_expected_value_negative():
    """EV negativo cuando la prob real < precio de mercado."""
    ev = MarketAnalyzer._expected_value(true_prob=0.40, market_price=0.70)
    assert ev < 0, f"EV debería ser negativo: {ev:.4f}"


def test_kelly_fraction_positive_edge():
    """Kelly > 0 cuando hay edge positivo."""
    kelly = MarketAnalyzer._kelly_fraction(true_prob=0.75, market_price=0.55, multiplier=0.25)
    assert kelly > 0


def test_kelly_fraction_no_edge():
    """Kelly ≈ 0 cuando no hay ventaja real."""
    kelly = MarketAnalyzer._kelly_fraction(true_prob=0.50, market_price=0.55, multiplier=0.25)
    assert kelly <= 0


def test_confidence_far_from_strike():
    """Mayor confianza cuando el spot está lejos del strike."""
    high_conf = MarketAnalyzer._calculate_confidence(
        spot=100_000, strike=50_000, days=0.5, sigma=0.65
    )
    low_conf = MarketAnalyzer._calculate_confidence(
        spot=51_000, strike=50_000, days=0.5, sigma=0.65
    )
    assert high_conf > low_conf
