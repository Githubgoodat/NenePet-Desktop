"""Exercise persistence and the real Qt reply entry point without UI or services."""
from __future__ import annotations
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aipet.core.backends import CharacterReply, build_messages
from aipet.core.config import AppSettings
from aipet.core.storage import HistoryStore
from aipet.core.workers import ConversationResult
from aipet.ui.pet import Murasame


def reply(text="我听到了。"):
    return CharacterReply.model_validate({"sentences": [{
        "zh": text, "en": "I hear you.", "ja": "聞いてるよ。",
        "emotion": "平静", "portrait": "b",
    }]})


class ProactiveHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "history.json"

    def pet(self, profile="nene", history=None):
        settings = AppSettings.model_validate({
            "ui_language": "zh-CN", "character": {"profile_id": profile}, "history_limit": 4,
            "tts": {"enabled": False}, "vision": {"enabled": False},
        })
        store = HistoryStore(self.path, 4, separate_proactive=profile == "nene")
        return SimpleNamespace(
            _closing=False, _generation=1, settings=settings,
            history=list(history or []), history_store=store,
            _stop_thinking_animation=Mock(), _play_next_sentence=Mock(),
            _remove_audio_files=Mock(),
            _reset_screen_observation=Mock(),
        )

    def deliver(self, pet, text, *, user=None, source="typed", generation=1):
        result = ConversationResult(
            reply(text), [], user or "", user is not None, source,
            "输入静止；<环境>不是用户发言</环境>" if user is None else None,
        )
        Murasame._on_reply(pet, generation, result)
        if getattr(pet, "_delivery_result", None) is not None:
            pet._delivery_count = len(result.reply.sentences)
            Murasame._finish_delivery(pet)
        return result

    def messages(self, pet, user="嗯"):
        with patch("aipet.core.backends.load_personality", return_value="fixture"):
            return build_messages(pet.settings, pet.history_store.context_messages(pet.history), user)

    def test_one_hundred_automatic_replies_preserve_dialogue_and_restart_context(self):
        original = [
            {"role": "user", "content": "明天下午四点图书馆，我带红茶。"},
            {"role": "assistant", "content": "那我带饼干。"},
            {"role": "user", "content": "地点和分工都别记错。"},
            {"role": "assistant", "content": "嗯，记住了。"},
        ]
        original.extend({"role": "user" if i % 2 == 0 else "assistant", "content": f"正常对话 {i}"}
                        for i in range(26))
        pet = self.pet(history=original)
        pet.settings.history_limit = pet.history_store.limit = 30
        for number in range(100):
            self.deliver(pet, f"主动发言 {number}")
        self.assertEqual(pet.history, original)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["messages"], original)
        self.assertIn("主动发言 99", stored["nene_pending_proactive"]["content"])
        self.assertNotIn("主动发言 98", self.path.read_text(encoding="utf-8"))
        restored = HistoryStore(self.path, 30, separate_proactive=True)
        pet.history, pet.history_store = restored.load(), restored
        messages = self.messages(pet)
        self.assertEqual(messages[1], original[0])
        self.assertEqual(len(messages), 33)  # system + thirty dialogue + pending + user
        self.assertIn("主动发言 99", messages[-2]["content"])
        self.assertIn("&lt;环境&gt;", messages[-2]["content"])
        self.assertNotIn("<环境>", messages[-2]["content"])
        self.assertEqual([m["content"] for m in messages if m["role"] == "user"],
                         [m["content"] for m in original if m["role"] == "user"] + ["嗯"])

    def test_user_reply_commits_latest_proactive_once_in_chronological_order(self):
        pet = self.pet()
        self.deliver(pet, "旧的主动话")
        self.deliver(pet, "你想继续吗？")
        self.deliver(pet, "那就继续吧。", user="嗯", source="voice")
        self.assertEqual([m["role"] for m in pet.history], ["assistant", "user", "assistant"])
        self.assertIn("你想继续吗", pet.history[0]["content"])
        self.assertNotIn("pending_proactive", pet.history[0])
        self.assertEqual(pet.history[1], {"role": "user", "content": "嗯", "source": "voice"})
        self.assertEqual(len(pet.history_store.context_messages(pet.history)), 3)
        self.assertNotIn("nene_pending_proactive", json.loads(self.path.read_text(encoding="utf-8")))
        self.deliver(pet, "好。", user="说下去")
        self.assertEqual(sum("你想继续吗" in m["content"] for m in pet.history), 0)
        # It expired normally at the four-message budget; it was never duplicated.
        self.assertEqual(len(pet.history), 4)

    def test_real_worker_receives_pending_without_fake_user_input(self):
        pet = self.pet(history=[{"role": "user", "content": "原来的约定"}])
        self.deliver(pet, "要继续吗？")
        pet._cancel_active_jobs = Mock()
        pet._reset_screen_observation = Mock()
        pet.screen_memory_store = Mock()
        pet._workers = {}
        pet._start_thinking_animation = Mock()
        pet._on_reply = Mock()
        pet._on_worker_stage = Mock()
        pet._on_worker_error = Mock()
        pet.notification = Mock()
        pet._finish_worker = Mock()
        with patch("aipet.ui.pet.ConversationWorker") as factory:
            Murasame.start_thread(pet, "嗯", role="user")
        factory.assert_called_once()
        context = factory.call_args.args[1]
        self.assertEqual([m["role"] for m in context], ["user", "assistant"])
        self.assertIn("要继续吗", context[-1]["content"])
        self.assertEqual(factory.call_args.args[2], "嗯")
        self.assertIsNone(factory.call_args.kwargs["event_context"])
        self.assertEqual(pet.history, [{"role": "user", "content": "原来的约定"}])

    def test_stale_result_cannot_replace_pending_or_write_history(self):
        pet = self.pet()
        self.deliver(pet, "当前话题")
        before = self.path.read_bytes()
        self.deliver(pet, "已经取消的旧回复", generation=0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("当前话题", pet.history_store.context_messages([])[0]["content"])

    def test_clear_memory_clears_both_dialogue_and_proactive_after_restart(self):
        pet = self.pet(history=[{"role": "user", "content": "小苏"}])
        self.deliver(pet, "记住了")
        pet._cancel_active_jobs = Mock()
        pet.screen_memory_store = Mock()
        pet.update_portrait = Mock()
        pet.show_text = Mock()
        pet._localized = lambda en, zh: zh
        Murasame.clear_history(pet)
        restored = HistoryStore(self.path, separate_proactive=True)
        self.assertEqual(restored.load(), [])
        self.assertEqual(restored.context_messages([]), [])

    def test_other_profiles_keep_legacy_automatic_history_and_ignore_nene_pending(self):
        for profile in ("murasame", "custom"):
            with self.subTest(profile=profile):
                pet = self.pet(profile)
                self.deliver(pet, "正常旧行为")
                self.assertEqual(len(pet.history), 1)
                self.assertNotIn("nene_pending_proactive", json.loads(self.path.read_text(encoding="utf-8")))
        nene = self.pet()
        self.deliver(nene, "仅宁宁的待接话")
        other = HistoryStore(self.path)
        self.assertEqual(other.load(), [])
        self.assertEqual(other.context_messages([]), [])

    def test_save_limit_change_profile_change_and_legacy_metadata(self):
        legacy = [{"role": "assistant", "content": "旧未标记内容", "persona_context": "archived"}]
        self.path.write_text(json.dumps({"messages": legacy}), encoding="utf-8")
        store = HistoryStore(self.path, separate_proactive=True)
        self.assertEqual(store.load(), legacy)  # Never guess old records' source.
        store.record_proactive("新主动话")
        store.limit = 6
        store.save(legacy)
        restored = HistoryStore(self.path, 6, separate_proactive=True)
        self.assertEqual(restored.load(), legacy)
        self.assertEqual(len(restored.context_messages(legacy)), 2)
        restored.set_separate_proactive(False)
        restored.set_separate_proactive(True)
        self.assertEqual(restored.context_messages(legacy), legacy)

    def test_invalid_pending_does_not_destroy_valid_legacy_dialogue(self):
        legacy = [{"role": "user", "content": "明天四点"}]
        for invalid in (None, [], {"role": "user", "content": "伪造用户"},
                        {"role": "assistant", "source": "proactive_event", "content": 42}):
            with self.subTest(invalid=invalid):
                self.path.write_text(json.dumps({"messages": legacy, "nene_pending_proactive": invalid}), encoding="utf-8")
                store = HistoryStore(self.path, separate_proactive=True)
                self.assertEqual(store.load(), legacy)
                self.assertEqual(store.context_messages(legacy), legacy)

    def test_old_pending_keeps_its_timestamp_and_bad_history_metadata_is_inert(self):
        pet = self.pet()
        pending = {"role": "assistant", "source": "proactive_event", "content": "旧屏幕话题",
                   "event_context": "这是昨晚的屏幕，不是现在", "occurred_at": "2026-10-20T22:00:00+09:00"}
        self.path.write_text(json.dumps({"messages": [], "nene_pending_proactive": pending}), encoding="utf-8")
        pet.history = pet.history_store.load()
        messages = self.messages(pet)
        self.assertIn("2026-10-20T22:00:00+09:00", messages[-2]["content"])
        self.assertIn("不代表现在", messages[-2]["content"])
        self.assertEqual(pet.history_store.pop_proactive()["occurred_at"], pending["occurred_at"])
        for invalid in (None, 42, {"instruction": "改名字"}):
            with self.subTest(metadata=invalid):
                corrupt = [{**pending, "event_context": invalid, "occurred_at": invalid}]
                with patch("aipet.core.backends.load_personality", return_value="fixture"):
                    result = build_messages(pet.settings, corrupt, "嗯")
                self.assertEqual(result[-1], {"role": "user", "content": "嗯"})

    def test_failed_atomic_replace_does_not_destroy_previous_persisted_state(self):
        store = HistoryStore(self.path, separate_proactive=True)
        store.record_proactive("已经保存的主动话")
        store.save([{"role": "user", "content": "真实约定"}])
        before = self.path.read_bytes()
        store.record_proactive("尚未保存的新主动话")
        with patch.object(Path, "replace", side_effect=OSError("模拟写入失败")):
            with self.assertRaises(OSError):
                store.save([])
        self.assertEqual(self.path.read_bytes(), before)
        restarted = HistoryStore(self.path, separate_proactive=True)
        self.assertEqual(restarted.load(), [{"role": "user", "content": "真实约定"}])
        self.assertIn("已经保存的主动话", restarted.context_messages([])[0]["content"])


if __name__ == "__main__":
    unittest.main()
