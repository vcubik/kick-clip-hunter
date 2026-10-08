"""Tests of the test suite's own safety net (tests/conftest.py).

The suite runs on a developer machine next to a real `data/` directory, real
Kick credentials in `.env` and, quite possibly, the live service. These tests
pin down the guarantees that make that safe - if one of them fails, no other
result of the run should be trusted.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from kick_clip_hunter import clip_creator, config, db, kick_stream, recorder

REPOSITORY = Path(__file__).resolve().parents[2]


def inside_repository(path: Path) -> bool:
    return REPOSITORY in path.resolve().parents or path.resolve() == REPOSITORY


class TestFilesystem:
    def test_the_working_directory_is_a_scratch_directory(self, tmp_path):
        assert Path.cwd().resolve() == tmp_path.resolve()
        assert not inside_repository(Path.cwd())

    def test_the_database_is_a_scratch_file(self, tmp_path):
        assert tmp_path.resolve() in db.DB_PATH.resolve().parents
        assert not inside_repository(db.DB_PATH)

    def test_every_data_path_resolves_outside_the_repository(self):
        for path in (recorder.RECORDINGS_DIR, recorder.CLIPS_DIR, recorder.DEBUG_PLAYLIST_DIR):
            assert not inside_repository(path)

    def test_each_test_starts_with_an_empty_database(self):
        connection = db.get_connection()
        try:
            assert connection.execute("SELECT COUNT(*) FROM moments").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM streamers").fetchone()[0] == 0
            db.add_streamer(connection, 1, "left_behind_by_this_test")
        finally:
            connection.close()

    def test_and_does_not_see_what_another_test_stored(self):
        # Companion of the test above, whichever order they run in.
        connection = db.get_connection()
        try:
            assert connection.execute("SELECT COUNT(*) FROM streamers").fetchone()[0] == 0
        finally:
            connection.close()

    def test_the_services_log_file_is_outside_the_repository(self):
        import logging

        from kick_clip_hunter import main

        handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
        log_files = [Path(h.baseFilename) for h in handlers if Path(h.baseFilename).name == main.LOG_FILE.name]
        assert log_files, "the app's log handler should be installed once the app is imported"
        assert not any(inside_repository(path) for path in log_files)

    def test_test_databases_trade_crash_safety_for_speed(self):
        connection = db.get_connection()
        try:
            assert connection.execute("PRAGMA synchronous").fetchone()[0] == 0
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
        finally:
            connection.close()


class TestCredentials:
    def test_the_service_sees_test_credentials_not_the_developers(self):
        settings = config.load_settings()

        assert settings.kick_client_id == "test-client-id"
        assert settings.kick_client_secret == "test-client-secret"

    def test_no_real_certificate_authority_is_trusted(self):
        trust_store = Path(os.environ["SSL_CERT_DIR"])

        assert trust_store.is_dir()
        assert list(trust_store.iterdir()) == []
        assert "SSL_CERT_FILE" not in os.environ


class TestForbiddenSideEffects:
    @pytest.mark.parametrize("host", ["api.kick.com", "kick.com", "7tv.io", "example.org"])
    def test_resolving_a_real_host_is_refused(self, no_outbound_network, host):
        with pytest.raises(RuntimeError, match="must stay offline"):
            socket.getaddrinfo(host, 443)

        assert no_outbound_network == [f"{host}:443"]
        no_outbound_network.clear()  # acknowledged - otherwise this test would fail at teardown

    def test_connecting_to_a_real_address_is_refused(self, no_outbound_network):
        with socket.socket() as sock, pytest.raises(RuntimeError, match="must stay offline"):
            sock.connect(("93.184.216.34", 80))

        assert no_outbound_network == ["93.184.216.34:80"]
        no_outbound_network.clear()

    def test_loopback_is_allowed(self, no_outbound_network):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.socket() as client:
                client.connect(server.getsockname())

        assert socket.getaddrinfo("127.0.0.1", 80)
        assert no_outbound_network == []

    def test_capturing_a_stream_url_cannot_open_a_browser(self, no_browser):
        with pytest.raises(RuntimeError, match="launch a browser"):
            kick_stream.get_live_stream_url("some_channel")

        assert no_browser == ["sync_playwright"]
        no_browser.clear()

    def test_publishing_a_clip_cannot_open_a_browser(self, no_browser):
        with pytest.raises(RuntimeError, match="launch a browser"):
            clip_creator.create_clip("some_channel", start_time=0)

        no_browser.clear()

    def test_exiting_the_process_is_intercepted(self, process_exits):
        os._exit(3)

        assert process_exits == [3]
