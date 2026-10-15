"""An in-memory stand-in for Twitch's side of the service.

`FakeTwitchApi` answers what the service asks of Twitch over HTTP - its
OAuth server and Helix - and of the emote services that know Twitch
channels (7TV's lookup by a Twitch id), with the same response shapes. Like
`FakeKickApi` it plugs in as an `httpx` transport, so the real client code
runs unmodified; the `twitch_api` fixture routes every request for one of
its hosts here and everything else on to the fake Kick.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from tests.support.kick_api import seventv_emote_id

HOSTS = frozenset({"id.twitch.tv", "api.twitch.tv", "api.betterttv.net", "api.frankerfacez.com"})


def bttv_emote_id(name: str) -> str:
    """The id the fake BetterTTV gives an emote of that name."""
    return "b" + name.encode().hex()


def ffz_emote_id(name: str) -> int:
    """The id the fake FrankerFaceZ gives an emote of that name."""
    return int.from_bytes(name.encode()[:6], "big")


@dataclass
class FakeTwitchChannel:
    login: str
    user_id: str
    display_name: str = ""
    is_live: bool = False
    # When the current stream began, as Helix says it ("...Z").
    started_at: str | None = None
    stream_id: str = "1"
    # 7TV emote names of the channel; None = no 7TV account at all.
    seventv_emotes: list[str] | None = field(default_factory=list)
    # The same on BetterTTV and FrankerFaceZ; None = no account there.
    bttv_emotes: list[str] | None = field(default_factory=list)
    ffz_emotes: list[str] | None = field(default_factory=list)
    # The recording Twitch makes of the stream that is on (its VOD); None
    # for a channel that keeps no past broadcasts. `older_archives` are of
    # streams that are over.
    archive_id: str | None = None
    older_archives: list[str] = field(default_factory=list)
    # The channel's own subscriber badges: the picture's address by the
    # badge version (months, with the tier's thousands above the first).
    subscriber_badges: dict[str, str] = field(default_factory=dict)

    def user_json(self) -> dict:
        return {
            "id": self.user_id,
            "login": self.login,
            "display_name": self.display_name or self.login,
            "type": "",
            "broadcaster_type": "",
        }

    def stream_json(self) -> dict:
        return {
            "id": self.stream_id,
            "user_id": self.user_id,
            "user_login": self.login,
            "user_name": self.display_name or self.login,
            "type": "live",
            "started_at": self.started_at,
        }


@dataclass(frozen=True)
class FakeAccount:
    """A Twitch account that can sign in for the service to read chat as."""

    login: str = "chat_reader"
    user_id: str = "777"


@dataclass
class _DeviceFlow:
    # Who confirms the code shown, after how many polls; None = nobody yet.
    account: FakeAccount | None = None
    polls_before: int = 0
    denied: bool = False
    polls: int = 0


@dataclass
class FakeTwitchApi:
    channels: dict[str, FakeTwitchChannel] = field(default_factory=dict)
    token_lifetime_seconds: int = 3600
    user_token_lifetime_seconds: int = 14400
    # Force an HTTP status for every request whose URL contains the key.
    failures: dict[str, int] = field(default_factory=dict)
    # Every request received, as (method, host + path, parsed body or query).
    requests: list[tuple[str, str, object]] = field(default_factory=list)
    tokens_issued: int = 0
    # The device flows started, by their device code.
    device_flows: dict[str, _DeviceFlow] = field(default_factory=dict)
    # What the next device flow started comes to, as sign_in_will_happen set it.
    next_sign_in: _DeviceFlow = field(default_factory=_DeviceFlow)
    # The accounts' tokens that work: access tokens, and refresh tokens
    # (each works once).
    user_tokens: dict[str, FakeAccount] = field(default_factory=dict)
    refresh_tokens: dict[str, FakeAccount] = field(default_factory=dict)
    # Scopes an access token was given, by token.
    token_scopes: dict[str, list[str]] = field(default_factory=dict)
    user_tokens_issued: int = 0
    # The EventSub subscriptions made, in order, and the status a
    # subscription to a channel (by its Twitch id) is refused with.
    eventsub_subscriptions: list[dict] = field(default_factory=list)
    subscribe_failures: dict[str, int] = field(default_factory=dict)

    def sign_in_will_happen(
        self, account: FakeAccount | None = None, *, after_polls: int = 0, denied: bool = False
    ) -> None:
        """The code the next device flow shows is confirmed by `account`
        after that many polls have been answered "pending" - or turned down."""
        self.next_sign_in = _DeviceFlow(account=account or FakeAccount(), polls_before=after_polls, denied=denied)

    def tokens_for(self, account: FakeAccount | None = None, scopes: tuple[str, ...] = ("user:read:chat",)) -> dict:
        """A working pair of tokens for an account, as the token endpoint
        hands them out."""
        account = account or FakeAccount()
        self.user_tokens_issued += 1
        access, refresh = f"user-token-{self.user_tokens_issued}", f"refresh-token-{self.user_tokens_issued}"
        self.user_tokens[access] = account
        self.refresh_tokens[refresh] = account
        self.token_scopes[access] = list(scopes)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": self.user_token_lifetime_seconds,
            "scope": list(scopes),
            "token_type": "bearer",
        }

    def add_channel(self, login: str, user_id: str, **kwargs) -> FakeTwitchChannel:
        channel = FakeTwitchChannel(login=login, user_id=user_id, **kwargs)
        self.channels[login] = channel
        return channel

    def calls(self, method: str, path_fragment: str) -> list[object]:
        """Bodies/queries of the recorded requests matching a method and path."""
        return [body for m, path, body in self.requests if m == method and path_fragment in path]

    def owns(self, request: httpx.Request) -> bool:
        return request.url.host in HOSTS or (
            request.url.host == "7tv.io" and request.url.path.startswith("/v3/users/twitch/")
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
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

        if request.url.host == "id.twitch.tv" and path == "/oauth2/token":
            return self._token(body)  # type: ignore[arg-type]
        if request.url.host == "id.twitch.tv" and path == "/oauth2/device":
            device_code = f"device-{len(self.device_flows) + 1}"
            self.device_flows[device_code] = self.next_sign_in
            self.next_sign_in = _DeviceFlow()
            return httpx.Response(
                200,
                json={
                    "device_code": device_code,
                    "user_code": "ABCDEFGH",
                    "verification_uri": "https://www.twitch.tv/activate?public=true&device-code=ABCDEFGH",
                    "expires_in": 1800,
                    "interval": 5,
                },
            )
        if request.url.host == "id.twitch.tv" and path == "/oauth2/validate":
            token = request.headers.get("authorization", "").removeprefix("OAuth ")
            account = self.user_tokens.get(token)
            if account is None:
                return httpx.Response(401, json={"status": 401, "message": "invalid access token"})
            return httpx.Response(
                200,
                json={
                    "client_id": "twitch-client-id",
                    "login": account.login,
                    "scopes": self.token_scopes.get(token, []),
                    "user_id": account.user_id,
                    "expires_in": self.user_token_lifetime_seconds,
                },
            )

        if request.url.host == "api.twitch.tv":
            if not request.headers.get("client-id"):
                return httpx.Response(401, json={"message": "Client ID is missing"})
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return httpx.Response(401, json={"message": "OAuth token is missing"})
            if path == "/helix/users":
                logins = request.url.params.get_list("login")
                ids = request.url.params.get_list("id")
                found = [
                    channel.user_json()
                    for channel in self.channels.values()
                    if channel.login in logins or channel.user_id in ids
                ]
                return httpx.Response(200, json={"data": found})
            if path == "/helix/streams":
                logins = request.url.params.get_list("user_login")
                ids = request.url.params.get_list("user_id")
                live = [
                    channel.stream_json()
                    for channel in self.channels.values()
                    if channel.is_live and (channel.login in logins or channel.user_id in ids)
                ]
                return httpx.Response(200, json={"data": live, "pagination": {}})
            if path == "/helix/videos":
                channel = self._by_id(request.url.params.get("user_id", ""))
                videos = []
                if channel is not None and request.url.params.get("type") == "archive":
                    if channel.archive_id is not None and channel.is_live:
                        videos.append({"id": channel.archive_id, "stream_id": channel.stream_id, "type": "archive"})
                    videos.extend(
                        {"id": video_id, "stream_id": f"old-{video_id}", "type": "archive"}
                        for video_id in channel.older_archives
                    )
                return httpx.Response(200, json={"data": videos, "pagination": {}})
            if path == "/helix/chat/badges":
                channel = self._by_id(request.url.params.get("broadcaster_id", ""))
                versions = [
                    {
                        "id": version,
                        "image_url_1x": image.replace("/3", "/1"),
                        "image_url_2x": image.replace("/3", "/2"),
                        "image_url_4x": image,
                        "title": "Subscriber",
                    }
                    for version, image in (channel.subscriber_badges if channel else {}).items()
                ]
                sets = [{"set_id": "subscriber", "versions": versions}] if versions else []
                return httpx.Response(200, json={"data": sets})
            if path == "/helix/eventsub/subscriptions" and request.method == "POST":
                return self._subscribe(request, body)  # type: ignore[arg-type]

        if request.url.host == "api.betterttv.net" and path.startswith("/3/cached/users/twitch/"):
            channel = self._by_id(path.rsplit("/", 1)[1])
            if channel is None or channel.bttv_emotes is None:
                return httpx.Response(404, json={"message": "user not found"})
            emotes = [
                {"id": bttv_emote_id(name), "code": name, "imageType": "png", "animated": False}
                for name in channel.bttv_emotes
            ]
            return httpx.Response(200, json={"id": "x", "bots": [], "channelEmotes": emotes, "sharedEmotes": []})

        if request.url.host == "api.frankerfacez.com" and path.startswith("/v1/room/id/"):
            channel = self._by_id(path.rsplit("/", 1)[1])
            if channel is None or channel.ffz_emotes is None:
                return httpx.Response(404, json={"error": "Not Found", "status": 404})
            emotes = [
                {"id": ffz_emote_id(name), "name": name, "width": 64, "height": 32} for name in channel.ffz_emotes
            ]
            return httpx.Response(200, json={"room": {"set": 1}, "sets": {"1": {"id": 1, "emoticons": emotes}}})

        if request.url.host == "7tv.io" and path.startswith("/v3/users/twitch/"):
            user_id = path.rsplit("/", 1)[1]
            channel = next((c for c in self.channels.values() if c.user_id == user_id), None)
            if channel is None or channel.seventv_emotes is None:
                return httpx.Response(404, json={"error": "Unknown User"})
            emotes = [
                {"id": seventv_emote_id(name), "name": name, "data": {"host": {"files": []}}}
                for name in channel.seventv_emotes
            ]
            return httpx.Response(200, json={"emote_set": {"emotes": emotes}})

        return httpx.Response(404, json={"message": f"fake Twitch has no route for {request.method} {url}"})

    def _subscribe(self, request: httpx.Request, body: dict) -> httpx.Response:
        """An EventSub subscription, made with an account's token."""
        token = request.headers["authorization"].removeprefix("Bearer ")
        account = self.user_tokens.get(token)
        if account is None:
            return httpx.Response(401, json={"status": 401, "message": "Invalid OAuth token"})
        broadcaster = body["condition"]["broadcaster_user_id"]
        if broadcaster in self.subscribe_failures:
            status = self.subscribe_failures[broadcaster]
            return httpx.Response(status, json={"status": status, "message": "subscription refused"})
        subscription = {
            "id": f"subscription-{len(self.eventsub_subscriptions) + 1}",
            "type": body["type"],
            "version": body["version"],
            "condition": body["condition"],
            "transport": body["transport"],
            "account": account.login,
        }
        self.eventsub_subscriptions.append(subscription)
        return httpx.Response(202, json={"data": [subscription], "total": len(self.eventsub_subscriptions)})

    def _by_id(self, user_id: str) -> FakeTwitchChannel | None:
        return next((channel for channel in self.channels.values() if channel.user_id == user_id), None)

    def subscribed_on(self, session_id: str) -> list[str]:
        """The channels (Twitch ids) whose chat is subscribed to a session."""
        return [
            subscription["condition"]["broadcaster_user_id"]
            for subscription in self.eventsub_subscriptions
            if subscription["transport"].get("session_id") == session_id
        ]

    def _token(self, body: dict) -> httpx.Response:
        if body.get("grant_type") == "client_credentials":
            self.tokens_issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"twitch-app-token-{self.tokens_issued}",
                    "expires_in": self.token_lifetime_seconds,
                    "token_type": "bearer",
                },
            )
        if body.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
            flow = self.device_flows.get(body.get("device_code", ""))
            if flow is None:
                return httpx.Response(400, json={"status": 400, "message": "invalid device code"})
            if flow.denied:
                return httpx.Response(400, json={"status": 400, "message": "access_denied"})
            flow.polls += 1
            if flow.account is None or flow.polls <= flow.polls_before:
                return httpx.Response(400, json={"status": 400, "message": "authorization_pending"})
            del self.device_flows[body["device_code"]]
            return httpx.Response(200, json=self.tokens_for(flow.account))
        if body.get("grant_type") == "refresh_token":
            account = self.refresh_tokens.pop(body.get("refresh_token", ""), None)
            if account is None:
                return httpx.Response(400, json={"status": 400, "message": "Invalid refresh token"})
            return httpx.Response(200, json=self.tokens_for(account))
        return httpx.Response(400, json={"status": 400, "message": "invalid grant type"})
