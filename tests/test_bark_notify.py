"""Synthetic Bark responses and fake secret protection; never real pushes."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from aipet.core import bark_notify as bark
from aipet.core.bark_notify import (
    BarkClient, BarkConfig, BarkNotifyError, BarkSendResult, load_config,
    logout, normalize_device_key, save_config,
)


KEY = "SYNTHETIC-SECRET-KEY_123"


class FakeResponse:
    def __init__(self, data=None, *, status=200, raw=None):
        self.status_code = status
        self.raw = json.dumps(data).encode("utf-8") if raw is None else raw
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self.raw), chunk_size):
            yield self.raw[index:index + chunk_size]

    def close(self):
        self.closed = True


class BarkHTTPTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.client = BarkClient(self.session)

    def reply(self, data=None, *, status=200, raw=None):
        result = FakeResponse(data, status=status, raw=raw)
        self.session.request.return_value = result
        return result

    def error(self, code, operation):
        with self.assertRaises(BarkNotifyError) as caught:
            operation()
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertEqual(str(error), code)
        self.assertNotIn(KEY, repr(error))
        self.assertNotIn("PRIVATE", repr(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        return error

    def test_key_normalization_accepts_plain_keys_and_exact_service_share_urls(self):
        for value in (KEY, " " + KEY + " ", "https://api.day.app/" + KEY,
                      "https://api.day.app/" + KEY + "/"):
            with self.subTest(value=value):
                self.assertEqual(normalize_device_key(value), KEY)
        self.assertEqual(normalize_device_key("a"), "a")
        self.assertEqual(normalize_device_key("x" * 256), "x" * 256)

    def test_key_validation_rejects_other_servers_paths_queries_ports_and_auth(self):
        for value in (None, [], "x" * 257, "密钥", "a.b", "a+b", "a/b", KEY + "\r\nInjected:x",
                      "http://api.day.app/" + KEY, "https://evil.invalid/" + KEY,
                      "https://api.day.app.evil.invalid/" + KEY,
                      "https://api.day.app:443/" + KEY, "https://api.day.app:8443/" + KEY,
                      "https://user:pass@api.day.app/" + KEY,
                      "https://api.day.app/" + KEY + "/body", "https://api.day.app//" + KEY,
                      "https://api.day.app/" + KEY + "?url=PRIVATE", "https://api.day.app/" + KEY + "#PRIVATE",
                      "https://api.day.app/%53ECRET", "https://api.day.app/" + KEY + "//"):
            with self.subTest(kind=type(value).__name__):
                self.error("invalid_key", lambda: normalize_device_key(value))

    def test_missing_keys_and_invalid_notices_do_not_make_requests(self):
        for value in ("", " "):
            self.error("key_missing", lambda: self.client.send_notice(value, "Notice"))
        for text in (None, [], "", " ", "x" * 2001, "invalid\ud800"):
            self.error("invalid_notice", lambda: self.client.send_notice(KEY, text))
        self.session.request.assert_not_called()

    def test_fixed_endpoint_and_minimal_json_keep_key_out_of_url_headers_and_actions(self):
        response = self.reply({"code": 200, "message": "success", "timestamp": 1})
        result = self.client.send_notice("https://api.day.app/" + KEY + "/", "第一行\n第二行")
        self.assertEqual(result, BarkSendResult(accepted=True))
        arguments, options = self.session.request.call_args
        self.assertEqual(arguments, ("POST", "https://api.day.app/push"))
        self.assertEqual(options["json"], {"device_key": KEY, "title": "宁宁·AI任务提醒",
                                           "body": "第一行\n第二行", "group": "NenePet"})
        self.assertNotIn(KEY, repr(arguments))
        self.assertNotIn(KEY, repr(options["headers"]))
        self.assertNotIn("params", options)
        self.assertFalse({"url", "action", "copy", "autoCopy", "level", "critical"}.intersection(options["json"]))
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertEqual(options["timeout"], 8)
        self.assertFalse(self.session.trust_env)
        self.assertTrue(response.closed)
        self.session.request.assert_called_once()

    def test_business_acceptance_requires_an_actual_integer_200(self):
        for code in (0, 201, 400, -1):
            with self.subTest(code=code):
                self.reply({"code": code, "message": KEY + " PRIVATE"})
                self.error("api_rejected", lambda: self.client.send_notice(KEY, "Notice"))
        for code in ("200", 200.0, True, None, []):
            with self.subTest(code=code):
                self.reply({"code": code, "message": KEY + " PRIVATE"})
                self.error("bad_response", lambda: self.client.send_notice(KEY, "Notice"))
        self.reply({"message": "PRIVATE " + KEY})
        self.error("acceptance_unknown", lambda: self.client.send_notice(KEY, "Notice"))

    def test_http_errors_and_redirects_are_not_followed_or_retried(self):
        for status, expected in ((301, "redirect_blocked"), (302, "redirect_blocked"),
                                 (307, "redirect_blocked"), (308, "redirect_blocked"),
                                 (400, "http_error"), (403, "http_error"), (429, "http_error"), (500, "http_error")):
            with self.subTest(status=status):
                self.session.request.reset_mock()
                response = self.reply({"code": 200, "message": KEY}, status=status)
                self.error(expected, lambda: self.client.send_notice(KEY, "Notice"))
                self.session.request.assert_called_once()
                self.assertTrue(response.closed)

    def test_network_failures_do_not_retain_original_request_exception_or_retry(self):
        self.session.request.side_effect = requests.ConnectionError("PRIVATE " + KEY)
        self.error("network_error", lambda: self.client.send_notice(KEY, "Notice"))
        self.session.request.assert_called_once()

    def test_invalid_json_encoding_and_duplicate_business_fields_fail_safely(self):
        for raw in (b"PRIVATE-invalid-json", b"\xff", b"[]", b"null",
                    json.dumps({"code": 200}).encode("utf-16"), b'{"code":400,"code":200}'):
            with self.subTest(raw_length=len(raw)):
                response = self.reply(raw=raw)
                self.error("bad_response", lambda: self.client.send_notice(KEY, "Notice"))
                self.assertTrue(response.closed)

    def test_stream_size_is_bounded_and_slow_drips_have_a_deadline(self):
        self.assertEqual(bark.MAX_RESPONSE_BYTES, 65536)
        response = self.reply(raw=b" " * (bark.MAX_RESPONSE_BYTES + 1))
        self.error("response_too_large", lambda: self.client.send_notice(KEY, "Notice"))
        self.assertTrue(response.closed)
        response = self.reply({"code": 200})
        with patch.object(bark.time, "monotonic", side_effect=[0, 9]):
            self.error("network_error", lambda: self.client.send_notice(KEY, "Notice"))
        self.assertTrue(response.closed)

    def test_close_is_explicit_and_cleanup_failure_does_not_leak(self):
        self.client.close()
        self.session.close.assert_called_once_with()
        self.session.close.side_effect = RuntimeError("PRIVATE " + KEY)
        self.client.close()
        self.assertNotIn(KEY, repr(BarkConfig(enabled=True, device_key=KEY)))
        with self.assertRaises(FrozenInstanceError):
            BarkConfig().device_key = KEY
        self.assertEqual(str(BarkNotifyError(KEY)), "bad_response")


class BarkConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="nene-bark-fixture-")))
        self.path = self.root / "nested" / "bark-notify.json"
        self.clear = {}
        def protect(value):
            self.clear["opaque-encrypted-fixture"] = value
            return "opaque-encrypted-fixture"
        self.protect = self.enterContext(patch("aipet.core.credentials.protect_secret", side_effect=protect))
        self.unprotect = self.enterContext(patch("aipet.core.credentials.unprotect_secret",
                                                side_effect=lambda value: self.clear[value]))
        self.config = BarkConfig(enabled=True, device_key=KEY)

    def error(self, operation):
        with self.assertRaises(BarkNotifyError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "config_error")
        self.assertNotIn(KEY, repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    def test_absent_config_stays_disabled_and_reading_creates_nothing(self):
        self.assertEqual(load_config(self.path), BarkConfig())
        self.assertFalse(self.path.parent.exists())
        self.protect.assert_not_called()
        self.unprotect.assert_not_called()

    def test_all_state_is_protected_and_no_key_enabled_flag_or_url_is_plaintext(self):
        save_config(self.path, self.config)
        raw = self.path.read_text(encoding="utf-8")
        stored = json.loads(raw)
        self.assertEqual(set(stored), {"format", "encrypted_state"})
        self.assertEqual(stored["format"], bark._CONFIG_FORMAT)
        for secret in (KEY, "api.day.app", "enabled", "device_key"):
            self.assertNotIn(secret, raw)
        self.assertEqual(load_config(self.path), self.config)
        self.assertEqual(json.loads(self.clear[stored["encrypted_state"]]), {"enabled": True, "device_key": KEY})

    def test_saving_normalizes_share_url_and_disabled_empty_state_round_trips(self):
        save_config(self.path, BarkConfig(True, "https://api.day.app/" + KEY + "/"))
        self.assertEqual(load_config(self.path), self.config)
        save_config(self.path, BarkConfig())
        self.assertEqual(load_config(self.path), BarkConfig())

    def test_enabled_requires_a_key_and_invalid_types_do_not_create_files(self):
        for config in (BarkConfig(True, ""), BarkConfig(True, "invalid/key"), BarkConfig(False, "invalid/key"),
                       BarkConfig(1, KEY), BarkConfig(False, None), object()):
            with self.subTest(config_type=type(config).__name__):
                self.error(lambda: save_config(self.path, config))
        self.assertFalse(self.path.parent.exists())

    def test_foreign_files_are_never_overwritten_deleted_or_decrypted(self):
        self.path.parent.mkdir()
        self.path.write_text('{"belongs_to":"another-program"}', encoding="utf-8")
        before = self.path.read_bytes()
        for operation in (lambda: load_config(self.path), lambda: save_config(self.path, self.config), lambda: logout(self.path)):
            self.error(operation)
            self.assertEqual(self.path.read_bytes(), before)
        self.unprotect.assert_not_called()

    def test_logout_removes_only_owned_config_and_missing_logout_is_a_noop(self):
        unrelated = self.root / "my-extension.json"
        unrelated.write_text("PRIVATE user file", encoding="utf-8")
        logout(self.path)
        self.assertFalse(self.path.parent.exists())
        save_config(self.path, self.config)
        logout(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "PRIVATE user file")

    def test_protection_or_atomic_replace_failure_preserves_saved_state(self):
        save_config(self.path, self.config)
        before = self.path.read_bytes()
        with patch("aipet.core.credentials.protect_secret", side_effect=RuntimeError("PRIVATE " + KEY)):
            self.error(lambda: save_config(self.path, replace(self.config, enabled=False)))
        self.assertEqual(self.path.read_bytes(), before)
        with patch.object(bark.os, "replace", side_effect=PermissionError("PRIVATE " + KEY)):
            self.error(lambda: save_config(self.path, replace(self.config, enabled=False)))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".nene-bark-*.tmp")), [])

    def test_empty_protection_and_oversized_envelope_are_rejected(self):
        for encrypted in ("", None, "x" * bark.MAX_CONFIG_BYTES):
            with self.subTest(length=len(encrypted) if isinstance(encrypted, str) else None), \
                    patch("aipet.core.credentials.protect_secret", return_value=encrypted):
                self.error(lambda: save_config(self.path, self.config))
        self.assertFalse(self.path.parent.exists())

    def test_malformed_unknown_or_foreign_encryption_loads_fail_closed(self):
        self.path.parent.mkdir()
        values = [b"\xff", b"[]", b"x" * (bark.MAX_CONFIG_BYTES + 1),
                  json.dumps({"format": bark._CONFIG_FORMAT, "encrypted_state": ""}).encode(),
                  json.dumps({"format": bark._CONFIG_FORMAT, "encrypted_state": "foreign-account"}).encode(),
                  ('{"format":"' + bark._CONFIG_FORMAT + '","encrypted_state":"a","encrypted_state":"b"}').encode()]
        for raw in values:
            with self.subTest(length=len(raw)):
                self.path.write_bytes(raw)
                self.error(lambda: load_config(self.path))

    def test_invalid_decrypted_fields_and_enabled_without_key_fail_closed(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"format": bark._CONFIG_FORMAT, "encrypted_state": "opaque-encrypted-fixture"}), encoding="utf-8")
        for value in ({"enabled": True, "device_key": ""}, {"enabled": True, "device_key": "bad/key"},
                      {"enabled": "yes", "device_key": KEY}, {"enabled": False},
                      {"enabled": False, "device_key": "", "url": "PRIVATE"}, []):
            with self.subTest(value_type=type(value).__name__):
                self.clear["opaque-encrypted-fixture"] = json.dumps(value)
                self.error(lambda: load_config(self.path))

    def test_relative_traversal_and_linked_paths_fail_without_writing(self):
        for path in (Path("relative-bark-config.json"), self.root / "unused" / ".." / "config.json"):
            for operation in (lambda: load_config(path), lambda: save_config(path, self.config), lambda: logout(path)):
                self.error(operation)
        for linked in (self.path, self.root):
            with self.subTest(linked=linked), patch.object(Path, "is_symlink", autospec=True,
                                                         side_effect=lambda candidate: candidate == linked):
                self.error(lambda: save_config(self.path, self.config))
                self.error(lambda: load_config(self.path))
                self.error(lambda: logout(self.path))
        self.assertFalse(self.path.parent.exists())

    def test_network_share_paths_are_rejected_before_any_filesystem_query(self):
        path = Path(r"\\example.invalid\private-share\bark-config.json")
        with patch.object(Path, "is_symlink", side_effect=AssertionError("Do not query network shares")):
            self.error(lambda: load_config(path))
            self.error(lambda: save_config(path, self.config))
            self.error(lambda: logout(path))


if __name__ == "__main__":
    unittest.main()
