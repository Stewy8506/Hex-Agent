"""Teams control-plane client, account refresh, and CLI regressions."""

from __future__ import annotations

import argparse
import base64
import io
import json
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from cloud.auth_client import HexAuthClient
from cloud.session import KEYCHAIN_SERVICE, SESSION_ACCOUNT, CloudAccountError, CloudAccountManager, MemorySecretStore
from harness_core.client.cli import build_parser
from harness_core.client.commands.team import run_team_command


BASE_URL = "https://backend.example"
TOKEN = "access-token"
SIGNED_TURN = {
    "bundle": {"project_session_id": "proj_1", "host_epoch": 1, "turn_seq": 1},
    "author_device_record_id": "dev_author",
    "protocol_version": 1,
    "signature": "ed25519:test",
}


def _response(payload: object) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    return response


def _jwt(expires_at: int) -> str:
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return f"{encode({'alg': 'EdDSA'})}.{encode({'exp': expires_at, 'sub': 'usr_1'})}.sig"


class TeamClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = HexAuthClient(BASE_URL)

    def test_team_operations_use_authenticated_backend_contract(self) -> None:
        cases = [
            ("create", lambda: self.client.create_team(TOKEN, " Core "), "POST", "/v1/teams", {"name": "Core"}, {"team_id": "team_1"}),
            ("list", lambda: self.client.list_teams(TOKEN), "GET", "/v1/teams", None, {"teams": [{"team_id": "team_1"}]}, "teams"),
            ("invite", lambda: self.client.create_team_invite(TOKEN, "team_1", " DEV@example.com ", "admin"), "POST", "/v1/teams/team_1/invites", {"email": "dev@example.com", "role": "admin"}, {"invite_token": "invite-secret"}),
            ("accept", lambda: self.client.accept_team_invite(TOKEN, "hex_inv_test"), "POST", "/v1/teams/invites/accept", {"invite_token": "hex_inv_test"}, {"team_id": "team_1"}),
            ("members", lambda: self.client.list_team_members(TOKEN, "team_1"), "GET", "/v1/teams/team_1/members", None, {"members": [{"user_id": "usr_1"}]}, "members"),
            ("role", lambda: self.client.update_team_member_role(TOKEN, "team_1", "usr_2", "observer"), "PATCH", "/v1/teams/team_1/members/usr_2", {"role": "observer"}, {"role": "observer"}),
            ("session-create", lambda: self.client.create_project_session(TOKEN, "team_1", " App ", "dev_1", "repo_sha"), "POST", "/v1/teams/team_1/sessions", {"name": "App", "primary_device_record_id": "dev_1", "repo_fingerprint": "repo_sha"}, {"session": {"project_session_id": "proj_1"}}),
            ("sessions", lambda: self.client.list_project_sessions(TOKEN, "team_1"), "GET", "/v1/teams/team_1/sessions", None, {"sessions": [{"session": {"project_session_id": "proj_1"}}]}, "sessions"),
            ("lease", lambda: self.client.get_session_lease(TOKEN, "proj_1"), "GET", "/v1/sessions/proj_1/lease", None, {"payload": {"host_epoch": 1}}),
            ("turn", lambda: self.client.commit_session_turn(TOKEN, "proj_1", "dev_1", SIGNED_TURN), "POST", "/v1/sessions/proj_1/turn-commit", {"coordinator_device_record_id": "dev_1", "signed_bundle": SIGNED_TURN}, {"payload": {"turn_seq": 1}}),
            ("failover-failed", lambda: self.client.failover_project_session(TOKEN, "proj_1", failed_device_id="dev_1"), "POST", "/v1/sessions/proj_1/failover", {"failed_device_id": "dev_1"}, {"payload": {"host_epoch": 2}}),
            ("failover-target", lambda: self.client.failover_project_session(TOKEN, "proj_1", target_device_id="dev_2"), "POST", "/v1/sessions/proj_1/failover", {"target_device_id": "dev_2"}, {"payload": {"host_epoch": 2}}),
        ]
        for case in cases:
            label, operation, method, path, body, response, *result_key = case
            with self.subTest(label=label), patch("cloud.auth_client.urllib.request.urlopen", return_value=_response(response)) as urlopen:
                result = operation()
                expected = response[result_key[0]] if result_key else response
                self.assertEqual(result, expected)
                request = urlopen.call_args.args[0]
                self.assertEqual(request.full_url, BASE_URL + path)
                self.assertEqual(request.get_method(), method)
                self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
                actual_body = json.loads(request.data) if request.data is not None else None
                self.assertEqual(actual_body, body)

    def test_team_validation_rejects_invalid_roles_names_and_paths(self) -> None:
        invalid = [
            lambda: self.client.create_team(TOKEN, "  "),
            lambda: self.client.create_team_invite(TOKEN, "team_1", "", "developer"),
            lambda: self.client.create_team_invite(TOKEN, "team_1", "dev@example.com", "owner"),
            lambda: self.client.update_team_member_role(TOKEN, "team_1", "usr_2", "owner"),
            lambda: self.client.create_project_session(TOKEN, "team_1", "", "dev_1"),
            lambda: self.client.accept_team_invite(TOKEN, ""),
            lambda: self.client.commit_session_turn(TOKEN, "../proj_1", "dev_1", SIGNED_TURN),
            lambda: self.client.commit_session_turn(TOKEN, "proj_1", "dev/1", SIGNED_TURN),
            lambda: self.client.commit_session_turn(TOKEN, "proj_1", "dev_1", []),
            lambda: self.client.create_team_invite(TOKEN, "team_1?x=1", "dev@example.com"),
            lambda: self.client.update_team_member_role(TOKEN, "team_1", "usr_2#fragment", "developer"),
            lambda: self.client.create_project_session(TOKEN, "team_1", "App", "dev%2f1"),
            lambda: self.client.get_session_lease(TOKEN, "proj_1/lease"),
            lambda: self.client.failover_project_session(TOKEN, "proj_1"),
            lambda: self.client.failover_project_session(TOKEN, "proj_1", failed_device_id="dev_1", target_device_id="dev_2"),
        ]
        for index, operation in enumerate(invalid):
            with self.subTest(index=index), patch("cloud.auth_client.urllib.request.urlopen", return_value=_response({"teams": []})) as urlopen:
                with self.assertRaises(ValueError):
                    operation()
                urlopen.assert_not_called()


class TeamAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemorySecretStore()
        self.manager = CloudAccountManager(base_url=BASE_URL, store=self.store)
        self.manager._write_blob(SESSION_ACCOUNT, {
            "access_token": _jwt(int(time.time()) + 3600),
            "refresh_token": "refresh-token",
            "email": "dev@example.com",
            "user_id": "usr_1",
            "device_record_id": "dev_1",
            "saved_at": int(time.time()),
        })

    def test_account_wrappers_share_refresh_and_signed_turn_contract(self) -> None:
        client = MagicMock()
        client.list_teams.return_value = [{"team_id": "team_1"}]
        client.create_team.return_value = {"team_id": "team_1"}
        client.create_team_invite.return_value = {"invite_token": "invite-secret"}
        client.accept_team_invite.return_value = {"team_id": "team_1"}
        client.list_team_members.return_value = [{"user_id": "usr_1"}]
        client.update_team_member_role.return_value = {"role": "observer"}
        client.create_project_session.return_value = {"session": {"project_session_id": "proj_1"}}
        client.list_project_sessions.return_value = []
        client.get_session_lease.return_value = {"payload": {"host_epoch": 1}}
        client.failover_project_session.return_value = {"payload": {"host_epoch": 2}}
        client.commit_session_turn.return_value = {"payload": {"turn_seq": 1}}
        with patch("cloud.session.HexAuthClient", return_value=client):
            self.manager.create_team("Core")
            self.manager.list_teams()
            self.manager.create_team_invite("team_1", "dev@example.com", "observer")
            self.manager.accept_team_invite("hex_inv_test")
            self.manager.list_team_members("team_1")
            self.manager.update_team_member_role("team_1", "usr_2", "observer")
            self.manager.create_project_session("team_1", "App", "dev_1", "repo_sha")
            self.manager.list_project_sessions("team_1")
            self.manager.get_session_lease("proj_1")
            self.manager.failover_project_session("proj_1", target_device_id="dev_2")
            self.manager.commit_session_turn("proj_1", "dev_1", SIGNED_TURN)
        token = json.loads(self.store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT))["access_token"]
        client.create_team.assert_called_once_with(token, "Core")
        client.list_teams.assert_called_once_with(token)
        client.create_team_invite.assert_called_once_with(token, "team_1", "dev@example.com", "observer")
        client.accept_team_invite.assert_called_once_with(token, "hex_inv_test")
        client.list_team_members.assert_called_once_with(token, "team_1")
        client.update_team_member_role.assert_called_once_with(token, "team_1", "usr_2", "observer")
        client.create_project_session.assert_called_once_with(token, "team_1", "App", "dev_1", "repo_sha")
        client.list_project_sessions.assert_called_once_with(token, "team_1")
        client.get_session_lease.assert_called_once_with(token, "proj_1")
        client.failover_project_session.assert_called_once_with(token, "proj_1", failed_device_id=None, target_device_id="dev_2")
        client.commit_session_turn.assert_called_once_with(token, "proj_1", "dev_1", SIGNED_TURN)

    def test_team_request_retries_once_after_token_refresh(self) -> None:
        unauthorized = urllib.error.HTTPError(BASE_URL, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"expired"}'))
        with patch("cloud.auth_client.urllib.request.urlopen", side_effect=[
            unauthorized,
            _response({"access_token": "refreshed-token", "refresh_token": "rotated-refresh"}),
            _response({"teams": [{"team_id": "team_1"}]}),
        ]) as urlopen:
            result = self.manager.list_teams()
        self.assertEqual(result, [{"team_id": "team_1"}])
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual([request.full_url for request in requests], [
            BASE_URL + "/v1/teams",
            BASE_URL + "/v1/auth/refresh",
            BASE_URL + "/v1/teams",
        ])
        self.assertEqual(requests[-1].get_header("Authorization"), "Bearer refreshed-token")
        session = json.loads(self.store.get(KEYCHAIN_SERVICE, SESSION_ACCOUNT))
        self.assertEqual(session["refresh_token"], "rotated-refresh")

    def test_team_request_requires_signed_in_account(self) -> None:
        self.store.delete(KEYCHAIN_SERVICE, SESSION_ACCOUNT)
        with self.assertRaises(CloudAccountError):
            self.manager.list_teams()


class TeamCommandTests(unittest.TestCase):
    def test_parser_registers_all_team_operations(self) -> None:
        parser = build_parser()
        cases = [
            (["team"], "list"),
            (["team", "--json"], "list"),
            (["team", "--json", "list"], "list"),
            (["team", "create", "--name", "Core"], "create"),
            (["team", "invite", "--team", "team_1", "--email", "dev@example.com", "--role", "observer"], "invite"),
            (["team", "accept", "--token", "hex_inv_test"], "accept"),
            (["team", "members", "--team", "team_1"], "members"),
            (["team", "role", "--team", "team_1", "--user", "usr_2", "--role", "admin"], "role"),
            (["team", "sessions", "--team", "team_1"], "sessions"),
            (["team", "session-create", "--team", "team_1", "--name", "App", "--device", "dev_1"], "session-create"),
            (["team", "lease", "--session", "proj_1"], "lease"),
            (["team", "failover", "--session", "proj_1", "--target-device", "dev_2"], "failover"),
        ]
        for argv, command in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(args.cmd, "team")
                self.assertEqual(args.team_cmd, command)
        self.assertTrue(parser.parse_args(["team", "--json"]).json)
        self.assertTrue(parser.parse_args(["team", "--json", "list"]).json)
        self.assertTrue(parser.parse_args(["team", "list", "--json"]).json)

    def test_cli_json_and_human_output_and_validation(self) -> None:
        manager = MagicMock()
        manager.list_teams.return_value = [{"team_id": "team_1", "name": "Core", "role": "admin"}]
        manager.create_team_invite.return_value = {
            "team_id": "team_1", "invited_email": "dev@example.com", "role": "developer",
            "invite_token": "invite-secret",
        }
        manager.list_project_sessions.return_value = [{
            "session": {"project_session_id": "proj_1", "name": "App"},
            "signed_lease": {"payload": {"host_epoch": 2}},
        }]
        manager.failover_project_session.return_value = {"payload": {"host_epoch": 2}}
        with patch("cloud.session.CloudAccountManager", return_value=manager):
            output = io.StringIO()
            with redirect_stdout(output):
                code = run_team_command(argparse.Namespace(team_cmd="list", json=True, base_url=BASE_URL))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue()), {"teams": manager.list_teams.return_value})

            output = io.StringIO()
            with redirect_stdout(output):
                code = run_team_command(argparse.Namespace(team_cmd="invite", team="team_1", email="dev@example.com", role="developer", json=False, base_url=BASE_URL))
            self.assertEqual(code, 0)
            self.assertIn("invite-secret", output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                code = run_team_command(argparse.Namespace(team_cmd="sessions", team="team_1", json=False, base_url=BASE_URL))
            self.assertEqual(code, 0)
            self.assertIn("proj_1  App  epoch 2", output.getvalue())

            errors = io.StringIO()
            with redirect_stderr(errors):
                code = run_team_command(argparse.Namespace(team_cmd="failover", session="proj_1", failed_device=None, target_device=None, json=False, base_url=BASE_URL))
            self.assertEqual(code, 2)
            self.assertIn("exactly one", errors.getvalue())
            manager.failover_project_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
