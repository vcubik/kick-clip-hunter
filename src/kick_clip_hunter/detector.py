"""Heuristics for flagging potentially viral moments from chat activity.

Keeps a short in-memory rolling window per channel of per-message entries
and compares four independent signals against the channel's own recent
baseline (or, for laugh/emote_mention, a flat minimum since their baseline
is normally near zero):

- message_rate: overall message volume spike
- emotes: native Kick emote spike
- laugh: the Czech "xD"/"xDDDD" laugh convention - a strong, high-precision
  signal, so it needs fewer occurrences but counts for more in the score
- emote_mention: a channel's 7TV emote name typed as plain text - noisier
  than laugh (some emote names double as ordinary words), so it needs both
  more occurrences and more distinct people saying it

Every count-based signal also requires a minimum number of *distinct
senders*, not just raw message count - Kick doesn't rate-limit a single
account by default, so one person spamming would otherwise look identical
to a genuine crowd reaction.

State is per-process and not persisted - after a restart, a channel needs
a warm-up period before it can trust its own baseline again (see the
history-length check below). Thresholds are a first pass, expected to keep
being tuned once we've watched detections against real streams.
"""

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

SHORT_WINDOW_SECONDS = 10
BASELINE_WINDOW_SECONDS = 300
COOLDOWN_SECONDS = 60

MESSAGE_SPIKE_MULTIPLIER = 3.0
MIN_SHORT_WINDOW_MESSAGES = 8
MIN_UNIQUE_SENDERS_MESSAGE_RATE = 5

EMOTE_SPIKE_MULTIPLIER = 3.0
MIN_SHORT_WINDOW_EMOTES = 10
MIN_UNIQUE_SENDERS_EMOTE_RATE = 5

MIN_SHORT_WINDOW_LAUGHS = 2
MIN_UNIQUE_SENDERS_LAUGH = 2
LAUGH_SCORE_WEIGHT = 2.0

MIN_SHORT_WINDOW_EMOTE_MENTIONS = 5
MIN_UNIQUE_SENDERS_EMOTE_MENTION = 5

# Matches the exaggerated "xDDDD" laugh (not plain "xd", which is too
# common on its own to be a useful signal).
LAUGH_PATTERN = re.compile(r"xd{2,}", re.IGNORECASE)

# each entry: (timestamp, sender, emote_count, is_laugh, is_emote_mention)
_entries: dict[str, deque] = defaultdict(deque)
_last_moment_at: dict[str, float] = {}


@dataclass
class Spike:
    reasons: list[str]
    score: float
    message_count: int
    baseline_message_rate: float
    current_message_rate: float
    emote_count: int
    keyword_hits: int  # laughs + emote mentions combined, for display/storage


def classify_message(content: str, channel_keywords: set[str] = frozenset()) -> tuple[bool, bool]:
    """Returns (is_laugh, is_emote_mention) for a chat message's text."""
    is_laugh = bool(LAUGH_PATTERN.search(content))
    lowered = content.lower()
    is_emote_mention = any(keyword in lowered for keyword in channel_keywords)
    return is_laugh, is_emote_mention


def record_message(
    channel_slug: str,
    *,
    sender: str,
    emote_count: int = 0,
    is_laugh: bool = False,
    is_emote_mention: bool = False,
    now: float | None = None,
) -> Spike | None:
    now = now if now is not None else time.monotonic()
    entries = _entries[channel_slug]
    entries.append((now, sender, emote_count, is_laugh, is_emote_mention))

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
    short_unique_senders = len({e[1] for e in short})
    short_emote_count = sum(e[2] for e in short)
    short_emote_unique_senders = len({e[1] for e in short if e[2] > 0})
    short_laugh_count = sum(1 for e in short if e[3])
    short_laugh_unique_senders = len({e[1] for e in short if e[3]})
    short_mention_count = sum(1 for e in short if e[4])
    short_mention_unique_senders = len({e[1] for e in short if e[4]})

    baseline_seconds = history_seconds - SHORT_WINDOW_SECONDS
    baseline_message_count = len(entries) - short_message_count
    baseline_emote_count = sum(e[2] for e in entries) - short_emote_count
    baseline_message_rate = baseline_message_count / baseline_seconds
    baseline_emote_rate = baseline_emote_count / baseline_seconds
    current_message_rate = short_message_count / SHORT_WINDOW_SECONDS
    current_emote_rate = short_emote_count / SHORT_WINDOW_SECONDS

    reasons = []
    scores = []

    if (
        short_message_count >= MIN_SHORT_WINDOW_MESSAGES
        and short_unique_senders >= MIN_UNIQUE_SENDERS_MESSAGE_RATE
        and current_message_rate >= baseline_message_rate * MESSAGE_SPIKE_MULTIPLIER
    ):
        reasons.append("message_rate")
        scores.append(current_message_rate / baseline_message_rate if baseline_message_rate > 0 else current_message_rate)

    if (
        short_emote_count >= MIN_SHORT_WINDOW_EMOTES
        and short_emote_unique_senders >= MIN_UNIQUE_SENDERS_EMOTE_RATE
        and current_emote_rate >= baseline_emote_rate * EMOTE_SPIKE_MULTIPLIER
    ):
        reasons.append("emotes")
        scores.append(current_emote_rate / baseline_emote_rate if baseline_emote_rate > 0 else current_emote_rate)

    if (
        short_laugh_count >= MIN_SHORT_WINDOW_LAUGHS
        and short_laugh_unique_senders >= MIN_UNIQUE_SENDERS_LAUGH
    ):
        reasons.append("laugh")
        scores.append(short_laugh_count * LAUGH_SCORE_WEIGHT)

    if (
        short_mention_count >= MIN_SHORT_WINDOW_EMOTE_MENTIONS
        and short_mention_unique_senders >= MIN_UNIQUE_SENDERS_EMOTE_MENTION
    ):
        reasons.append("emote_mention")
        scores.append(float(short_mention_count))

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
        keyword_hits=short_laugh_count + short_mention_count,
    )
