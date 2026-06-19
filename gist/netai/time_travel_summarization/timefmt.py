"""Single source of truth for the event-timestamp format.

The SAME format is used by the burned-in video overlay AND the collisions CSV, so
inference (the VLM reads the overlay) and ground-truth labels (parsed from the CSV)
always use the same representation. Change ``PRECISION`` in one place to widen or
narrow detail for both.

    PRECISION = "seconds"       -> "HH:MM:SS"       (default; current granularity)
    PRECISION = "milliseconds"  -> "HH:MM:SS.mmm"   (finer; see caveat)

Caveat for "milliseconds": an object-object collision writes two CSV rows (one per
object) stamped microseconds apart, so at ms precision they may land on different
keys and the pair-grouping in ``utils/build_dataset.py`` would split them. Moving to
ms granularity therefore ALSO needs both rows of one collision stamped with a shared
event time (a recorder change). Seconds granularity is unaffected.

Dependency-free (stdlib only) so it is importable from physics/, app/, and utils/.
The high-rate position trace (``physics/trace_recorder.py``) intentionally keeps its
own date+ms format — it is an internal reference for observability, not shown/labeled.
"""

from datetime import date as _date
from datetime import datetime, timedelta

PRECISION = "seconds"  # "seconds" | "milliseconds"


def format_event_time(dt: datetime) -> str:
    """Format an event time for the overlay and the collisions CSV."""
    if PRECISION == "milliseconds":
        return dt.strftime("%H:%M:%S.%f")[:-3]
    return dt.strftime("%H:%M:%S")


def parse_event_time(s: str, anchor: datetime) -> datetime:
    """Parse an event-time string into a full datetime anchored to ``anchor``'s date.

    Accepts the date-less overlay/CSV forms ("HH:MM:SS", "HH:MM:SS.mmm") and the
    legacy date-ful forms ("YYYY-MM-DD HH:MM:SS[.ffffff]"). For date-less inputs the
    date comes from ``anchor`` (e.g. capture_start); if the time-of-day is earlier
    than the anchor's, it is treated as having rolled past midnight (+1 day).
    """
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    t = None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            t = datetime.strptime(s, fmt).time()
            break
        except ValueError:
            pass
    if t is None:
        raise ValueError(f"unrecognized event time: {s!r}")
    anchor_date = anchor.date() if anchor is not None else _date.today()
    dt = datetime.combine(anchor_date, t)
    if anchor is not None and dt < anchor - timedelta(seconds=1):
        dt += timedelta(days=1)  # rolled past midnight during the capture
    return dt
