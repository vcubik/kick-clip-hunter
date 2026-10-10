"""Heuristics for flagging potentially viral moments from chat activity.

Everything is judged relative to the channel's *own* recent baseline, not
fixed absolute numbers - a "boring" low-traffic stream and a busy,
interactive one both need their own reference point. Four independent
signals are tracked this way over a rolling window per channel:

- message_rate: overall message volume spike. Too weak to stand on its own -
  chat gets busier for all sorts of mundane reasons (polls, greetings,
  arguments, plain spam), and in review a bare message_rate spike was ~94%
  false positives. It no longer fires a moment by itself: it only contributes
  (as a small score booster, MESSAGE_RATE_SCORE_WEIGHT) when a laugh/emote
  signal already fired in the same window.
- emotes: a spike in distinct senders using a native Kick emote (not a raw
  emote-position count - one person stacking several emotes in a message,
  or repeating one across several messages, still only counts as one)
- laugh: the Czech "xD"/"xDDDD" laugh convention - about as reliable a sign
  of a funny moment as chat gets, so it needs a lower multiplier. Not all
  laughs are equal either: the exaggerated "xDDDD" form counts for more
  than a bare "xd" (see LAUGH_STRONG_WEIGHT / LAUGH_WEAK_WEIGHT), same
  weighting idea as emote_mention below
- emote_mention: a channel's 7TV emote name typed as plain text - noisier
  than laugh (some emote names double as ordinary words), so it needs a
  higher multiplier to fire. Not all emote mentions are equal: emotes whose
  name itself signals laughing (KEKW, LUL, OMEGALUL, ...) count for more
  than other emotes (see EMOTE_MENTION_LAUGH_WEIGHT / _OTHER_WEIGHT) - both
  for reaching the threshold and for the final score.

Emotes and emote_mention both exclude a third category entirely (weight
0.0): emote names that signal "dancing/vibing to a song" (catJAM,
headBang, beeBobble, ...) rather than laughing at something. That pattern
gets spammed by many distinct senders in near-perfect sync whenever music
plays, which produces the exact same burst shape the emotes signal is
looking for - in practice it was the single biggest source of false
"emotes" moments, so it's excluded outright rather than just downweighted
(see is_dance_emote_name).

Every signal also requires a baseline-relative spike in *distinct
senders*, not just raw count - Kick doesn't rate-limit a single account,
so one person spamming would otherwise look identical to a genuine crowd
reaction. For the reaction signals that sender count also has to reach a
floor that grows with how many people are chatting (see
MIN_REACTION_UNIQUE_FRACTION). message_rate additionally requires a baseline-relative spike in
*distinct message content*: a giveaway-style raid where many different
real accounts all paste the same non-emote phrase has high sender
diversity but low content diversity, and shouldn't count. That guard is
skipped for emotes/laugh/emote_mention, where many people repeating the
same emote *is* the genuine pattern.

Detection fires a moment at the first instant a signal crosses threshold,
but the moment doesn't have to end there: reaction_active() reports whether
new people are still piling into the reaction after that instant (see
SUSTAIN_FRACTION), which main.py's moment session uses to hold the moment
open and extend the clip for as long as that's true. Most moments don't
qualify and get a short fixed-length clip.

State is per-process and not persisted - after a restart, a channel needs
a warm-up period before it can trust its own baseline again (see the
history-length check below). Multipliers are a first pass, expected to
keep being tuned once we've watched detections against real streams.
"""

import re
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass

SHORT_WINDOW_SECONDS = 10
BASELINE_WINDOW_SECONDS = 300
COOLDOWN_SECONDS = 60

# Once a moment has fired, main.py keeps it "open" and extends the clip while
# the reaction is still going (see its moment session). "Still going" is
# judged against this fraction of the firing threshold, counting only people
# who reacted *after* the moment fired (see reaction_active). It used to be a
# deliberately weaker bar (0.5) so a fading tail kept the clip open, but in
# practice that extended nearly every clip to the session's hard cap. At 1.0
# an extension has to be earned: the reaction after the trigger must be big
# enough to have fired a moment on its own. On a replayed day of chat that
# left over half the moments at their base length and extended the rest.
SUSTAIN_FRACTION = 1.0
# ...and at least this many distinct people still reacting, however small the
# channel.
MIN_SUSTAIN_UNIQUE = 2

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
# The floor was 5 until a moment fired on a chat of 18 from three people
# posting one emote each (one of them a laugh emote: 1 + 3.5 + 1 = 5.5).
MIN_ABSOLUTE_COUNT_FLOOR = 6
MIN_ABSOLUTE_COUNT_FRACTION = 0.3
MIN_ABSOLUTE_COUNT_CEILING = 40
MIN_ABSOLUTE_LAUGHS = 2
MIN_ABSOLUTE_UNIQUE = 3
# How many *distinct* people have to be reacting (laughing / posting the
# emote) in the short window scales with the channel the same way: 3 people
# typing "xd" within 10 seconds is a crowd reaction in a chat of 20, and
# background noise in a chat of 100+ - replaying a busy channel's chat, a
# fixed floor of 3 was what let through a moment every ~5 minutes, half of
# them never more than 5 laughs deep. The ratio thresholds don't catch this:
# laughs are rare enough in the baseline that almost any small cluster clears
# them. On that replay (~80 distinct chatters per 5 minutes) this fraction
# cut 13 moments/hour to about 5; 0.125 gave ~4/hour and 0.15 ~2.5/hour.
MIN_REACTION_UNIQUE_FRACTION = 0.10
MIN_REACTION_UNIQUE_CEILING = 12


def _dynamic_min_count(baseline_unique_senders: int) -> int:
    return min(
        MIN_ABSOLUTE_COUNT_CEILING,
        max(MIN_ABSOLUTE_COUNT_FLOOR, round(baseline_unique_senders * MIN_ABSOLUTE_COUNT_FRACTION)),
    )


def _dynamic_min_reaction_unique(baseline_unique_senders: int) -> int:
    return min(
        MIN_REACTION_UNIQUE_CEILING,
        max(MIN_ABSOLUTE_UNIQUE, round(baseline_unique_senders * MIN_REACTION_UNIQUE_FRACTION)),
    )


# message_rate never triggers a moment on its own (see record_message), so
# these thresholds only gate whether it gets folded in as a booster next to a
# real laugh/emote signal. Kept deliberately high - a busy-chat spike has to
# be genuinely large to add anything.
MESSAGE_SPIKE_MULTIPLIER = 4.5
MESSAGE_UNIQUE_SENDER_MULTIPLIER = 4.0
MESSAGE_UNIQUE_CONTENT_MULTIPLIER = 4.0
# Chat just getting busier is a weak proxy for "something funny happened", so
# even as a booster it contributes only a fraction of a laugh/laugh-emote
# signal's score.
MESSAGE_RATE_SCORE_WEIGHT = 0.3

EMOTE_SPIKE_MULTIPLIER = 3.0

LAUGH_MULTIPLIER = 2.0
LAUGH_UNIQUE_SENDER_MULTIPLIER = 2.0
# Someone writing "xDDDD" is about as reliable a sign of a funny moment as
# chat gets - weighted heavily so a laugh-triggered moment's score clearly
# stands out from message_rate/emotes/emote_mention ones. Bare "xd" (a
# single d) is a weaker version of the same signal - still worth counting,
# just not as strongly.
LAUGH_STRONG_WEIGHT = 5.0
LAUGH_WEAK_WEIGHT = 3.0

EMOTE_MENTION_MULTIPLIER = 4.0
EMOTE_MENTION_UNIQUE_SENDER_MULTIPLIER = 4.0

# Not all 7TV emote mentions are equally meaningful. An emote whose own name
# signals laughing (KEKW, LUL, OMEGALUL, ...) counts for more than any other
# emote - both toward the threshold and the final score - reflecting that
# it's a stronger, more laugh-specific signal, though still weaker than the
# channel-agnostic xD/xDDDD pattern above.
EMOTE_MENTION_LAUGH_WEIGHT = 3.5
EMOTE_MENTION_OTHER_WEIGHT = 1.0

# Same idea for native Kick emotes (the [emote:id:name] tokens Kick embeds
# in message content): a genuinely laugh-related emote (emojiLol,
# collectibleswideomelaugh, KEKW, ...) counts for more than an unrelated one
# (asmonSmash, resttD, ...) that just happens to be popular. Dance/music
# emotes (beeBobble, catJAM, ...) are a third category, excluded entirely -
# see DANCE_EMOTE_NAME_PATTERNS / _emote_name_weight.
EMOTE_LAUGH_WEIGHT = 3.5
EMOTE_OTHER_WEIGHT = 1.0

# Ratios can blow up when the baseline is a tiny-but-nonzero number (e.g. one
# incidental "xd" in 5 minutes) - technically correct, not meaningfully
# informative as a score. Cap it so scores stay on a sane, comparable scale.
MAX_RATIO_SCORE = 20.0

# A pure ratio makes a 3-4 message burst off a near-silent baseline score the
# same 20/20 as a 50-message eruption, which was the "high score, nothing
# happened" complaint in review. Damp the final score by how much real volume
# was behind it, reaching full strength around this many messages. This only
# scales the reported score for ranking/display; it does not change whether a
# moment fires (that's decided purely by the ratio thresholds above).
VOLUME_DAMPEN_REFERENCE = 10.0

# Matches the exaggerated "xDDDD" laugh. Word-bounded so it doesn't match
# inside unrelated words.
LAUGH_STRONG_PATTERN = re.compile(r"\bxd{2,}\b", re.IGNORECASE)
# Matches bare "xd" as its own word (not e.g. "maxdps") - weaker signal than
# the exaggerated form above.
LAUGH_WEAK_PATTERN = re.compile(r"\bxd\b", re.IGNORECASE)

# Substrings (checked against a lowercased emote name) that mark an emote as
# laugh-related. A first pass based on well-known Twitch/7TV emotes (KEKW,
# KEKWait, LUL, LULW, OMEGALUL, LOL, pepeLaugh, monkaLaugh, ...) - expected
# to be tuned as we see which emotes actually show up in real streams.
LAUGH_EMOTE_NAME_PATTERNS = ("kek", "lul", "lol", "haha", "laugh", "joy")


def is_laugh_emote_name(emote_name: str) -> bool:
    lowered = emote_name.lower()
    return any(pattern in lowered for pattern in LAUGH_EMOTE_NAME_PATTERNS)


# Substrings marking an emote as the "dancing/vibing to music" convention
# (catJAM, GooseJAM, headBang, beeBobble, EDMusiC, ...) rather than a
# reaction to something funny. Deliberately narrower than
# LAUGH_EMOTE_NAME_PATTERNS - each pattern here was picked because it
# actually showed up spamming chat during a music segment, not as a
# speculative guess, to keep the false-negative risk (an unrelated emote
# name that happens to contain "jam") low.
DANCE_EMOTE_NAME_PATTERNS = ("danc", "bobble", "jam", "headbang", "boogie", "groov", "musi")


def is_dance_emote_name(emote_name: str) -> bool:
    lowered = emote_name.lower()
    return any(pattern in lowered for pattern in DANCE_EMOTE_NAME_PATTERNS)


def _emote_name_weight(emote_name: str) -> float:
    """Classifies a single emote name as laugh > dance (excluded) > other."""
    if is_laugh_emote_name(emote_name):
        return EMOTE_LAUGH_WEIGHT
    if is_dance_emote_name(emote_name):
        return 0.0
    return EMOTE_OTHER_WEIGHT


# Kick embeds native emotes as "[emote:12345:emojiLol]" tokens directly in
# the message content.
NATIVE_EMOTE_TOKEN_PATTERN = re.compile(r"\[emote:\d+:([^\]]+)\]")


def classify_native_emotes(content: str) -> float:
    """Returns the highest emote weight among any native Kick emotes in content, or 0.0 if none."""
    names = NATIVE_EMOTE_TOKEN_PATTERN.findall(content)
    if not names:
        return 0.0
    return max(_emote_name_weight(name) for name in names)


# Real 7TV emote sets include very short names (e.g. "lo", "re", "xd",
# "bla") that are meaningless as substrings - they match inside all sorts of
# ordinary words ("c-LO-vek", "t-RE-ba", "napad-LO") and turn emote_mention
# into a near-random trigger. Anything shorter than this is dropped instead
# of being stored as a keyword at all.
MIN_EMOTE_NAME_LENGTH = 4


def classify_emote_names(emote_names: list[str]) -> dict[str, float]:
    """Maps each 7TV emote name (lowercased, to match classify_message's lookup) to its mention weight.

    Emote names shorter than MIN_EMOTE_NAME_LENGTH, and dance/music-hype
    names (see is_dance_emote_name), are skipped entirely - typing one as
    plain text is no more a sign of comedy than posting it as a native
    emote is.
    """
    return {
        name.lower(): EMOTE_MENTION_LAUGH_WEIGHT if is_laugh_emote_name(name) else EMOTE_MENTION_OTHER_WEIGHT
        for name in emote_names
        if len(name) >= MIN_EMOTE_NAME_LENGTH and not is_dance_emote_name(name)
    }


# Chat's answer to a streamer's yes/no question is a wall of bare "1"s and
# "2"s. That's a poll, not a reaction: it says nothing about whether
# something funny happened, yet it floods the window with messages from many
# distinct senders - inflating message_rate now and, once it slides into the
# baseline, the chat-size figures the other thresholds scale from. These
# messages are ignored by the detector entirely.
VOTE_MESSAGES = frozenset({"1", "2"})


def is_vote_message(content: str) -> bool:
    return content.strip() in VOTE_MESSAGES


# each entry: (timestamp, sender, normalized_content, emote_count, emote_weight, laugh_weight, mention_weight)
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


def classify_message(content: str, channel_keyword_weights: dict[str, float] | None = None) -> tuple[float, float]:
    """Returns (laugh_weight, mention_weight) for a chat message's text.

    laugh_weight is 0.0 (no match), LAUGH_WEAK_WEIGHT (bare "xd"), or
    LAUGH_STRONG_WEIGHT ("xddd" or more d's).

    mention_weight is 0.0 if no channel keyword matched, otherwise the
    highest weight among the keywords that did (channel_keyword_weights
    maps each 7TV emote name to EMOTE_MENTION_LAUGH_WEIGHT or
    EMOTE_MENTION_OTHER_WEIGHT, set when the channel was subscribed).
    """
    if LAUGH_STRONG_PATTERN.search(content):
        laugh_weight = LAUGH_STRONG_WEIGHT
    elif LAUGH_WEAK_PATTERN.search(content):
        laugh_weight = LAUGH_WEAK_WEIGHT
    else:
        laugh_weight = 0.0

    lowered = content.lower()
    matched_weights = [weight for keyword, weight in (channel_keyword_weights or {}).items() if keyword in lowered]
    mention_weight = max(matched_weights) if matched_weights else 0.0
    return laugh_weight, mention_weight


def laugh_emote_names(emote_names: Iterable[str]) -> list[str]:
    """Those of a channel's 7TV emote names (the keys of its keyword
    weights, lowercased) that signal laughing."""
    return [name for name in emote_names if is_laugh_emote_name(name)]


def is_laughing(content: str, laugh_names: Iterable[str] = ()) -> bool:
    """Whether a chat message is someone laughing, by any of the signs the
    detector listens for: the typed laugh, a native laugh emote, or one of
    the channel's laugh emotes named in the text (`laugh_names`, see
    laugh_emote_names).

    Unlike the weights above this is a plain yes or no. It is for showing
    what chat was doing - the laughing line on the dashboard's trace - not
    for deciding whether something was a moment.
    """
    if LAUGH_WEAK_PATTERN.search(content) or LAUGH_STRONG_PATTERN.search(content):
        return True
    if any(is_laugh_emote_name(name) for name in NATIVE_EMOTE_TOKEN_PATTERN.findall(content)):
        return True
    lowered = content.lower()
    return any(name in lowered for name in laugh_names)


def _reaction_weight(entries: list, index: int) -> float:
    """Total weight of one reaction signal (entry field `index`) across
    entries, counting each sender at most once per SHORT_WINDOW_SECONDS.

    Kick doesn't rate-limit an account, so one person pasting the same emote
    or "xd" five times in a row would otherwise carry five people's worth of
    weight - which, together with two other chatters, was enough to clear the
    weight threshold and the distinct-sender floor at once. Only a sender's
    strongest message in each window-sized slice of time counts, so the
    short window and the (much longer) baseline are measured the same way.
    """
    strongest: dict[tuple[str, int], float] = {}
    for entry in entries:
        weight = entry[index]
        if weight > 0:
            key = (entry[1], int(entry[0] // SHORT_WINDOW_SECONDS))
            if weight > strongest.get(key, 0.0):
                strongest[key] = weight
    return sum(strongest.values())


def _short_reaction_weight(entries: list, index: int) -> float:
    """Same, for the short window itself: it is one slice, wherever it
    happens to fall, so each sender counts exactly once."""
    strongest: dict[str, float] = {}
    for entry in entries:
        if entry[index] > strongest.get(entry[1], 0.0):
            strongest[entry[1]] = entry[index]
    return sum(strongest.values())


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


def reaction_active(channel_slug: str, now: float | None = None, since: float | None = None) -> bool:
    """Whether the channel's short window still shows a laugh/emote reaction
    at SUSTAIN_FRACTION of the firing bar.

    `since` (same monotonic clock) limits that to messages newer than it.
    A moment session passes the instant its moment fired: the laughs that
    fired it sit in the short window for another 10 seconds, so without this
    every moment looked "still active" for a while with nobody new reacting,
    and every clip got extended. Only a reaction that keeps drawing in new
    people after the trigger counts as still going.

    Read-only: it decides when an already-open moment's reaction has died
    down, without recording anything, touching the cooldown, or opening a new
    moment. Uses the same monotonic clock as record_message, so as real time
    passes with no new messages the short window empties and this goes False.
    """
    now = now if now is not None else time.monotonic()
    entries = _entries.get(channel_slug)
    if not entries:
        return False

    short_cutoff = now - SHORT_WINDOW_SECONDS
    short_start = short_cutoff if since is None else max(short_cutoff, since)
    short = [e for e in entries if e[0] >= short_start and e[0] > (since if since is not None else -1.0)]
    if not short:
        return False
    baseline = [e for e in entries if e[0] < short_cutoff]
    baseline_seconds = max(short_cutoff - entries[0][0], 1e-9)

    # The same crowd-size floor as firing, scaled down like the ratio is:
    # without it a single straggler typing "xd" every so often was enough to
    # hold a moment open to its hard cap, which is where the routinely
    # two-minute clips came from.
    min_unique = max(
        MIN_SUSTAIN_UNIQUE,
        round(_dynamic_min_reaction_unique(len({e[1] for e in baseline})) * SUSTAIN_FRACTION),
    )

    # (entry index, absolute floor, firing multiplier) for each reaction
    # signal - emote_weight (4), laugh_weight (5), mention_weight (6).
    # message_rate is intentionally not a sustain signal: volume alone
    # shouldn't hold a moment open.
    signals = (
        (5, MIN_ABSOLUTE_LAUGHS, LAUGH_MULTIPLIER),
        (4, MIN_ABSOLUTE_LAUGHS, EMOTE_SPIKE_MULTIPLIER),
        (6, MIN_ABSOLUTE_LAUGHS, EMOTE_MENTION_MULTIPLIER),
    )
    for index, min_absolute, multiplier in signals:
        if len({e[1] for e in short if e[index] > 0}) < min_unique:
            continue
        ratio = _spike_ratio(
            _short_reaction_weight(short, index), _reaction_weight(baseline, index), baseline_seconds, min_absolute
        )
        if ratio is not None and ratio >= multiplier * SUSTAIN_FRACTION:
            return True
    return False


def record_message(
    channel_slug: str,
    *,
    sender: str,
    content: str = "",
    emote_count: int = 0,
    emote_weight: float = 0.0,
    laugh_weight: float = 0.0,
    mention_weight: float = 0.0,
    now: float | None = None,
) -> Spike | None:
    if is_vote_message(content):
        return None

    now = now if now is not None else time.monotonic()
    entries = _entries[channel_slug]
    entries.append((now, sender, content.strip().lower(), emote_count, emote_weight, laugh_weight, mention_weight))

    cutoff = now - BASELINE_WINDOW_SECONDS
    while entries and entries[0][0] < cutoff:
        entries.popleft()

    history_seconds = now - entries[0][0]
    if history_seconds < BASELINE_WINDOW_SECONDS - SHORT_WINDOW_SECONDS:
        return None  # not enough history yet to trust a baseline

    last_moment_at = _last_moment_at.get(channel_slug)
    if last_moment_at is not None and now - last_moment_at < COOLDOWN_SECONDS:
        return None

    short_cutoff = now - SHORT_WINDOW_SECONDS
    short = [e for e in entries if e[0] >= short_cutoff]
    baseline = [e for e in entries if e[0] < short_cutoff]
    baseline_seconds = history_seconds - SHORT_WINDOW_SECONDS

    short_message_count = len(short)
    short_unique_senders = len({e[1] for e in short})
    short_unique_contents = len({e[2] for e in short if e[2]})
    short_emote_count = sum(e[3] for e in short)
    short_emote_weight = _short_reaction_weight(short, 4)
    short_emote_unique_senders = len({e[1] for e in short if e[4] > 0})
    short_laugh_weight = _short_reaction_weight(short, 5)
    short_laugh_count = sum(1 for e in short if e[5] > 0)
    short_laugh_unique_senders = len({e[1] for e in short if e[5] > 0})
    short_mention_weight = _short_reaction_weight(short, 6)
    short_mention_count = sum(1 for e in short if e[6] > 0)
    short_mention_unique_senders = len({e[1] for e in short if e[6] > 0})

    baseline_message_count = len(baseline)
    baseline_unique_senders = len({e[1] for e in baseline})
    baseline_unique_contents = len({e[2] for e in baseline if e[2]})
    baseline_emote_weight = _reaction_weight(baseline, 4)
    baseline_emote_unique_senders = len({e[1] for e in baseline if e[4] > 0})
    baseline_laugh_weight = _reaction_weight(baseline, 5)
    baseline_laugh_unique_senders = len({e[1] for e in baseline if e[5] > 0})
    baseline_mention_weight = _reaction_weight(baseline, 6)
    baseline_mention_unique_senders = len({e[1] for e in baseline if e[6] > 0})

    current_message_rate = short_message_count / SHORT_WINDOW_SECONDS
    baseline_message_rate = baseline_message_count / baseline_seconds if baseline_seconds > 0 else 0.0
    dynamic_min_count = _dynamic_min_count(baseline_unique_senders)
    min_reaction_unique = _dynamic_min_reaction_unique(baseline_unique_senders)

    reasons = []
    scores = []

    # Evaluated up front but held back: message_rate is only folded in below,
    # and only if a laugh/emote signal also fired (it never stands alone).
    message_ratio = _spike_ratio(short_message_count, baseline_message_count, baseline_seconds, dynamic_min_count)
    sender_ratio = _spike_ratio(short_unique_senders, baseline_unique_senders, baseline_seconds, MIN_ABSOLUTE_UNIQUE)
    content_ratio = _spike_ratio(short_unique_contents, baseline_unique_contents, baseline_seconds, MIN_ABSOLUTE_UNIQUE)
    message_rate_fired = (
        message_ratio is not None
        and message_ratio >= MESSAGE_SPIKE_MULTIPLIER
        and sender_ratio is not None
        and sender_ratio >= MESSAGE_UNIQUE_SENDER_MULTIPLIER
        and content_ratio is not None
        and content_ratio >= MESSAGE_UNIQUE_CONTENT_MULTIPLIER
    )

    # Judged by distinct senders and by a weighted sum that favors
    # laugh-related emotes (emojiLol, KEKW, ...) over unrelated ones
    # (asmonSmash, beeBobble, ...) that just happen to be popular. One person
    # stacking several emotes in a single message, or spamming the same one
    # across several messages, only counts once in both (see
    # classify_native_emotes and _reaction_weight).
    emote_ratio = _spike_ratio(short_emote_weight, baseline_emote_weight, baseline_seconds, dynamic_min_count)
    emote_sender_ratio = _spike_ratio(
        short_emote_unique_senders, baseline_emote_unique_senders, baseline_seconds, MIN_ABSOLUTE_UNIQUE
    )
    if (
        emote_ratio is not None
        and emote_ratio >= EMOTE_SPIKE_MULTIPLIER
        and emote_sender_ratio is not None
        and emote_sender_ratio >= EMOTE_SPIKE_MULTIPLIER
        and short_emote_unique_senders >= min_reaction_unique
    ):
        reasons.append("emotes")
        scores.append(min(emote_ratio, MAX_RATIO_SCORE))

    # laugh_weight sums LAUGH_STRONG_WEIGHT/_WEAK_WEIGHT per laughing
    # sender, so "xddd"+ reaches the threshold - and score - faster than an
    # equal number of bare "xd"s would.
    laugh_ratio = _spike_ratio(short_laugh_weight, baseline_laugh_weight, baseline_seconds, MIN_ABSOLUTE_LAUGHS)
    laugh_sender_ratio = _spike_ratio(
        short_laugh_unique_senders, baseline_laugh_unique_senders, baseline_seconds, MIN_ABSOLUTE_UNIQUE
    )
    if (
        laugh_ratio is not None
        and laugh_ratio >= LAUGH_MULTIPLIER
        and laugh_sender_ratio is not None
        and laugh_sender_ratio >= LAUGH_UNIQUE_SENDER_MULTIPLIER
        and short_laugh_unique_senders >= min_reaction_unique
    ):
        reasons.append("laugh")
        scores.append(min(laugh_ratio, MAX_RATIO_SCORE))

    # mention_weight sums EMOTE_MENTION_LAUGH_WEIGHT/_OTHER_WEIGHT per
    # mentioning sender, so laugh-related emotes reach the threshold - and score -
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
        and short_mention_unique_senders >= min_reaction_unique
    ):
        reasons.append("emote_mention")
        scores.append(min(mention_ratio, MAX_RATIO_SCORE))

    # message_rate rides along only when a real laugh/emote signal already
    # fired - on its own a volume spike is almost always a false positive.
    if message_rate_fired and reasons:
        reasons.insert(0, "message_rate")
        scores.append(min(message_ratio, MAX_RATIO_SCORE) * MESSAGE_RATE_SCORE_WEIGHT)

    if not reasons:
        return None

    # Scale the reported score by how much real volume backed the spike, so a
    # 3-4 message burst can't present as the same score as a big eruption.
    volume_factor = min(1.0, short_message_count / VOLUME_DAMPEN_REFERENCE)

    _last_moment_at[channel_slug] = now
    return Spike(
        reasons=reasons,
        score=max(scores) * volume_factor,
        message_count=short_message_count,
        baseline_message_rate=baseline_message_rate,
        current_message_rate=current_message_rate,
        emote_count=short_emote_count,
        keyword_hits=short_laugh_count + short_mention_count,
    )
