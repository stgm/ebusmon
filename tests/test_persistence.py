import json
from datetime import date

import persistence
import state

DAY = date(2026, 5, 4)


def write_day(cfg, d, *lines):
    cfg.data_dir.mkdir(exist_ok=True)
    path = cfg.data_dir / f"{d.isoformat()}.jsonl"
    path.write_text("".join(line + "\n" for line in lines))
    return path


# ── round trip ────────────────────────────────────────────────────────────────

def test_append_then_load_round_trip(pipeline):
    record = {"ts": "2026-08-16T10:00:30", "room_temp": 21.0}
    persistence.append_record(record)
    assert persistence.load_day(date.today()) == [record]


def test_append_creates_the_data_dir(pipeline):
    assert not pipeline.data_dir.exists()
    persistence.append_record({"ts": "2026-08-16T10:00:30"})
    assert pipeline.data_dir.exists()


def test_reading_does_not_create_the_data_dir(pipeline):
    assert persistence.load_day(DAY) == []
    assert persistence.available_days() == []
    assert not pipeline.data_dir.exists()


def test_records_append_rather_than_overwrite(pipeline):
    persistence.append_record({"ts": "a"})
    persistence.append_record({"ts": "b"})
    assert [r["ts"] for r in persistence.load_day(date.today())] == ["a", "b"]


# ── tolerating damaged files ──────────────────────────────────────────────────

def test_malformed_line_is_skipped(pipeline):
    """A crash mid-write can leave a partial line; the rest must still load."""
    write_day(pipeline, DAY,
              json.dumps({"ts": "a", "room_temp": 1.0}),
              '{"ts": "b", "room_te',
              json.dumps({"ts": "c", "room_temp": 3.0}))
    assert [r["ts"] for r in persistence.load_day(DAY)] == ["a", "c"]


def test_blank_lines_are_skipped(pipeline):
    write_day(pipeline, DAY, json.dumps({"ts": "a"}), "", "   ",
              json.dumps({"ts": "b"}))
    assert len(persistence.load_day(DAY)) == 2


def test_missing_file_gives_an_empty_list(pipeline):
    assert persistence.load_day(DAY) == []


# ── available_days ────────────────────────────────────────────────────────────

def test_available_days_is_sorted_oldest_first(pipeline):
    for d in ["2026-05-04", "2026-03-28", "2026-04-15"]:
        write_day(pipeline, date.fromisoformat(d), "{}")
    assert persistence.available_days() == ["2026-03-28", "2026-04-15", "2026-05-04"]


def test_available_days_ignores_files_that_are_not_dates(pipeline):
    write_day(pipeline, DAY, "{}")
    (pipeline.data_dir / "backup.jsonl").write_text("{}")
    (pipeline.data_dir / "2026-05-05.txt").write_text("{}")
    assert persistence.available_days() == ["2026-05-04"]


def test_available_days_with_no_data_dir(pipeline):
    assert persistence.available_days() == []


# ── pivoting to series ────────────────────────────────────────────────────────

def test_day_series_pivots_records_into_per_key_lists(pipeline):
    write_day(pipeline, DAY,
              json.dumps({"ts": "t1", "room_temp": 20.0, "heat_curve": 1.0}),
              json.dumps({"ts": "t2", "room_temp": 21.0, "heat_curve": 1.5}))
    series = persistence.day_series(DAY)
    assert series["room_temp"] == [{"ts": "t1", "value": 20.0},
                                   {"ts": "t2", "value": 21.0}]
    assert series["heat_curve"] == [{"ts": "t1", "value": 1.0},
                                    {"ts": "t2", "value": 1.5}]


def test_day_series_resolves_the_log_key_override(pipeline):
    """On disk the field is `return_temp`; the API serves it under its own key."""
    write_day(pipeline, DAY, json.dumps({"ts": "t1", "return_temp": 30.0}))
    series = persistence.day_series(DAY)
    assert series["run_data_return_temp"] == [{"ts": "t1", "value": 30.0}]
    assert "return_temp" not in series


def test_day_series_ignores_keys_that_are_no_longer_configured(pipeline):
    write_day(pipeline, DAY, json.dumps({"ts": "t1", "room_temp": 20.0,
                                         "removed_field": 99.0}))
    assert "removed_field" not in persistence.day_series(DAY)


def test_day_series_skips_a_key_missing_from_a_record(pipeline):
    """A field added mid-day has no data in the earlier records."""
    write_day(pipeline, DAY,
              json.dumps({"ts": "t1"}),
              json.dumps({"ts": "t2", "room_temp": 21.0}))
    assert persistence.day_series(DAY)["room_temp"] == [{"ts": "t2", "value": 21.0}]


def test_day_series_lists_every_configured_key_even_when_empty(pipeline):
    write_day(pipeline, DAY, json.dumps({"ts": "t1", "room_temp": 20.0}))
    series = persistence.day_series(DAY)
    assert set(series) == set(state.fields)
    assert series["heat_curve"] == []


# ── restore ───────────────────────────────────────────────────────────────────

def test_restore_today_fills_the_live_series(pipeline):
    persistence.append_record({"ts": "t1", "room_temp": 20.0})
    persistence.append_record({"ts": "t2", "room_temp": 21.0})

    persistence.restore_today()

    assert list(state.series["room_temp"]) == [{"ts": "t1", "value": 20.0},
                                               {"ts": "t2", "value": 21.0}]


def test_restore_today_with_no_file_leaves_the_series_empty(pipeline):
    persistence.restore_today()
    assert all(len(s) == 0 for s in state.series.values())
