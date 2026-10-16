"""What the Health page says: whether what the service depends on is there.

Five things have to hold for a moment to become a clip, and none of them
fails loudly. Webhooks have to arrive from Kick; Kick has to still have the
chat subscription of every watched channel, which it has dropped without a
word more than once; the recorders have to be bringing the live streams in;
the disk has to have room; and the tunnel Kick reaches the service through
has to be up. A chat that has gone quiet because a stream is over looks
exactly like one that has gone quiet because of any of these, and until this
page the only trace of the difference was a line in the log.

Twitch's chat comes another way, over a connection the service keeps open
(twitch_chat.py), on behalf of an account signed in for it: where that
stands is a part of its own, there for as long as the service is set up for
Twitch or watches a Twitch channel. YouTube's chat is read while a channel
is live, by looking at it every few seconds (youtube_chat.py), and has a
part of its own while a YouTube channel is watched. TikTok's chat is read
while a TikTok channel's stream is recorded (tiktok_chat.py), and has a part
of its own while a TikTok channel is watched. The bot that writes in chat
(chat_bot.py) has one while it writes in a channel's chat or an account is
signed in for it on Kick: as whom it writes, and whether it can. And the
notifications (notify.py) have one while they go anywhere: where, and
whether the last one went out.

So what the service notices as it runs is kept - in memory, by main.py and
recording_manager.py, and the subscriptions Kick dropped in the database as
well, so that a restart does not forget them - and put into words here.

Pure, like dashboard_view.py: what was noticed comes in as `Facts`, the
clock with it, and what comes out is what each part of the page says and
whether it is trouble. The count beside "Health" in the bar of every page is
made by the same judgement, from the same facts, so the two cannot disagree.

Trouble is what is worth opening the page for, and is kept rare: a single
channel whose chat is quiet is said in its row but is no trouble, because
chats do go quiet. Nothing at all arriving while a channel is live is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from . import tunnel
from .dashboard_view import (
    count_words,
    duration_words,
    list_words,
    sentence_case,
    since_words,
    size_words,
    when_words,
)

# A webhook is only expected while a channel is live and its chat is
# talking, so silence means something only once it has lasted: with a
# channel live for this long and nothing arrived from Kick in all that time,
# either the subscriptions are gone or Kick can't reach the service.
CHAT_SILENT_SECONDS = 600
# The loop that looks at the channels rests half a minute between rounds,
# and a round in which several channels have just gone live takes a few
# minutes of opening browser windows. One that has not finished for this
# long is not going round any more.
LOOP_LATE_SECONDS = 600
# A recorder whose footage stalls restarts itself within a couple of minutes
# (recorder.STALL_TIMEOUT_SECONDS, then the loop's next round). One that has
# had none for this long has restarted and is no better for it.
FOOTAGE_LATE_SECONDS = 300
# A check of the subscriptions that is this many times its interval overdue
# is not being made any more.
CHECK_LATE_ROUNDS = 2
# Twitch's chat reader connects again at once when its connection is lost;
# one that has had none for this long is not getting one.
TWITCH_LOST_SECONDS = 120
# YouTube's chat reader asks again within seconds when YouTube does not
# answer; one that has had no answer about a channel for this long is not
# getting one.
YOUTUBE_LOST_SECONDS = 120
# TikTok's chat reader tries a connection that failed again within a minute
# at first, and less often the longer it keeps failing: a chat it has not
# been able to read for this long is not going to be read by itself.
TIKTOK_LOST_SECONDS = 300


@dataclass(frozen=True)
class Channel:
    """A watched channel, as far as its health goes."""

    slug: str
    # Which platform it is on (see platforms.py).
    platform: str = "kick"
    # What the watchlist says of it right now ("Live", "Not live", ...) and
    # whether its stream is coming in.
    now_words: str = "Not live"
    coming_in: bool = False
    # Since when its platform has said it is live, as the recording loop saw it:
    # None for a channel that is not, and for one the loop does not look at.
    live_since: datetime | None = None
    # Why the loop's last look at it came to nothing, if it did.
    trouble: str | None = None
    # When the newest chat message from it arrived.
    last_chat_at: datetime | None = None
    # Its recorder, while that runs: since when, when it last saved footage
    # and whether the broadcast's own recording was found.
    recording_since: datetime | None = None
    footage_at: datetime | None = None
    has_vod: bool = False


@dataclass(frozen=True)
class TwitchChat:
    """Where reading Twitch's chat stands (see twitch_chat.ChatReader)."""

    # Whether the service is set up for Twitch, and how many Twitch channels
    # are on the watchlist.
    set_up: bool
    channels: int = 0
    # The account chat is read as, if one is signed in.
    account: str | None = None
    # How many channels' chat comes in over the connection, since when that
    # has been open (None while there is none) and when it last said
    # anything.
    reading: int = 0
    connected_since: datetime | None = None
    last_heard_at: datetime | None = None
    # What is wrong, since when, and whether it takes an account signed in.
    trouble: str | None = None
    trouble_since: datetime | None = None
    needs_sign_in: bool = False
    # The channels whose chat Twitch refused, by name, and why.
    refused: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class YouTubeChat:
    """Where reading YouTube's chat stands (see youtube_chat.ChatReader)."""

    # How many YouTube channels are on the watchlist.
    channels: int = 0
    # The channels whose chat is being read, and since when.
    reading: tuple[tuple[str, datetime], ...] = ()
    # The live channels whose chat cannot be read, and why.
    unavailable: tuple[tuple[str, str], ...] = ()
    # The channels YouTube could not be asked about: why, and since when.
    failing: tuple[tuple[str, str, datetime], ...] = ()
    # When YouTube last answered.
    last_heard_at: datetime | None = None


@dataclass(frozen=True)
class BotChannel:
    """A channel the bot writes in the chat of (see chat_bot.ChatBot)."""

    slug: str
    platform: str = "kick"
    # When the last message went out there, and why the last one did not,
    # if it did not - until the next goes out.
    sent_at: datetime | None = None
    trouble: str | None = None


@dataclass(frozen=True)
class BotAccount:
    """The account the bot writes as on a platform."""

    platform: str
    # Whether the service is set up for the platform at all (Twitch's
    # application, see config.py).
    set_up: bool = True
    # Who it writes as, if an account has signed in, and whether that let
    # it write in chat - None where that is not known.
    login: str | None = None
    may_write: bool | None = None
    # Why an account has to sign in (again) before it can write, if one has.
    sign_in: str | None = None


@dataclass(frozen=True)
class ChatBot:
    """Where the bot in chat stands: the channels it writes in the chat of,
    the accounts it writes as, where its Kick account signs in, and where
    Kick sends the browser back to then - the Kick app's redirect URL."""

    channels: tuple[BotChannel, ...] = ()
    accounts: tuple[BotAccount, ...] = ()
    sign_in_url: str = ""
    redirect_uri: str = ""

    def account(self, platform: str) -> BotAccount | None:
        return next((account for account in self.accounts if account.platform == platform), None)


@dataclass(frozen=True)
class NoteDestination:
    """Where notifications go (see notify.Notifier): what it is called, where
    in it they go, when the last one went out there, and why the last one
    did not, if it did not."""

    name: str
    where: str = ""
    sent_at: datetime | None = None
    trouble: str | None = None


@dataclass(frozen=True)
class Notifications:
    """Where notifications go, what they tell of - as the Channels page's
    switches have it - and where their links lead ("" for nowhere)."""

    destinations: tuple[NoteDestination, ...] = ()
    clips: bool = True
    clip_videos: bool = False
    recordings: bool = True
    problems: bool = True
    dashboard_url: str = ""


@dataclass(frozen=True)
class TikTokChat:
    """Where reading TikTok's chat stands (see tiktok_chat.ChatReader)."""

    # How many TikTok channels are on the watchlist, and how many of them
    # are being recorded - whose chat is wanted.
    channels: int = 0
    recorded: int = 0
    # The channels whose chat is being read, and since when.
    reading: tuple[tuple[str, datetime], ...] = ()
    # The recorded channels that are not live yet.
    waiting: tuple[str, ...] = ()
    # The channels whose chat could not be read: why, and since when.
    failing: tuple[tuple[str, str, datetime], ...] = ()
    # When the last word of TikTok's came.
    last_heard_at: datetime | None = None


@dataclass(frozen=True)
class SubscriptionCheck:
    """What a check of Kick's chat subscriptions found."""

    at: datetime
    # How many channels were on the watchlist.
    watched: int
    # The ones Kick had no subscription for, and those of them that could
    # not be subscribed again.
    missing: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    # Why Kick could not be asked at all, if it could not.
    error: str | None = None


@dataclass(frozen=True)
class Drop:
    """A check that found subscriptions missing: what is remembered of it."""

    at: datetime
    watched: int
    missing: tuple[str, ...]
    failed: tuple[str, ...] = ()

    def stored(self) -> dict[str, Any]:
        """As it is kept in the database (see db.add_subscription_drop)."""
        return {
            "at": self.at.isoformat(),
            "watched": self.watched,
            "missing": list(self.missing),
            "failed": list(self.failed),
        }


def drop_of(check: SubscriptionCheck) -> Drop | None:
    """What is worth remembering of a check: nothing, unless it found a
    subscription missing."""
    if not check.missing:
        return None
    return Drop(check.at, check.watched, check.missing, check.failed)


def read_drops(stored: list) -> tuple[Drop, ...]:
    """The drops kept in the database, as they were stored. One that can't
    be read is left out rather than taking the page down with it."""
    drops = []
    for entry in stored:
        try:
            drops.append(
                Drop(
                    datetime.fromisoformat(entry["at"]),
                    int(entry["watched"]),
                    tuple(str(slug) for slug in entry["missing"]),
                    tuple(str(slug) for slug in entry.get("failed") or ()),
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return tuple(drops)


@dataclass(frozen=True)
class Rename:
    """A watched channel that renamed itself on its platform, and which the
    watchlist followed to its new name."""

    at: datetime
    old: str
    new: str

    def stored(self) -> dict[str, Any]:
        """As it is kept in the database (see db.add_channel_rename)."""
        return {"at": self.at.isoformat(), "old": self.old, "new": self.new}


def read_renames(stored: list) -> tuple[Rename, ...]:
    """The renames kept in the database, as they were stored. One that can't
    be read is left out."""
    renames = []
    for entry in stored:
        try:
            renames.append(Rename(datetime.fromisoformat(entry["at"]), str(entry["old"]), str(entry["new"])))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return tuple(renames)


@dataclass(frozen=True)
class Facts:
    """Everything the page is made from, as it stood at `now`."""

    now: datetime
    started_at: datetime
    channels: tuple[Channel, ...] = ()
    watching: bool = True
    # Webhooks, since the service started: when the last one arrived, and
    # how many were turned away for a signature that was not Kick's.
    last_webhook_at: datetime | None = None
    refused: int = 0
    last_refused_at: datetime | None = None
    # The last check of the subscriptions, how often one is made, and the
    # checks that found some missing, newest first.
    subscriptions: SubscriptionCheck | None = None
    check_seconds: float = 600.0
    drops: tuple[Drop, ...] = ()
    # When the recording loop last got through its channels, and how long it
    # rests between rounds.
    round_finished_at: datetime | None = None
    round_seconds: float = 30.0
    # The channels that renamed themselves and were followed, newest first.
    renames: tuple[Rename, ...] = ()
    # The disk recordings are kept on: what is free, what would be once the
    # recordings that run were saved (both None if it could not be looked
    # at), and how little that may be. `use` is what takes the room, as
    # (what, bytes).
    free: int | None = 0
    room: int | None = 0
    min_free: int = 0
    use: tuple[tuple[str, int], ...] = ()
    # The tunnel as it was last looked at, when that was, and the port this
    # service answers on where that is known.
    funnel: tunnel.Funnel | None = None
    funnel_looked_at: datetime | None = None
    port: int | None = None
    # Twitch's chat, while the service is set up for Twitch or watches a
    # Twitch channel; None otherwise.
    twitch: TwitchChat | None = None
    # YouTube's chat, while a YouTube channel is watched; None otherwise.
    youtube: YouTubeChat | None = None
    # TikTok's chat, while a TikTok channel is watched; None otherwise.
    tiktok: TikTokChat | None = None
    # The bot in chat.
    bot: ChatBot | None = None
    # Notifications, while they go anywhere; None otherwise.
    notifications: Notifications | None = None


# -- words ------------------------------------------------------------------


def span_words(seconds: float) -> str:
    """A length of time to the precision worth reading on a page that says
    it again every few seconds: seconds within a minute, minutes within an
    hour, hours and minutes past it."""
    if seconds < 60:
        return count_words(int(seconds), "second")
    if seconds < 3600:
        return count_words(int(seconds // 60), "minute")
    return duration_words(seconds)


def ago_words(then: datetime, now: datetime, today: date) -> str:
    """How long ago something was - or, once that is more than a day, when."""
    seconds = max(0.0, (now - then).total_seconds())
    if seconds < 2:
        return "just now"
    if seconds < 86400:
        return f"{span_words(seconds)} ago"
    return since_words(then.astimezone(), today)


def flag_words(troubles: int) -> str:
    """What stands beside "Health" in the bar: nothing while all is well."""
    return count_words(troubles, "problem") if troubles else ""


def _seconds(now: datetime, then: datetime) -> float:
    return (now - then).total_seconds()


def _title(then: datetime | None, today: date) -> str:
    """The time itself, for whoever points at how long ago it was."""
    return when_words(then.astimezone(), today) if then else ""


# -- webhooks -----------------------------------------------------------------


def webhooks(facts: Facts, today: date) -> dict[str, Any]:
    now = facts.now
    heard = facts.last_webhook_at
    # Silence is counted from when there was a service to hear anything.
    quiet = _seconds(now, max(heard, facts.started_at) if heard else facts.started_at)
    # Webhooks are Kick's: only its channels' silence says something of them.
    kick = [channel for channel in facts.channels if channel.platform == "kick"]
    long_live = [
        channel for channel in kick if channel.live_since and _seconds(now, channel.live_since) >= CHAT_SILENT_SECONDS
    ]
    trouble = bool(long_live) and quiet >= CHAT_SILENT_SECONDS

    since_start = f"since the service started {ago_words(facts.started_at, now, today)}"
    more = []
    if trouble:
        how_long = since_start if heard is None else f"for {span_words(quiet)}"
        are = "is" if len(long_live) == 1 else "are"
        said = (
            f"Nothing has arrived from Kick {how_long}, "
            f"though {count_words(len(long_live), 'watched channel')} {are} live."
        )
        more.append(
            "Kick has dropped the chat subscriptions, can't reach this service or no longer has its address. "
            "Subscriptions and Tunnel below tell the first two; the third is the webhook URL in the Kick app."
        )
    elif heard is None:
        said = f"No webhook has arrived {since_start}."
    else:
        said = f"The last webhook arrived {ago_words(heard, now, today)}."
    if not trouble and quiet >= CHAT_SILENT_SECONDS and not any(channel.live_since for channel in kick):
        more.append("No watched channel is live, so none is expected.")
    if facts.refused:
        were = "delivery was" if facts.refused == 1 else "deliveries were"
        last = f", the last one {ago_words(facts.last_refused_at, now, today)}" if facts.last_refused_at else ""
        more.append(f"{facts.refused} {were} refused for a signature that is not Kick's{last}.")
    if not facts.watching:
        more.append("Watching is off, so what arrives is not kept unless its channel is being recorded.")

    rows = []
    for channel in facts.channels:
        last = channel.last_chat_at
        note = ""
        if channel.live_since and _seconds(now, channel.live_since) >= CHAT_SILENT_SECONDS:
            silent = _seconds(now, max(last, channel.live_since) if last else channel.live_since)
            if silent >= CHAT_SILENT_SECONDS:
                note = f"Quiet for {span_words(silent)} while live"
        rows.append(
            {
                "slug": channel.slug,
                "now": {"live": channel.coming_in, "words": channel.now_words},
                "chat": sentence_case(ago_words(last, now, today)) if last else "Nothing yet",
                "chat_title": _title(last, today),
                "note": note,
            }
        )
    return {"trouble": trouble, "said": said, "more": more, "rows": rows}


# -- subscriptions ------------------------------------------------------------


def _missing_words(missing: tuple[str, ...], watched: int) -> str:
    """Which channels a check found without a subscription. All of them at
    once is what Kick dropping everything looks like, so it is said so."""
    if len(missing) >= watched:
        return "the watched channel" if watched == 1 else f"any of the {watched} watched channels"
    return f"{len(missing)} of the {watched} watched channels ({list_words(list(missing))})"


def _again_words(missing: tuple[str, ...], failed: tuple[str, ...]) -> str:
    # A channel's name is never what a sentence begins with: it is spelled
    # as its owner spells it, capitals or none.
    if failed:
        return f"Subscribing {list_words(list(failed))} again failed. The server log says why."
    return "It was subscribed again." if len(missing) == 1 else "All of them were subscribed again."


def subscriptions(facts: Facts, today: date) -> dict[str, Any]:
    now = facts.now
    check = facts.subscriptions
    every = span_words(facts.check_seconds)
    more = [f"Checked when the service starts and every {every} after that. What is missing is subscribed again."]
    trouble = False
    if check is None:
        said = "Not checked yet."
    else:
        when = ago_words(check.at, now, today)
        if check.error:
            trouble = True
            said = f"Kick could not be asked about its subscriptions {when} ({check.error}). The server log says why."
        elif check.watched == 0:
            said = "No channels are on the watchlist, so there is nothing to subscribe to."
        elif check.missing:
            trouble = bool(check.failed)
            said = (
                f"Kick had no chat subscription for {_missing_words(check.missing, check.watched)} "
                f"when it was checked {when}. {_again_words(check.missing, check.failed)}"
            )
        else:
            whom = "the watched channel" if check.watched == 1 else f"all {check.watched} watched channels"
            said = f"Kick has a chat subscription for {whom}. Checked {when}."
        if _seconds(now, check.at) >= CHECK_LATE_ROUNDS * facts.check_seconds:
            # Whatever the last check found, it is not what holds now.
            trouble = True
            said = f"The subscriptions were last checked {when}, though a check is due every {every}."
            more = ["The loop that checks them has stopped. The server log may say why; a restart starts it again."]

    drops = []
    for drop in facts.drops:
        everyone = len(drop.missing) >= drop.watched
        if everyone:
            who = "The watched channel" if drop.watched == 1 else f"All {drop.watched} channels"
        else:
            who = f"{len(drop.missing)} of {drop.watched} channels: {list_words(list(drop.missing))}"
        done = f"Subscribing {list_words(list(drop.failed))} again failed" if drop.failed else "Subscribed again"
        drops.append({"when": when_words(drop.at.astimezone(), today), "who": who, "what": done})
    return {"trouble": trouble, "said": said, "more": more, "drops": drops}


# -- Twitch's chat -------------------------------------------------------------


def twitch_part(facts: Facts, today: date) -> dict[str, Any]:
    """Where reading Twitch's chat stands. Shown only while the service is
    set up for Twitch or watches a Twitch channel."""
    chat = facts.twitch
    nothing: dict[str, Any] = {"shown": False, "trouble": False, "said": "", "more": [], "rows": []}
    if chat is None:
        return nothing
    now = facts.now
    rows = [{"slug": slug, "why": why} for slug, why in chat.refused]
    more: list[str] = []
    are = "is" if chat.channels == 1 else "are"
    if not chat.set_up:
        said = (
            f"{count_words(chat.channels, 'Twitch channel')} {are} on the watchlist, but the service is not set up "
            "for Twitch: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not in .env."
        )
        return {**nothing, "shown": True, "trouble": True, "said": said}
    if chat.channels == 0:
        return {**nothing, "shown": True, "said": "No Twitch channels are on the watchlist."}

    trouble = bool(chat.refused)
    if chat.connected_since is not None:
        whose = "the Twitch channel" if chat.channels == 1 else f"all {chat.channels} Twitch channels"
        if chat.reading < chat.channels:
            whose = f"{chat.reading} of the {chat.channels} Twitch channels"
        said = f"The chat of {whose} comes in, read as {chat.account}."
        more.append(f"Connected {ago_words(chat.connected_since, now, today)}.")
        if chat.last_heard_at is not None:
            more.append(f"The last word from Twitch came {ago_words(chat.last_heard_at, now, today)}.")
        if chat.needs_sign_in:
            trouble = True
            more.append(
                f"{chat.trouble} Channels added now are not read, and the next connection will need an account "
                "signed in with scripts/twitch_login.py."
            )
    elif chat.needs_sign_in:
        trouble = True
        said = f"{chat.trouble} So no Twitch chat comes in."
        more.append("Sign one in with scripts/twitch_login.py; the service picks it up within a minute.")
    elif chat.trouble is not None:
        lost_for = _seconds(now, chat.trouble_since) if chat.trouble_since else 0.0
        if lost_for >= TWITCH_LOST_SECONDS:
            trouble = True
            said = f"No Twitch chat has come in for {span_words(lost_for)}: {chat.trouble}."
            more.append("The service keeps connecting again. The server log says more.")
        else:
            said = f"Connecting to Twitch's chat again ({chat.trouble})."
    else:
        said = "Connecting to Twitch's chat."
    if chat.refused:
        more.append("Twitch refused the chat of the channels below; it is asked for again every ten minutes.")
    return {"shown": True, "trouble": trouble, "said": said, "more": more, "rows": rows}


# -- YouTube's chat ------------------------------------------------------------


def youtube_part(facts: Facts, today: date) -> dict[str, Any]:
    """Where reading YouTube's chat stands. Shown only while a YouTube
    channel is on the watchlist. A chat its streamer turned off is said, but
    is no trouble; YouTube not answering for long is."""
    chat = facts.youtube
    if chat is None or chat.channels == 0:
        return {"shown": False, "trouble": False, "said": "", "more": [], "rows": []}
    now = facts.now
    lost = [(slug, why, since) for slug, why, since in chat.failing if _seconds(now, since) >= YOUTUBE_LOST_SECONDS]
    rows = [{"slug": slug, "why": why} for slug, why in chat.unavailable]
    for slug, why, since in lost:
        rows.append({"slug": slug, "why": f"No answer from YouTube for {span_words(_seconds(now, since))}: {why}"})
    more: list[str] = []
    reading = len(chat.reading)
    if lost:
        _, why, since = min(lost, key=lambda failing: failing[2])
        names = list_words([slug for slug, _, _ in lost])
        said = f"YouTube has not answered about {names} for {span_words(_seconds(now, since))}: {why}."
        more.append("The service keeps asking. The server log says more.")
    elif reading:
        if reading == chat.channels:
            whose = "the YouTube channel" if reading == 1 else f"all {reading} YouTube channels"
        else:
            whose = f"{reading} of the {chat.channels} YouTube channels"
            if not (chat.unavailable or chat.failing):
                more.append("The others are not live.")
        said = f"The chat of {whose} comes in."
    elif chat.failing:
        slug, why, _ = chat.failing[0]
        said = f"Asking YouTube about {slug} again ({why})."
    elif chat.unavailable:
        said = "No YouTube chat comes in: the chat of the live channels below cannot be read."
    elif chat.channels == 1:
        said = "The YouTube channel is not live, so no YouTube chat comes in."
    else:
        said = f"None of the {chat.channels} YouTube channels is live, so no YouTube chat comes in."
    if chat.unavailable:
        more.append("YouTube says the chat below cannot be read - it is tried again every five minutes.")
    if chat.last_heard_at is not None:
        more.append(f"The last answer from YouTube came {ago_words(chat.last_heard_at, now, today)}.")
    return {"shown": True, "trouble": bool(lost), "said": said, "more": more, "rows": rows}


# -- TikTok's chat -------------------------------------------------------------


def tiktok_part(facts: Facts, today: date) -> dict[str, Any]:
    """Where reading TikTok's chat stands. Shown only while a TikTok channel
    is on the watchlist; its chat is read only while its stream is being
    recorded. A chat that has not been read for long, while its stream is
    recorded and live, is trouble."""
    chat = facts.tiktok
    if chat is None or chat.channels == 0:
        return {"shown": False, "trouble": False, "said": "", "more": [], "rows": []}
    now = facts.now
    lost = [(slug, why, since) for slug, why, since in chat.failing if _seconds(now, since) >= TIKTOK_LOST_SECONDS]
    rows = [
        {"slug": slug, "why": f"Not read for {span_words(_seconds(now, since))}: {why}"} for slug, why, since in lost
    ]
    more: list[str] = []
    reading = len(chat.reading)
    if lost:
        _, why, since = min(lost, key=lambda failing: failing[2])
        names = list_words([slug for slug, _, _ in lost])
        said = f"The chat of {names} has not been read for {span_words(_seconds(now, since))}: {why}."
        more.append("The service keeps trying, less often the longer it fails. The server log says more.")
    elif chat.recorded == 0:
        said = (
            "The TikTok channel is not being recorded, so its chat is not read."
            if chat.channels == 1
            else "No TikTok channel is being recorded, so no TikTok chat is read."
        )
        more.append("A TikTok channel's chat is read while its stream is recorded, and kept with the recording.")
    elif reading:
        if reading == chat.recorded:
            whose = "the recorded TikTok channel" if reading == 1 else f"all {reading} recorded TikTok channels"
        else:
            whose = f"{reading} of the {chat.recorded} recorded TikTok channels"
        said = f"The chat of {whose} comes in."
    elif chat.failing:
        slug, why, _ = chat.failing[0]
        said = f"Connecting to the chat of {slug} again ({why})."
    elif chat.waiting:
        said = "No TikTok chat comes in: the recorded channels are not live yet."
    else:
        said = "Connecting to TikTok's chat."
    if chat.waiting and (reading or lost or chat.failing):
        more.append(f"Recorded but not live yet: {list_words(list(chat.waiting))}.")
    if chat.recorded:
        more.append("It is read through Euler Stream, which signs the requests TikTok asks for.")
    if chat.last_heard_at is not None:
        more.append(f"The last word from TikTok came {ago_words(chat.last_heard_at, now, today)}.")
    return {"shown": True, "trouble": bool(lost), "said": said, "more": more, "rows": rows}


# -- the bot in chat -----------------------------------------------------------

# How each platform is called in a sentence, and how its account signs in.
PLATFORM_WORDS = {"kick": "Kick", "twitch": "Twitch", "youtube": "YouTube", "tiktok": "TikTok"}
TWITCH_SIGN_IN_WORDS = "scripts/twitch_login.py signs one in"
YOUTUBE_WORDS = (
    "The bot cannot write in YouTube's chat - the service reads it as a visitor who is not signed in - so "
    "there its commands are carried out without an answer."
)
TIKTOK_WORDS = (
    "The bot cannot write in TikTok's chat - the service only reads it, as nobody, while the stream is "
    "recorded - so there its commands are carried out without an answer."
)
# The platforms whose chat the bot never writes in, and what is said of each.
NOT_WRITTEN = {"youtube": YOUTUBE_WORDS, "tiktok": TIKTOK_WORDS}


def bot_account_words(account: BotAccount | None, platform: str) -> str:
    """As whom the bot writes on a platform, or why it cannot - in a
    sentence, for the Health page and a channel's Settings."""
    name = PLATFORM_WORDS.get(platform, platform)
    if platform in NOT_WRITTEN:
        return NOT_WRITTEN[platform]
    if account is None or not account.set_up:
        return f"The service is not set up for {name}, so the bot cannot write there."
    if account.sign_in:
        return account.sign_in
    if account.login is None:
        if platform == "twitch":
            return f"No Twitch account has signed in for the bot to write as: {TWITCH_SIGN_IN_WORDS}."
        return f"No {name} account has signed in for the bot to write as."
    if account.may_write is False:
        again = "Sign it in again with scripts/twitch_login.py." if platform == "twitch" else "Sign it in again."
        return f"{account.login} did not let the bot write in {name}'s chat when it signed in. {again}"
    if platform == "twitch":
        return f"On Twitch the bot writes as {account.login}, the account chat is read as."
    return f"On {name} the bot writes as {account.login}."


def _cannot_write(channel: BotChannel, bot: ChatBot) -> str | None:
    """Why the bot cannot write in a Kick or Twitch channel's chat, where it
    cannot: no account to write as, or the last message did not go out."""
    account = bot.account(channel.platform)
    if account is None or not account.set_up or account.sign_in or not account.login or account.may_write is False:
        return bot_account_words(account, channel.platform)
    return channel.trouble


def bot_part(facts: Facts, today: date) -> dict[str, Any]:
    """Where the bot in chat stands. Shown while it writes in a channel's
    chat, an account has signed in for it on Kick, or a Kick channel is
    watched - where its account signs in, before any channel has it writing.
    A channel it cannot write in is trouble: whoever runs the channel is
    waiting for answers."""
    bot = facts.bot
    nothing: dict[str, Any] = {"shown": False, "trouble": False, "said": "", "more": [], "rows": [], "sign_in": None}
    if bot is None:
        return nothing
    kick = bot.account("kick")
    watched_on_kick = any(channel.platform == "kick" for channel in facts.channels)
    if not bot.channels and not (kick and kick.login) and not watched_on_kick:
        return nothing
    now = facts.now
    on = {channel.platform for channel in bot.channels}
    rows = []
    writing = []
    failing = []
    for channel in bot.channels:
        if channel.platform in NOT_WRITTEN:
            # Never written in, and said once below - no trouble.
            note = f"Cannot write in {PLATFORM_WORDS[channel.platform]}'s chat"
        else:
            writing.append(channel.slug)
            note = _cannot_write(channel, bot) or ""
            if note:
                failing.append(channel.slug)
        last = channel.sent_at
        rows.append(
            {
                "slug": channel.slug,
                "platform": PLATFORM_WORDS.get(channel.platform, channel.platform),
                "sent": sentence_case(ago_words(last, now, today)) if last else "Nothing yet",
                "sent_title": _title(last, today),
                "note": note,
            }
        )
    if failing:
        said = f"The bot cannot write in the chat of {list_words(failing)}."
    elif writing:
        said = f"The bot writes in the chat of {count_words(len(writing), 'channel')}."
    elif bot.channels:
        cannot = [f"{PLATFORM_WORDS[platform]}'s" for platform in NOT_WRITTEN if platform in on]
        said = f"The bot writes in no channel's chat: it cannot write in {list_words(cannot)}."
    else:
        said = "The bot writes in no channel's chat. A channel's settings switch it on."
    # Kick's account always, Twitch's where it can write or is meant to,
    # YouTube and TikTok where it is meant to and cannot.
    named = ["kick"]
    twitch = bot.account("twitch")
    if (twitch is not None and twitch.set_up) or "twitch" in on:
        named.append("twitch")
    named += [platform for platform in NOT_WRITTEN if platform in on]
    more = [bot_account_words(bot.account(platform), platform) for platform in named]
    if kick is None or kick.login is None or kick.sign_in or kick.may_write is False:
        more.append(
            f"The Kick app has to have {bot.redirect_uri} as its redirect URL, and the scopes chat:write and "
            "user:read, for an account to sign in."
        )
    sign_in = {"url": bot.sign_in_url, "words": "Sign in another account" if kick and kick.login else "Sign in on Kick"}
    return {"shown": True, "trouble": bool(failing), "said": said, "more": more, "rows": rows, "sign_in": sign_in}


# -- notifications ------------------------------------------------------------


def _telling_words(notes: Notifications) -> str:
    told = []
    if notes.clips:
        told.append("clips, with the clip itself where it fits" if notes.clip_videos else "clips")
    if notes.recordings:
        told.append("recordings of whole streams")
    if notes.problems:
        told.append("what on this page needs a look")
    if not told:
        return "They tell of nothing: the Channels page has every kind switched off."
    return f"They tell of {list_words(told)}. The Channels page switches each kind on and off."


def notifications_part(facts: Facts, today: date) -> dict[str, Any]:
    """Where notifications go, and whether the last one went out everywhere.
    Shown while they go anywhere. A destination that turned the last one
    down is trouble: what it was told of, nobody heard."""
    notes = facts.notifications
    if notes is None or not notes.destinations:
        return {"shown": False, "trouble": False, "said": "", "more": [], "rows": []}
    failing = [destination.name for destination in notes.destinations if destination.trouble]
    if failing:
        said = f"The last notification could not be sent to {list_words(failing)}."
    else:
        said = f"Notifications go to {list_words([destination.name for destination in notes.destinations])}."
    more = [_telling_words(notes)]
    if notes.dashboard_url:
        more.append(f"Their links lead to the dashboard at {notes.dashboard_url}.")
    else:
        more.append("They have no links to the dashboard: DASHBOARD_URL in .env says where it is reached from.")
    rows = []
    for destination in notes.destinations:
        last = destination.sent_at
        rows.append(
            {
                "name": destination.name,
                "where": destination.where,
                "sent": sentence_case(ago_words(last, facts.now, today)) if last else "Nothing yet",
                "sent_title": _title(last, today),
                "note": destination.trouble or "",
            }
        )
    return {"shown": True, "trouble": bool(failing), "said": said, "more": more, "rows": rows}


# -- recorders ----------------------------------------------------------------


def _footage_late(channel: Channel, now: datetime) -> float | None:
    """How long a running recorder has gone without footage, where that is
    long enough to say so."""
    if channel.recording_since is None:
        return None
    late = _seconds(now, channel.footage_at or channel.recording_since)
    return late if late >= FOOTAGE_LATE_SECONDS else None


def recorders(facts: Facts, today: date) -> dict[str, Any]:
    now = facts.now
    rows = []
    troubled = []
    for channel in facts.channels:
        notes = []
        if channel.trouble:
            notes.append(f"The last look at it failed: {channel.trouble}")
        late = _footage_late(channel, now)
        if late is not None:
            notes.append(f"No footage for {span_words(late)}")
        if notes:
            troubled.append(channel.slug)
        recording = channel.recording_since is not None
        footage = ""
        if recording:
            footage = (
                sentence_case(ago_words(channel.footage_at, now, today)) if channel.footage_at else "None yet"
            )
        rows.append(
            {
                "slug": channel.slug,
                "now": {"live": channel.coming_in, "words": channel.now_words},
                "footage": footage,
                "footage_title": _title(channel.footage_at, today) if recording else "",
                "since": sentence_case(since_words(channel.recording_since.astimezone(), today)) if recording else "",
                "vod": ("Found" if channel.has_vod else "Not available") if recording else "",
                "note": ". ".join(notes),
            }
        )

    finished = facts.round_finished_at
    every = span_words(facts.round_seconds)
    loop_late = finished is not None and _seconds(now, finished) >= LOOP_LATE_SECONDS
    coming_in = sum(1 for channel in facts.channels if channel.coming_in)
    more = []
    if loop_late:
        said = (
            f"The loop that looks at the channels last finished a round {ago_words(finished, now, today)}, "
            f"though it goes round every {every}. Until it does again, a channel going live is not noticed."
        )
    else:
        if troubled:
            said = f"Something is wrong with {list_words(troubled)}."
        elif not facts.channels:
            said = "No channels are on the watchlist."
        elif coming_in == 0:
            said = "No watched channel is live, so no stream is coming in."
        else:
            said = f"{count_words(coming_in, 'stream')} {'is' if coming_in == 1 else 'are'} coming in."
        if finished is None:
            more.append("The loop that looks at the channels has not finished its first round yet.")
        else:
            more.append(
                f"Channels are looked at every {every}; the last round finished {ago_words(finished, now, today)}."
            )
    if not facts.watching:
        more.append("Watching is off, so only channels whose streams are recorded whole are looked at.")
    # Followed, so no trouble: listed because the watchlist's name changed
    # without anyone changing it, and what was kept under the old name
    # (past moments, recordings) stays under that.
    renames = [
        {"when": when_words(rename.at.astimezone(), today), "old": rename.old, "new": rename.new}
        for rename in facts.renames
    ]
    return {"trouble": loop_late or bool(troubled), "said": said, "more": more, "rows": rows, "renames": renames}


# -- the disk -----------------------------------------------------------------


def disk(facts: Facts) -> dict[str, Any]:
    rows = [{"what": what, "size": size_words(size)} for what, size in facts.use]
    if facts.free is None or facts.room is None:
        said = "The disk recordings are kept on could not be looked at."
        return {"trouble": True, "said": said, "more": [], "rows": rows}
    spoken_for = max(facts.free - facts.room, 0)
    trouble = facts.room < facts.min_free
    limit = (
        f"With less than {size_words(facts.min_free)} left no recording is started, and those that run are ended."
    )
    more = []
    if trouble and spoken_for:
        said = (
            f"Only {size_words(max(facts.room, 0))} would be left on the disk recordings are kept on "
            f"once those that run are saved. {limit}"
        )
    elif trouble:
        said = f"Only {size_words(facts.free)} is free on the disk recordings are kept on. {limit}"
    else:
        said = f"{size_words(facts.free)} free on the disk recordings are kept on."
        more.append(limit)
    if spoken_for:
        more.append(
            f"{size_words(spoken_for)} of what is free is spoken for by recordings that run or are being saved: "
            "saving one takes as much room again as it has kept."
        )
    return {"trouble": trouble, "said": said, "more": more, "rows": rows}


# -- the tunnel ---------------------------------------------------------------


def tunnel_part(facts: Facts, today: date) -> dict[str, Any]:
    funnel = facts.funnel
    if funnel is None:
        return {"trouble": False, "said": "Not looked at yet.", "more": []}

    more = []
    if funnel.state == tunnel.UP:
        said = f"Tailscale Funnel carries {funnel.address} to this service."
        more.append(f"It forwards to {funnel.target}. The Kick app has to have that address as its webhook URL.")
    elif funnel.state == tunnel.NOT_INSTALLED:
        said = "No Tailscale client was found on this machine, so there is no tunnel to look at from here."
        more.append("If Kick reaches the service some other way, the webhooks above are the sign that it does.")
    elif funnel.state == tunnel.NOT_ANSWERING:
        said = f"Tailscale could not be asked about its Funnel ({funnel.said})."
    elif funnel.state == tunnel.NOT_CONNECTED:
        says = f" (it says {funnel.said})" if funnel.said else ""
        said = f"Tailscale is not connected{says}, so Kick can't reach this service."
        if funnel.said == "NoState":
            more.append("On Windows it says NoState for as long as its tray app is not running.")
    elif funnel.state == tunnel.NO_ROUTE:
        said = (
            f"Tailscale is connected, but nothing forwards {tunnel.WEBHOOK_PATH} to this service, "
            "so Kick can't reach it."
        )
        more.append("The README has the command that sets the Funnel up.")
    elif funnel.state == tunnel.TAILNET_ONLY:
        said = (
            f"{funnel.address} is served inside the tailnet only. Funnel is not on for it, "
            "so Kick can't reach this service."
        )
    else:
        port = f" ({facts.port})" if facts.port else ""
        said = (
            f"The Funnel forwards {funnel.address} to {funnel.target}, "
            f"which is not the port this service answers on{port}."
        )
    if funnel.warnings:
        more.append(f"Tailscale itself says: {' '.join(funnel.warnings)}")
    if facts.funnel_looked_at:
        more.append(f"Looked at {ago_words(facts.funnel_looked_at, facts.now, today)}.")
    return {"trouble": funnel.state not in (tunnel.UP, tunnel.NOT_INSTALLED), "said": said, "more": more}


# -- the page -----------------------------------------------------------------

# The parts in the order the page has them: what each is called in a
# sentence, and whether that is more than one thing.
PARTS = (
    ("webhooks", "webhooks", True),
    ("subscriptions", "subscriptions", True),
    ("twitch", "Twitch chat", False),
    ("youtube", "YouTube chat", False),
    ("tiktok", "TikTok chat", False),
    ("bot", "the chat bot", False),
    ("notifications", "notifications", True),
    ("recorders", "the recorders", True),
    ("disk", "the disk", False),
    ("tunnel", "the tunnel", False),
)


def report(facts: Facts, today: date) -> dict[str, Any]:
    """What the page says: each part, how many of them are trouble, and a
    sentence on top that says which."""
    parts = {
        "webhooks": webhooks(facts, today),
        "subscriptions": subscriptions(facts, today),
        "twitch": twitch_part(facts, today),
        "youtube": youtube_part(facts, today),
        "tiktok": tiktok_part(facts, today),
        "bot": bot_part(facts, today),
        "notifications": notifications_part(facts, today),
        "recorders": recorders(facts, today),
        "disk": disk(facts),
        "tunnel": tunnel_part(facts, today),
    }
    shown = [(name, words, plural) for name, words, plural in PARTS if parts[name].get("shown", True)]
    troubled = [(words, plural) for name, words, plural in shown if parts[name]["trouble"]]
    if troubled:
        need = "need" if len(troubled) > 1 or troubled[0][1] else "needs"
        lead = f"{sentence_case(list_words([words for words, _ in troubled]))} {need} a look."
    else:
        lead = f"{sentence_case(list_words([words for _, words, _ in shown]))} all look fine."
    started = facts.started_at.astimezone()
    return {
        "troubles": len(troubled),
        "lead": lead,
        "running": (
            f"The service has been running for {span_words(_seconds(facts.now, facts.started_at))}, "
            f"since {since_words(started, today)}."
        ),
        **parts,
    }
