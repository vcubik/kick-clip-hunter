"""An in-memory stand-in for the two HTTP APIs the service talks to.

`FakeKickApi` answers the same endpoints, with the same response shapes, as
Kick's public API (token, channel lookup, event subscriptions, webhook public
key), the channel lookup of kick.com's own API (for subscriber badges) and
its list of a channel's streams with their VODs, and 7TV's emote-set lookup. It plugs in as an `httpx` transport, so the
real client code runs unmodified - request building, auth headers, status
handling and JSON parsing are all exercised; only the network is replaced.

It is also the account the bot writes in Kick's chat as: Kick's OAuth 2.1
flow with PKCE - the consent page a browser is sent to (`consent`, which a
test calls with the address the service sent the browser to), the code
exchanged for an account's tokens, their renewal - who a user token is of,
and the chat a message is written in (`chat_written`).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import httpx

# What the service is set up with in the test run (see conftest.py).
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
# What a sign-in of the bot's account is asked for, and given.
BOT_SCOPES = ("chat:write", "user:read")


def seventv_emote_id(name: str) -> str:
    """The id the fake 7TV gives an emote of that name, unless the channel
    says otherwise: made from the name, so two channels' emotes of one name
    are the same emote only if nothing is set to tell them apart."""
    return "7TV" + name.encode().hex().upper()


@dataclass
class FakeChannel:
    slug: str
    broadcaster_user_id: int
    is_live: bool = False
    # ISO timestamp of when the current stream started, as Kick reports it.
    start_time: str | None = None
    # 7TV emote names connected to the channel; None = no 7TV account at all.
    seventv_emotes: list[str] | None = field(default_factory=list)
    # False = a 7TV account that has no emote set selected ("emote_set": null).
    seventv_has_emote_set: bool = True
    # Emote ids by name, for the names that should not get seventv_emote_id's.
    seventv_ids: dict[str, object] = field(default_factory=dict)
    # (width, height) of an emote's smallest picture by name; 32x32 otherwise.
    # None = 7TV lists no picture files for it.
    seventv_sizes: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    # The channel's own subscriber badges: the picture's address by the
    # months it is shown from, as kick.com lists them.
    subscriber_badges: dict[int, str] = field(default_factory=dict)
    # The channel's newest streams, as kick.com lists them with their VODs
    # (see kick_video).
    videos: list[dict] = field(default_factory=list)

    def seventv_emote(self, name: str) -> dict:
        """One entry of the channel's emote set, in the shape 7TV sends."""
        emote_id = self.seventv_ids.get(name, seventv_emote_id(name))
        size = self.seventv_sizes.get(name, (32, 32))
        files = []
        if size is not None:
            files = [
                {"name": f"{scale}x.{kind}", "width": size[0] * scale, "height": size[1] * scale}
                for kind in ("avif", "webp")
                for scale in (1, 2, 3, 4)
            ]
        return {
            "id": emote_id,
            "name": name,
            "data": {"id": emote_id, "host": {"url": f"//cdn.7tv.app/emote/{emote_id}", "files": files}},
        }


def kick_video(
    started: datetime,
    seconds: float,
    source: str = "",
    *,
    live: bool = False,
    status: str = "public",
) -> dict:
    """A stream as kick.com lists it with its VOD: when it began (in UTC,
    written without saying so, as kick.com writes it), how long it ran (0
    while it is on), the master playlist of its VOD - empty where the VOD is
    for subscribers only - and what kick.com says of the VOD."""
    return {
        "id": int(started.timestamp()),
        "start_time": started.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "duration": 0 if live else int(seconds * 1000),
        "is_live": live,
        "source": source if status == "public" else "",
        "video": {"uuid": f"video-{int(started.timestamp())}", "status": status},
    }


@dataclass(frozen=True)
class FakeKickAccount:
    """A Kick account that can sign in for the bot to write as."""

    name: str = "clip_bot"
    user_id: int = 555


@dataclass(frozen=True)
class _Consent:
    """A code the consent page handed out: whose, for which challenge and
    redirect URL, and with what it lets the application do."""

    account: FakeKickAccount
    challenge: str
    redirect_uri: str
    scopes: tuple[str, ...]


def pkce_challenge(verifier: str) -> str:
    """PKCE's S256 challenge of a verifier, as Kick checks it."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class OtherFake(Protocol):
    """A fake of another service, whose requests go the same way as Kick's."""

    def owns(self, request: httpx.Request) -> bool: ...

    def handle(self, request: httpx.Request) -> httpx.Response: ...


@dataclass
class FakeKickApi:
    channels: dict[str, FakeChannel] = field(default_factory=dict)
    # broadcaster_user_ids that currently have a chat.message.sent subscription.
    subscribed: list[int] = field(default_factory=list)
    public_key_pem: str = ""
    token_lifetime_seconds: int = 3600
    # What the channel lookup answers for a slug that does not exist. The
    # real API responds 400; None makes it return an empty result instead.
    unknown_slug_status: int | None = 400
    # Force an HTTP status for every request whose URL contains the key.
    failures: dict[str, int] = field(default_factory=dict)
    # broadcaster_user_ids whose subscription request is refused.
    subscribe_failures: set[int] = field(default_factory=set)
    # Every request received, as (method, url path, parsed body or query).
    requests: list[tuple[str, str, object]] = field(default_factory=list)
    tokens_issued: int = 0
    # 7TV's global emote set, the one every channel has on top of its own:
    # its seventv_* fields are what the fake 7TV answers with.
    seventv_global: FakeChannel = field(default_factory=lambda: FakeChannel("", 0))
    # Fakes of other platforms a test has as well (Twitch's, YouTube's): a
    # request one of them owns is answered by it.
    others: list[OtherFake] = field(default_factory=list)
    # Accounts signing in for the bot: the codes the consent page handed
    # out, the user tokens that work (access tokens, and refresh tokens,
    # each of which works once) and what each lets the application do.
    user_token_lifetime_seconds: int = 7200
    consents: dict[str, _Consent] = field(default_factory=dict)
    user_tokens: dict[str, FakeKickAccount] = field(default_factory=dict)
    user_refresh_tokens: dict[str, tuple[FakeKickAccount, tuple[str, ...]]] = field(default_factory=dict)
    user_token_scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    user_tokens_issued: int = 0
    codes_issued: int = 0
    # Every chat message written, in order: the channel, what was written,
    # and as whom. A channel listed in `chat_refusals` turns messages down
    # with that status; one in `chat_unsent` takes them and sends none.
    chat_written: list[dict] = field(default_factory=list)
    chat_refusals: dict[int, int] = field(default_factory=dict)
    chat_unsent: set[int] = field(default_factory=set)

    def add_channel(self, slug: str, broadcaster_user_id: int, **kwargs) -> FakeChannel:
        channel = FakeChannel(slug=slug, broadcaster_user_id=broadcaster_user_id, **kwargs)
        self.channels[slug] = channel
        return channel

    def rename_channel(self, old: str, new: str) -> FakeChannel:
        """The channel renames itself on Kick: it keeps its id, and is found
        by its new name only."""
        channel = self.channels.pop(old)
        channel.slug = new
        self.channels[new] = channel
        return channel

    def remove_channel(self, slug: str) -> None:
        """The channel is gone from Kick, by name and by id."""
        del self.channels[slug]

    def calls(self, method: str, path_fragment: str) -> list[object]:
        """Bodies/queries of the recorded requests matching a method and path."""
        return [body for m, path, body in self.requests if m == method and path_fragment in path]

    def written_in(self, broadcaster_user_id: int) -> list[str]:
        """What was written in a channel's chat, in order."""
        return [sent["content"] for sent in self.chat_written if sent["broadcaster_user_id"] == broadcaster_user_id]

    def user_tokens_for(self, account: FakeKickAccount | None = None, scopes: tuple[str, ...] = BOT_SCOPES) -> dict:
        """A working pair of an account's tokens, as the token endpoint hands
        them out."""
        account = account or FakeKickAccount()
        self.user_tokens_issued += 1
        access, refresh = f"kick-user-token-{self.user_tokens_issued}", f"kick-refresh-token-{self.user_tokens_issued}"
        self.user_tokens[access] = account
        self.user_token_scopes[access] = tuple(scopes)
        self.user_refresh_tokens[refresh] = (account, tuple(scopes))
        return {
            "access_token": access,
            "token_type": "Bearer",
            "refresh_token": refresh,
            "expires_in": self.user_token_lifetime_seconds,
            "scope": " ".join(scopes),
        }

    def consent(
        self, authorize_url: str, account: FakeKickAccount | None = None, *, scopes: tuple[str, ...] | None = None
    ) -> httpx.URL:
        """Where Kick's consent page sends the browser back to once `account`
        has signed in there - sent there with `authorize_url`, which is
        checked as Kick checks it: the redirect URL, with a code for the
        account's tokens. It is given what it asked for unless `scopes`
        says less."""
        url = httpx.URL(authorize_url)
        asked = url.params
        assert (url.host, url.path) == ("id.kick.com", "/oauth/authorize"), authorize_url
        assert asked["response_type"] == "code"
        assert asked["client_id"] == CLIENT_ID
        assert asked["code_challenge_method"] == "S256"
        assert asked["state"] and asked["code_challenge"]
        self.codes_issued += 1
        code = f"code-{self.codes_issued}"
        granted = tuple(asked["scope"].split()) if scopes is None else scopes
        self.consents[code] = _Consent(
            account or FakeKickAccount(), asked["code_challenge"], asked["redirect_uri"], granted
        )
        return httpx.URL(asked["redirect_uri"], params={"code": code, "state": asked["state"]})

    def turn_down(self, authorize_url: str) -> httpx.URL:
        """Where the consent page sends the browser back to when the account
        does not let the application in."""
        asked = httpx.URL(authorize_url).params
        return httpx.URL(
            asked["redirect_uri"],
            params={"error": "access_denied", "error_description": "The user denied access", "state": asked["state"]},
        )

    def _user_token(self, body: dict) -> httpx.Response:
        """The token endpoint, for a code from the consent page or a refresh
        token."""
        if (body.get("client_id"), body.get("client_secret")) != (CLIENT_ID, CLIENT_SECRET):
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "Client unknown"})
        if body.get("grant_type") == "refresh_token":
            found = self.user_refresh_tokens.pop(body.get("refresh_token", ""), None)
            if found is None:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "Invalid refresh token"}
                )
            return httpx.Response(200, json=self.user_tokens_for(*found))
        consent = self.consents.pop(body.get("code", ""), None)
        if consent is None:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid code"})
        if body.get("redirect_uri") != consent.redirect_uri:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Redirect URI mismatch"})
        if pkce_challenge(body.get("code_verifier", "")) != consent.challenge:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Code verifier mismatch"})
        return httpx.Response(200, json=self.user_tokens_for(consent.account, consent.scopes))

    def _as_user(self, request: httpx.Request, token: str, account: FakeKickAccount, body: object) -> httpx.Response:
        """What a user token asks of the public API: who it is of, or a
        message written in a channel's chat."""
        scopes = self.user_token_scopes.get(token, ())
        if request.url.path == "/public/v1/users":
            if "user:read" not in scopes:
                return httpx.Response(403, json={"message": "Forbidden", "data": None})
            user = {"user_id": account.user_id, "name": account.name, "email": "", "profile_picture": ""}
            return httpx.Response(200, json={"data": [user], "message": "OK"})
        if request.url.path == "/public/v1/chat" and request.method == "POST":
            if "chat:write" not in scopes:
                return httpx.Response(403, json={"message": "Forbidden", "data": None})
            assert isinstance(body, dict) and body.get("type") == "user", body
            broadcaster, content = body.get("broadcaster_user_id"), body.get("content")
            if not isinstance(broadcaster, int) or not content or len(content) > 500:
                return httpx.Response(400, json={"message": "Invalid request", "data": None})
            if broadcaster in self.chat_refusals:
                status = self.chat_refusals[broadcaster]
                return httpx.Response(status, json={"message": "You cannot chat here", "data": None})
            if broadcaster in self.chat_unsent:
                return httpx.Response(200, json={"data": {"is_sent": False, "message_id": ""}, "message": "OK"})
            self.chat_written.append({"broadcaster_user_id": broadcaster, "content": content, "sender": account.name})
            sent = {"is_sent": True, "message_id": f"written-{len(self.chat_written)}"}
            return httpx.Response(200, json={"data": sent, "message": "OK"})
        return httpx.Response(401, json={"message": "Unauthorized", "data": None})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        for other in self.others:
            if other.owns(request):
                return other.handle(request)
        url = str(request.url)
        path = request.url.path
        if request.method == "POST" and request.content:
            content_type = request.headers.get("content-type", "")
            body: object = (
                json.loads(request.content)
                if "json" in content_type
                else dict(httpx.QueryParams(request.content.decode()))
            )
        else:
            body = dict(request.url.params)
        self.requests.append((request.method, f"{request.url.host}{path}", body))

        for fragment, status in self.failures.items():
            if fragment in url:
                return httpx.Response(status, json={"message": "forced failure"})

        if (
            request.url.host == "id.kick.com"
            and path == "/oauth/token"
            and body.get("grant_type") != "client_credentials"
        ):
            return self._user_token(body)  # type: ignore[arg-type]
        if request.url.host == "id.kick.com" and path == "/oauth/token":
            self.tokens_issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"app-token-{self.tokens_issued}",
                    "token_type": "Bearer",
                    "expires_in": self.token_lifetime_seconds,
                },
            )

        if request.url.host == "api.kick.com":
            if path == "/public/v1/public-key":
                return httpx.Response(200, json={"data": {"public_key": self.public_key_pem}})
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if token in self.user_tokens:
                return self._as_user(request, token, self.user_tokens[token], body)
            if path == "/public/v1/chat":
                return httpx.Response(401, json={"message": "Unauthorized", "data": None})
            if not request.headers.get("authorization", "").startswith("Bearer app-token-"):
                return httpx.Response(401, json={"message": "Unauthorized"})
            if path == "/public/v1/channels" and "broadcaster_user_id" in request.url.params:
                wanted = int(request.url.params["broadcaster_user_id"])
                channel = next((c for c in self.channels.values() if c.broadcaster_user_id == wanted), None)
                return httpx.Response(200, json={"data": [self._channel_json(channel)] if channel else []})
            if path == "/public/v1/channels":
                channel = self.channels.get(request.url.params.get("slug", ""))
                if channel is None and self.unknown_slug_status is not None:
                    return httpx.Response(self.unknown_slug_status, json={"message": "Invalid request"})
                return httpx.Response(200, json={"data": [self._channel_json(channel)] if channel else []})
            if path == "/public/v1/events/subscriptions" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": f"subscription-{user_id}",
                                "broadcaster_user_id": user_id,
                                "event": "chat.message.sent",
                                "method": "webhook",
                            }
                            for user_id in self.subscribed
                        ]
                    },
                )
            if path == "/public/v1/events/subscriptions" and request.method == "DELETE":
                gone = set(request.url.params.get_list("id"))
                self.subscribed[:] = [user_id for user_id in self.subscribed if f"subscription-{user_id}" not in gone]
                return httpx.Response(204)
            if path == "/public/v1/events/subscriptions" and request.method == "POST":
                if body["broadcaster_user_id"] in self.subscribe_failures:  # type: ignore[index]
                    return httpx.Response(500, json={"message": "Internal Server Error"})
                self.subscribed.append(body["broadcaster_user_id"])  # type: ignore[index]
                return httpx.Response(200, json={"data": [{"name": "chat.message.sent", "version": 1}]})

        if request.url.host == "kick.com" and path.startswith("/api/v2/channels/") and path.endswith("/videos"):
            wanted = path.split("/")[-2].lower()
            channel = next((c for slug, c in self.channels.items() if slug.lower() == wanted), None)
            if channel is None:
                return httpx.Response(404, json={"message": "Channel not found"})
            return httpx.Response(200, json=channel.videos)

        if request.url.host == "kick.com" and path.startswith("/api/v2/channels/"):
            # Any spelling of a channel's slug finds it, as on kick.com.
            wanted = path.rsplit("/", 1)[1].lower()
            channel = next((c for slug, c in self.channels.items() if slug.lower() == wanted), None)
            if channel is None:
                return httpx.Response(404, json={"message": "Channel not found"})
            return httpx.Response(200, json=self._site_channel_json(channel))

        if request.url.host == "7tv.io" and path == "/v3/emote-sets/global":
            emotes = [self.seventv_global.seventv_emote(n) for n in self.seventv_global.seventv_emotes or []]
            return httpx.Response(200, json={"id": "GLOBAL", "name": "Global Emotes", "emotes": emotes})

        if request.url.host == "7tv.io" and path.startswith("/v3/users/kick/"):
            user_id = int(path.rsplit("/", 1)[1])
            channel = next((c for c in self.channels.values() if c.broadcaster_user_id == user_id), None)
            if channel is None or channel.seventv_emotes is None:
                return httpx.Response(404, json={"error": "Unknown User"})
            if not channel.seventv_has_emote_set:
                return httpx.Response(200, json={"emote_set": None})
            return httpx.Response(
                200, json={"emote_set": {"emotes": [channel.seventv_emote(n) for n in channel.seventv_emotes]}}
            )

        return httpx.Response(404, json={"message": f"fake API has no route for {request.method} {url}"})

    @staticmethod
    def _site_channel_json(channel: FakeChannel) -> dict:
        badges = [
            {
                "id": 1000 + months,
                "channel_id": channel.broadcaster_user_id + 1,
                "months": months,
                "badge_image": {"srcset": "", "src": image},
            }
            for months, image in channel.subscriber_badges.items()
        ]
        return {
            "id": channel.broadcaster_user_id + 1,
            "user_id": channel.broadcaster_user_id,
            "slug": channel.slug,
            "subscriber_badges": badges,
        }

    @staticmethod
    def _channel_json(channel: FakeChannel) -> dict:
        return {
            "slug": channel.slug,
            "broadcaster_user_id": channel.broadcaster_user_id,
            "stream": {"is_live": channel.is_live, "start_time": channel.start_time},
        }
