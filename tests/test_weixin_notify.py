"""Only synthetic HTTP replies and fake secret protection; never Weixin login."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from aipet.core.weixin_notify import (
    CLIENT_VERSION, DEFAULT_BASE_URL, MAX_RESPONSE_BYTES, WeixinClient, WeixinConfig,
    WeixinCredentials, WeixinNotifyError, load_config, logout, save_config,
)


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


class WeixinHTTPTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.client = WeixinClient(self.session)
        self.credentials = WeixinCredentials("TOKEN-SENTINEL", "bot-fixture@im.bot",
            "self-fixture@im.wechat", DEFAULT_BASE_URL)

    def reply(self, data=None, *, status=200, raw=None):
        result = FakeResponse(data, status=status, raw=raw)
        self.session.request.return_value = result
        return result

    def error(self, code, operation):
        with self.assertRaises(WeixinNotifyError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("TOKEN-SENTINEL", str(caught.exception))
        self.assertNotIn("PRIVATE-SENTINEL", repr(caught.exception))
        return caught.exception

    def test_qr_request_matches_public_header_and_body_without_existing_accounts(self):
        response = self.reply({"qrcode": "QR-SENTINEL", "qrcode_img_content": "IMAGE-SENTINEL"})
        qr = self.client.request_qr()
        args, options = self.session.request.call_args
        self.assertEqual(args, ("POST", DEFAULT_BASE_URL + "/ilink/bot/get_bot_qrcode"))
        self.assertEqual(options["params"], {"bot_type": "3"})
        self.assertEqual(options["json"], {"local_token_list": []})
        self.assertEqual(options["headers"]["AuthorizationType"], "ilink_bot_token")
        self.assertEqual(options["headers"]["iLink-App-Id"], "bot")
        self.assertEqual(options["headers"]["iLink-App-ClientVersion"], CLIENT_VERSION)
        self.assertIn("X-WECHAT-UIN", options["headers"])
        self.assertNotIn("Authorization", options["headers"])
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(options["timeout"], 8)
        self.assertFalse(self.session.trust_env)
        self.assertEqual(qr.qrcode, "QR-SENTINEL")
        self.assertNotIn("SENTINEL", repr(qr))
        self.assertTrue(response.closed)

    def test_qr_status_classification_and_get_headers(self):
        for raw, status in (("wait", "pending"), ("scaned", "scanned"), ("expired", "expired"),
                            ("need_verifycode", "needs_verification"), ("verify_code_blocked", "needs_verification")):
            with self.subTest(status=raw):
                self.reply({"status": raw})
                self.assertEqual(self.client.poll_qr("QR-SENTINEL").status, status)
        args, options = self.session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(options["params"], {"qrcode": "QR-SENTINEL"})
        self.assertEqual(set(options["headers"]), {"iLink-App-Id", "iLink-App-ClientVersion"})
        self.assertFalse(options["allow_redirects"])

    def test_confirmed_login_returns_only_valid_own_recipient_credentials(self):
        self.reply({"status": "confirmed", "bot_token": "TOKEN-SENTINEL", "ilink_bot_id": "bot-fixture@im.bot",
                    "ilink_user_id": "self-fixture@im.wechat", "baseurl": "https://region.weixin.qq.com/"})
        result = self.client.poll_qr("QR-SENTINEL")
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.credentials.scanner_id, "self-fixture@im.wechat")
        self.assertEqual(result.credentials.base_url, "https://region.weixin.qq.com")
        self.assertNotIn("SENTINEL", repr(result))
        self.assertNotIn("SENTINEL", repr(result.credentials))

    def test_protocol_redirect_is_validated_but_no_http_redirect_is_followed(self):
        self.reply({"status": "scaned_but_redirect", "redirect_host": "region.weixin.qq.com"})
        result = self.client.poll_qr("QR-SENTINEL")
        self.assertEqual((result.status, result.poll_base_url), ("pending", "https://region.weixin.qq.com"))
        self.assertEqual(self.session.request.call_count, 1)
        self.reply({"status": "scaned_but_redirect", "redirect_host": "weixin.qq.com.evil.invalid"})
        self.error("unsafe_url", lambda: self.client.poll_qr("QR-SENTINEL"))
        self.reply({"PRIVATE-SENTINEL": "ignored"}, status=302)
        self.error("redirect_blocked", self.client.request_qr)
        self.reply({"status": "binded_redirect"})
        self.error("already_bound", lambda: self.client.poll_qr("QR-SENTINEL"))

    def test_untrusted_hosts_and_auth_injection_are_rejected_before_http(self):
        for url in ("http://ilinkai.weixin.qq.com", "https://weixin.qq.com.evil.invalid", "https://evil.invalid",
                    "https://user:pass@ilinkai.weixin.qq.com", "https://ilinkai.weixin.qq.com:444",
                    "https://ilinkai.weixin.qq.com/other", "https://ilinkai.weixin.qq.com/?token=secret"):
            with self.subTest(url=url):
                self.error("unsafe_url", lambda: self.client.get_updates(replace(self.credentials, base_url=url)))
        self.error("bad_response", lambda: self.client.get_updates(replace(self.credentials, bot_token="token\r\nHeader:private")))
        self.session.request.assert_not_called()

    def test_updates_only_project_owner_context_and_cursor_not_message_bodies(self):
        self.reply({"ret": 0, "get_updates_buf": "CURSOR-SENTINEL", "msgs": [
            {"from_user_id": "other-user@im.wechat", "context_token": "OTHER-CONTEXT", "item_list": ["PRIVATE-SENTINEL"]},
            {"from_user_id": self.credentials.scanner_id, "message_type": 1, "context_token": "OWNER-CONTEXT", "item_list": [{"text": "execute arbitrary command"}]},
            {"from_user_id": self.credentials.scanner_id, "message_type": 2, "context_token": "BOT-CONTEXT"},
            {"from_user_id": self.credentials.scanner_id, "message_type": 1, "group_id": "group-fixture", "context_token": "GROUP-CONTEXT"},
            {"from_user_id": self.credentials.scanner_id, "context_token": "UNTYPED-CONTEXT"},
        ]})
        update = self.client.get_updates(self.credentials, "previous-cursor")
        self.assertEqual(update.context_token, "OWNER-CONTEXT")
        self.assertEqual(update.cursor, "CURSOR-SENTINEL")
        self.assertNotIn("PRIVATE", repr(update))
        self.assertNotIn("execute", repr(update))
        options = self.session.request.call_args.kwargs
        self.assertEqual(options["timeout"], 5)
        self.assertEqual(options["json"]["get_updates_buf"], "previous-cursor")
        self.assertEqual(options["json"]["base_info"]["bot_agent"], "NenePet/1.0")
        self.assertEqual(options["headers"]["Authorization"], "Bearer TOKEN-SENTINEL")

    def test_empty_updates_preserve_cursor_without_claiming_a_context(self):
        self.reply({"ret": 0, "msgs": []})
        update = self.client.get_updates(self.credentials, "previous")
        self.assertEqual((update.context_token, update.cursor), (None, "previous"))

    def test_malformed_qr_status_and_slow_response_fail_with_fixed_codes(self):
        self.reply({"status": {"PRIVATE-SENTINEL": "not a status"}})
        self.error("bad_response", lambda: self.client.poll_qr("QR-SENTINEL"))
        self.reply({"qrcode": "QR-SENTINEL", "qrcode_img_content": "IMAGE-SENTINEL"})
        with patch("aipet.core.weixin_notify.time.monotonic", side_effect=[0, 9]):
            self.error("network_error", self.client.request_qr)

    def test_send_fixes_recipient_and_requires_explicit_context(self):
        self.error("context_missing", lambda: self.client.send_notice(self.credentials, "", "Phase ended"))
        self.session.request.assert_not_called()
        self.reply({"ret": 0})
        accepted = self.client.send_notice(self.credentials, "OWNER-CONTEXT", "Phase ended; inspect the result.")
        self.assertTrue(accepted.accepted)
        message = self.session.request.call_args.kwargs["json"]["msg"]
        self.assertEqual(message["to_user_id"], self.credentials.scanner_id)
        self.assertEqual(message["from_user_id"], "")
        self.assertEqual(message["message_type"], 2)
        self.assertEqual(message["message_state"], 2)
        self.assertEqual(message["context_token"], "OWNER-CONTEXT")
        self.assertEqual(message["item_list"], [{"type": 1, "text_item": {"text": "Phase ended; inspect the result."}}])

    def test_unknown_or_failed_business_response_never_becomes_accepted(self):
        for data, code in (({}, "acceptance_unknown"), ({"ret": "0"}, "bad_response"),
                           ({"ret": True}, "bad_response"), ({"ret": 0, "errcode": "0"}, "bad_response"),
                           ({"ret": -2, "errmsg": "TOKEN-SENTINEL PRIVATE-SENTINEL"}, "api_rejected_minus2"),
                           ({"ret": -14, "errmsg": "TOKEN-SENTINEL PRIVATE-SENTINEL"}, "session_expired"),
                           ({"ret": 0, "errcode": 71}, "api_rejected")):
            with self.subTest(data=data):
                self.reply(data)
                self.error(code, lambda: self.client.send_notice(self.credentials, "OWNER-CONTEXT", "Notice"))

    def test_network_http_json_and_size_errors_are_safe_and_responses_close(self):
        self.session.request.side_effect = requests.ConnectionError("TOKEN-SENTINEL PRIVATE-SENTINEL")
        self.error("network_error", self.client.request_qr)
        self.session.request.side_effect = None
        for response, code in ((FakeResponse(raw=b"{private invalid json"), "bad_response"),
                               (FakeResponse(status=500, raw=b"TOKEN-SENTINEL PRIVATE-SENTINEL"), "http_error"),
                               (FakeResponse(raw=b" " * (MAX_RESPONSE_BYTES + 1)), "response_too_large")):
            self.session.request.return_value = response
            self.error(code, self.client.request_qr)
            self.assertTrue(response.closed)
        self.client.close()
        self.session.close.assert_called_once_with()


class WeixinConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="nene-weixin-fixture-")))
        self.path = self.root / "weixin-notify.json"
        self.clear = {}
        def protect(value):
            self.clear["opaque-encrypted-fixture"] = value
            return "opaque-encrypted-fixture"
        self.enterContext(patch("aipet.core.credentials.protect_secret", side_effect=protect))
        self.enterContext(patch("aipet.core.credentials.unprotect_secret", side_effect=lambda value: self.clear[value]))
        self.config = WeixinConfig(True, WeixinCredentials("TOKEN-SENTINEL", "bot@im.bot", "self@im.wechat", DEFAULT_BASE_URL),
                                    "CONTEXT-SENTINEL", "CURSOR-SENTINEL")

    def test_absent_configuration_is_disabled_and_does_not_create_files(self):
        self.assertEqual(load_config(self.path), WeixinConfig())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_full_state_is_encrypted_and_only_explicit_path_is_written(self):
        unrelated = self.root / "my-extension.json"
        unrelated.write_text("private extension sentinel", encoding="utf-8")
        save_config(self.path, self.config)
        stored = self.path.read_text(encoding="utf-8")
        self.assertNotIn("SENTINEL", stored)
        self.assertNotIn("self@im.wechat", stored)
        self.assertEqual(load_config(self.path), self.config)
        self.assertNotIn("SENTINEL", repr(self.config))
        logout(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "private extension sentinel")

    def test_unknown_files_are_never_replaced_or_deleted(self):
        self.path.write_text('{"belongs_to":"another-program"}', encoding="utf-8")
        before = self.path.read_bytes()
        for operation in (lambda: save_config(self.path, self.config), lambda: logout(self.path), lambda: load_config(self.path)):
            with self.assertRaises(WeixinNotifyError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "config_error")
            self.assertEqual(self.path.read_bytes(), before)

    def test_encryption_failure_preserves_saved_configuration(self):
        save_config(self.path, self.config)
        before = self.path.read_bytes()
        with patch("aipet.core.credentials.protect_secret", side_effect=OSError("PRIVATE-SENTINEL")):
            with self.assertRaises(WeixinNotifyError) as caught:
                save_config(self.path, replace(self.config, enabled=False))
        self.assertEqual(str(caught.exception), "config_error")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".nene-weixin-*.tmp")), [])

    def test_damaged_or_foreign_encryption_and_relative_paths_fail_safely(self):
        self.path.write_text('{"format":"nenepet-weixin-notify-v1","encrypted_state":"unknown-other-account"}', encoding="utf-8")
        for path in (self.path, Path("relative-config.json")):
            with self.subTest(path=path), self.assertRaises(WeixinNotifyError) as caught:
                load_config(path)
            self.assertEqual(str(caught.exception), "config_error")
        self.assertFalse((self.root / "relative-config.json").exists())


if __name__ == "__main__":
    unittest.main()
