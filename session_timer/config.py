"""Central configuration for session-timer.

Everything tunable lives here.  Deployment-dependent values are read
from the environment; behavioral limits are constants (edit this file
to change them) so behavior stays predictable.
"""

from __future__ import annotations

import os
import re

# ------------------------------------------------- session names (URLs)
# ASCII-only by design (URL secrecy, simple routing; see DESIGN.md).
NAME_MAX_LENGTH = 128
NAME_RE = re.compile(rf"^[A-Za-z0-9_-]{{1,{NAME_MAX_LENGTH}}}$")

# ------------------------------------------------------------- events
EVENT_NAME_MAX_LENGTH = 128
MAX_EVENTS_PER_SESSION = 128
# Durations cap at one day: a daily template has no longer events, and
# an unbounded value would render garbage client-side (JS number
# precision) and allow nonsense schedules.
MAX_DURATION_SECONDS = 24 * 3600
# PUT /api/{name}/events.csv body cap: 128 events x ~200 bytes is a few
# KB; 1 MB leaves generous headroom while bounding request memory.
MAX_CSV_UPLOAD_BYTES = 1_000_000

# ----------------------------------------------------------- sessions
MAX_SESSIONS = 1024
# DESIGN.md: expire after 31 days idle.
SESSION_EXPIRY_SECONDS = 31 * 24 * 3600
# When the session cap is hit, only sessions idle at least this long
# may be evicted to free a slot; otherwise creation is refused.
SESSION_EVICTION_IDLE_SECONDS = 3600

# ------------------------------------------------- persistence (opt-in)
# Set SESSION_TIMER_DATA_DIR to enable snapshots; unset = memory-only.
DATA_DIR = os.environ.get("SESSION_TIMER_DATA_DIR") or None
SNAPSHOT_INTERVAL_SECONDS = int(
    os.environ.get("SESSION_TIMER_SNAPSHOT_INTERVAL", "300")
)
# Fail soft on a garbage value (or one below 5 s, i.e. a hot snapshot
# loop): the interval is an operator convenience, not a security
# boundary, so it clamps to the default instead of crashing at startup
# or thrashing the disk.
try:
    SNAPSHOT_INTERVAL_SECONDS = max(5, int(SNAPSHOT_INTERVAL_SECONDS))
except (TypeError, ValueError):
    SNAPSHOT_INTERVAL_SECONDS = 300

if not isinstance(SNAPSHOT_INTERVAL_SECONDS, int) or SNAPSHOT_INTERVAL_SECONDS < 5:
    SNAPSHOT_INTERVAL_SECONDS = 300

# ------------------------------------------------- default timezone
# Optional default session timezone (IANA zone name, e.g.
# "Europe/Berlin"): used when a session has no timezone of its own.
# Set via the SESSION_TIMER_TZ environment variable.
DEFAULT_TZ = os.environ.get("SESSION_TIMER_TZ") or None

# ------------------------------------- local server (console script)
HOST = "127.0.0.1"
PORT = 8000

# --------------------------------------- admin page banner message
# Optional sysadmin message (SESSION_TIMER_MESSAGE) shown as a yellow
# bar at the top of the admin page.  Read once at import: changing it
# requires a server restart.  Stripped and capped (a runaway value
# would wreck the page layout); unset/empty -> None.
ADMIN_MESSAGE = (os.environ.get("SESSION_TIMER_MESSAGE") or "").strip()[:512] or None
