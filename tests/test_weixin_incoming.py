"""Synthetic getupdates replies only; no real binding, message or API call."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

from aipet.core.weixin_notify import (
    DEFAULT_BASE_URL, MAX_PROVIDER_MESSAGE_ID_CHARS, MAX_REPLY_TEXT_CHARS, WeixinClient, WeixinCredentials,
    WeixinIncomingReply, WeixinNotifyError, WeixinReplyUpdate, WeixinUpdate,
)


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code = status
        self.raw = json.dumps(data).encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self.raw), chunk_size):
            yield self.raw[index:index + chunk_size]

    def close(self):
        self.closed = True


class WeixinIncomingTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.client = WeixinClient(self.session)
        self.credentials = WeixinCredentials(
            "PRIVATE-TOKEN", "fixture-bot@im.bot", "fixture-self@im.wechat", DEFAULT_BASE_URL,
        )

    def message(self, **changes):
        value = {
            "from_user_id": self.credentials.scanner_id, "to_user_id": self.credentials.bot_id,
            "message_type": 1, "message_id": "9007199254740993",
            "context_token": "PRIVATE-CONTEXT", "create_time_ms": 1760000000123,
            "item_list": [{"type": 1, "text_item": {"text": "回复 deadbeef 这是普通答复"}}],
        }
        value.update(changes)
        return value

    def response(self, messages=(), **changes):
        data = {"ret": 0, "get_updates_buf": "PRIVATE-NEXT-CURSOR", "msgs": list(messages)}
        data.update(changes)
        response = FakeResponse(data)
        self.session.request.return_value = response
        return response

    def receive(self, *messages):
        self.response(messages)
        return self.client.get_reply_updates(self.credentials, "previous-cursor")

    def test_owner_plain_text_is_projected_by_one_bounded_long_poll(self):
        response = self.response([self.message()])
        update = self.client.get_reply_updates(self.credentials, "previous-cursor")
        self.assertIsInstance(update, WeixinReplyUpdate)
        self.assertEqual(update.context_token, "PRIVATE-CONTEXT")
        self.assertEqual(update.cursor, "PRIVATE-NEXT-CURSOR")
        self.assertEqual(update.messages, (
            WeixinIncomingReply("9007199254740993", "回复 deadbeef 这是普通答复", 1760000000123),
        ))
        self.session.request.assert_called_once()
        args, options = self.session.request.call_args
        self.assertEqual(args, ("POST", DEFAULT_BASE_URL + "/ilink/bot/getupdates"))
        self.assertEqual(options["timeout"], 35)
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(options["json"]["get_updates_buf"], "previous-cursor")
        self.assertEqual(options["headers"]["Authorization"], "Bearer PRIVATE-TOKEN")
        self.assertFalse(self.session.trust_env)
        self.assertTrue(response.closed)

    def test_default_get_updates_still_returns_context_without_message_bodies(self):
        self.response([self.message()])
        update = self.client.get_updates(self.credentials, "previous-cursor")
        self.assertIsInstance(update, WeixinUpdate)
        self.assertEqual(update.context_token, "PRIVATE-CONTEXT")
        self.assertFalse(hasattr(update, "messages"))
        self.assertFalse(hasattr(update, "text"))
        self.assertNotIn("普通答复", repr(update))
        self.session.request.assert_called_once()

    def test_same_owner_type_is_required_and_other_contexts_cannot_replace_owners(self):
        invalid = (
            self.message(from_user_id="somebody-else@im.wechat", context_token="OTHER-CONTEXT"),
            self.message(message_type=2, context_token="BOT-CONTEXT"),
            self.message(message_type=True, context_token="BOOL-CONTEXT"),
            self.message(message_type="1", context_token="STRING-CONTEXT"),
            self.message(message_type=1.0, context_token="FLOAT-CONTEXT"),
            self.message(group_id="some-group", context_token="GROUP-CONTEXT"),
            self.message(group_id=0, context_token="NONSTRING-GROUP-CONTEXT"),
            self.message(to_user_id="another-bot@im.bot", context_token="OTHER-BOT-CONTEXT"),
            self.message(to_user_id=False, context_token="BAD-RECIPIENT-CONTEXT"),
        )
        update = self.receive(self.message(), *invalid)
        self.assertEqual(len(update.messages), 1)
        self.assertEqual(update.context_token, "PRIVATE-CONTEXT")

    def test_optional_receiver_and_empty_direct_group_are_accepted_without_weakening_owner(self):
        values = []
        for index, receiver in enumerate((None, "", self.credentials.bot_id)):
            values.append(self.message(message_id=str(index + 1), to_user_id=receiver, group_id=""))
        absent = self.message(message_id="4")
        del absent["to_user_id"]
        values.append(absent)
        update = self.receive(*values)
        self.assertEqual([row.message_id for row in update.messages], ["1", "2", "3", "4"])

    def test_uint64_ids_remain_lossless_for_string_and_exact_python_integer(self):
        maximum = (1 << 64) - 1
        update = self.receive(
            self.message(message_id=str(maximum)), self.message(message_id=maximum),
            self.message(message_id=9007199254740993), self.message(message_id=9007199254740994),
        )
        self.assertEqual([row.message_id for row in update.messages],
                         [str(maximum), str(maximum), "9007199254740993", "9007199254740994"])

    def test_missing_float_boolean_negative_overflow_or_unsafe_id_is_rejected(self):
        bad_ids = (None, 0, -1, True, False, 1.0, (1 << 64), "", "0", "01", "+1", "-1",
                   "١٢", "18446744073709551616", "9" * 100, "bad\x00id", "bad\nid", "\ud800", [], {})
        for identity in bad_ids:
            with self.subTest(identity=identity):
                self.assertEqual(self.receive(self.message(message_id=identity)).messages, ())
        missing = self.message()
        del missing["message_id"]
        self.assertEqual(self.receive(missing).messages, ())

    def test_item_message_id_accepts_optional_top_level_protocol_shape(self):
        identity = "provider-message:fixture-01@wechat"
        item = {"type": 1, "msg_id": identity,
                "text_item": {"text": "回复 deadbeef 这是普通答复"},
                "image_item": None, "voice_item": None, "file_item": None,
                "video_item": None, "tool_call_start_item": None, "tool_call_result_item": None}
        missing = self.message(item_list=[item])
        del missing["message_id"]
        values = [missing] + [self.message(message_id=absent, item_list=[item])
                              for absent in (None, "", "   ")]
        update = self.receive(*values)
        expected = "weixin-id-sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.assertEqual(len(update.messages), len(values))
        self.assertEqual({row.message_id for row in update.messages}, {expected})
        self.assertTrue(all(row.text == "回复 deadbeef 这是普通答复" for row in update.messages))

    def test_same_id_cross_field_and_numeric_wire_types_have_one_replay_key(self):
        item = {"type": 1, "text_item": {"text": "回复 deadbeef yes"}}
        maximum = (1 << 64) - 1
        for identity in ("opaque:provider/id", str(maximum)):
            with self.subTest(identity=identity):
                top = self.message(message_id=identity)
                fallback = self.message(message_id=None, item_list=[item | {"msg_id": identity}])
                trimmed_top = self.message(message_id=" " + identity + " ")
                trimmed_item = self.message(message_id="", item_list=[item | {"msg_id": " " + identity + " "}])
                keys = [row.message_id for row in self.receive(top, fallback, trimmed_top, trimmed_item).messages]
                self.assertEqual(len(keys), 4)
                self.assertEqual(len(set(keys)), 1)
        numeric = self.receive(self.message(message_id=maximum),
                               self.message(message_id=None, item_list=[item | {"msg_id": maximum}]))
        self.assertEqual([row.message_id for row in numeric.messages], [str(maximum), str(maximum)])

    def test_top_level_id_has_priority_and_invalid_supplied_id_is_not_hidden_by_fallback(self):
        item = {"type": 1, "msg_id": "valid-item-id", "text_item": {"text": "回复 deadbeef yes"}}
        update = self.receive(self.message(message_id="7", item_list=[item]))
        self.assertEqual(update.messages[0].message_id, "7")
        for bad in (0, "0", False, True, 1.0, -1, "-1", [], {}, "bad\nid", "\t",
                    "x" * (MAX_PROVIDER_MESSAGE_ID_CHARS + 1)):
            with self.subTest(top_level_type=type(bad).__name__):
                self.assertEqual(self.receive(self.message(message_id=bad, item_list=[item])).messages, ())
        # An invalid unused item ID cannot override a valid preferred ID.
        update = self.receive(self.message(message_id="7", item_list=[item | {"msg_id": []}]))
        self.assertEqual(update.messages[0].message_id, "7")

    def test_bounded_opaque_ids_use_a_disjoint_collision_resistant_namespace(self):
        def key(identity):
            return self.receive(self.message(message_id=identity)).messages[0].message_id
        raw = "fixture opaque id:/@-_."
        normalised = key(raw)
        self.assertTrue(normalised.startswith("weixin-id-sha256:"))
        self.assertNotEqual(normalised, key(normalised))
        self.assertNotEqual(normalised, key(raw + "different"))
        self.assertNotEqual(normalised, key("123"))
        self.assertEqual(key("x" * MAX_PROVIDER_MESSAGE_ID_CHARS),
                         "weixin-id-sha256:" + hashlib.sha256(
                             ("x" * MAX_PROVIDER_MESSAGE_ID_CHARS).encode("utf-8")).hexdigest())
        self.assertEqual(self.receive(self.message(
            message_id="x" * (MAX_PROVIDER_MESSAGE_ID_CHARS + 1))).messages, ())
        # These are literal opaque strings; JSON float/bool values stay invalid.
        self.assertEqual(key("1e3"), "weixin-id-sha256:" + hashlib.sha256(b"1e3").hexdigest())

    def test_missing_stable_id_does_not_use_seq_client_id_timestamp_or_quote(self):
        quote = {"svr_id": "123", "message_item": {"msg_id": "quoted-id"}}
        item = {"type": 1, "text_item": {"text": "回复 deadbeef yes"}, "ref_msg": quote}
        no_id = self.message(message_id=None, seq=7, client_id="client-id", item_list=[item])
        self.assertEqual(self.receive(no_id).messages, ())
        for bad in (0, "0", -1, True, False, 1.0, "bad\x00id", "bad\nid", "\ud800",
                    "x" * (MAX_PROVIDER_MESSAGE_ID_CHARS + 1), [], {}):
            with self.subTest(item_id_type=type(bad).__name__):
                self.assertEqual(self.receive(self.message(
                    message_id=None, item_list=[item | {"msg_id": bad}])).messages, ())

    def test_item_id_fallback_does_not_relax_owner_direct_or_single_text_checks(self):
        item = {"type": 1, "msg_id": "fixture-item-id", "text_item": {"text": "回复 deadbeef yes"}}
        for changes in ({"from_user_id": "other@im.wechat"}, {"to_user_id": "other@im.bot"},
                        {"group_id": "group"}, {"message_type": 2},
                        {"item_list": [item, item]}, {"item_list": [item | {"image_item": {}}]},
                        {"item_list": [item | {"type": 3}]}):
            with self.subTest(changed_fields=tuple(changes)):
                self.assertEqual(self.receive(self.message(
                    message_id=None, item_list=[item]) | changes).messages, ())

    def test_text_is_preserved_with_newlines_and_tab_without_parsing_as_commands(self):
        text = "回复 deadbeef 第一行\n第二行\t$() & powershell /approve"
        update = self.receive(self.message(item_list=[{"type": 1, "text_item": {"text": text}}]))
        self.assertEqual(update.messages[0].text, text)
        # Transport does not decide that a message is a valid task answer.
        ordinary = self.receive(self.message(item_list=[{"type": 1, "text_item": {"text": "随便聊一句"}}]))
        self.assertEqual(ordinary.messages[0].text, "随便聊一句")

    def test_text_limit_is_character_based_and_boundary_is_accepted(self):
        boundary = "中" * MAX_REPLY_TEXT_CHARS
        update = self.receive(self.message(item_list=[{"type": 1, "text_item": {"text": boundary}}]))
        self.assertEqual(update.messages[0].text, boundary)
        oversized = self.receive(self.message(item_list=[{"type": 1, "text_item": {"text": boundary + "x"}}]))
        self.assertEqual(oversized.messages, ())

    def test_invalid_empty_or_control_text_is_rejected_without_body_in_exception(self):
        for text in (None, 7, {}, [], "", " \t\n", "reply\x00secret", "reply\x1b[31msecret", "\ud800"):
            with self.subTest(text_type=type(text).__name__):
                update = self.receive(self.message(item_list=[{"type": 1, "text_item": {"text": text}}]))
                self.assertEqual(update.messages, ())

    def test_multi_item_or_disguised_media_is_not_joined_into_a_reply(self):
        text_item = {"type": 1, "text_item": {"text": "回复 deadbeef yes"}}
        lists = (
            [], [text_item, text_item],
            [text_item, {"type": 2, "image_item": {"url": "https://private.invalid"}}],
            [{"type": 3, "voice_item": {"text": "回复 deadbeef voice transcript"}}],
            [{"type": 4, "file_item": {"file_name": "private.txt"}}],
            [{"type": 5, "video_item": {}}],
            [{"type": 11, "tool_call_start_item": {"tool_name": "execute"}}],
            [{"type": 1, "text_item": {"text": "回复 deadbeef yes"}, "image_item": {}}],
            [{"type": True, "text_item": {"text": "回复 deadbeef yes"}}],
            [{"type": "1", "text_item": {"text": "回复 deadbeef yes"}}],
        )
        for items in lists:
            with self.subTest(items=items):
                self.assertEqual(self.receive(self.message(item_list=items)).messages, ())

    def test_quoted_forwarded_or_referenced_body_never_becomes_the_new_answer(self):
        quote = {
            "title": "PRIVATE QUOTED SUMMARY", "svr_id": "1234",
            "message_item": {"type": 1, "text_item": {"text": "回复 deadbeef PRIVATE QUOTED COMMAND"}},
        }
        top_level = "回复 deadbeef 只保留我刚写的文字"
        update = self.receive(self.message(item_list=[
            {"type": 1, "text_item": {"text": top_level}, "ref_msg": quote},
        ]))
        self.assertEqual(update.messages[0].text, top_level)
        self.assertNotIn("QUOTED", update.messages[0].text)
        no_new_text = self.receive(self.message(item_list=[{"type": 1, "ref_msg": quote}]))
        self.assertEqual(no_new_text.messages, ())

    def test_optional_epoch_timestamp_is_not_invented_and_bad_metadata_does_not_drop_text(self):
        missing = self.message()
        del missing["create_time_ms"]
        update = self.receive(missing, self.message(message_id="2", create_time_ms=None),
                              self.message(message_id="3", create_time_ms=0))
        self.assertEqual([row.create_time_ms for row in update.messages], [None, None, 0])
        for created in (-1, True, False, "1760000000123", 1760000000123.0, (1 << 53), {}, []):
            with self.subTest(created=created):
                messages = self.receive(self.message(create_time_ms=created)).messages
                self.assertEqual(len(messages), 1)
                self.assertIsNone(messages[0].create_time_ms)
                self.assertEqual(messages[0].text, "回复 deadbeef 这是普通答复")

    def test_missing_cursor_preserves_previous_and_empty_batch_has_no_fabricated_messages(self):
        for value in (None, ""):
            self.response((), get_updates_buf=value)
            update = self.client.get_reply_updates(self.credentials, "previous-cursor")
            self.assertEqual(update, WeixinReplyUpdate(None, "previous-cursor", ()))

    def test_malformed_envelope_uses_fixed_existing_error(self):
        for bad in (None, {}, "PRIVATE-MESSAGE"):
            self.response(msgs=bad)
            with self.assertRaises(WeixinNotifyError) as caught:
                self.client.get_reply_updates(self.credentials)
            self.assertEqual(str(caught.exception), "bad_response")
            self.assertNotIn("PRIVATE", repr(caught.exception))

    def test_business_and_network_errors_keep_existing_fixed_codes(self):
        for data, expected in (({"ret": -2}, "api_rejected_minus2"),
                               ({"ret": -14}, "session_expired"),
                               ({"ret": "0"}, "bad_response"),
                               ({"ret": 9}, "api_rejected")):
            self.response(**data)
            with self.assertRaises(WeixinNotifyError) as caught:
                self.client.get_reply_updates(self.credentials)
            self.assertEqual(str(caught.exception), expected)
        self.session.request.side_effect = requests.Timeout("PRIVATE-TOKEN PRIVATE-MESSAGE")
        with self.assertRaises(WeixinNotifyError) as caught:
            self.client.get_reply_updates(self.credentials)
        self.assertEqual(str(caught.exception), "network_error")
        self.assertNotIn("PRIVATE", repr(caught.exception))

    def test_raw_objects_are_immutable_and_repr_does_not_expose_text_context_or_cursor(self):
        update = self.receive(self.message())
        for value in (update, update.messages[0]):
            for secret in ("PRIVATE", "普通答复", "deadbeef", "9007199254740993"):
                self.assertNotIn(secret, repr(value))
        with self.assertRaises(FrozenInstanceError):
            update.messages[0].text = "changed"
        with self.assertRaises(FrozenInstanceError):
            update.cursor = "changed"

    def test_credentials_and_cursor_are_validated_without_opening_any_binding(self):
        self.response([self.message()])
        with patch.object(Path, "open", side_effect=AssertionError("No binding reads or writes")):
            update = self.client.get_reply_updates(self.credentials)
        self.assertEqual(len(update.messages), 1)
        self.session.request.reset_mock()
        for credentials, cursor in ((replace(self.credentials, base_url="https://evil.invalid"), ""),
                                    (replace(self.credentials, bot_token="token\r\nprivate"), ""),
                                    (self.credentials, "unsafe\ncursor")):
            with self.subTest(cursor=cursor):
                with self.assertRaises(WeixinNotifyError):
                    self.client.get_reply_updates(credentials, cursor)
        self.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
