"""How promising a moment is: the chance, from its chat alone, that it is
rated 4 or 5 on the review page. The review queue can be ordered by it.

Three things about a moment's chat told the rated ones apart, together
better than any of them alone:

- how many people laughed around it (`moments.laughing`, see
  detector.laughing_people) - the strongest by far;
- how many of the chat that was - the laughing against the square root of
  the people who chatted in the detector's baseline window before it
  (`moments.chatters`), as the detector's own thresholds grow. Ten people
  laughing in a chat of thirty is a bigger reaction than ten in a chat of
  three hundred, and without this a small channel's best moments sat
  under a big channel's ordinary ones;
- how long the reaction held the moment open past its detection window.

Fitted on about 1,400 rated moments (a logistic regression, rated 4-5
against the rest, checked stream by stream so no stream was judged by a
fit that had seen it): its order told a 4-5 from the rest about 70 % of
the time, the same as the laughing and length alone, but put half again as
many of the smaller channels' good moments into the top third of the
queue. What else was tried added nothing once these were known: how tight
the burst of laughing was against how spread out over its ten seconds, how
many people joined in after it fired, how long the laughing went on, the
message rate, and a fit of its own for each channel (too few rated moments
per channel; it did worse). The detector's own score told nothing.

The weights were fitted on the same moments they were checked on as a
whole, so the chance is a guide, not a promise kept. Pure: no I/O.
"""

from __future__ import annotations

import math

from . import detector

# The fitted weights (see the module docstring), on the natural logarithm of
# one more than each figure.
INTERCEPT = -4.23
LAUGHING_WEIGHT = 0.50
SHARE_WEIGHT = 0.75
HELD_WEIGHT = 0.24


def promise(laughing: int, reaction_seconds: float, chatters: int) -> float:
    """The chance, 0 to 1, that a moment is rated 4 or 5: `laughing` people
    laughed around it, its reaction lasted `reaction_seconds` (its window,
    detection included) and `chatters` people chatted in the baseline window
    before it fired."""
    laughing = max(0, laughing)
    share = laughing / math.sqrt(max(1, chatters))
    held = max(0.0, reaction_seconds - detector.SHORT_WINDOW_SECONDS)
    score = (
        INTERCEPT
        + LAUGHING_WEIGHT * math.log1p(laughing)
        + SHARE_WEIGHT * math.log1p(share)
        + HELD_WEIGHT * math.log1p(held)
    )
    return 1.0 / (1.0 + math.exp(-score))


def chatters_before(senders_and_times: list[tuple[str, float]], fired_at: float) -> int:
    """How many people chatted in the detector's baseline window before a
    moment fired at `fired_at`: from BASELINE_WINDOW_SECONDS before it up to
    the start of its short window, as the detector counts them
    (`baseline_unique_senders`). For a moment stored before the count was
    kept; `senders_and_times` are the messages the detector would have been
    given (ignored ones left out), each by its sender and arrival."""
    since = fired_at - detector.BASELINE_WINDOW_SECONDS
    until = fired_at - detector.SHORT_WINDOW_SECONDS
    return len({detector.sender_key(sender) for sender, at in senders_and_times if since <= at < until})
