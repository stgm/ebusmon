import correction

LO, HI = 10.0, 35.0


def window(*values):
    return [{"ts": f"t{i}", "value": v} for i, v in enumerate(values)]


def test_no_corrections_when_all_in_bounds():
    assert correction.correct_window(window(20.0, 20.5, 21.0), LO, HI) == []


def test_single_spike_is_interpolated_from_its_neighbours():
    fixes = correction.correct_window(window(20.0, 20.5, 99.0, 21.0, 21.5), LO, HI)
    assert fixes == [{"ts": "t2", "value": 20.75, "was": 99.0}]


def test_run_of_two_is_interpolated_in_steps():
    fixes = correction.correct_window(window(10.0, 99.0, 99.0, 40.0), LO, HI)
    # 40.0 is itself out of bounds, so the only good anchors are 10.0 and ... none
    # on the right. Nothing is correctable.
    assert fixes == []

    fixes = correction.correct_window(window(10.0, 99.0, 99.0, 22.0), LO, HI)
    assert [f["value"] for f in fixes] == [14.0, 18.0]
    assert [f["ts"] for f in fixes] == ["t1", "t2"]


def test_run_of_three_is_left_alone():
    """A long run is more likely a real state change than a glitch."""
    assert correction.correct_window(window(20.0, 99.0, 99.0, 99.0, 21.0), LO, HI) == []


def test_spike_at_newest_end_is_left_for_a_later_call():
    """No good reading after it yet, so there is nothing to interpolate towards."""
    assert correction.correct_window(window(20.0, 20.5, 99.0), LO, HI) == []


def test_spike_at_oldest_end_is_left_alone():
    """No good reading before it, so there is no left anchor."""
    assert correction.correct_window(window(99.0, 20.0, 20.5), LO, HI) == []


def test_below_lower_bound_is_corrected_too():
    fixes = correction.correct_window(window(20.0, -50.0, 22.0), LO, HI)
    assert fixes == [{"ts": "t1", "value": 21.0, "was": -50.0}]


def test_bounds_are_inclusive():
    assert correction.correct_window(window(20.0, LO, 20.0), LO, HI) == []
    assert correction.correct_window(window(20.0, HI, 20.0), LO, HI) == []


def test_two_separate_spikes_are_both_corrected():
    fixes = correction.correct_window(window(10.0, 99.0, 20.0, 99.0, 30.0), LO, HI)
    assert [f["value"] for f in fixes] == [15.0, 25.0]
    assert [f["ts"] for f in fixes] == ["t1", "t3"]


def test_input_is_not_mutated():
    points = window(20.0, 99.0, 22.0)
    before = [dict(p) for p in points]
    correction.correct_window(points, LO, HI)
    assert points == before
