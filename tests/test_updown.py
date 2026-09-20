from datetime import datetime

from bot.models import Market, Side
from bot.updown import prob_up, side_edges, taker_fee_per_share


def _market(p_up, ask_up, ask_dn):
    return Market(
        id="1", question="BTC 5m", condition_id="", token_id_yes="a", token_id_no="b",
        yes_price=ask_up, no_price=ask_dn, volume=0, liquidity=1000,
        end_date=datetime.utcnow(), category="updown-5m", asset="BTC",
        kind="updown", p_model_yes=p_up,
    )


def test_fee_is_largest_at_fifty_cents_and_zero_at_extremes():
    assert taker_fee_per_share(0.5) > taker_fee_per_share(0.9) > taker_fee_per_share(0.99)
    assert abs(taker_fee_per_share(0.5) - 0.0175) < 1e-9


def test_prob_up_is_half_when_price_at_reference_at_start():
    assert abs(prob_up(100, 100, 100, 0, 300, 1e-4) - 0.5) < 1e-6


def test_prob_up_rises_with_price_and_time_locks_it_in():
    early = prob_up(100, 100.05, 100.02, 60, 300, 1e-4)
    late = prob_up(100, 100.05, 100.05, 280, 300, 1e-4)
    assert 0.5 < early < late <= 1.0


def test_prob_up_symmetry():
    up = prob_up(100, 100.1, 100.05, 120, 300, 1e-4)
    down = prob_up(100, 99.9, 99.95, 120, 300, 1e-4)
    assert abs(up + down - 1.0) < 1e-3


def test_prob_up_settled_window():
    assert prob_up(100, 101, 100.5, 300, 300, 1e-4) == 1.0
    assert prob_up(100, 99, 99.5, 300, 300, 1e-4) == 0.0


def test_side_edges_pick_underpriced_side_and_include_fee():
    m = _market(p_up=0.80, ask_up=0.55, ask_dn=0.46)
    edges = {s: e for s, _, _, _, e in side_edges(m)}
    assert edges[Side.YES] > 0.05 > edges[Side.NO]
    # el edge del lado YES es p - haircut - (ask + fee)
    yes = [x for x in side_edges(m) if x[0] == Side.YES][0]
    assert abs(yes[4] - (yes[1] - (0.55 + taker_fee_per_share(0.55)))) < 1e-9
