"""Where the tunnel Kick reaches the service through stands, as worked out
from what the machine's Tailscale client says.

`read` is given the client's two JSON answers as they are printed (the
shapes are in tests/support/tailscale.py); `look` runs the client, which
here is `FakeTailscale` - nothing on the machine is asked.
"""

from __future__ import annotations

import subprocess

from kick_clip_hunter import tunnel
from tests.support.tailscale import ADDRESS, HOST, serve_config

RUNNING = {"BackendState": "Running", "Health": []}


class TestReadingWhatTailscaleSays:
    def test_a_funnel_to_the_services_port_is_up(self):
        funnel = tunnel.read(RUNNING, serve_config(8000), port=8000)

        assert funnel.state == tunnel.UP
        assert funnel.address == ADDRESS
        assert funnel.target == "http://127.0.0.1:8000/webhooks/kick"

    def test_without_knowing_the_services_port_a_funnel_is_taken_to_be_right(self):
        assert tunnel.read(RUNNING, serve_config(9999)).state == tunnel.UP

    def test_a_funnel_to_another_port_is_told_apart(self):
        funnel = tunnel.read(RUNNING, serve_config(8000), port=8001)

        assert funnel.state == tunnel.OTHER_PORT
        assert (funnel.address, funnel.target) == (ADDRESS, "http://127.0.0.1:8000/webhooks/kick")

    def test_a_path_served_without_funnel_stays_inside_the_tailnet(self):
        funnel = tunnel.read(RUNNING, serve_config(8000, funnel=False), port=8000)

        assert funnel.state == tunnel.TAILNET_ONLY
        assert funnel.address == ADDRESS

    def test_funnel_switched_off_for_the_site_is_the_same(self):
        serve = serve_config(8000)
        serve["AllowFunnel"] = {f"{HOST}:443": False}

        assert tunnel.read(RUNNING, serve, port=8000).state == tunnel.TAILNET_ONLY

    def test_a_client_that_serves_nothing_has_no_route(self):
        funnel = tunnel.read(RUNNING, {})

        assert funnel == tunnel.Funnel(tunnel.NO_ROUTE)

    def test_a_funnel_for_some_other_path_is_no_route_to_the_webhook(self):
        assert tunnel.read(RUNNING, serve_config(8000, mount="/elsewhere")).state == tunnel.NO_ROUTE

    def test_a_path_that_only_begins_like_the_webhooks_is_another_path(self):
        assert tunnel.read(RUNNING, serve_config(8000, mount="/webhooks/ki")).state == tunnel.NO_ROUTE

    def test_the_whole_service_behind_the_funnel_covers_the_webhook(self):
        funnel = tunnel.read(RUNNING, serve_config(8000, mount="/"), port=8000)

        assert funnel.state == tunnel.UP
        assert (funnel.address, funnel.target) == (ADDRESS, "http://127.0.0.1:8000")

    def test_the_nearest_mount_point_is_the_one_that_answers(self):
        serve = serve_config(8000, mount="/")
        serve["Web"][f"{HOST}:443"]["Handlers"]["/webhooks"] = {"Proxy": "http://127.0.0.1:8001"}

        assert tunnel.read(RUNNING, serve, port=8000).target == "http://127.0.0.1:8001"

    def test_a_path_that_serves_files_forwards_nothing(self):
        serve = serve_config(8000)
        serve["Web"][f"{HOST}:443"]["Handlers"] = {"/webhooks/kick": {"Path": "C:/somewhere"}}

        assert tunnel.read(RUNNING, serve).state == tunnel.NO_ROUTE

    def test_a_site_on_another_port_has_it_in_its_address(self):
        funnel = tunnel.read(RUNNING, serve_config(8000, site=f"{HOST}:8443"))

        assert funnel.address == f"https://{HOST}:8443/webhooks/kick"

    def test_of_several_sites_the_one_that_works_is_the_one_reported(self):
        serve = serve_config(8001, site=f"{HOST}:8443")
        working = serve_config(8000)
        serve["Web"].update(working["Web"])
        serve["AllowFunnel"].update(working["AllowFunnel"])

        funnel = tunnel.read(RUNNING, serve, port=8000)

        assert (funnel.state, funnel.address) == (tunnel.UP, ADDRESS)

    def test_a_target_written_without_a_scheme_still_has_a_port(self):
        serve = serve_config(8000, target="127.0.0.1:8000")

        assert tunnel.read(RUNNING, serve, port=8000).state == tunnel.UP
        assert tunnel.read(RUNNING, serve, port=8001).state == tunnel.OTHER_PORT

    def test_a_target_whose_port_cannot_be_told_is_not_called_wrong(self):
        serve = serve_config(8000, target="http://127.0.0.1:not-a-port/webhooks/kick")

        assert tunnel.read(RUNNING, serve, port=8000).state == tunnel.UP

    def test_a_target_on_the_default_port_is_compared_by_it(self):
        serve = serve_config(8000, target="http://127.0.0.1/webhooks/kick")

        assert tunnel.read(RUNNING, serve, port=80).state == tunnel.UP
        assert tunnel.read(RUNNING, serve, port=8000).state == tunnel.OTHER_PORT

    def test_a_client_that_is_not_connected_says_what_it_is_instead(self):
        funnel = tunnel.read({"BackendState": "NoState"}, serve_config(8000), port=8000)

        assert funnel == tunnel.Funnel(tunnel.NOT_CONNECTED, said="NoState")

    def test_a_client_that_says_nothing_of_its_state_is_not_connected(self):
        assert tunnel.read({}, {}).state == tunnel.NOT_CONNECTED

    def test_tailscales_own_warnings_come_along(self):
        status = {"BackendState": "Running", "Health": ["Tailscale can't reach the configured DNS servers."]}

        funnel = tunnel.read(status, serve_config(8000), port=8000)

        assert funnel.state == tunnel.UP
        assert funnel.warnings == ("Tailscale can't reach the configured DNS servers.",)


class TestAskingTheClient:
    def test_it_asks_for_the_state_and_then_for_what_is_served(self, tailscale):
        funnel = tunnel.look(8000)

        assert tailscale.commands == [["status", "--json"], ["funnel", "status", "--json"]]
        assert (funnel.state, funnel.address) == (tunnel.UP, ADDRESS)

    def test_the_services_port_is_what_the_funnel_is_held_against(self, tailscale):
        assert tunnel.look(8001).state == tunnel.OTHER_PORT

    def test_a_machine_without_tailscale_is_not_installed(self):
        assert tunnel.look(8000) == tunnel.Funnel(tunnel.NOT_INSTALLED)

    def test_a_client_that_is_not_on_the_path_is_looked_for_where_windows_puts_it(self, tailscale):
        tailscale.only_as = tunnel.TAILSCALE_ON_WINDOWS

        assert tunnel.look(8000).state == tunnel.UP

    def test_a_client_that_is_not_connected_is_not_asked_what_it_serves(self, tailscale):
        tailscale.backend = "Stopped"

        funnel = tunnel.look(8000)

        assert tailscale.commands == [["status", "--json"]]
        assert (funnel.state, funnel.said) == (tunnel.NOT_CONNECTED, "Stopped")

    def test_a_command_that_fails_says_why_in_its_own_words(self, tailscale):
        tailscale.fails = subprocess.CalledProcessError(
            1, ["tailscale"], stderr="failed to connect to local Tailscale service; is Tailscale running?\nmore\n"
        )

        funnel = tunnel.look(8000)

        assert funnel.state == tunnel.NOT_ANSWERING
        assert funnel.said == "failed to connect to local Tailscale service; is Tailscale running?"

    def test_a_command_that_fails_without_a_word_says_how_it_ended(self, tailscale):
        tailscale.fails = subprocess.CalledProcessError(3, ["tailscale"], stderr="")

        assert tunnel.look(8000).said == "exit code 3"

    def test_a_client_that_does_not_answer_in_time_is_not_waited_for(self, tailscale):
        tailscale.fails = subprocess.TimeoutExpired(["tailscale"], tunnel.COMMAND_TIMEOUT_SECONDS)

        funnel = tunnel.look(8000)

        assert funnel.state == tunnel.NOT_ANSWERING
        assert funnel.said == f"no answer within {tunnel.COMMAND_TIMEOUT_SECONDS} seconds"

    def test_a_client_that_cannot_be_started_is_not_answering(self, tailscale):
        tailscale.fails = PermissionError("access denied")

        funnel = tunnel.look(8000)

        assert (funnel.state, funnel.said) == (tunnel.NOT_ANSWERING, "PermissionError")

    def test_an_answer_that_is_not_json_is_no_answer(self, tailscale):
        tailscale.prints = "Tailscale is stopped."

        assert tunnel.look(8000).state == tunnel.NOT_ANSWERING

    def test_nor_is_json_of_the_wrong_shape(self, tailscale):
        tailscale.prints = "[]"

        assert tunnel.look(8000).state == tunnel.NOT_ANSWERING
