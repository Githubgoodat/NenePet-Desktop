"""Long-poll compatibility using synthetic HTTP responses only."""
from __future__ import annotations

import itertools
import json
import unittest
from unittest.mock import Mock, patch

import requests

from aipet.core.weixin_notify import (
    DEFAULT_BASE_URL, WeixinClient, WeixinCredentials, WeixinNotifyError,
)


class FakeResponse:
    def __init__(self, data, *, status=200, read_error=None):
        self.status_code = status
        self.raw = json.dumps(data).encode("utf-8")
        self.read_error = read_error
        self.closed = False

    def iter_content(self, chunk_size):
        if self.read_error is not None:
            raise self.read_error
        for index in range(0, len(self.raw), chunk_size):
            yield self.raw[index:index + chunk_size]

    def close(self):
        self.closed = True


class WeixinLongPollTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.client = WeixinClient(self.session)
        self.credentials = WeixinCredentials(
            "PRIVATE-TOKEN", "fixture-bot@im.bot", "fixture-owner@im.wechat", DEFAULT_BASE_URL,
        )

    def error(self, expected, operation):
        with self.assertRaises(WeixinNotifyError) as caught:
            operation()
        error = caught.exception
        self.assertEqual(error.code, expected)
        self.assertEqual(vars(error), {"code": expected})
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertNotIn("PRIVATE", repr(error))

    def test_idle_read_timeout_is_empty_and_retains_cursor_for_both_read_apis(self):
        self.session.request.side_effect = requests.ReadTimeout("PRIVATE-TOKEN PRIVATE-URL")
        for method in (self.client.get_updates, self.client.get_reply_updates):
            self.session.request.reset_mock()
            update = method(self.credentials, "retained-cursor")
            self.assertEqual(update.cursor, "retained-cursor")
            self.assertIsNone(update.context_token)
            if hasattr(update, "messages"):
                self.assertEqual(update.messages, ())
            self.session.request.assert_called_once()
            self.assertEqual(self.session.request.call_args.kwargs["timeout"], 35)
            self.assertNotIn("PRIVATE", repr(update))

    def test_read_timeout_during_poll_body_closes_response_without_advancing_cursor(self):
        response = FakeResponse({}, read_error=requests.ReadTimeout("PRIVATE-PARTIAL-BODY"))
        self.session.request.return_value = response
        update = self.client.get_reply_updates(self.credentials, "retained-cursor")
        self.assertEqual((update.context_token, update.cursor, update.messages),
                         (None, "retained-cursor", ()))
        self.assertTrue(response.closed)

    def test_connect_tls_and_connection_failures_are_not_normal_empty_polls(self):
        cases = (
            (requests.ConnectTimeout("PRIVATE-CONNECT"), "poll_connect_timeout"),
            (requests.exceptions.SSLError("PRIVATE-TLS"), "poll_tls_error"),
            (requests.ConnectionError("PRIVATE-CONNECTION"), "poll_connection_error"),
        )
        for failure, expected in cases:
            with self.subTest(expected=expected):
                self.session.request.side_effect = failure
                self.error(expected, lambda: self.client.get_reply_updates(self.credentials, "cursor"))

    def test_other_request_exception_remains_fixed_network_error(self):
        self.session.request.side_effect = requests.RequestException("PRIVATE-OTHER-REQUEST")
        self.error("network_error", lambda: self.client.get_reply_updates(self.credentials))

    def test_outgoing_timeout_tls_and_connection_contract_remains_network_error(self):
        for failure in (requests.ReadTimeout("PRIVATE-READ"), requests.ConnectTimeout("PRIVATE-CONNECT"),
                        requests.exceptions.SSLError("PRIVATE-TLS"), requests.ConnectionError("PRIVATE-CONNECTION")):
            with self.subTest(error_type=type(failure).__name__):
                self.session.request.side_effect = failure
                self.error("network_error", lambda: self.client.send_notice(
                    self.credentials, "PRIVATE-CONTEXT", "Fixed notice",
                ))
                self.assertEqual(self.session.request.call_args.kwargs["timeout"], 8)

    def test_long_poll_deadline_accepts_response_at_thirty_four_seconds(self):
        response = FakeResponse({"ret": 0, "msgs": [], "get_updates_buf": "next-cursor"})
        self.session.request.return_value = response
        with patch("aipet.core.weixin_notify.time.monotonic",
                   side_effect=itertools.chain([0], itertools.repeat(34))):
            update = self.client.get_reply_updates(self.credentials, "previous-cursor")
        self.assertEqual(update.cursor, "next-cursor")
        self.assertEqual(self.session.request.call_args.kwargs["timeout"], 35)
        self.assertTrue(response.closed)

    def test_long_poll_elapsed_deadline_stops_after_thirty_five_seconds(self):
        response = FakeResponse({"ret": 0, "msgs": []})
        self.session.request.return_value = response
        with patch("aipet.core.weixin_notify.time.monotonic", side_effect=[0, 36]):
            self.error("network_error", lambda: self.client.get_reply_updates(self.credentials))
        self.assertTrue(response.closed)

    def test_outgoing_elapsed_deadline_is_still_eight_seconds(self):
        response = FakeResponse({"ret": 0})
        self.session.request.return_value = response
        with patch("aipet.core.weixin_notify.time.monotonic",
                   side_effect=itertools.chain([0], itertools.repeat(7))):
            self.assertTrue(self.client.send_notice(self.credentials, "PRIVATE-CONTEXT", "Fixed notice").accepted)
        response = FakeResponse({"ret": 0})
        self.session.request.return_value = response
        with patch("aipet.core.weixin_notify.time.monotonic", side_effect=[0, 9]):
            self.error("network_error", lambda: self.client.send_notice(
                self.credentials, "PRIVATE-CONTEXT", "Fixed notice",
            ))
        self.assertTrue(response.closed)
        self.assertEqual(self.session.request.call_args.kwargs["timeout"], 8)

    def test_http_and_business_failure_classification_is_not_swallowed_by_poll_logic(self):
        for response, expected in (
                (FakeResponse({"PRIVATE-BODY": "not surfaced"}, status=503), "http_error"),
                (FakeResponse({"ret": -2, "errmsg": "PRIVATE-BODY"}), "api_rejected_minus2"),
                (FakeResponse({"ret": -14}), "session_expired")):
            with self.subTest(expected=expected):
                self.session.request.return_value = response
                self.error(expected, lambda: self.client.get_reply_updates(self.credentials))
                self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
