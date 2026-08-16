"""
Bounds-based outlier correction.

A reading outside [lo, hi] is treated as a glitch and replaced by linear
interpolation between its surrounding good neighbours.

Two limits apply. A run of out-of-bounds points is only corrected if it is at
most 2 long — a longer run is more likely a real state change than a glitch, so
it is left alone. And a run needs a good point on both sides to interpolate
between, so a bad point at either end of the window is skipped and reconsidered
on a later call, once more readings have arrived.

WINDOW is how many recent raw readings the caller keeps per field. It sets how
long a bad point stays correctable: it must still be in the window when a good
reading arrives after it.
"""

WINDOW = 5


def correct_window(points: list[dict], lo: float, hi: float) -> list[dict]:
    """
    Scan `points` oldest → newest and interpolate over short out-of-bounds runs.

    `points` is a list of {"ts": str, "value": float}. It is not modified — the
    function is pure, and the caller applies the result to its own window.

    Returns a list of {"ts", "value", "was"} dicts, one per corrected point:
    the timestamp it applies to, the interpolated value, and the reading it
    replaces (for logging).
    """
    win = list(points)
    n = len(win)
    corrections: list[dict] = []
    i = 0

    while i < n:
        if lo <= win[i]["value"] <= hi:
            i += 1
            continue

        run_end = i + 1
        while run_end < n and not (lo <= win[run_end]["value"] <= hi):
            run_end += 1
        run_len = run_end - i

        if run_len <= 2 and i > 0 and run_end < n:
            left_val  = win[i - 1]["value"]
            right_val = win[run_end]["value"]
            steps     = run_len + 1

            for offset in range(run_len):
                point  = win[i + offset]
                interp = round(left_val + (right_val - left_val) * (offset + 1) / steps, 3)
                win[i + offset] = {**point, "value": interp}
                corrections.append({"ts": point["ts"], "value": interp,
                                    "was": point["value"]})

        i = run_end

    return corrections
