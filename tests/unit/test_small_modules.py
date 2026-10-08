"""The small supporting modules: configuration, time display, the Windows
console fix, the logging setup, and the bits of the browser-driven clip
publisher that can be checked without a browser."""

from __future__ import annotations

import ctypes
import dataclasses
import logging
import logging.handlers
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kick_clip_hunter import clip_creator, config, kick_session, main, timeutil, win_console


class TestSettings:
    def test_credentials_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("KICK_CLIENT_ID", "an-id")
        monkeypatch.setenv("KICK_CLIENT_SECRET", "a-secret")

        settings = config.load_settings()

        assert (settings.kick_client_id, settings.kick_client_secret) == ("an-id", "a-secret")

    @pytest.mark.parametrize("missing", ["KICK_CLIENT_ID", "KICK_CLIENT_SECRET"])
    def test_a_missing_credential_is_named_in_the_error(self, monkeypatch, missing):
        monkeypatch.delenv(missing)

        with pytest.raises(KeyError, match=missing):
            config.load_settings()

    def test_settings_cannot_be_changed_after_loading(self):
        settings = config.load_settings()

        with pytest.raises(dataclasses.FrozenInstanceError):
            settings.kick_client_secret = "something else"


class TestLocalTime:
    FORMAT = "%Y-%m-%d %H:%M:%S"

    def test_shows_a_utc_timestamp_in_the_machines_time_zone(self):
        moment = datetime(2026, 3, 1, 20, 15, 30, tzinfo=timezone.utc)

        assert timeutil.to_local(moment.isoformat()) == moment.astimezone().strftime(self.FORMAT)

    def test_accepts_the_z_suffix_sqlite_writes(self):
        # The default of every *_at column is strftime('...Z', 'now').
        assert timeutil.to_local("2026-03-01T20:15:30.123Z") == timeutil.to_local("2026-03-01T20:15:30.123+00:00")

    def test_the_same_instant_reads_the_same_whatever_offset_it_was_stored_with(self):
        assert timeutil.to_local("2026-03-01T22:15:30+02:00") == timeutil.to_local("2026-03-01T20:15:30+00:00")

    def test_an_hour_later_reads_an_hour_later(self):
        earlier = datetime.strptime(timeutil.to_local("2026-03-01T20:00:00+00:00"), self.FORMAT)
        later = datetime.strptime(timeutil.to_local("2026-03-01T21:00:00+00:00"), self.FORMAT)

        assert later - earlier == timedelta(hours=1)


class FakeConsole:
    """Stands in for kernel32's console functions."""

    QUICK_EDIT = 0x0040
    EXTENDED_FLAGS = 0x0080

    def __init__(self, mode: int | None, set_succeeds: bool = True) -> None:
        self.mode = mode  # None: standard input is not a console
        self.set_succeeds = set_succeeds
        self.set_calls: list[int] = []

    def GetStdHandle(self, which):
        assert which == -10, "must ask for the standard *input* handle"
        return 1234

    def GetConsoleMode(self, handle, mode_reference):
        if self.mode is None:
            return 0
        mode_reference._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle, mode):
        self.set_calls.append(mode)
        if self.set_succeeds:
            self.mode = mode
        return int(self.set_succeeds)


class TestQuickEdit:
    """A click in a Windows console with QuickEdit on blocks every write to
    it - which froze the whole service until a key was pressed."""

    @pytest.fixture
    def console(self, monkeypatch):
        def install(mode, **kwargs) -> FakeConsole:
            fake = FakeConsole(mode, **kwargs)
            monkeypatch.setattr(sys, "platform", "win32")
            monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=fake), raising=False)
            return fake

        return install

    @pytest.fixture
    def at_exit(self, monkeypatch) -> list:
        registered: list = []
        monkeypatch.setattr(win_console.atexit, "register", lambda function, *args: registered.append((function, args)))
        return registered

    def test_turns_quick_edit_off_and_leaves_the_other_modes_alone(self, console, at_exit):
        other_modes = 0x0001 | 0x0004 | 0x0100
        fake = console(other_modes | FakeConsole.QUICK_EDIT)

        assert win_console.disable_quick_edit() is True

        assert fake.mode & FakeConsole.QUICK_EDIT == 0
        assert fake.mode & other_modes == other_modes
        # Windows ignores a QuickEdit change unless this flag accompanies it.
        assert fake.mode & FakeConsole.EXTENDED_FLAGS

    def test_puts_the_original_mode_back_when_the_process_exits(self, console, at_exit):
        original = 0x0001 | FakeConsole.QUICK_EDIT
        fake = console(original)
        win_console.disable_quick_edit()

        ((restore, arguments),) = at_exit
        restore(*arguments)

        assert fake.mode == original

    def test_does_nothing_when_quick_edit_is_already_off(self, console, at_exit):
        fake = console(0x0001 | FakeConsole.EXTENDED_FLAGS)

        assert win_console.disable_quick_edit() is False
        assert fake.set_calls == [] and at_exit == []

    def test_does_nothing_without_a_console(self, console, at_exit):
        # Redirected input, a service, CI.
        fake = console(None)

        assert win_console.disable_quick_edit() is False
        assert fake.set_calls == [] and at_exit == []

    def test_reports_failure_if_windows_refuses_the_change(self, console, at_exit):
        fake = console(FakeConsole.QUICK_EDIT, set_succeeds=False)

        assert win_console.disable_quick_edit() is False
        assert fake.mode == FakeConsole.QUICK_EDIT and at_exit == []

    def test_is_a_no_op_on_other_platforms(self, monkeypatch, at_exit):
        monkeypatch.setattr(sys, "platform", "linux")

        assert win_console.disable_quick_edit() is False
        assert at_exit == []


def log_record(message: str, error: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(error), error, None) if error is not None else None
    return logging.LogRecord("asyncio", logging.ERROR, __file__, 1, message, None, exc_info)


class TestLogNoiseFilter:
    NOISE = "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)"

    @pytest.fixture
    def noise_filter(self) -> logging.Filter:
        return main._DropConnectionResetNoise()

    def test_drops_the_harmless_connection_reset_traceback(self, noise_filter):
        record = log_record(self.NOISE, ConnectionResetError(10054, "connection forcibly closed by the remote host"))

        assert noise_filter.filter(record) is False

    def test_keeps_any_other_error_from_the_same_callback(self, noise_filter):
        assert noise_filter.filter(log_record(self.NOISE, OSError("disk on fire"))) is True

    def test_keeps_a_connection_reset_reported_from_anywhere_else(self, noise_filter):
        record = log_record("Exception in callback fetch_playlist()", ConnectionResetError(10054, "reset"))

        assert noise_filter.filter(record) is True

    def test_keeps_messages_that_carry_no_exception(self, noise_filter):
        assert noise_filter.filter(log_record(self.NOISE)) is True

    def test_is_installed_on_the_logger_asyncio_reports_through(self):
        assert any(isinstance(f, main._DropConnectionResetNoise) for f in logging.getLogger("asyncio").filters)


class TestLogFile:
    @pytest.fixture(autouse=True)
    def info_level(self, caplog):
        # In production main.py's basicConfig sets the root logger to INFO.
        # Under pytest the root logger already has pytest's own handlers by
        # the time the app is imported, which makes basicConfig a no-op.
        caplog.set_level(logging.INFO, logger="kick_clip_hunter")

    def app_log_handler(self) -> logging.handlers.RotatingFileHandler:
        (handler,) = [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.FileHandler) and Path(h.baseFilename).name == main.LOG_FILE.name
        ]
        return handler

    def test_the_service_log_goes_to_a_file_as_well_as_the_console(self):
        handler = self.app_log_handler()

        main.logger.info("log file check %s", "marker-4f1c")
        handler.flush()

        last_line = Path(handler.baseFilename).read_text(encoding="utf-8").splitlines()[-1]
        assert last_line.endswith("INFO log file check marker-4f1c")
        # "2026-03-01 20:15:30 INFO ..." - sortable, same as on the console.
        datetime.strptime(last_line[:19], "%Y-%m-%d %H:%M:%S")

    def test_the_file_is_rotated_so_it_cannot_fill_the_disk(self):
        handler = self.app_log_handler()

        assert handler.maxBytes == main.LOG_FILE_MAX_BYTES > 0
        assert handler.backupCount == main.LOG_FILE_BACKUPS > 0

    def test_non_ascii_chat_survives_the_trip_to_the_file(self):
        handler = self.app_log_handler()

        main.logger.info("[some_channel] alice: žluťoučký kůň 🐴")
        handler.flush()

        assert "žluťoučký kůň 🐴" in Path(handler.baseFilename).read_text(encoding="utf-8")

    def test_per_request_http_client_logging_is_silenced(self):
        # Playlist polling would otherwise write a multi-kilobyte URL every
        # couple of seconds.
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


class TestClipPublisher:
    """clip_creator.py drives a browser; the part that reads the livestream
    out of the channel page's HTML is plain parsing."""

    # How the page embeds it: JSON inside a JavaScript string, quotes escaped.
    PAGE = (
        'self.__next_f.push([1,"...\\"channel\\":{\\"id\\":77,\\"slug\\":\\"some_channel\\"},'
        '\\"livestream\\":{\\"id\\":98765432,\\"slug\\":\\"236fdbf1-some-slugified-title\\",\\"is_live\\":true}..."])'
    )

    def test_finds_the_current_livestreams_id_and_slug(self):
        assert clip_creator._extract_livestream(self.PAGE) == (98765432, "236fdbf1-some-slugified-title")

    def test_takes_the_livestream_not_the_channel(self):
        livestream_id, slug = clip_creator._extract_livestream(self.PAGE)

        assert livestream_id != 77 and slug != "some_channel"

    @pytest.mark.parametrize(
        "html",
        ["", "<html><body>offline</body></html>", '\\"livestream\\":null', '"livestream":{"id":1,"slug":"unescaped"}'],
    )
    def test_a_page_without_a_live_stream_is_an_error(self, html):
        with pytest.raises(clip_creator.ClipCreationError, match="is it live"):
            clip_creator._extract_livestream(html)

    def test_the_clip_source_buffer_matches_what_the_site_requests(self):
        assert clip_creator.SOURCE_BUFFER_SECONDS == 180


class TestBrowserSession:
    def test_a_saved_login_is_detected_by_its_state_file(self):
        assert kick_session.has_saved_session() is False

        kick_session.SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        kick_session.SESSION_STATE_PATH.write_text("{}")

        assert kick_session.has_saved_session() is True
