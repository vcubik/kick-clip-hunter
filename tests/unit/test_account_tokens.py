"""account_tokens.py: what is kept of an account the service acts as - on
Twitch to read chat and write in it, on Kick to write in it - and the keeper
that renews its token before it runs out.

Each platform's own sign-in and renewal is tested with it
(test_twitch_auth.py, test_kick_auth.py). Here is what they share, with a
renewal handed in.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from kick_clip_hunter import account_tokens
from kick_clip_hunter.account_tokens import AccountToken, SignInNeeded

pytestmark = pytest.mark.anyio

NOW = datetime.now(timezone.utc)


def token(name: str = "someone", runs_out_in: float = 3600, scopes: tuple[str, ...] | None = ("chat:write",)):
    return AccountToken(
        f"access-{name}",
        f"refresh-{name}",
        datetime.now(timezone.utc) + timedelta(seconds=runs_out_in),
        "7",
        name,
        scopes,
    )


class TestWhatIsKept:
    def test_what_the_account_let_the_service_do_is_read_back_with_it(self, tmp_path):
        kept = token(scopes=("chat:write", "user:read"))

        account_tokens.save(kept, tmp_path / "token.json")

        assert account_tokens.load(tmp_path / "token.json") == kept

    def test_a_token_kept_before_that_was_says_nothing_of_it(self, tmp_path):
        older = {"access_token": "a", "refresh_token": "r", "expires_at": NOW.isoformat(), "user_id": "7", "login": "x"}
        (tmp_path / "token.json").write_text(json.dumps(older), encoding="utf-8")

        read = account_tokens.load(tmp_path / "token.json")

        assert (read.login, read.scopes) == ("x", None)
        assert read.may("chat:write") is None

    def test_it_says_whether_the_account_let_it_do_something(self):
        assert token(scopes=("chat:write",)).may("chat:write") is True
        assert token(scopes=("user:read",)).may("chat:write") is False

    @pytest.mark.parametrize(
        ("given", "read"),
        [
            ("chat:write user:read", ("chat:write", "user:read")),
            (["user:read:chat", "user:write:chat"], ("user:read:chat", "user:write:chat")),
            ("", ()),
            (None, None),
            ([1, 2], None),
            ({"scope": "x"}, None),
        ],
    )
    def test_what_a_platform_says_a_token_may_do_is_read_either_way_it_says_it(self, given, read):
        assert account_tokens.scopes_of(given) == read


class TestTheKeeper:
    @pytest.fixture
    def renewals(self) -> list[AccountToken]:
        return []

    def keeper(self, tmp_path, renewals: list[AccountToken]) -> account_tokens.TokenKeeper:
        async def renew(old: AccountToken) -> AccountToken:
            renewals.append(old)
            return token(f"{old.login}+", scopes=old.scopes)

        return account_tokens.TokenKeeper(tmp_path / "token.json", renew, "Nobody has signed in here.")

    async def test_without_an_account_it_says_so_in_its_own_words(self, tmp_path, renewals):
        keeper = self.keeper(tmp_path, renewals)

        with pytest.raises(SignInNeeded, match="Nobody has signed in here"):
            await keeper.token()

        assert (keeper.trouble, keeper.account) == ("Nobody has signed in here.", None)

    async def test_a_token_about_to_run_out_is_renewed_as_it_was_told_and_kept(self, tmp_path, renewals):
        account_tokens.save(token(runs_out_in=60), tmp_path / "token.json")
        keeper = self.keeper(tmp_path, renewals)

        renewed = await keeper.token()

        assert [old.login for old in renewals] == ["someone"]
        assert renewed.login == "someone+"
        assert account_tokens.load(tmp_path / "token.json") == renewed
        assert await keeper.token() == renewed  # and only once

    async def test_tokens_it_is_handed_are_kept_and_gone_on_with(self, tmp_path, renewals):
        keeper = self.keeper(tmp_path, renewals)
        with pytest.raises(SignInNeeded):
            await keeper.token()

        keeper.keep(token("new"))

        assert (await keeper.token()).login == "new"
        assert account_tokens.load(tmp_path / "token.json").login == "new"
        assert keeper.trouble is None

    async def test_a_file_written_since_it_was_read_is_read_again(self, tmp_path, renewals):
        keeper = self.keeper(tmp_path, renewals)
        account_tokens.save(token("first"), tmp_path / "token.json")
        assert (await keeper.token()).login == "first"

        account_tokens.save(token("second", scopes=("chat:write", "more")), tmp_path / "token.json")

        assert keeper.account.login == "second"
        assert (await keeper.token()).scopes == ("chat:write", "more")
        assert renewals == []

    async def test_an_account_signed_in_while_the_last_was_being_renewed_is_not_written_over(self, tmp_path):
        account_tokens.save(token("old", runs_out_in=60), tmp_path / "token.json")

        async def renew(old: AccountToken) -> AccountToken:
            # Meanwhile, someone signs another account in - within the same
            # tick of the file system's clock, as on Windows, and as long.
            before = (tmp_path / "token.json").stat()
            account_tokens.save(token("new"), tmp_path / "token.json")
            os.utime(tmp_path / "token.json", ns=(before.st_atime_ns, before.st_mtime_ns))
            return token("old+")

        keeper = account_tokens.TokenKeeper(tmp_path / "token.json", renew, "Nobody has signed in here.")

        assert (await keeper.token()).login == "new"
        assert account_tokens.load(tmp_path / "token.json").login == "new"
        assert keeper.account.login == "new"

    async def test_one_that_cannot_be_read_changes_nothing(self, tmp_path, renewals):
        keeper = self.keeper(tmp_path, renewals)
        account_tokens.save(token("first"), tmp_path / "token.json")
        assert (await keeper.token()).login == "first"

        (tmp_path / "token.json").write_text("half a toke", encoding="utf-8")

        assert (await keeper.token()).login == "first"
