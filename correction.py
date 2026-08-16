import config
import state


def _in_bounds(key: str, value: float) -> bool:
    lo, hi = config.BOUNDS[key]
    return lo <= value <= hi


def check_and_correct(key: str) -> list[dict]:
    """
    Called after a new raw point has been appended to state._windows[key].

    Scans the window from oldest to newest. Any run of 1 or 2 consecutive
    out-of-bounds points flanked by in-bounds points on both sides is replaced
    by linear interpolation between those flanking points.

    Returns a list of {ts, value} correction dicts to broadcast (may be empty).
    """
    if key not in config.BOUNDS:
        return []

    win = list(state._windows[key])
    n   = len(win)
    corrections = []
    i = 0

    while i < n:
        if _in_bounds(key, win[i]["value"]):
            i += 1
            continue

        run_end = i + 1
        while run_end < n and not _in_bounds(key, win[run_end]["value"]):
            run_end += 1

        run_len = run_end - i

        if run_len <= 2 and i > 0 and run_end < n:
            left_val  = win[i - 1]["value"]
            right_val = win[run_end]["value"]
            steps     = run_len + 1

            for offset in range(run_len):
                pt     = win[i + offset]
                interp = round(left_val + (right_val - left_val) * (offset + 1) / steps, 3)
                print(f"[bounds] {key}: {pt['value']} out of {config.BOUNDS[key]}, "
                      f"corrected → {interp}")
                win[i + offset] = {**pt, "value": interp}
                corrections.append({"ts": pt["ts"], "value": interp})

            real_win = state._windows[key]
            for offset in range(run_len):
                idx_from_end = n - (i + offset) - 1
                real_win.rotate(idx_from_end + 1)
                real_win[0] = win[i + offset]
                real_win.rotate(-(idx_from_end + 1))

        i = run_end

    return corrections
