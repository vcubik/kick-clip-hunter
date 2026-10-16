"""The Twitch account the service reads chat as, and the bot writes in it
as: signing it in with the device flow, keeping its tokens, renewing them
before they run out.

Everything goes through the real code against `FakeTwitchApi`; waiting for a
code to be confirmed is handed a sleep that does not sleep.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import twitch_login
from kick_clip_hunter import twitch_auth
from kick_clip_hunter.twitch_auth import AccountToken, SignInNeeded
from tests.support.twitch_api import SCOPES, FakeAccount

pytestmark = pytest.mark.anyio

CLIENT_ID = "twitch-client-id"
SECRET = "twitch-client-secret"
NOW = datetime.now(timezone.utc)


def no_wait():
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return sleep, waits


def account_token(
    twitch_api, account: FakeAccount | None = None, runs_out_in: float = 3600, scopes: tuple[str, ...] = SCOPES
) -> AccountToken:
    """A token the fake Twitch takes, as the service would have it kept."""
    account = account or FakeAccount()
    tokens = twitch_api.tokens_for(account, scopes)
    return AccountToken(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=runs_out_in),
        user_id=account.user_id,
        login=account.login,
    )


class TestKeepingTheTokens:
    def test_what_is_kept_is_what_is_read_back(self, tmp_path):
        token = AccountToken("access", "refresh", NOW, "777", "chat_reader")

        twitch_auth.save(token, tmp_path / "token.json")

        assert twitch_auth.load(tmp_path / "token.json") == token

    def test_without_a_file_nobody_has_signed_in(self, tmp_path):
        assert twitch_auth.load(tmp_path / "nothing.json") is None

    @pytest.mark.parametrize("text", ["", "not json", "{}", '{"access_token": "a"}'])
    def test_a_file_that_cannot_be_read_is_nobody_signed_in(self, tmp_path, text):
        (tmp_path / "token.json").write_text(text, encoding="utf-8")

        assert twitch_auth.load(tmp_path / "token.json") is None

    def test_it_is_kept_under_data_by_default(self):
        token = AccountToken("access", "refresh", NOW, "777", "chat_reader")

        twitch_auth.save(token)

        assert twitch_auth.TOKEN_FILE.is_file()
        assert twitch_auth.load() == token


class TestSigningIn:
    async def test_the_flow_starts_with_the_applications_id_asking_to_read_and_write_chat(self, twitch_api):
        started = await twitch_auth.start_sign_in(CLIENT_ID)

        assert started["user_code"] == "ABCDEFGH"
        assert twitch_api.calls("POST", "/oauth2/device") == [
            {"client_id": CLIENT_ID, "scopes": "user:read:chat user:write:chat"}
        ]

    async def test_what_the_account_let_the_service_do_is_kept_with_it(self, twitch_api):
        twitch_api.sign_in_will_happen()
        started = await twitch_auth.start_sign_in(CLIENT_ID)

        token = await twitch_auth.finish_sign_in(CLIENT_ID, SECRET, started, sleep=no_wait()[0])

        assert token.scopes == ("user:read:chat", "user:write:chat")
        assert token.may(twitch_auth.WRITE_SCOPE) is True
        assert twitch_auth.load() is None  # keeping it is the script's

    async def test_it_waits_for_the_code_to_be_confirmed_and_learns_whose_account_it_is(self, twitch_api):
        twitch_api.sign_in_will_happen(FakeAccount("someone", "4242"), after_polls=2)
        started = await twitch_auth.start_sign_in(CLIENT_ID)
        sleep, waits = no_wait()

        token = await twitch_auth.finish_sign_in(CLIENT_ID, SECRET, started, sleep=sleep)

        assert (token.login, token.user_id) == ("someone", "4242")
        assert waits == [5.0, 5.0]
        assert token.expires_at > datetime.now(timezone.utc) + timedelta(hours=3)
        (asked, *_) = twitch_api.calls("POST", "/oauth2/token")
        assert asked["grant_type"] == twitch_auth.DEVICE_GRANT
        assert asked["device_code"] == started["device_code"]

    async def test_a_code_turned_down_is_no_sign_in(self, twitch_api):
        twitch_api.sign_in_will_happen(denied=True)
        started = await twitch_auth.start_sign_in(CLIENT_ID)

        with pytest.raises(SignInNeeded, match="access_denied"):
            await twitch_auth.finish_sign_in(CLIENT_ID, SECRET, started, sleep=no_wait()[0])

    async def test_a_code_not_confirmed_in_time_is_no_sign_in(self, twitch_api):
        started = await twitch_auth.start_sign_in(CLIENT_ID)  # nobody confirms it
        clock = [0.0]

        async def sleep(seconds: float) -> None:
            clock[0] += seconds

        with pytest.raises(SignInNeeded, match="not confirmed in time"):
            await twitch_auth.finish_sign_in(CLIENT_ID, SECRET, started, sleep=sleep, clock=lambda: clock[0])

        assert clock[0] <= started["expires_in"]

    async def test_asked_to_slow_down_it_waits_longer(self, twitch_api, monkeypatch):
        twitch_api.sign_in_will_happen(after_polls=2)
        started = await twitch_auth.start_sign_in(CLIENT_ID)
        real = twitch_api._token
        answers = iter(["slow_down"])

        def slow_once(body):
            said = next(answers, None)
            if said:
                return httpx.Response(400, json={"status": 400, "message": said})
            return real(body)

        monkeypatch.setattr(twitch_api, "_token", slow_once)
        sleep, waits = no_wait()

        await twitch_auth.finish_sign_in(CLIENT_ID, SECRET, started, sleep=sleep)

        assert waits[0] == 10.0


class TestRenewing:
    async def test_a_token_is_renewed_with_its_refresh_token_which_is_then_spent(self, twitch_api):
        token = account_token(twitch_api)

        renewed = await twitch_auth.renew(token, CLIENT_ID, SECRET)

        assert renewed.access_token != token.access_token
        assert renewed.refresh_token != token.refresh_token
        assert (renewed.user_id, renewed.login) == (token.user_id, token.login)
        with pytest.raises(SignInNeeded, match="lapsed"):
            await twitch_auth.renew(token, CLIENT_ID, SECRET)

    async def test_what_twitch_says_the_renewed_token_may_do_is_kept(self, twitch_api):
        # An account signed in before the bot could write: what it let the
        # service do was not kept with it, and is learnt at its next renewal.
        token = dataclasses.replace(account_token(twitch_api, scopes=("user:read:chat",)), scopes=None)
        assert token.may(twitch_auth.WRITE_SCOPE) is None

        renewed = await twitch_auth.renew(token, CLIENT_ID, SECRET)

        assert renewed.scopes == ("user:read:chat",)
        assert renewed.may(twitch_auth.WRITE_SCOPE) is False

    async def test_a_token_twitch_no_longer_takes_is_found_out_by_validating_it(self, twitch_api):
        with pytest.raises(SignInNeeded):
            await twitch_auth.validate("not-a-token")


class TestTokenKeeper:
    async def test_without_a_signed_in_account_there_is_no_token(self, twitch_api):
        keeper = twitch_auth.TokenKeeper(CLIENT_ID, SECRET)

        with pytest.raises(SignInNeeded):
            await keeper.token()

        assert "No Twitch account has signed in" in keeper.trouble
        assert keeper.account is None

    async def test_a_token_with_time_left_is_used_as_it_is(self, twitch_api):
        token = account_token(twitch_api, runs_out_in=3600)
        twitch_auth.save(token)
        keeper = twitch_auth.TokenKeeper(CLIENT_ID, SECRET)

        assert await keeper.token() == token
        assert twitch_api.calls("POST", "/oauth2/token") == []
        assert keeper.trouble is None

    async def test_a_token_about_to_run_out_is_renewed_and_kept(self, twitch_api):
        token = account_token(twitch_api, runs_out_in=60)
        twitch_auth.save(token)
        keeper = twitch_auth.TokenKeeper(CLIENT_ID, SECRET)

        renewed = await keeper.token()

        assert renewed.access_token != token.access_token
        assert twitch_auth.load() == renewed
        # The next one asked for is the same, renewed once.
        assert await keeper.token() == renewed
        assert len(twitch_api.calls("POST", "/oauth2/token")) == 1

    async def test_it_can_be_renewed_whatever_is_left_of_it(self, twitch_api):
        token = account_token(twitch_api, runs_out_in=3600)
        twitch_auth.save(token)
        keeper = twitch_auth.TokenKeeper(CLIENT_ID, SECRET)

        assert (await keeper.token(renew_now=True)).access_token != token.access_token

    async def test_a_sign_in_that_lapsed_is_said_and_not_kept_pretending(self, twitch_api):
        token = account_token(twitch_api, runs_out_in=60)
        twitch_api.refresh_tokens.clear()
        twitch_auth.save(token)
        keeper = twitch_auth.TokenKeeper(CLIENT_ID, SECRET)

        with pytest.raises(SignInNeeded):
            await keeper.token()

        assert "lapsed" in keeper.trouble

    async def test_a_new_sign_in_is_picked_up_from_the_file(self, twitch_api):
        keeper = twitch_auth.TokenKeeper(CLIENT_ID, SECRET)
        with pytest.raises(SignInNeeded):
            await keeper.token()

        token = account_token(twitch_api)
        twitch_auth.save(token)

        assert await keeper.token() == token
        assert keeper.trouble is None

    async def test_a_new_sign_in_takes_the_place_of_the_account_in_use(self, twitch_api):
        # The script signs an account in while the service runs on another:
        # the next token is the new account's, and renewing it later keeps
        # the new one - not the old one written back over it.
        old = account_token(twitch_api, FakeAccount("old_reader", "1"))
        twitch_auth.save(old)
        keeper = twitch_auth.TokenKeeper(CLIENT_ID, SECRET)
        assert await keeper.token() == old

        new = account_token(twitch_api, FakeAccount("new_reader", "2"), runs_out_in=60)
        twitch_auth.save(new)
        renewed = await keeper.token()

        assert (renewed.login, keeper.account.login, twitch_auth.load().login) == ("new_reader",) * 3

    async def test_a_sign_in_that_lapsed_is_not_tried_again_until_another_is_made(self, twitch_api):
        twitch_auth.save(account_token(twitch_api, runs_out_in=60))
        twitch_api.refresh_tokens.clear()
        keeper = twitch_auth.TokenKeeper(CLIENT_ID, SECRET)
        with pytest.raises(SignInNeeded):
            await keeper.token()
        asked = len(twitch_api.calls("POST", "/oauth2/token"))

        with pytest.raises(SignInNeeded, match="lapsed"):
            await keeper.token()

        assert len(twitch_api.calls("POST", "/oauth2/token")) == asked
        assert keeper.account is None
        signed_in_again = account_token(twitch_api)
        twitch_auth.save(signed_in_again)
        assert await keeper.token() == signed_in_again

    async def test_two_asking_at_once_renew_it_once(self, twitch_api):
        twitch_auth.save(account_token(twitch_api, runs_out_in=60))
        keeper = twitch_auth.TokenKeeper(CLIENT_ID, SECRET)

        first, second = await asyncio.gather(keeper.token(), keeper.token())

        assert first == second
        assert len(twitch_api.calls("POST", "/oauth2/token")) == 1


class TestLoginScript:
    async def test_it_shows_the_code_and_keeps_the_account_once_confirmed(self, twitch_api, capsys):
        twitch_api.sign_in_will_happen(FakeAccount("someone", "4242"))

        await twitch_login.main()

        output = capsys.readouterr().out
        assert "confirm the code ABCDEFGH" in output
        assert "Signed in as someone." in output
        assert twitch_auth.load().login == "someone"
        assert twitch_auth.load().may(twitch_auth.WRITE_SCOPE) is True

    async def test_without_twitch_set_up_it_says_what_is_missing(self, kick_api):
        with pytest.raises(SystemExit, match="TWITCH_CLIENT_ID"):
            await twitch_login.main()

    async def test_a_code_turned_down_keeps_nothing(self, twitch_api):
        twitch_api.sign_in_will_happen(denied=True)

        with pytest.raises(SystemExit, match="did not sign the account in"):
            await twitch_login.main()

        assert twitch_auth.load() is None
