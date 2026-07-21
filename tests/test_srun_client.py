import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import uestc_srun_autoconnect as srun_module
from uestc_srun_autoconnect import (
    PortalProtocolError,
    PortalContext,
    Settings,
    SrunClient,
    build_srun_material,
    load_env_file,
    parse_json_or_jsonp,
)


_DOMAIN_UNSET = object()
_ONLINE_STATE_UNSET = object()


def settings_from_environment(
    username="student001",
    account_domain=_DOMAIN_UNSET,
):
    environment = {
        "UESTC_USERNAME": username,
        "UESTC_PASSWORD": "test-password",
        "UESTC_ALLOW_INSECURE_HTTP": "1",
    }
    if account_domain is not _DOMAIN_UNSET:
        environment["UESTC_ACCOUNT_DOMAIN"] = account_domain
    with patch.dict(os.environ, environment, clear=True):
        return Settings.from_environment()


class SrunAlgorithmTests(unittest.TestCase):
    def test_portal_javascript_vector(self):
        token = "0123456789abcdef" * 4
        hmd5, info, checksum = build_srun_material(
            "testuser",
            "testpass",
            "10.1.2.3",
            "1",
            token,
        )
        self.assertEqual(hmd5, "40fc04063af6053bd63a578b42274cb0")
        self.assertEqual(
            info,
            "{SRBX1}5uWiKejMz2+plGClEcKSw3Z4I7w/8zy8Cu7G/RX8yB4dDeWDhsc+ovh9+"
            "lnsjS2lzOo/9QxTQjLtSDy3GGnxbCy3QIt6JZRokTYXat2j70loPvMbMxtHd9hky+"
            "dC7oz1xPde4S==",
        )
        self.assertEqual(checksum, "363a28adecec6e5b9a3cb00e34d9407ab9767712")

    def test_json_and_jsonp(self):
        self.assertEqual(parse_json_or_jsonp('{"error":"ok"}')["error"], "ok")
        self.assertEqual(
            parse_json_or_jsonp('cb({"error":"ok"});', expected_callback="cb")["error"],
            "ok",
        )
        with self.assertRaises(PortalProtocolError):
            parse_json_or_jsonp('other({"error":"ok"});', expected_callback="cb")


class OnlineStateTests(unittest.TestCase):
    @staticmethod
    def make_client(payload):
        client = SrunClient.__new__(SrunClient)
        client.logger = logging.getLogger("srun-test")
        client._request_json = lambda *args, **kwargs: payload
        return client

    def test_portal_online_is_authoritative(self):
        self.assertIs(self.make_client({"error": "ok"}).query_online_state(), True)

    def test_portal_offline_overrides_ping_assumptions(self):
        client = self.make_client({"error": "not_online_error"})
        client.http_probe_online = Mock(return_value=True)

        self.assertIs(client.is_online(), False)
        client.http_probe_online.assert_not_called()

    def test_unknown_portal_state_uses_http_probe(self):
        client = self.make_client({"error": "unexpected_portal_reply"})
        client.http_probe_online = Mock(return_value=True)

        self.assertIs(client.is_online(), True)
        client.http_probe_online.assert_called_once_with()


class AccountDomainTests(unittest.TestCase):
    def test_default_domain_is_appended_for_bare_campus_account(self):
        settings = settings_from_environment("student001")

        self.assertEqual(settings.username, "student001")
        self.assertEqual(settings.account_domain, "@dx-uestc")
        self.assertEqual(settings.portal_username, "student001@dx-uestc")

    def test_existing_domain_is_preserved(self):
        settings = settings_from_environment("student001@dx")

        self.assertEqual(settings.portal_username, "student001@dx")

    def test_explicit_empty_domain_keeps_bare_account(self):
        settings = settings_from_environment("student001", account_domain="")

        self.assertEqual(settings.account_domain, "")
        self.assertEqual(settings.portal_username, "student001")


class EnvFileRefreshTests(unittest.TestCase):
    def test_refresh_updates_loaded_value_and_clears_it_when_file_is_deleted(self):
        key = "UESTC_TEST_REFRESH_VALUE"
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / ".env"
            path.write_text(f"{key}=first\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.dict(srun_module._LOADED_ENV_VALUES, {}, clear=True),
            ):
                load_env_file(path)
                self.assertEqual(os.environ.get(key), "first")

                path.write_text(f"{key}=second\n", encoding="utf-8")
                load_env_file(path, refresh=True)
                self.assertEqual(os.environ.get(key), "second")

                path.unlink()
                load_env_file(path, refresh=True)
                self.assertNotIn(key, os.environ)
                self.assertNotIn(key, srun_module._LOADED_ENV_VALUES)

    def test_refresh_never_overwrites_real_environment_values(self):
        external_before = "UESTC_TEST_EXTERNAL_BEFORE"
        external_after = "UESTC_TEST_EXTERNAL_AFTER"
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / ".env"
            path.write_text(
                f"{external_before}=file-one\n{external_after}=file-one\n",
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {external_before: "external-before"},
                    clear=True,
                ),
                patch.dict(srun_module._LOADED_ENV_VALUES, {}, clear=True),
            ):
                load_env_file(path)
                self.assertEqual(os.environ[external_before], "external-before")
                self.assertEqual(os.environ[external_after], "file-one")

                # Simulate a real process-level override after the first file load.
                os.environ[external_after] = "external-after"
                path.write_text(
                    f"{external_before}=file-two\n{external_after}=file-two\n",
                    encoding="utf-8",
                )
                load_env_file(path, refresh=True)
                self.assertEqual(os.environ[external_before], "external-before")
                self.assertEqual(os.environ[external_after], "external-after")

                path.unlink()
                load_env_file(path, refresh=True)
                self.assertEqual(os.environ[external_before], "external-before")
                self.assertEqual(os.environ[external_after], "external-after")


class AuthenticationResultTests(unittest.TestCase):
    @staticmethod
    def make_client(login_payload, *, online_state=_ONLINE_STATE_UNSET):
        settings = settings_from_environment("student001")
        client = SrunClient(settings, logging.getLogger("srun-auth-test"))
        client.discover_portal = Mock(return_value=PortalContext(
            origin="http://aaa.uestc.edu.cn/",
            ac_id="1",
            nas_ip="10.0.0.1",
            ap_id="ap-1",
            ap_ip="10.0.0.2",
        ))

        calls = []

        def request_json(path, params, timeout=10):
            calls.append((path, dict(params), timeout))
            if path == "/cgi-bin/get_challenge":
                return {
                    "error": "ok",
                    "challenge": "0123456789abcdef" * 2,
                    "client_ip": "10.1.2.3",
                }
            if path == "/cgi-bin/srun_portal":
                return login_payload
            if path == "/cgi-bin/rad_user_info":
                return {"error": "ok"}
            raise AssertionError(f"unexpected portal path: {path}")

        client._request_json = request_json
        if online_state is not _ONLINE_STATE_UNSET:
            client.query_online_state = Mock(return_value=online_state)
        return client, calls

    def test_effective_username_is_used_for_challenge_material_and_login(self):
        client, calls = self.make_client({"error": "ok", "suc_msg": "login_ok"})

        with patch("uestc_srun_autoconnect.time.sleep", return_value=None):
            result = client.authenticate()

        self.assertTrue(result.success)
        effective_username = "student001@dx-uestc"
        challenge_params = next(
            params for path, params, _ in calls if path == "/cgi-bin/get_challenge"
        )
        login_params = next(
            params for path, params, _ in calls if path == "/cgi-bin/srun_portal"
        )
        self.assertEqual(challenge_params["username"], effective_username)
        self.assertEqual(login_params["username"], effective_username)

        hmd5, info, checksum = build_srun_material(
            effective_username,
            "test-password",
            "10.1.2.3",
            "1",
            "0123456789abcdef" * 2,
        )
        self.assertEqual(login_params["password"], "{MD5}" + hmd5)
        self.assertEqual(login_params["info"], info)
        self.assertEqual(login_params["chksum"], checksum)

    def test_known_error_codes_have_safe_retry_classification(self):
        cases = (
            (
                {"error": "fail", "ecode": "E2532", "error_msg": "Too frequent"},
                False,
                10,
            ),
            (
                {
                    "error": "fail",
                    "ecode": "E2533",
                    "error_msg": "Password attempts exceeded",
                },
                False,
                300,
            ),
            (
                {
                    "error": "fail",
                    "ecode": "E2553",
                    "error_msg": "Account or password error",
                },
                True,
                0,
            ),
            (
                {"error": "fail", "ecode": "E2806", "error_msg": "No products found"},
                True,
                0,
            ),
        )

        for payload, expected_fatal, expected_retry_after in cases:
            with self.subTest(ecode=payload["ecode"]):
                client, _ = self.make_client(payload)
                result = client.authenticate()
                self.assertFalse(result.success)
                self.assertIs(result.fatal, expected_fatal)
                self.assertEqual(result.retry_after, expected_retry_after)

    def test_e2620_is_success_only_when_online_state_confirms_it(self):
        payload = {
            "error": "fail",
            "ecode": "E2620",
            "error_msg": "Already online",
        }

        client, _ = self.make_client(payload, online_state=True)
        result = client.authenticate()
        self.assertTrue(result.success)
        self.assertFalse(result.fatal)

        for state in (False, None):
            with self.subTest(online_state=state):
                client, _ = self.make_client(payload, online_state=state)
                result = client.authenticate()
                self.assertFalse(result.success)
                self.assertFalse(result.fatal)
                self.assertEqual(result.retry_after, 15)


if __name__ == "__main__":
    unittest.main()
