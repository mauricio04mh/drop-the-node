import pytest

from dtn.detector import FailureDetector

STORES = ("a", "b", "c")


def detector(**kw):
    return FailureDetector(STORES, timeout_min=kw.pop("tmin", 0.2), timeout_max=kw.pop("tmax", 2.0), **kw)


def test_without_measurements_waits_the_maximum_and_hedges_early():
    d = detector()
    assert d.timeout("a") == 2.0
    assert d.hedge_delay("a") == 0.2


def test_first_sample_sets_mean_and_half_deviation():
    d = detector(tmin=0.0)
    d.observe("a", 0.1)
    # SRTT = 0.1, RTTVAR = 0.05
    assert d.hedge_delay("a") == pytest.approx(0.1 + 2 * 0.05)
    assert d.timeout("a") == pytest.approx(0.1 + 4 * 0.05)


def test_steady_latency_tightens_the_timeout_down_to_the_minimum():
    d = detector()
    for _ in range(100):
        d.observe("a", 0.01)
    assert d.timeout("a") == 0.2  # clamped to the minimum


def test_jittery_latency_widens_the_timeout():
    steady, jittery = detector(tmin=0.0), detector(tmin=0.0)
    for i in range(50):
        steady.observe("a", 0.05)
        jittery.observe("a", 0.01 if i % 2 else 0.09)
    assert jittery.timeout("a") > steady.timeout("a")


def test_timeout_never_exceeds_the_maximum():
    d = detector()
    d.observe("a", 30.0)
    assert d.timeout("a") == 2.0


def test_fixed_hedge_delay_overrides_the_estimate():
    d = detector(hedge_delay=0.0)
    d.observe("a", 0.5)
    assert d.hedge_delay("a") == 0.0


def test_suspects_move_to_the_end_and_come_back_when_readmitted():
    d = detector()
    assert d.suspect("a") is True
    assert d.suspect("a") is False  # already suspect
    assert d.prefer(["a", "b", "c"]) == ["b", "c", "a"]
    assert d.suspects() == ["a"]
    assert d.readmit("a") is True
    assert d.prefer(["a", "b", "c"]) == ["a", "b", "c"]
