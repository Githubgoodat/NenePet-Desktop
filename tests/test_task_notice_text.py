"""Fixed character-voiced metadata formatting, without APIs or chat content."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aipet.ui.weixin_notify import notice_text


class TaskNoticeTextTests(unittest.TestCase):
    def record(self, *, source="codex", status="question_opened", project="暮色工房"):
        return SimpleNamespace(
            source=source, status=status, project=project,
            task_id="PRIVATE-SESSION-012345", turn_id="PRIVATE-TURN-678901",
            notice_id="PRIVATE-NOTICE-abcdef", device_key="PRIVATE-DEVICE-KEY",
            bot_token="PRIVATE-BOT-TOKEN", prompt="PRIVATE-PROMPT",
        )

    def test_each_state_keeps_its_factual_meaning_and_uses_project_once(self):
        expected = {
            "needs_attention": ("授权请求", "记录到授权请求", "是否还需要处理"),
            "turn_ended": ("本轮结束", "这一轮结束", "不代表整个任务完成"),
            "phase_ended": ("阶段结束", "这个阶段结束", "不代表整个任务完成"),
            "interrupted": ("本轮中断", "这一轮中断", "要不要继续"),
            "failed": ("来源上报失败", "上报了失败", "核对"),
            "input_requested": ("输入请求", "发起了输入请求", "回 Codex 看看"),
            "question_opened": ("提问事件", "有个问题想请你看看", "任务可能仍在继续"),
        }
        with patch("requests.Session", side_effect=AssertionError("Formatter must not use a model or network")):
            for status, facts in expected.items():
                with self.subTest(status=status):
                    record = self.record(status=status)
                    text = notice_text(record)
                    self.assertIn("来源：Codex", text)
                    for fact in facts:
                        self.assertIn(fact, text)
                    self.assertEqual(text.count(record.project), 1)
                    self.assertEqual(len(text.splitlines()), 4)
                    for private in (record.task_id, record.turn_id, record.notice_id,
                                    record.device_key, record.bot_token, record.prompt):
                        self.assertNotIn(private, text)
                    for unsupported_claim in ("测试通过", "成功完成", "主人", "喵", "还没回答", "正在等你"):
                        self.assertNotIn(unsupported_claim, text)

    def test_grok_input_report_is_distinguished_from_codex_authorization(self):
        record = self.record(source="grok_bot", status="needs_attention")
        text = notice_text(record)
        self.assertIn("来源：grok bot（协作上报）", text)
        self.assertIn("状态：需要输入", text)
        self.assertIn("那边说需要你补充信息", text)
        self.assertNotIn("授权请求", text)
        self.assertNotIn("任务已暂停", text)
        self.assertEqual(text.count(record.project), 1)

    def test_only_authorized_basename_leaves_for_absolute_and_relative_paths(self):
        cases = (
            (r"C:\Users\PRIVATE-OWNER\Documents\暮色工房", "暮色工房"),
            ("/home/PRIVATE-OWNER/Nene Project/", "Nene Project"),
            (r"\\PRIVATE-SERVER\PRIVATE-SHARE\Project", "Project"),
            (r"relative\PRIVATE-PARENT\Project", "Project"),
            ("relative/PRIVATE-PARENT/Project", "Project"),
            (r"C:Project", "Project"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                text = notice_text(self.record(project=value))
                self.assertIn("〈" + expected + "〉", text)
                self.assertNotIn("PRIVATE-", text)
                self.assertNotIn(value, text)
                self.assertNotIn("\\", text)
                self.assertEqual(len(text.splitlines()), 4)

    def test_controls_bidi_newlines_and_template_delimiters_cannot_add_message_lines(self):
        value = "  C:\\PRIVATE-OWNER\\〈Project〉\r\nName\x00\u202e\t  "
        text = notice_text(self.record(project=value))
        self.assertIn("〈Project Name〉", text)
        self.assertEqual(len(text.splitlines()), 4)
        self.assertEqual(text.count("〈"), 1)
        self.assertEqual(text.count("〉"), 1)
        for bad in ("\r", "\x00", "\u202e", "\t", "PRIVATE-OWNER"):
            self.assertNotIn(bad, text)

    def test_project_name_is_bounded_to_seventy_characters(self):
        text = notice_text(self.record(project="宁" * 90))
        self.assertIn("〈" + "宁" * 70 + "〉", text)
        self.assertNotIn("宁" * 71, text)

    def test_missing_invalid_roots_urls_and_identity_labels_fall_back(self):
        cases = (None, 0, [], "", "   ", ".", "..", "/", "C:\\",
                 "https://api.day.app/PRIVATE-DEVICE-KEY/",
                 "PRIVATE-SESSION-012345-assets", "PRIVATE-TURN-678901",
                 "PRIVATE-NOTICE-abcdef")
        for value in cases:
            with self.subTest(value=value):
                text = notice_text(self.record(project=value))
                self.assertIn("〈未命名项目〉", text)
                self.assertNotIn("PRIVATE-", text)
                self.assertEqual(len(text.splitlines()), 4)

    def test_short_identifier_substrings_do_not_hide_a_real_project_name(self):
        record = self.record(project="game")
        record.task_id, record.turn_id, record.notice_id = "a", "me", "g"
        self.assertIn("〈game〉", notice_text(record))
        record.project = "a"
        self.assertIn("〈未命名项目〉", notice_text(record))
        record.project, record.task_id = "project-session1234-assets", "session1234"
        self.assertIn("〈未命名项目〉", notice_text(record))

    def test_input_notification_can_report_a_separate_observed_authorization_fact(self):
        for status in ("input_requested", "question_opened"):
            with self.subTest(status=status):
                record = self.record(status=status)
                record.authorization_observed = True
                text = notice_text(record)
                self.assertIn("／授权记录", text)
                self.assertIn("也记录到了授权请求", text)
                self.assertEqual(text.count(record.project), 1)
                for false_claim in ("授权已通过", "已经批准", "同一次调用", "任务已暂停", "还没回答"):
                    self.assertNotIn(false_claim, text)
        for value in (False, 1, "true", None):
            with self.subTest(non_boolean_flag=value):
                record = self.record()
                record.authorization_observed = value
                self.assertNotIn("授权记录", notice_text(record))
                self.assertNotIn("也记录到了", notice_text(record))
        ordinary = self.record(status="turn_ended")
        ordinary.authorization_observed = True
        self.assertNotIn("也记录到了", notice_text(ordinary))

    def test_unknown_source_state_or_invalid_snapshot_produces_no_message(self):
        for source, status in (("unknown", "turn_ended"), ("codex", "unknown"), (None, "turn_ended"),
                               ([], "turn_ended"), ("codex", []), ("grok_bot", "question_opened"),
                               ("grok_bot", "input_requested")):
            with self.subTest(source=source, status=status):
                self.assertIsNone(notice_text(self.record(source=source, status=status)))
        self.assertIsNone(notice_text(object()))


if __name__ == "__main__":
    unittest.main()
