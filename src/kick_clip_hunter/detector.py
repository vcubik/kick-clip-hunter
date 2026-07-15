"""Heuristics for flagging potentially viral moments from chat activity.

Everything is judged relative to the channel's *own* recent baseline, not
fixed absolute numbers - a "boring" low-traffic stream and a busy,
interactive one both need their own reference point. Four independent
signals are tracked this way over a rolling window per channel:

- message_rate: overall message volume spike
- emotes: native Kick emote spike
- laugh: the Czech "xD"/"xDDDD" laugh convention - about as reliable a sign
  of a funny moment as chat gets, so it needs a lower multiplier but counts
  for the most in the score (see LAUGH_SCORE_WEIGHT)
- emote_mention: a channel's 7TV emote name typed as plain text - noisier
  than laugh (some emote names double as ordinary words), so it needs a
  higher multiplier to fire. Not all emote mentions are equal: emotes whose
  name itself signals laughing (KEKW, LUL, OMEGALUL, ...) count for more
  than other emotes (see EMOTE_MENTION_LAUGH_WEIGHT / _OTHER_WEIGHT) - both
  for reaching the threshold and for the final score.

Every signal also requires a baseline-relative spike in *distinct
senders*, not just raw count - Kick doesn't rate-limit a single account,
so one person spamming would otherwise look identical to a genuine crowd
reaction. message_rate additionally requires a baseline-relative spike in
*distinct message content*: a giveaway-style raid where many different
real accounts all paste the same non-emote phrase has high sender
diversity but low content diversity, and shouldn't count. That guard is
skipped for emotes/laugh/emote_mention, where many people repeating the
same emote *is* the genuine pattern.

State is per-process and not persisted - after a restart, a channel needs
a warm-up period before it can trust its own baseline again (see the
history-length check below). Multipliers are a first pass, expected to
keep being tuned once we've watched detections against real streams.
"""

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

SHORT_WINDOW_SECONDS = 10
BASELINE_WINDOW_SECONDS = 300
COOLDOWN_SECONDS = 60

# Absolute floors. The baseline-relative ratio alone isn't enough on a very
# quiet channel - a jump from 1 message/10s to 3-4 messages/10s clears a 3x
# ratio easily but still isn't a real "moment" by volume. These floors make
# sure there's genuine activity underneath the ratio, not just noise from a
# tiny baseline.
#
# MIN_ABSOLUTE_COUNT itself isn't fixed - it's a fraction of that channel's
# own baseline_unique_senders (how many distinct people actually chatted in
# the last ~5 minutes), clamped to a sane range. A channel where 10 people
# chat in 5 minutes and one where 300 do need a different bar for what
# counts as trivially few messages; scaling off Kick's viewer_count was
# considered and rejected, since bots inflate it and it doesn't reflect who's
# actually chatting.
MIN_ABSOLUTE_COUNT_FLOOR = 5
MIN_ABSOLUTE_COUNT_FRACTION = 0.3
MIN_ABSOLUTE_COUNT_CEILING = 40
MIN_ABSOLUTE_LAUGHS = 2
MIN_ABSOLUTE_UNIQUE = 3


def _dynamic_min_count(baseline_unique_senders: int) -> int:
    return min(
        MIN_ABSOLUTE_COUNT_CEILING,
        max(MIN_ABSOLUTE_COUNT_FLOOR, round(baseline_unique_senders * MIN_ABSOLUTE_COUNT_FRACTION)),
    )


MESSAGE_SPIKE_MULTIPLIER = 3.0
MESSAGE_UNIQUE_SENDER_MULTIPLIER = 3.0
MESSAGE_UNIQUE_CONTENT_MULTIPLIER = 3.0

EMOTE_SPIKE_MULTIPLIER = 3.0
EMOTE_UNIQUE_SENDER_MULTIPLIER = 3.0

LAUGH_MULTIPLIER = 2.0
LAUGH_UNIQUE_SENDER_MULTIPLIER = 2.0
# Someone writing "xDDDD" is about as reliable a sign of a funny moment as
# chat gets - weighted heavily so a laugh-triggered moment's score clearly
# stands out from message_rate/emotes/emote_mention ones.
LAUGH_SCORE_WEIGHT = 5.0

EMOTE_MENTION_MULTIPLIER = 4.0
EMOTE_MENTION_UNIQUE_SENDER_MULTIPLIER = 4.0

# Not all 7TV emote mentions are equally meaningful. An emote whose own name
# signals laughing (KEKW, LUL, OMEGALUL, ...) counts for more than any other
# emote - both toward the threshold and the final score - reflecting that
# it's a stronger, more laugh-specific signal, though still weaker than the
# channel-agnostic xD/xDDDD pattern above.
EMOTE_MENTION_LAUGH_WEIGHT = 2.5
EMOTE_MENTION_OTHER_WEIGHT = 1.0

# Matches the exaggerated "xDDDD" laugh (not plain "xd", which is too
# common on its own to be a useful signal).
LAUGH_PATTERN = re.compile(r"xd{2,}", re.IGNORECASE)

# Substrings (checked against a lowercased emote name) that mark an emote as
# laugh-related. A first pass based on well-known Twitch/7TV emotes (KEKW,
# KEKWait, LUL, LULW, OMEGALUL, LOL, pepeLaugh, monkaLaugh, ...) - expected
# to be tuned as we see which emotes actually show up in real streams.
LAUGH_EMOTE_NAME_PATTERNS = ("kek", "lul", "lol", "haha", "laugh")


def is_laugh_emote_name(emote_name: str) -> bool:
    lowered = emote_name.lower()
    return any(pattern in lowered for pattern in LAUGH_EMOTE_NAME_PATTERNS)


def classify_emote_names(emote_names: list[str]) -> dict[str, float]:
    """Maps each 7TV emote name (lowercased, to match classify_message's lookup) to its mention weight."""
    return {
        name.lower(): EMOTE_MENTION_LAUGH_WEIGHT if is_laugh_emote_name(name) else EMOTE_MENTION_OTHER_WEIGHT
        for name in emote_names
    }


# each entry: (timestamp, sender, normalized_content, emote_count, is_laugh, mention_weight)
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


def classify_message(content: str, channel_keyword_weights: dict[str, float] = {}) -> tuple[bool, float]:
    """Returns (is_laugh, mention_weight) for a chat message's text.

    mention_weight is 0.0 if no channel keyword matched, otherwise the
    highest weight among the keywords that did (channel_keyword_weights
    maps each 7TV emote name to EMOTE_MENTION_LAUGH_WEIGHT or
    EMOTE_MENTION_OTHER_WEIGHT, set when the channel was subscribed).
    """
    is_laugh = bool(LAUGH_PATTERN.search(content))
    lowered = content.lower()
    matched_weights = [weight for keyword, weight in channel_keyword_weights.items() if keyword in lowered]
    mention_weight = max(matched_weights) if matched_weights else 0.0
    return is_laugh, mention_weight


def _spike_ratio(
    short_count: float, baseline_count: float, baseline_seconds: float, min_absolute: float
) -> float | None:
    """Ratio of the short-window rate to the baseline rate, or None if short_count is below the sanity floor.

    If the signal never happened during the baseline window, there's no rate
    to divide by - fall back to how many times over the sanity floor the
    short-window count is, so rare-but-real signals (like laugh) can still
    fire from a cold baseline instead of being compared against an
    unreachably small absolute rate.
    """
    if short_count < min_absolute:
        return None
    baseline_rate = baseline_count / baseline_seconds if baseline_seconds > 0 else 0.0
    if baseline_rate > 0:
        current_rate = short_count / SHORT_WINDOW_SECONDS
        return current_rate / baseline_rate
    return short_count / min_absolute


def record_message(
    channel_slug: str,
    *,
    sender: str,
    content: str = "",
    emote_count: int = 0,
    is_laugh: bool = False,
    mention_weight: float = 0.0,
    now: float | None = None,
) -> Spike | None:
    now = now if now is not None else time.monotonic()
    entries = _entries[channel_slug]
    entries.append((now, sender, content.strip().lower(), emote_count, is_laugh, mention_weight))

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
    baseline = [e for e in entries if e[0] < short_cutoff]
    baseline_seconds = history_seconds - SHORT_WINDOW_SECONDS

    short_message_count = len(short)
    short_unique_senders = len({e[1] for e in short})
    short_unique_contents = len({e[2] for e in short if e[2]})
    short_emote_count = sum(e[3] for e in short)
    short_emote_unique_senders = len({e[1] for e in short if e[3] > 0})
    short_laugh_count = sum(1 for e in short if e[4])
    short_laugh_unique_senders = len({e[1] for e in short if e[4]})
    short_mention_weight = sum(e[5] for e in short)
    short_mention_count = sum(1 for e in short if e[5] > 0)
    short_mention_unique_senders = len({e[1] for e in short if e[5] > 0})

    baseline_message_count = len(baseline)
    baseline_unique_senders = len({e[1] for e in baseline})
    baseline_unique_contents = len({e[2] for e in baseline if e[2]})
    baseline_emote_count = sum(e[3] for e in baseline)
    baseline_emote_unique_senders = len({e[1] for e in baseline if e[3] > 0})
    baseline_laugh_count = sum(1 for e in baseline if e[4])
    baseline_laugh_unique_senders = len({e[1] for e in baseline if e[4]})
    baseline_mention_weight = sum(e[5] for e in baseline)
    baseline_mention_unique_senders = len({e[1] for e in baseline if e[5] > 0})

    current_message_rate = short_message_count / SHORT_WINDOW_SECONDS
    baseline_message_rate = baseline_message_count / baseline_seconds if baseline_seconds > 0 else 0.0
    dynamic_min_count = _dynamic_min_count(baseline_unique_senders)

    reasons = []
    scores = []

    message_ratio = _spike_ratio(short_message_count, baseline_message_count, baseline_seconds, dynamic_min_count)
    sender_ratio = _spike_ratio(short_unique_senders, baseline_unique_senders, baseline_seconds, MIN_ABSOLUTE_UNIQUE)
    content_ratio = _spike_ratio(short_unique_contents, baseline_unique_contents, baseline_seconds, MIN_ABSOLUTE_UNIQUE)
    if (
        message_ratio is not None
        and message_ratio >= MESSAGE_SPIKE_MULTIPLIER
        and sender_ratio is not None
        and sender_ratio >= MESSAGE_UNIQUE_SENDER_MULTIPLIER
        and content_ratio is not None
        and content_ratio >= MESSAGE_UNIQUE_CONTENT_MULTIPLIER
    ):
        reasons.append("message_rate")
        scores.append(message_ratio)

    emote_ratio = _spike_ratio(short_emote_count, baseline_emote_count, baseline_seconds, dynamic_min_count)
    emote_sender_ratio = _spike_ratio(
        short_emote_unique_senders, baseline_emote_unique_senders, baseline_seconds, MIN_ABSOLUTE_UNIQUE
    )
    if (
        emote_ratio is not None
        and emote_ratio >= EMOTE_SPIKE_MULTIPLIER
        and emote_sender_ratio is not None
        and emote_sender_ratio >= EMOTE_UNIQUE_SENDER_MULTIPLIER
    ):
        reasons.append("emotes")
        scores.append(emote_ratio)

    laugh_ratio = _spike_ratio(short_laugh_count, baseline_laugh_count, baseline_seconds, MIN_ABSOLUTE_LAUGHS)
    laugh_sender_ratio = _spike_ratio(
        short_laugh_unique_senders, baseline_laugh_unique_senders, baseline_seconds, MIN_ABSOLUTE_UNIQUE
    )
    if (
        laugh_ratio is not None
        and laugh_ratio >= LAUGH_MULTIPLIER
        and laugh_sender_ratio is not None
        and laugh_sender_ratio >= LAUGH_UNIQUE_SENDER_MULTIPLIER
    ):
        reasons.append("laugh")
        scores.append(laugh_ratio * LAUGH_SCORE_WEIGHT)

    # mention_weight sums EMOTE_MENTION_LAUGH_WEIGHT/_OTHER_WEIGHT per
    # occurrence, so laugh-related emotes reach the threshold - and score -
    # faster than an equal number of other-emote mentions would.
    mention_ratio = _spike_ratio(short_mention_weight, baseline_mention_weight, baseline_seconds, dynamic_min_count)
    mention_sender_ratio = _spike_ratio(
        short_mention_unique_senders, baseline_mention_unique_senders, baseline_seconds, MIN_ABSOLUTE_UNIQUE
    )
    if (
        mention_ratio is not None
        and mention_ratio >= EMOTE_MENTION_MULTIPLIER
        and mention_sender_ratio is not None
        and mention_sender_ratio >= EMOTE_MENTION_UNIQUE_SENDER_MULTIPLIER
    ):
        reasons.append("emote_mention")
        scores.append(mention_ratio)

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
