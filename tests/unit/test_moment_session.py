"""The decision a moment session makes on every poll: keep the moment open,
push its end out, or close it and cut the clip.

`_session_should_close` is deliberately pure - it gets the clock and the
"is chat still reacting" answer handed in - so the whole lifetime of a moment
can be walked through here second by second without waiting for any of it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import main, tuning

TRIGGER = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
QUIET = tuning.MOMENT_SESSION_QUIET_SECONDS
MAX = tuning.MOMENT_SESSION_MAX_SECONDS


def after(seconds: float) -> datetime:
    return TRIGGER + timedelta(seconds=seconds)


@pytest.fixture
def session() -> main._MomentSession:
    return main._MomentSession(
        moment_id=1,
        channel="some_channel",
        window_start=TRIGGER - timedelta(seconds=10),
        trigger_time=TRIGGER,
        window_end=TRIGGER,
        last_active=TRIGGER,
        triggered_at=0.0,
        tuned=tuning.recommended(),
    )


def test_stays_open_right_after_the_trigger(session):
    assert main._session_should_close(session, reaction_is_active=False, now=after(1)) is False


def test_closes_once_the_reaction_has_been_quiet_long_enough(session):
    assert main._session_should_close(session, False, after(QUIET - 0.1)) is False
    assert main._session_should_close(session, False, after(QUIET)) is True


def test_a_quiet_moment_keeps_the_end_it_had_at_the_trigger(session):
    main._session_should_close(session, False, after(QUIET))

    assert session.window_end == TRIGGER


def test_an_active_reaction_pushes_the_end_out_to_now(session):
    assert main._session_should_close(session, True, after(3)) is False

    assert session.window_end == after(3)
    assert session.last_active == after(3)


def test_the_quiet_period_counts_from_the_last_time_chat_was_reacting(session):
    main._session_should_close(session, True, after(6))

    # Twelve seconds after the trigger, but only six after the last activity.
    assert main._session_should_close(session, False, after(6 + QUIET - 0.1)) is False
    assert main._session_should_close(session, False, after(6 + QUIET)) is True


def test_the_clip_ends_where_the_reaction_did_not_where_the_session_closed(session):
    main._session_should_close(session, True, after(6))
    main._session_should_close(session, False, after(6 + QUIET))

    assert session.window_end == after(6)


def test_a_reaction_that_never_stops_is_cut_off_at_the_cap(session):
    closed_at = None
    for second in range(0, int(MAX) + 30, 3):
        if main._session_should_close(session, True, after(second)):
            closed_at = second
            break

    assert closed_at is not None
    assert MAX <= closed_at < MAX + 3
    assert session.window_end == after(closed_at)


def test_the_cap_is_measured_from_the_trigger_not_from_the_last_activity(session):
    main._session_should_close(session, True, after(MAX - 1))

    assert main._session_should_close(session, False, after(MAX)) is True


def test_a_channel_with_limits_of_its_own_is_held_to_those(session):
    session.tuned = replace(session.tuned, quiet_seconds=QUIET * 2, max_open_seconds=MAX * 3)

    assert main._session_should_close(session, False, after(QUIET * 2 - 0.1)) is False
    assert main._session_should_close(session, False, after(QUIET * 2)) is True


def test_and_so_is_its_cap(session):
    session.tuned = replace(session.tuned, quiet_seconds=QUIET, max_open_seconds=MAX / 2)

    assert main._session_should_close(session, True, after(MAX / 2 - 1)) is False
    assert main._session_should_close(session, True, after(MAX / 2)) is True


def test_the_quiet_period_fits_inside_the_cap():
    # Otherwise every moment would run to the cap and "quiet" would mean nothing.
    assert 0 < tuning.MOMENT_SESSION_POLL_SECONDS < QUIET < MAX


def test_walking_through_a_typical_extended_moment(session):
    """Chat keeps reacting for nine seconds after the trigger, then stops."""
    poll = tuning.MOMENT_SESSION_POLL_SECONDS
    reacting_until = 9
    second = 0.0
    while True:
        second += poll
        if main._session_should_close(session, second <= reacting_until, after(second)):
            break

    assert session.window_end == after(reacting_until)
    assert second == pytest.approx(reacting_until + QUIET, abs=poll)
