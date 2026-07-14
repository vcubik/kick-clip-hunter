"""Heuristics for flagging potentially viral moments from chat activity.

Keeps a short in-memory rolling window per channel of (timestamp, emote_count,
keyword_hit) entries and compares three independent signals - message rate,
emote rate, and keyword frequency - against the channel's own recent
baseline (or, for keywords, a flat minimum since their baseline is normally
near zero). State is per-process and not persisted - after a restart, a
channel needs a warm-up period before it can trust its own baseline again
(see the history-length check below).

The keyword list and thresholds below are a first pass, expected to be
tuned once we've watched detections against real streams (see roadmap M3).
"""

import time
from collections import defaultdict, deque
from dataclasses import dataclass

SHORT_WINDOW_SECONDS = 10
BASELINE_WINDOW_SECONDS = 300
COOLDOWN_SECONDS = 60

MESSAGE_SPIKE_MULTIPLIER = 3.0
MIN_SHORT_WINDOW_MESSAGES = 8

EMOTE_SPIKE_MULTIPLIER = 3.0
MIN_SHORT_WINDOW_EMOTES = 10

MIN_SHORT_WINDOW_KEYWORD_HITS = 3

KEYWORDS = {
    "kekw", "omg", "wtf", "lul", "lmao", "lmaoo", "pog", "poggers",
    "no way", "wait what", "clip it", "clip that",
}

_entries: dict[str, deque] = defaultdict(deque)  # each entry: (timestamp, emote_count, keyword_hit)
_last_moment_at: dict[str, float] = {}


@dataclass
class Spike:
    reasons: list[str]
    score: float
    message_count: int
    baseline_message_rate: float
    current_message_rate: float
    emote_count: int
    keyword_hits: int


def matches_keyword(content: str) -> bool:
    lowered = content.lower()
    return any(keyword in lowered for keyword in KEYWORDS)


def record_message(
    channel_slug: str,
    *,
    emote_count: int = 0,
    keyword_hit: bool = False,
    now: float | None = None,
) -> Spike | None:
    now = now if now is not None else time.monotonic()
    entries = _entries[channel_slug]
    entries.append((now, emote_count, keyword_hit))

    cutoff = now - BASELINE_WINDOW_SECONDS
    while entries and entries[0][0] < cutoff:
        entries.popleft()

    history_seconds = now - entries[0][0]
    if history_seconds < BASELINE_WINDOW_SECONDS - SHORT_WINDOW_SECONDS:
        return None  # not enough history yet to trust a baseline

    if now - _last_moment_at.get(channel_slug, 0.0) < COOLDOWN_SECONDS:
        return None

    short_cutoff = now - SHORT_WINDOW_SECONDS
    short = [e for e in entries if e[0] >= short_cutoff]
    short_message_count = len(short)
    short_emote_count = sum(e[1] for e in short)
    short_keyword_hits = sum(1 for e in short if e[2])

    baseline_seconds = history_seconds - SHORT_WINDOW_SECONDS
    baseline_message_count = len(entries) - short_message_count
    baseline_emote_count = sum(e[1] for e in entries) - short_emote_count
    baseline_message_rate = baseline_message_count / baseline_seconds
    baseline_emote_rate = baseline_emote_count / baseline_seconds
    current_message_rate = short_message_count / SHORT_WINDOW_SECONDS
    current_emote_rate = short_emote_count / SHORT_WINDOW_SECONDS

    reasons = []
    scores = []

    if (
        short_message_count >= MIN_SHORT_WINDOW_MESSAGES
        and current_message_rate >= baseline_message_rate * MESSAGE_SPIKE_MULTIPLIER
    ):
        reasons.append("message_rate")
        scores.append(current_message_rate / baseline_message_rate if baseline_message_rate > 0 else current_message_rate)

    if (
        short_emote_count >= MIN_SHORT_WINDOW_EMOTES
        and current_emote_rate >= baseline_emote_rate * EMOTE_SPIKE_MULTIPLIER
    ):
        reasons.append("emotes")
        scores.append(current_emote_rate / baseline_emote_rate if baseline_emote_rate > 0 else current_emote_rate)

    if short_keyword_hits >= MIN_SHORT_WINDOW_KEYWORD_HITS:
        reasons.append("keywords")
        scores.append(float(short_keyword_hits))

    if not reasons:
        return None

    _last_moment_at[channel_slug] = now
    return Spike(
        reasons=reasons,
        score=max(scores),
        message_count=short_message_count,
        baseline_message_rate=baseline_message_rate,
        current_message_rate=current_message_rate,
        emote_count=short_emote_count,
        keyword_hits=short_keyword_hits,
    )
