"""Message-rate spike heuristic for flagging potentially viral moments.

Keeps a short in-memory rolling window of message timestamps per channel and
flags a spike when the recent (short-window) rate is well above the
channel's own recent baseline rate. State is per-process and not persisted -
after a restart, a channel just needs a few minutes of traffic before it
can trust its own baseline again (see the history-length check below).
"""

import time
from collections import defaultdict, deque
from dataclasses import dataclass

SHORT_WINDOW_SECONDS = 10
BASELINE_WINDOW_SECONDS = 300
SPIKE_MULTIPLIER = 3.0
MIN_SHORT_WINDOW_MESSAGES = 8
COOLDOWN_SECONDS = 60

_timestamps: dict[str, deque] = defaultdict(deque)
_last_moment_at: dict[str, float] = {}


@dataclass
class Spike:
    message_count: int
    baseline_rate: float
    current_rate: float
    score: float


def record_message(channel_slug: str, now: float | None = None) -> Spike | None:
    now = now if now is not None else time.monotonic()
    timestamps = _timestamps[channel_slug]
    timestamps.append(now)

    cutoff = now - BASELINE_WINDOW_SECONDS
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()

    history_seconds = now - timestamps[0]
    if history_seconds < BASELINE_WINDOW_SECONDS - SHORT_WINDOW_SECONDS:
        return None  # not enough history yet to trust a baseline

    short_cutoff = now - SHORT_WINDOW_SECONDS
    short_count = sum(1 for t in timestamps if t >= short_cutoff)
    if short_count < MIN_SHORT_WINDOW_MESSAGES:
        return None

    if now - _last_moment_at.get(channel_slug, 0.0) < COOLDOWN_SECONDS:
        return None

    baseline_count = len(timestamps) - short_count
    baseline_seconds = history_seconds - SHORT_WINDOW_SECONDS
    baseline_rate = baseline_count / baseline_seconds
    current_rate = short_count / SHORT_WINDOW_SECONDS

    if current_rate < baseline_rate * SPIKE_MULTIPLIER:
        return None

    _last_moment_at[channel_slug] = now
    score = current_rate / baseline_rate if baseline_rate > 0 else current_rate
    return Spike(message_count=short_count, baseline_rate=baseline_rate, current_rate=current_rate, score=score)
