import json

import aggregate
import state


def written_records(cfg):
    path = next((cfg.data_dir).glob("*.jsonl"))
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── averaging ─────────────────────────────────────────────────────────────────

def test_minute_average_is_written_and_pushed_to_the_series(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    for value in (20.0, 21.0, 22.0):
        aggregate.add_sample("room_temp", "2026-08-16T10:00:05", value)
    aggregate.roll_minute("2026-08-16T10:01")

    records = written_records(pipeline)
    assert records == [{"ts": "2026-08-16T10:00:30", "room_temp": 21.0}]
    assert list(state.series["room_temp"]) == [
        {"ts": "2026-08-16T10:00:30", "value": 21.0}
    ]


def test_the_log_key_override_names_the_field_on_disk(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    aggregate.add_sample("run_data_return_temp", "2026-08-16T10:00:05", 30.0)
    aggregate.roll_minute("2026-08-16T10:01")

    record = written_records(pipeline)[0]
    assert "return_temp" in record and "run_data_return_temp" not in record
    # ... but the in-memory series is still keyed the normal way
    assert list(state.series["run_data_return_temp"])[0]["value"] == 30.0


def test_the_record_timestamp_is_the_middle_of_the_minute(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    aggregate.add_sample("room_temp", "2026-08-16T10:00:05", 20.0)
    aggregate.roll_minute("2026-08-16T10:01")
    assert written_records(pipeline)[0]["ts"] == "2026-08-16T10:00:30"


def test_nothing_is_written_for_an_empty_minute(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    aggregate.roll_minute("2026-08-16T10:01")
    assert not list((pipeline.data_dir).glob("*.jsonl")) or written_records(pipeline) == []


def test_staying_within_a_minute_does_not_flush(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    aggregate.add_sample("room_temp", "2026-08-16T10:00:05", 20.0)
    aggregate.roll_minute("2026-08-16T10:00")
    assert len(state.series["room_temp"]) == 0


# ── correction reaching the bucket ────────────────────────────────────────────

def test_a_spike_is_corrected_before_it_reaches_the_average(pipeline):
    """
    The glitch is only detectable once a good reading follows it, by which time
    it is already in the bucket — so the correction has to reach back into it.
    """
    aggregate.roll_minute("2026-08-16T10:00")
    for i, value in enumerate([20.0, 20.5, 99.0, 21.0, 21.5]):
        aggregate.add_sample("room_temp", f"2026-08-16T10:00:{i:02d}", value)
    aggregate.roll_minute("2026-08-16T10:01")

    # Without the correction the average would be 36.4
    assert written_records(pipeline)[0]["room_temp"] == 20.75


def test_the_correction_names_the_timestamp_it_applies_to(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    fixes = []
    for i, value in enumerate([20.0, 20.5, 99.0, 21.0]):
        fixes += aggregate.add_sample("room_temp", f"2026-08-16T10:00:{i:02d}", value)

    assert fixes == [{"ts": "2026-08-16T10:00:02", "value": 20.75, "was": 99.0}]


def test_the_correction_lands_on_the_spike_not_a_later_reading(pipeline):
    """
    Regression test for the patch this replaces, which scanned the bucket
    backwards for the last value differing from the corrected one. Here that is
    the *good* reading after the spike, so the old code overwrote a healthy
    sample and left the spike in place.
    """
    aggregate.roll_minute("2026-08-16T10:00")
    for i, value in enumerate([20.0, 99.0, 25.0]):
        aggregate.add_sample("room_temp", f"2026-08-16T10:00:{i:02d}", value)

    bucket = dict(aggregate.minute_bucket["room_temp"])
    assert bucket["2026-08-16T10:00:01"] == 22.5   # the spike, interpolated
    assert bucket["2026-08-16T10:00:02"] == 25.0   # untouched

    aggregate.roll_minute("2026-08-16T10:01")
    # the old behaviour left 99.0 in the bucket and averaged to 47.167
    assert written_records(pipeline)[0]["room_temp"] == 22.5


def test_a_field_without_bounds_is_never_corrected(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    fixes = []
    for i, value in enumerate([1.0, 999.0, 1.0]):
        fixes += aggregate.add_sample("heat_curve", f"2026-08-16T10:00:{i:02d}", value)

    assert fixes == []
    assert "heat_curve" not in aggregate.windows


# ── lifecycle ─────────────────────────────────────────────────────────────────

def test_shutdown_flushes_the_minute_in_progress(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    aggregate.add_sample("room_temp", "2026-08-16T10:00:05", 20.0)
    aggregate.shutdown_flush()

    assert written_records(pipeline)[0]["room_temp"] == 20.0


def test_shutdown_with_an_empty_bucket_writes_nothing(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    aggregate.shutdown_flush()
    assert not list((pipeline.data_dir).glob("*.jsonl"))


def test_reset_clears_the_day(pipeline):
    aggregate.roll_minute("2026-08-16T10:00")
    aggregate.add_sample("room_temp", "2026-08-16T10:00:05", 20.0)
    aggregate.roll_minute("2026-08-16T10:01")
    aggregate.add_sample("room_temp", "2026-08-16T10:01:05", 21.0)

    aggregate.reset()

    assert len(state.series["room_temp"]) == 0
    assert aggregate.minute_bucket == {}
    assert all(len(w) == 0 for w in aggregate.windows.values())
