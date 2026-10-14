"""What the Health page says, and what it calls trouble.

Everything is worked out from `health.Facts` - what the service noticed, and
the clock - so each test states the facts it is about and reads the words.
The thresholds are health.py's own constants: a test says "longer than
CHAT_SILENT_SECONDS", not "eleven minutes".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import health, tunnel
from kick_clip_hunter.dashboard_view import since_words, when_words
from kick_clip_hunter.health import Channel, Drop, Facts, SubscriptionCheck, TwitchChat

NOW = datetime(2026, 3, 1, 20, 0, 0, tzinfo=timezone.utc)
TODAY = NOW.astimezone().date()
STARTED = NOW - timedelta(hours=5)
GB = 1024**3


def ago(seconds: float = 0, **more: float) -> datetime:
    return NOW - timedelta(seconds=seconds, **more)


def facts(**known) -> Facts:
    known.setdefault("started_at", STARTED)
    return Facts(now=NOW, **known)


def live(slug: str = "channel_a", *, since: datetime, **known) -> Channel:
    return Channel(slug, now_words="Live", coming_in=True, live_since=since, **known)


LONG_LIVE = ago(health.CHAT_SILENT_SECONDS + 60)
LONG_AGO = ago(health.CHAT_SILENT_SECONDS + 30)


class TestHowLongAgo:
    @pytest.mark.parametrize(
        ("seconds", "words"),
        [
            (0, "just now"),
            (1.9, "just now"),
            (2, "2 seconds ago"),
            (59.9, "59 seconds ago"),
            (60, "1 minute ago"),
            (119, "1 minute ago"),
            (3599, "59 minutes ago"),
            (3600, "1 hour ago"),
            (2 * 3600 + 5 * 60 + 40, "2 hours 5 minutes ago"),
            (86399, "23 hours 59 minutes ago"),
        ],
    )
    def test_is_said_to_the_precision_worth_reading(self, seconds, words):
        assert health.ago_words(ago(seconds), NOW, TODAY) == words

    def test_past_a_day_it_is_when_it_was(self):
        then = ago(days=3, hours=2)

        assert health.ago_words(then, NOW, TODAY) == since_words(then.astimezone(), TODAY)

    def test_a_time_that_has_not_come_yet_is_now(self):
        # Two clocks read a moment apart.
        assert health.ago_words(NOW + timedelta(seconds=1), NOW, TODAY) == "just now"

    @pytest.mark.parametrize(
        ("seconds", "words"), [(1, "1 second"), (61, "1 minute"), (600, "10 minutes"), (7260, "2 hours 1 minute")]
    )
    def test_a_length_of_time_reads_the_same_way(self, seconds, words):
        assert health.span_words(seconds) == words


class TestWebhooks:
    def test_says_when_the_last_one_arrived(self):
        part = health.webhooks(facts(last_webhook_at=ago(4)), TODAY)

        assert part["said"] == "The last webhook arrived 4 seconds ago."
        assert part["trouble"] is False and part["more"] == []

    def test_says_so_when_none_has_arrived_since_the_service_started(self):
        part = health.webhooks(facts(started_at=ago(90)), TODAY)

        assert part["said"] == "No webhook has arrived since the service started 1 minute ago."
        assert part["trouble"] is False

    def test_silence_while_a_channel_is_live_is_trouble(self):
        part = health.webhooks(facts(last_webhook_at=LONG_AGO, channels=(live(since=LONG_LIVE),)), TODAY)

        assert part["trouble"] is True
        assert part["said"] == (
            f"Nothing has arrived from Kick for {health.span_words(health.CHAT_SILENT_SECONDS + 30)}, "
            "though 1 watched channel is live."
        )
        assert "webhook URL in the Kick app" in part["more"][0]

    def test_it_says_how_many_channels_are_live_through_it(self):
        channels = (live("channel_a", since=LONG_LIVE), live("channel_b", since=LONG_LIVE), Channel("channel_c"))

        part = health.webhooks(facts(last_webhook_at=LONG_AGO, channels=channels), TODAY)

        assert part["said"].endswith("though 2 watched channels are live.")

    def test_silence_since_the_service_started_is_said_as_that(self):
        part = health.webhooks(facts(channels=(live(since=LONG_LIVE),)), TODAY)

        assert part["trouble"] is True
        assert part["said"] == (
            "Nothing has arrived from Kick since the service started 5 hours ago, though 1 watched channel is live."
        )

    def test_silence_with_nobody_live_is_only_what_is_expected(self):
        part = health.webhooks(facts(last_webhook_at=LONG_AGO, channels=(Channel("channel_a"),)), TODAY)

        assert part["trouble"] is False
        assert part["more"] == ["No watched channel is live, so none is expected."]

    def test_a_channel_that_has_only_just_gone_live_is_given_time(self):
        channels = (live(since=ago(health.CHAT_SILENT_SECONDS - 5)),)

        part = health.webhooks(facts(last_webhook_at=LONG_AGO, channels=channels), TODAY)

        assert part["trouble"] is False and part["more"] == []

    def test_a_service_that_has_only_just_started_is_given_time_too(self):
        # The channel has been live for long, but there was nothing to hear
        # its chat with until a moment ago.
        part = health.webhooks(facts(started_at=ago(30), channels=(live(since=LONG_LIVE),)), TODAY)

        assert part["trouble"] is False

    def test_a_webhook_from_before_the_restart_does_not_count_against_it(self):
        part = health.webhooks(
            facts(started_at=ago(30), last_webhook_at=LONG_AGO, channels=(live(since=LONG_LIVE),)), TODAY
        )

        assert part["trouble"] is False

    def test_one_quiet_channel_among_talking_ones_is_said_in_its_row_and_is_no_trouble(self):
        channels = (
            live("talking", since=LONG_LIVE, last_chat_at=ago(3)),
            live("quiet", since=LONG_LIVE, last_chat_at=LONG_AGO),
        )

        part = health.webhooks(facts(last_webhook_at=ago(3), channels=channels), TODAY)

        assert part["trouble"] is False
        notes = {row["slug"]: row["note"] for row in part["rows"]}
        assert notes == {
            "talking": "",
            "quiet": f"Quiet for {health.span_words(health.CHAT_SILENT_SECONDS + 30)} while live",
        }

    def test_a_channel_that_never_said_anything_is_quiet_for_as_long_as_it_has_been_live(self):
        part = health.webhooks(facts(last_webhook_at=ago(3), channels=(live(since=LONG_LIVE),)), TODAY)

        assert part["rows"][0]["note"] == f"Quiet for {health.span_words(health.CHAT_SILENT_SECONDS + 60)} while live"

    def test_a_row_says_what_the_channel_is_doing_and_when_its_chat_last_spoke(self):
        spoke = ago(75)
        channels = (
            live("channel_a", since=ago(300), last_chat_at=spoke),
            Channel("channel_b", now_words="Not watched"),
        )

        rows = health.webhooks(facts(last_webhook_at=spoke, channels=channels), TODAY)["rows"]

        assert rows == [
            {
                "slug": "channel_a",
                "now": {"live": True, "words": "Live"},
                "chat": "1 minute ago",
                "chat_title": when_words(spoke.astimezone(), TODAY),
                "note": "",
            },
            {
                "slug": "channel_b",
                "now": {"live": False, "words": "Not watched"},
                "chat": "Nothing yet",
                "chat_title": "",
                "note": "",
            },
        ]

    @pytest.mark.parametrize(
        ("refused", "words"),
        [
            (1, "1 delivery was refused for a signature that is not Kick's, the last one 2 minutes ago."),
            (4, "4 deliveries were refused for a signature that is not Kick's, the last one 2 minutes ago."),
        ],
    )
    def test_deliveries_that_were_turned_away_are_counted(self, refused, words):
        part = health.webhooks(facts(last_webhook_at=ago(1), refused=refused, last_refused_at=ago(120)), TODAY)

        assert part["more"] == [words]
        assert part["trouble"] is False

    def test_with_watching_off_it_says_that_what_arrives_is_not_kept(self):
        part = health.webhooks(facts(last_webhook_at=ago(1), watching=False), TODAY)

        assert part["more"] == ["Watching is off, so what arrives is not kept unless its channel is being recorded."]


class TestSubscriptions:
    def check(self, **found) -> Facts:
        found.setdefault("watched", 3)
        return facts(subscriptions=SubscriptionCheck(at=ago(180), **found))

    def test_before_the_first_check_it_says_so(self):
        part = health.subscriptions(facts(), TODAY)

        assert (part["said"], part["trouble"]) == ("Not checked yet.", False)

    def test_all_present(self):
        part = health.subscriptions(self.check(), TODAY)

        assert part["said"] == "Kick has a chat subscription for all 3 watched channels. Checked 3 minutes ago."
        assert part["trouble"] is False

    def test_a_watchlist_of_one(self):
        part = health.subscriptions(self.check(watched=1), TODAY)

        assert part["said"] == "Kick has a chat subscription for the watched channel. Checked 3 minutes ago."

    def test_it_says_how_often_it_is_checked(self):
        part = health.subscriptions(facts(check_seconds=600), TODAY)

        assert part["more"] == [
            "Checked when the service starts and every 10 minutes after that. What is missing is subscribed again."
        ]

    def test_everything_dropped_at_once_is_said_as_that_and_is_mended(self):
        part = health.subscriptions(self.check(missing=("a", "b", "c")), TODAY)

        assert part["said"] == (
            "Kick had no chat subscription for any of the 3 watched channels when it was checked 3 minutes ago. "
            "All of them were subscribed again."
        )
        assert part["trouble"] is False

    def test_some_dropped_are_named(self):
        part = health.subscriptions(self.check(missing=("a", "b")), TODAY)

        assert part["said"].startswith("Kick had no chat subscription for 2 of the 3 watched channels (a and b) when")

    def test_one_dropped(self):
        part = health.subscriptions(self.check(missing=("b",)), TODAY)

        assert part["said"].endswith("It was subscribed again.")

    def test_the_only_channel_dropped(self):
        part = health.subscriptions(self.check(watched=1, missing=("a",)), TODAY)

        assert part["said"].startswith("Kick had no chat subscription for the watched channel when")

    def test_one_that_could_not_be_subscribed_again_is_trouble(self):
        part = health.subscriptions(self.check(missing=("a", "b", "c"), failed=("b",)), TODAY)

        assert part["trouble"] is True
        assert part["said"].endswith("Subscribing b again failed. The server log says why.")

    def test_kick_not_answering_is_trouble(self):
        part = health.subscriptions(self.check(error="ConnectError"), TODAY)

        assert part["trouble"] is True
        assert part["said"] == (
            "Kick could not be asked about its subscriptions 3 minutes ago (ConnectError). The server log says why."
        )

    def test_an_empty_watchlist_has_nothing_to_subscribe(self):
        part = health.subscriptions(self.check(watched=0), TODAY)

        assert part["said"] == "No channels are on the watchlist, so there is nothing to subscribe to."
        assert part["trouble"] is False

    def test_checks_that_have_stopped_coming_are_trouble_whatever_the_last_one_found(self):
        overdue = ago(health.CHECK_LATE_ROUNDS * 600)

        part = health.subscriptions(facts(subscriptions=SubscriptionCheck(at=overdue, watched=3)), TODAY)

        assert part["trouble"] is True
        assert (
            part["said"]
            == "The subscriptions were last checked 20 minutes ago, though a check is due every 10 minutes."
        )
        assert part["more"] == [
            "The loop that checks them has stopped. The server log may say why; a restart starts it again."
        ]

    def test_a_check_that_is_merely_due_is_not(self):
        due = ago(health.CHECK_LATE_ROUNDS * 600 - 1)

        assert (
            health.subscriptions(facts(subscriptions=SubscriptionCheck(at=due, watched=3)), TODAY)["trouble"] is False
        )

    def test_the_drops_that_are_remembered_are_listed_as_they_were_found(self):
        everything, some, failed = ago(days=2), ago(days=5), ago(hours=3)
        drops = (
            Drop(failed, 3, ("a", "b", "c"), ("b",)),
            Drop(everything, 3, ("a", "b", "c")),
            Drop(some, 4, ("a", "c")),
            Drop(some, 1, ("a",)),
        )

        rows = health.subscriptions(facts(drops=drops), TODAY)["drops"]

        assert rows == [
            {
                "when": when_words(failed.astimezone(), TODAY),
                "who": "All 3 channels",
                "what": "Subscribing b again failed",
            },
            {"when": when_words(everything.astimezone(), TODAY), "who": "All 3 channels", "what": "Subscribed again"},
            {
                "when": when_words(some.astimezone(), TODAY),
                "who": "2 of 4 channels: a and c",
                "what": "Subscribed again",
            },
            {"when": when_words(some.astimezone(), TODAY), "who": "The watched channel", "what": "Subscribed again"},
        ]

    def test_a_channels_name_is_never_given_a_capital(self):
        # It is spelled the way its owner spells it, and a sentence that
        # began with it would have to change that.
        part = health.subscriptions(
            facts(
                subscriptions=SubscriptionCheck(at=ago(1), watched=2, missing=("lowercase",), failed=("lowercase",)),
                drops=(Drop(ago(1), 2, ("lowercase",), ("lowercase",)),),
            ),
            TODAY,
        )

        assert "Lowercase" not in part["said"] + part["drops"][0]["who"] + part["drops"][0]["what"]


class TestRememberingDrops:
    def test_a_check_that_found_nothing_missing_is_not_worth_remembering(self):
        assert health.drop_of(SubscriptionCheck(at=NOW, watched=3)) is None
        assert health.drop_of(SubscriptionCheck(at=NOW, watched=3, error="ConnectError")) is None

    def test_one_that_did_is_remembered_with_what_it_found(self):
        check = SubscriptionCheck(at=NOW, watched=3, missing=("a", "b"), failed=("b",))

        assert health.drop_of(check) == Drop(NOW, 3, ("a", "b"), ("b",))

    def test_what_is_stored_reads_back_as_it_was(self):
        drops = (Drop(NOW, 3, ("a", "b"), ("b",)), Drop(ago(days=1), 1, ("a",)))

        assert health.read_drops([drop.stored() for drop in drops]) == drops

    def test_an_entry_that_cannot_be_read_is_left_out(self):
        good = Drop(NOW, 3, ("a",))
        stored = [
            "not a drop",
            {"at": "not a time", "watched": 3, "missing": ["a"]},
            {"watched": 3, "missing": ["a"]},
            {"at": NOW.isoformat(), "watched": "many", "missing": ["a"]},
            {"at": NOW.isoformat(), "watched": 3, "missing": 7},
            None,
            good.stored(),
        ]

        assert health.read_drops(stored) == (good,)


class TestRecorders:
    def recording(self, slug: str = "channel_a", **known) -> Channel:
        known.setdefault("recording_since", ago(3600))
        known.setdefault("footage_at", ago(3))
        return live(slug, since=ago(3600), **known)

    def test_with_nobody_live_no_stream_is_coming_in(self):
        part = health.recorders(facts(channels=(Channel("channel_a"),), round_finished_at=ago(12)), TODAY)

        assert part["said"] == "No watched channel is live, so no stream is coming in."
        assert part["more"] == ["Channels are looked at every 30 seconds; the last round finished 12 seconds ago."]
        assert part["trouble"] is False

    @pytest.mark.parametrize(("streams", "words"), [(1, "1 stream is coming in."), (2, "2 streams are coming in.")])
    def test_says_how_many_streams_are_coming_in(self, streams, words):
        channels = (*(self.recording(f"channel_{number}") for number in range(streams)), Channel("offline"))

        assert health.recorders(facts(channels=channels), TODAY)["said"] == words

    def test_an_empty_watchlist(self):
        assert health.recorders(facts(), TODAY)["said"] == "No channels are on the watchlist."

    def test_before_its_first_round_the_loop_says_so(self):
        part = health.recorders(facts(), TODAY)

        assert part["more"] == ["The loop that looks at the channels has not finished its first round yet."]
        assert part["trouble"] is False

    def test_a_loop_that_has_stopped_going_round_is_trouble(self):
        part = health.recorders(
            facts(channels=(self.recording(),), round_finished_at=ago(health.LOOP_LATE_SECONDS)), TODAY
        )

        assert part["trouble"] is True
        assert part["said"] == (
            f"The loop that looks at the channels last finished a round {health.span_words(health.LOOP_LATE_SECONDS)} "
            "ago, though it goes round every 30 seconds. Until it does again, a channel going live is not noticed."
        )
        assert part["more"] == []

    def test_a_round_that_is_merely_taking_long_is_not(self):
        part = health.recorders(facts(round_finished_at=ago(health.LOOP_LATE_SECONDS - 1)), TODAY)

        assert part["trouble"] is False

    def test_a_channel_the_loop_could_not_look_at_is_trouble(self):
        channels = (self.recording("fine"), Channel("broken", trouble="ConnectError"))

        part = health.recorders(facts(channels=channels), TODAY)

        assert part["trouble"] is True
        assert part["said"] == "Something is wrong with broken."
        assert [row["note"] for row in part["rows"]] == ["", "The last look at it failed: ConnectError"]

    def test_a_recorder_that_gets_no_footage_is_trouble(self):
        late = health.FOOTAGE_LATE_SECONDS + 20
        channels = (self.recording("stalled", footage_at=ago(late)), self.recording("fine"))

        part = health.recorders(facts(channels=channels), TODAY)

        assert part["trouble"] is True
        assert part["said"] == "Something is wrong with stalled."
        assert part["rows"][0]["note"] == f"No footage for {health.span_words(late)}"

    def test_one_that_has_never_had_any_is_counted_from_when_it_started(self):
        late = health.FOOTAGE_LATE_SECONDS + 20
        channels = (self.recording(recording_since=ago(late), footage_at=None),)

        part = health.recorders(facts(channels=channels), TODAY)

        assert part["rows"][0]["note"] == f"No footage for {health.span_words(late)}"
        assert part["rows"][0]["footage"] == "None yet"

    def test_footage_that_is_a_little_behind_is_not(self):
        channels = (self.recording(footage_at=ago(health.FOOTAGE_LATE_SECONDS - 1)),)

        assert health.recorders(facts(channels=channels), TODAY)["trouble"] is False

    def test_both_things_wrong_with_a_channel_are_said(self):
        channels = (self.recording(trouble="RecorderError", footage_at=ago(health.FOOTAGE_LATE_SECONDS)),)

        note = health.recorders(facts(channels=channels), TODAY)["rows"][0]["note"]

        late = health.span_words(health.FOOTAGE_LATE_SECONDS)
        assert note == f"The last look at it failed: RecorderError. No footage for {late}"

    def test_several_channels_in_trouble_are_all_named(self):
        channels = (Channel("a", trouble="x"), Channel("b", trouble="x"), Channel("c", trouble="x"))

        assert health.recorders(facts(channels=channels), TODAY)["said"] == "Something is wrong with a, b and c."

    def test_a_row_says_how_fresh_the_footage_is_since_when_and_whether_there_is_a_vod(self):
        since, footage = ago(2 * 3600), ago(4)
        channels = (
            self.recording("with_vod", recording_since=since, footage_at=footage, has_vod=True),
            self.recording("without_vod", recording_since=since, footage_at=footage),
            Channel("offline"),
        )

        rows = health.recorders(facts(channels=channels), TODAY)["rows"]

        assert rows[0] == {
            "slug": "with_vod",
            "now": {"live": True, "words": "Live"},
            "footage": "4 seconds ago",
            "footage_title": when_words(footage.astimezone(), TODAY),
            "since": since_words(since.astimezone(), TODAY).capitalize(),
            "vod": "Found",
            "note": "",
        }
        assert rows[1]["vod"] == "Not available"
        assert rows[2] == {
            "slug": "offline",
            "now": {"live": False, "words": "Not live"},
            "footage": "",
            "footage_title": "",
            "since": "",
            "vod": "",
            "note": "",
        }

    def test_with_watching_off_it_says_which_channels_are_still_looked_at(self):
        part = health.recorders(facts(watching=False, round_finished_at=ago(5)), TODAY)

        assert part["more"][-1] == "Watching is off, so only channels whose streams are recorded whole are looked at."


class TestDisk:
    def test_says_how_much_is_free_and_how_little_is_too_little(self):
        part = health.disk(facts(free=182 * GB, room=182 * GB, min_free=5 * GB))

        assert part["said"] == "182 GB free on the disk recordings are kept on."
        assert part["more"] == ["With less than 5 GB left no recording is started, and those that run are ended."]
        assert part["trouble"] is False

    def test_too_little_is_trouble(self):
        part = health.disk(facts(free=3 * GB, room=3 * GB, min_free=5 * GB))

        assert part["trouble"] is True
        assert part["said"] == (
            "Only 3 GB is free on the disk recordings are kept on. "
            "With less than 5 GB left no recording is started, and those that run are ended."
        )
        assert part["more"] == []

    def test_room_that_running_recordings_will_need_is_spoken_for(self):
        part = health.disk(facts(free=40 * GB, room=28 * GB, min_free=5 * GB))

        assert part["trouble"] is False
        assert part["more"][-1] == (
            "12 GB of what is free is spoken for by recordings that run or are being saved: "
            "saving one takes as much room again as it has kept."
        )

    def test_it_is_what_would_be_left_that_decides(self):
        part = health.disk(facts(free=14 * GB, room=2 * GB, min_free=5 * GB))

        assert part["trouble"] is True
        assert part["said"].startswith(
            "Only 2 GB would be left on the disk recordings are kept on once those that run are saved."
        )

    def test_recordings_that_need_more_than_there_is_leave_nothing(self):
        part = health.disk(facts(free=4 * GB, room=-2 * GB, min_free=5 * GB))

        assert part["said"].startswith("Only 0 MB would be left")

    def test_a_disk_that_could_not_be_looked_at_is_trouble(self):
        part = health.disk(facts(free=None, room=None, min_free=5 * GB, use=(("Clips", 3 * GB),)))

        assert (part["trouble"], part["said"]) == (True, "The disk recordings are kept on could not be looked at.")
        assert part["rows"] == [{"what": "Clips", "size": "3 GB"}]

    def test_what_takes_the_room_is_listed(self):
        part = health.disk(facts(use=(("Clips", 41 * GB), ("Logs", 40 * 1024**2))))

        assert part["rows"] == [{"what": "Clips", "size": "41 GB"}, {"what": "Logs", "size": "40 MB"}]


class TestTunnel:
    ADDRESS = "https://machine.tailnet.ts.net/webhooks/kick"
    TARGET = "http://127.0.0.1:8000/webhooks/kick"

    def part(self, funnel: tunnel.Funnel, **known) -> dict:
        return health.tunnel_part(facts(funnel=funnel, **known), TODAY)

    def test_before_it_was_looked_at_it_says_so(self):
        part = health.tunnel_part(facts(), TODAY)

        assert part == {"trouble": False, "said": "Not looked at yet.", "more": []}

    def test_a_funnel_that_is_up_says_where_it_leads(self):
        part = self.part(tunnel.Funnel(tunnel.UP, self.ADDRESS, self.TARGET), funnel_looked_at=ago(20))

        assert part["trouble"] is False
        assert part["said"] == f"Tailscale Funnel carries {self.ADDRESS} to this service."
        assert part["more"] == [
            f"It forwards to {self.TARGET}. The Kick app has to have that address as its webhook URL.",
            "Looked at 20 seconds ago.",
        ]

    def test_a_machine_without_tailscale_is_not_trouble(self):
        # The service may be reached some other way.
        part = self.part(tunnel.Funnel(tunnel.NOT_INSTALLED))

        assert part["trouble"] is False
        assert (
            part["said"] == "No Tailscale client was found on this machine, so there is no tunnel to look at from here."
        )

    def test_a_client_that_could_not_be_asked(self):
        part = self.part(tunnel.Funnel(tunnel.NOT_ANSWERING, said="no answer within 5 seconds"))

        assert part["trouble"] is True
        assert part["said"] == "Tailscale could not be asked about its Funnel (no answer within 5 seconds)."

    def test_tailscale_not_connected(self):
        part = self.part(tunnel.Funnel(tunnel.NOT_CONNECTED, said="Stopped"))

        assert part["trouble"] is True
        assert part["said"] == "Tailscale is not connected (it says Stopped), so Kick can't reach this service."
        assert part["more"] == []

    def test_what_nostate_means_on_windows_is_explained(self):
        part = self.part(tunnel.Funnel(tunnel.NOT_CONNECTED, said="NoState"))

        assert part["more"] == ["On Windows it says NoState for as long as its tray app is not running."]

    def test_nothing_forwarding_the_webhook(self):
        part = self.part(tunnel.Funnel(tunnel.NO_ROUTE))

        assert part["trouble"] is True
        assert part["said"] == (
            "Tailscale is connected, but nothing forwards /webhooks/kick to this service, so Kick can't reach it."
        )

    def test_served_inside_the_tailnet_only(self):
        part = self.part(tunnel.Funnel(tunnel.TAILNET_ONLY, self.ADDRESS, self.TARGET))

        assert part["trouble"] is True
        assert part["said"].startswith(f"{self.ADDRESS} is served inside the tailnet only.")

    def test_forwarding_to_another_port(self):
        part = self.part(tunnel.Funnel(tunnel.OTHER_PORT, self.ADDRESS, self.TARGET), port=8001)

        assert part["trouble"] is True
        assert part["said"] == (
            f"The Funnel forwards {self.ADDRESS} to {self.TARGET}, "
            "which is not the port this service answers on (8001)."
        )

    def test_tailscales_own_warnings_are_passed_on(self):
        funnel = tunnel.Funnel(tunnel.UP, self.ADDRESS, self.TARGET, warnings=("No DNS.", "No relay."))

        assert self.part(funnel)["more"][-1] == "Tailscale itself says: No DNS. No relay."


class TestThePageAsAWhole:
    def test_with_nothing_wrong_it_says_that_everything_looks_fine(self):
        report = health.report(facts(last_webhook_at=ago(2)), TODAY)

        assert report["troubles"] == 0
        assert report["lead"] == "Webhooks, subscriptions, the recorders, the disk and the tunnel all look fine."

    def test_it_says_how_long_the_service_has_been_running(self):
        report = health.report(facts(), TODAY)

        assert report["running"] == (
            f"The service has been running for 5 hours, since {since_words(STARTED.astimezone(), TODAY)}."
        )

    def test_what_is_trouble_is_named_in_the_order_of_the_page(self):
        report = health.report(
            facts(
                funnel=tunnel.Funnel(tunnel.NO_ROUTE),
                subscriptions=SubscriptionCheck(at=ago(5), watched=2, error="ConnectError"),
            ),
            TODAY,
        )

        assert report["troubles"] == 2
        assert report["lead"] == "Subscriptions and the tunnel need a look."

    @pytest.mark.parametrize(
        ("wrong", "lead"),
        [
            ({"funnel": tunnel.Funnel(tunnel.NO_ROUTE)}, "The tunnel needs a look."),
            ({"free": 0, "room": 0, "min_free": GB}, "The disk needs a look."),
            ({"channels": (Channel("a", trouble="x"),)}, "The recorders need a look."),
            ({"last_webhook_at": LONG_AGO, "channels": (live(since=LONG_LIVE),)}, "Webhooks need a look."),
        ],
    )
    def test_one_thing_wrong_is_said_in_the_singular_where_it_is_one_thing(self, wrong, lead):
        report = health.report(facts(**wrong), TODAY)

        assert (report["troubles"], report["lead"]) == (1, lead)

    def test_every_part_of_the_page_is_there(self):
        report = health.report(facts(), TODAY)

        assert [name for name, _, _ in health.PARTS] == [
            "webhooks",
            "subscriptions",
            "twitch",
            "recorders",
            "disk",
            "tunnel",
        ]
        for name, _, _ in health.PARTS:
            assert {"trouble", "said", "more"} <= report[name].keys()

    @pytest.mark.parametrize(("troubles", "words"), [(0, ""), (1, "1 problem"), (3, "3 problems")])
    def test_the_bar_says_how_much_is_wrong_and_nothing_while_all_is_well(self, troubles, words):
        assert health.flag_words(troubles) == words


class TestTwitchChat:
    """Where reading Twitch's chat stands: a part of the page only while the
    service is set up for Twitch or watches a Twitch channel."""

    def test_without_twitch_there_is_no_such_part(self):
        report = health.report(facts(last_webhook_at=ago(2)), TODAY)

        assert report["twitch"]["shown"] is False
        assert "Twitch" not in report["lead"]

    def test_a_twitch_channel_watched_without_twitch_set_up_is_trouble(self):
        part = health.twitch_part(facts(twitch=TwitchChat(set_up=False, channels=2)), TODAY)

        assert part["trouble"]
        assert part["said"] == (
            "2 Twitch channels are on the watchlist, but the service is not set up for Twitch: "
            "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not in .env."
        )

    def test_set_up_with_no_twitch_channel_there_is_nothing_to_read(self):
        part = health.twitch_part(facts(twitch=TwitchChat(set_up=True)), TODAY)

        assert (part["shown"], part["trouble"]) == (True, False)
        assert part["said"] == "No Twitch channels are on the watchlist."

    def test_chat_coming_in_says_whose_and_as_whom(self):
        chat = TwitchChat(
            set_up=True, channels=3, account="chat_reader", reading=3, connected_since=ago(600), last_heard_at=ago(5)
        )

        part = health.twitch_part(facts(twitch=chat), TODAY)

        assert not part["trouble"]
        assert part["said"] == "The chat of all 3 Twitch channels comes in, read as chat_reader."
        assert part["more"] == ["Connected 10 minutes ago.", "The last word from Twitch came 5 seconds ago."]

    def test_a_channel_whose_chat_twitch_refused_is_trouble_and_listed(self):
        chat = TwitchChat(
            set_up=True,
            channels=2,
            account="chat_reader",
            reading=1,
            connected_since=ago(60),
            refused=(("banned_one@twitch", "403 subscription refused"),),
        )

        part = health.twitch_part(facts(twitch=chat), TODAY)

        assert part["trouble"]
        assert part["said"] == "The chat of 1 of the 2 Twitch channels comes in, read as chat_reader."
        assert part["rows"] == [{"slug": "banned_one@twitch", "why": "403 subscription refused"}]

    def test_no_account_signed_in_is_trouble_at_once_and_says_what_to_do(self):
        chat = TwitchChat(
            set_up=True,
            channels=1,
            trouble="No Twitch account has signed in to read chat as.",
            trouble_since=ago(5),
            needs_sign_in=True,
        )

        part = health.twitch_part(facts(twitch=chat), TODAY)

        assert part["trouble"]
        assert part["said"] == "No Twitch account has signed in to read chat as. So no Twitch chat comes in."
        assert "scripts/twitch_login.py" in part["more"][0]

    def test_a_sign_in_that_lapsed_while_chat_comes_in_is_trouble_too(self):
        chat = TwitchChat(
            set_up=True,
            channels=1,
            account="chat_reader",
            reading=1,
            connected_since=ago(60),
            trouble="The account's sign-in has lapsed (Invalid refresh token).",
            needs_sign_in=True,
        )

        part = health.twitch_part(facts(twitch=chat), TODAY)

        assert part["trouble"]
        assert any("lapsed" in sentence for sentence in part["more"])

    def test_a_connection_lost_a_moment_ago_is_no_trouble_yet(self):
        chat = TwitchChat(set_up=True, channels=1, trouble="ConnectionLost", trouble_since=ago(10))

        part = health.twitch_part(facts(twitch=chat), TODAY)

        assert not part["trouble"]
        assert part["said"] == "Connecting to Twitch's chat again (ConnectionLost)."

    def test_a_connection_lost_for_long_is_trouble(self):
        chat = TwitchChat(
            set_up=True, channels=1, trouble="ConnectionLost", trouble_since=ago(health.TWITCH_LOST_SECONDS + 60)
        )

        part = health.twitch_part(facts(twitch=chat), TODAY)

        assert part["trouble"]
        assert part["said"] == "No Twitch chat has come in for 3 minutes: ConnectionLost."

    def test_before_the_first_connection_it_is_connecting(self):
        part = health.twitch_part(facts(twitch=TwitchChat(set_up=True, channels=1)), TODAY)

        assert (part["trouble"], part["said"]) == (False, "Connecting to Twitch's chat.")

    def test_it_is_named_in_the_lead_where_it_is_trouble_and_where_all_is_fine(self):
        fine = TwitchChat(set_up=True, channels=1, account="a", reading=1, connected_since=ago(5))
        wrong = TwitchChat(set_up=False, channels=1)

        assert health.report(facts(last_webhook_at=ago(2), twitch=fine), TODAY)["lead"] == (
            "Webhooks, subscriptions, Twitch chat, the recorders, the disk and the tunnel all look fine."
        )
        report = health.report(facts(last_webhook_at=ago(2), twitch=wrong), TODAY)
        assert (report["troubles"], report["lead"]) == (1, "Twitch chat needs a look.")


class TestWebhooksAreKicks:
    def test_a_live_twitch_channel_does_not_make_kicks_silence_trouble(self):
        twitch = live("twitch_channel@twitch", since=LONG_LIVE, platform="twitch")

        part = health.webhooks(facts(channels=(twitch,)), TODAY)

        assert not part["trouble"]

    def test_a_quiet_twitch_channel_is_still_said_in_its_row(self):
        twitch = live("twitch_channel@twitch", since=LONG_LIVE, platform="twitch", last_chat_at=LONG_AGO)

        (row,) = health.webhooks(facts(channels=(twitch,)), TODAY)["rows"]

        assert row["note"].startswith("Quiet for")
