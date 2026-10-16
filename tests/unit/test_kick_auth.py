"""kick_auth.py: the Kick account the bot writes in chat as - signed in
through Kick's OAuth 2.1 flow with PKCE, kept, renewed before it runs out.

Everything goes through the real code against `FakeKickApi`, whose consent
page hands out a code for the challenge the browser was sent with, and
whose token endpoint takes it only with the secret that challenge was made
of - as Kick does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kick_clip_hunter import account_tokens, kick_auth
from kick_clip_hunter.account_tokens import SignInNeeded
from tests.support.bot import kick_bot_signed_in
from tests.support.kick_api import CLIENT_ID, CLIENT_SECRET, FakeKickAccount

pytestmark = pytest.mark.anyio

REDIRECT = "http://localhost:8000/bot/kick/signed_in"


class TestStartingASignIn:
    def test_the_browser_is_sent_to_kicks_consent_page_with_a_challenge_made_of_a_secret(self):
        address, sign_in = kick_auth.start_sign_in(CLIENT_ID, REDIRECT, clock=lambda: 100.0)

        url = httpx.URL(address)
        assert (url.scheme, url.host, url.path) == ("https", "id.kick.com", "/oauth/authorize")
        assert dict(url.params) == {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "scope": "chat:write user:read",
            "code_challenge": kick_auth.challenge_of(sign_in.verifier),
            "code_challenge_method": "S256",
            "state": sign_in.state,
        }
        assert sign_in.verifier not in address
        assert sign_in.started_at == 100.0
        # Spaces as %20, which no reading of the address takes for anything else.
        assert "scope=chat%3Awrite%20user%3Aread" in address

    def test_every_sign_in_has_a_state_and_a_secret_of_its_own(self):
        (_, one), (_, other) = (
            kick_auth.start_sign_in(CLIENT_ID, REDIRECT),
            kick_auth.start_sign_in(CLIENT_ID, REDIRECT),
        )

        assert one.state != other.state and one.verifier != other.verifier
        # What PKCE allows a verifier to be.
        assert 43 <= len(one.verifier) <= 128

    def test_the_challenge_is_pkces(self):
        # The example of RFC 7636, appendix B.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

        assert kick_auth.challenge_of(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    def test_a_redirect_url_on_127_0_0_1_has_one_before_it_for_kicks_page_to_turn(self):
        address, _ = kick_auth.start_sign_in(CLIENT_ID, "http://127.0.0.1:8000/bot/kick/signed_in")

        names = [name for name, _value in httpx.URL(address).params.multi_items()]
        assert names[0] == "redirect"
        assert httpx.URL(address).params["redirect_uri"] == "http://127.0.0.1:8000/bot/kick/signed_in"

    def test_one_not_back_in_time_is_gone_by(self):
        _, sign_in = kick_auth.start_sign_in(CLIENT_ID, REDIRECT, clock=lambda: 0.0)

        assert not sign_in.gone_by(kick_auth.SIGN_IN_SECONDS)
        assert sign_in.gone_by(kick_auth.SIGN_IN_SECONDS + 1)


class TestFinishingIt:
    async def test_the_code_and_the_secret_are_exchanged_for_the_accounts_tokens(self, kick_api):
        address, sign_in = kick_auth.start_sign_in(CLIENT_ID, REDIRECT)
        back = kick_api.consent(address, FakeKickAccount("my_bot", 4242))

        token = await kick_auth.finish_sign_in(CLIENT_ID, CLIENT_SECRET, REDIRECT, sign_in, back.params["code"])

        assert (token.login, token.user_id) == ("my_bot", "4242")
        assert token.scopes == ("chat:write", "user:read")
        assert token.may(kick_auth.WRITE_SCOPE) is True
        assert token.access_token in kick_api.user_tokens
        assert token.expires_at > datetime.now(timezone.utc) + timedelta(hours=1)
        (asked,) = kick_api.calls("POST", "id.kick.com/oauth/token")
        assert asked["grant_type"] == "authorization_code"
        assert asked["redirect_uri"] == REDIRECT

    async def test_a_code_is_no_good_without_the_secret_its_challenge_was_made_of(self, kick_api):
        address, _sign_in = kick_auth.start_sign_in(CLIENT_ID, REDIRECT)
        back = kick_api.consent(address)
        _, another = kick_auth.start_sign_in(CLIENT_ID, REDIRECT)

        with pytest.raises(SignInNeeded, match="Code verifier mismatch"):
            await kick_auth.finish_sign_in(CLIENT_ID, CLIENT_SECRET, REDIRECT, another, back.params["code"])

    async def test_a_code_works_once(self, kick_api):
        address, sign_in = kick_auth.start_sign_in(CLIENT_ID, REDIRECT)
        code = kick_api.consent(address).params["code"]
        await kick_auth.finish_sign_in(CLIENT_ID, CLIENT_SECRET, REDIRECT, sign_in, code)

        with pytest.raises(SignInNeeded, match="Kick did not sign the account in"):
            await kick_auth.finish_sign_in(CLIENT_ID, CLIENT_SECRET, REDIRECT, sign_in, code)

    async def test_an_account_kick_does_not_say_who_it_is_of_is_no_sign_in(self, kick_api):
        # The Kick app lets it write, but not say who it is.
        address, sign_in = kick_auth.start_sign_in(CLIENT_ID, REDIRECT)
        code = kick_api.consent(address, scopes=("chat:write",)).params["code"]

        with pytest.raises(SignInNeeded, match="did not say whose account signed in"):
            await kick_auth.finish_sign_in(CLIENT_ID, CLIENT_SECRET, REDIRECT, sign_in, code)

    async def test_kick_not_answering_is_not_a_sign_in_refused(self, kick_api):
        kick_api.failures["oauth/token"] = 503
        _address, sign_in = kick_auth.start_sign_in(CLIENT_ID, REDIRECT)

        with pytest.raises(httpx.HTTPStatusError):
            await kick_auth.finish_sign_in(CLIENT_ID, CLIENT_SECRET, REDIRECT, sign_in, "code-1")


class TestRenewing:
    async def test_a_token_is_renewed_with_its_refresh_token_which_is_then_spent(self, kick_api):
        token = kick_bot_signed_in(kick_api, FakeKickAccount("my_bot", 4242))

        renewed = await kick_auth.renew(token, CLIENT_ID, CLIENT_SECRET)

        assert renewed.access_token != token.access_token
        assert renewed.refresh_token != token.refresh_token
        assert (renewed.login, renewed.user_id, renewed.scopes) == ("my_bot", "4242", token.scopes)
        with pytest.raises(SignInNeeded, match="sign-in on Kick has lapsed"):
            await kick_auth.renew(token, CLIENT_ID, CLIENT_SECRET)

    async def test_the_keeper_renews_one_about_to_run_out_and_keeps_it_where_the_bot_finds_it(self, kick_api):
        token = kick_bot_signed_in(kick_api, runs_out_in=60)
        keeper = kick_auth.TokenKeeper(CLIENT_ID, CLIENT_SECRET)

        renewed = await keeper.token()

        assert renewed.access_token != token.access_token
        assert account_tokens.load(kick_auth.TOKEN_FILE) == renewed
        assert kick_auth.TOKEN_FILE.as_posix() == "data/kick_bot_token.json"

    async def test_without_an_account_the_keeper_says_so(self, kick_api):
        keeper = kick_auth.TokenKeeper(CLIENT_ID, CLIENT_SECRET)

        with pytest.raises(SignInNeeded, match="No Kick account has signed in for the bot to write as"):
            await keeper.token()

    @pytest.mark.parametrize(("given", "seconds"), [(7200, 7200), ("3600", 3600), ("", 3600), (None, 3600)])
    async def test_how_long_a_token_lasts_is_read_however_kick_writes_it(self, kick_api, monkeypatch, given, seconds):
        # Kick's documentation writes it as a string.
        token = kick_bot_signed_in(kick_api)
        real = kick_api._user_token

        def answered_so(body):
            answer = real(body)
            payload = answer.json()
            payload["expires_in"] = given
            return httpx.Response(answer.status_code, json=payload)

        monkeypatch.setattr(kick_api, "_user_token", answered_so)
        before = datetime.now(timezone.utc)

        renewed = await kick_auth.renew(token, CLIENT_ID, CLIENT_SECRET)

        lasts = (renewed.expires_at - before).total_seconds()
        assert seconds - 5 <= lasts <= seconds + 5
