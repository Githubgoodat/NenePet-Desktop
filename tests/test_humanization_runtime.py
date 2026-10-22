"""Real Qt/worker/persistence flow with canned transport, no desktop or models.

These checks establish isolation and lifecycle behaviour, not LLM intelligence
or whether generated Japanese is faithful to the original character.
"""
from __future__ import annotations

import json
import os
import threading
import wave
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QObject, QRect, pyqtSignal

from aipet.application import configure_voice_trigger
from aipet.core.backends import APIBackend, CharacterReply, ScreenAnalysis
from aipet.core.config import AppSettings
from aipet.core.nene_continuity import ContinuityUpdate
from aipet.core.storage import ScreenMemoryEntry, ScreenMemoryStore
from aipet.core.workers import ConversationResult, ConversationWorker
from aipet.ui.pet import Murasame
from aipet.ui.live2d_renderer import OverlayLayout


def reply(text="好，我们一起读。", continuity=None):
    return CharacterReply.model_validate({"sentences": [{
        "zh": text, "en": "All right, let's read together.",
        "ja": "うん、一緒に読もう。", "emotion": "平静", "portrait": "b",
    }], "continuity": continuity})


class ClockPlayer(QObject):
    started = pyqtSignal()
    position_changed = pyqtSignal(float)
    finished = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.stop = Mock()
        self.disposed = False

    def play(self, path):
        self.started.emit()
        return True

    def deleteLater(self):
        self.disposed = True
        super().deleteLater()


class HumanizationRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requests = []
        self.workers = []
        self.deferred = False
        self.deliver = True
        self.filtered = False
        self.next_reply = reply()
        runtime = Mock()
        runtime.input.idle_seconds.return_value = 0.0
        runtime.windowing.topmost_available.return_value = False
        self.runtime = runtime
        for substitution in (
            patch.dict(os.environ, {"AIPET_DATA_DIR": str(self.root), "AIPET_RENDERER": ""}),
            patch("aipet.ui.pet.generate_fgimage", return_value=np.zeros((32, 16, 4), dtype=np.uint8)),
            patch.object(APIBackend, "_headers", return_value={}),
            patch.object(APIBackend, "_json_response", side_effect=self.transport),
            patch("requests.Session.request", side_effect=AssertionError("Real network forbidden")),
            patch("aipet.core.workers.TTSClient", side_effect=AssertionError("Real TTS forbidden")),
            patch("aipet.ui.pet.ConversationWorker", side_effect=self.worker_factory),
            # Unit tests must never access a real speaker. Individual clock
            # tests below replace this with a signal-driven fake player.
            patch("aipet.ui.pet.AudioPlayback", return_value=Mock(play=Mock(return_value=False))),
        ):
            substitution.start()
            self.addCleanup(substitution.stop)
        settings = AppSettings.model_validate({
            "mode": "api", "ui_language": "zh-CN", "history_limit": 30,
            "api": {"provider": "deepseek", "deepseek_api_key": "offline-test-only"},
            "character": {"profile_id": "nene", "name": "宁宁", "name_en": "Nene",
                          "personality_file": "prompt.nene.txt", "personality_file_en": "prompt.nene.txt"},
            "tts": {"enabled": False}, "stt": {"enabled": False}, "vision": {"enabled": False},
        })
        self.pet = Murasame(settings, runtime)
        self.addCleanup(self.pet.deleteLater)
        self.addCleanup(self.pet.shutdown)
        self.errors = []
        self.pet.notification.connect(lambda title, text: self.errors.append((title, text)))

    def transport(self, *args, **kwargs):
        payload = kwargs["json"]
        self.requests.append(payload)
        if "independent relationship-transition verifier" in payload["messages"][0]["content"]:
            update = self.next_reply.continuity
            content = json.dumps({"approved": True, "action": update.relationship_action,
                                  "basis": update.relationship_basis,
                                  "supported_quotes": update.relationship_evidence,
                                  "reason": "The fixture provides direct, explicit evidence."})
        else:
            content = self.next_reply.model_dump_json(exclude_none=True)
        return {"choices": [{"finish_reason": "content_filter" if self.filtered else "stop",
                             "message": {"content": content}}]}

    def worker_factory(self, *args, **kwargs):
        worker = ConversationWorker(*args, **kwargs)
        def start():
            if not self.deferred:
                worker.run()
                worker.finished.emit()
        worker.start = Mock(side_effect=start)
        self.workers.append(worker)
        return worker

    def exchange(self, text, candidate=None):
        self.next_reply = candidate or reply()
        self.pet._stop_playback()
        self.pet.start_thread(text)
        if self.deliver and self.pet._delivery_result is not None:
            self.complete_sentence()
            while self.pet._delivery_result is not None:
                self.pet._play_next_sentence()
                self.complete_sentence()
        return self.workers[-1]

    def complete_sentence(self):
        self.pet.typing_index = max(0, len(self.pet.full_text) - 1)
        self.pet._typing_step()

    def result(self, candidate=None, text="晚安。"):
        return ConversationResult(candidate or reply(), [], text, True, episode_id=str(uuid4()),
                                  user_at=datetime.now().astimezone().isoformat(), profile_id="nene")

    def attach_avatar(self):
        self.pet._viewer_layout = OverlayLayout(QRect(0, 0, 560, 780),
            QRect(200, 260, 150, 520), QRect(16, 8, 528, 236))
        self.pet._viewerex = Mock()
        self.pet._viewerex.performance_enabled = True
        return self.pet._viewerex

    def test_dnd_stops_sound_and_vision_blocks_all_direct_input_and_drops_late_results(self):
        self.deferred = True
        self.exchange("我们继续聊。")
        old = self.pet._generation
        pending = self.result()
        sound, viewer, vision = Mock(), Mock(), Mock()
        self.pet._sound, self.pet._viewerex, self.pet._vision_worker = sound, viewer, vision
        self.pet.set_dnd_enabled(True)
        sound.stop.assert_called_once()
        vision.cancel.assert_called_once()
        viewer.stop.assert_not_called()
        count = len(self.workers)
        self.pet.begin_text_input()
        self.pet.start_thread("暂停中不发送")
        self.assertFalse(self.pet.input_mode)
        self.assertEqual(len(self.workers), count)
        self.assertFalse(self.pet.idle_timer.isActive())
        self.assertEqual(self.pet.display_text, "")
        self.pet.set_dnd_enabled(False)
        self.pet._on_reply(old, pending)
        self.assertEqual(self.pet.history, [])
        self.assertFalse(self.pet.continuity_store.path.exists())
        self.assertTrue(self.pet._proactive_awaiting_user)
        self.assertFalse(self.pet._try_start_proactive_event("积压闲聊"))
        self.pet._viewerex, self.pet._vision_worker = None, None

    def test_old_screen_result_is_rejected_after_pause_and_resume(self):
        self.pet._screen_baseline_ready = True
        old = getattr(self.pet, "_screen_observation_generation", 0)
        self.pet.set_dnd_enabled(True)
        self.pet.set_dnd_enabled(False)
        analysis = ScreenAnalysis(software="Example", activity="A page", significant_change=True)
        self.pet._on_screen_analysis(analysis, observation_generation=old)
        self.assertEqual(self.pet.screen_memory_store.entries, [])

    def test_response_target_is_soft_and_cancel_invalidates_inflight_reply(self):
        self.deferred = True
        self.exchange("请继续。")
        old = self.pet._generation
        worker = self.workers[-1]
        self.pet._on_response_target_reached()
        self.assertTrue(self.pet._response_target_elapsed)
        self.assertFalse(worker._cancelled.is_set())
        self.assertIn("仍在处理", self.pet._thinking_label)
        self.pet.cancel_conversation()
        self.assertTrue(worker._cancelled.is_set())
        self.assertFalse(self.pet.response_target_timer.isActive())
        self.pet._on_reply(old, self.result())
        self.assertEqual(self.pet.history, [])

    def test_test_session_forks_formal_state_and_never_merges_relationship_changes(self):
        original = "约好了，周末我们一起读这本书。"
        self.exchange(original, reply(continuity={"mood": "期待", "mood_evidence": original,
            "items": [{"kind": "expectation", "text": "周末一起读书", "evidence": original}]}))
        self.assertEqual(self.errors, [])
        formal_history = self.pet.history_store.path
        formal_state = self.pet.continuity_store.path
        before_history, before_state = formal_history.read_bytes(), formal_state.read_bytes()
        self.pet.set_test_mode(True)
        sandbox = self.pet.history_store.path
        self.assertNotEqual(sandbox, formal_history)
        quote = "我认真确认了，我要结束我们的恋人关系。"
        update = {"relationship_action": "end", "relationship_basis": "user_confirmed_end",
                  "relationship_reason": "对方明确认真确认结束。", "relationship_evidence": [quote]}
        self.exchange(quote, reply("我明白了。", update))
        self.assertEqual(self.errors, [])
        self.assertEqual(self.pet.continuity_store.relationship_status, "separated")
        self.assertEqual(formal_history.read_bytes(), before_history)
        self.assertEqual(formal_state.read_bytes(), before_state)
        self.pet.set_test_mode(False)
        self.assertEqual(self.pet.continuity_store.relationship_status, "lovers")
        self.assertEqual(self.pet.history_store.path, formal_history)
        self.assertEqual(formal_history.read_bytes(), before_history)
        self.assertIn("结束", sandbox.read_text(encoding="utf-8"))

    def test_late_sandbox_reply_cannot_mutate_restored_formal_state(self):
        self.pet.set_test_mode(True)
        self.deferred = True
        self.exchange("测试里的对话")
        old = self.pet._generation
        self.pet.set_test_mode(False)
        self.pet._on_reply(old, self.result())
        self.assertEqual(self.pet.history, [])
        self.assertEqual(self.pet.continuity_store.relationship_status, "lovers")

    def test_test_outfit_is_never_selected_for_formal_config_persistence(self):
        original = self.pet.settings.character.outfit
        self.pet.set_test_mode(True)
        self.pet.settings.character.outfit = "sleepwear" if original != "sleepwear" else "casual"
        self.assertEqual(self.pet.settings_for_persistence().character.outfit, original)
        self.pet.set_test_mode(False)
        self.assertEqual(self.pet.settings.character.outfit, original)
        self.assertEqual(self.pet._current_outfit, original)

    def test_provider_filter_uses_system_status_and_no_formal_memory_or_tts(self):
        self.filtered = True
        self.exchange("一个普通问题。")
        self.assertTrue(self.errors)
        self.assertIn("系统", self.pet.display_text)
        self.assertEqual(self.pet.history_store.conversation_count, 0)
        self.assertEqual(self.pet.history, [])
        self.assertFalse(self.pet.continuity_store.path.exists())
        self.assertEqual(len(self.requests), 1)

    def test_character_can_refuse_in_valid_json_and_remain_a_real_exchange(self):
        self.exchange("能谈一下那件事吗？", reply("这件事我现在不想说。"))
        self.assertEqual(self.errors, [])
        self.assertEqual(self.pet.history_store.conversation_count, 1)
        self.assertIn("不想说", self.pet.history[-1]["content"])

    def test_silent_proactive_decision_is_not_spoken_or_added_to_history(self):
        self.next_reply = CharacterReply(should_speak=False, sentences=[])
        event = "如果没有值得聊的具体内容，这次保持安静。"
        self.pet._try_start_proactive_event(event)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.pet.history, [])
        self.assertTrue(self.pet._proactive_awaiting_user)
        self.assertFalse(self.pet._try_start_proactive_event(event))

    def test_new_concrete_reason_can_be_considered_after_unanswered_topic(self):
        self.next_reply = CharacterReply(should_speak=False, sentences=[])
        self.pet._try_start_proactive_event("第一件事", event_key="screen:first")
        self.pet._proactive_cooldown_until = 0
        self.assertFalse(self.pet._try_start_proactive_event("同一件事又描述一次", event_key="screen:first"))
        self.assertTrue(self.pet._try_start_proactive_event("另一件有具体依据的事情", event_key="screen:second"))

    def test_frozen_long_term_user_evidence_is_used_for_relationship_commit(self):
        old = "你认真整理的两页笔记我当废纸扔了。"
        self.exchange(old, reply("我很不高兴，请不要再这样对待我的用心。", {
            "items": [{"kind": "boundary", "text": "不要再故意轻视我整理的笔记", "evidence": old}]}))
        self.pet.history.clear()  # Simulate a short-context window rolling over.
        current = "你整理的笔记我还要撕掉，我就是故意无视你的边界。"
        update = {"relationship_action": "end", "relationship_basis": "continued_after_boundary",
                  "relationship_reason": "明确表达边界后，对方仍然故意继续伤害。",
                  "relationship_evidence": [old, current]}
        worker = self.exchange(current, reply("我不能再继续这段关系了。", update))
        self.assertIn(old, worker.user_evidence)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.pet.continuity_store.relationship_status, "separated")

    def test_unapproved_relationship_change_is_never_archived_or_played(self):
        quote = "我认真确认了，我要结束我们的恋人关系。"
        candidate = reply("我明白了。", {"relationship_action": "end",
            "relationship_basis": "user_confirmed_end", "relationship_reason": "明确结束",
            "relationship_evidence": [quote]})
        self.pet._on_reply(self.pet._generation, self.result(candidate, quote))
        self.assertEqual(self.pet.history, [])
        self.assertIsNone(self.pet._playback_result)

    def test_save_failure_after_display_reports_unsaved_state_truthfully(self):
        quote = "我认真确认了，我要结束我们的恋人关系。"
        candidate = reply("我明白了。", {"relationship_action": "end",
            "relationship_basis": "user_confirmed_end", "relationship_reason": "明确结束",
            "relationship_evidence": [quote]})
        candidate.continuity_review_approved = True
        with patch.object(self.pet.continuity_store, "apply", side_effect=OSError("mock disk error")):
            self.pet._on_reply(self.pet._generation, self.result(candidate, quote))
            self.assertEqual(self.pet.history, [])
            self.complete_sentence()
        self.assertEqual(len(self.pet.history), 2)
        self.assertEqual(self.pet.continuity_store.relationship_status, "lovers")
        self.assertTrue(any("已显示" in message for _, message in self.errors))

    def test_cancel_before_first_complete_subtitle_records_nothing(self):
        self.deliver = False
        quote = "我认真确认了，我要结束我们的恋人关系。"
        self.exchange(quote, reply("我明白了。", {"relationship_action": "end",
            "relationship_basis": "user_confirmed_end", "relationship_reason": "明确结束",
            "relationship_evidence": [quote]}))
        self.assertEqual(self.errors, [])
        self.assertEqual(self.pet.continuity_store.relationship_status, "lovers")
        self.assertEqual(self.pet.history, [])
        self.pet._typing_step()
        self.pet.set_dnd_enabled(True)
        self.assertEqual(self.pet.history, [])
        self.assertFalse(self.pet.continuity_store.path.exists())

    def test_interruption_records_only_complete_prefix_without_state_patch(self):
        self.deliver = False
        first = reply("先把这一页看完。", {"mood": "期待", "mood_evidence": "一起读书。"})
        first.sentences.append(reply("下一页我们明天再看。").sentences[0])
        self.exchange("一起读书。", first)
        self.complete_sentence()
        self.assertEqual(self.pet.history, [])
        self.pet._play_next_sentence()
        self.pet._typing_step()
        self.pet.cancel_conversation()
        self.assertEqual(len(self.pet.history), 2)
        archived = json.loads(self.pet.history[-1]["content"])
        self.assertEqual([s["zh"] for s in archived["sentences"]], ["先把这一页看完。"])
        self.assertIsNone(archived.get("continuity"))
        self.assertEqual(self.pet.history[-1]["delivery"], "interrupted")
        self.assertFalse(self.pet.continuity_store.path.exists())
        self.assertEqual(self.pet.history_store.conversation_count, 1)

    def test_major_change_commits_on_complete_subtitle_exactly_once(self):
        self.deliver = False
        quote = "我认真确认了，我要结束我们的恋人关系。"
        self.exchange(quote, reply("我明白了。", {"relationship_action": "end",
            "relationship_basis": "user_confirmed_end", "relationship_reason": "明确结束",
            "relationship_evidence": [quote]}))
        self.assertEqual(self.pet.continuity_store.relationship_status, "lovers")
        self.assertEqual(self.pet.history, [])
        self.complete_sentence()
        self.assertEqual(self.pet.continuity_store.relationship_status, "separated")
        self.assertEqual(self.pet.history_store.conversation_count, 1)
        self.pet._typing_step()
        self.pet._stop_playback()
        self.assertEqual(len(self.pet.history), 2)
        self.assertEqual(self.pet.history_store.conversation_count, 1)

    def test_timer_cannot_skip_a_subtitle_that_has_not_finished_typing(self):
        self.deliver = False
        candidate = reply("先把这一页看完。")
        candidate.sentences.append(reply("下一页我们明天再看。").sentences[0])
        self.exchange("一起读书。", candidate)
        self.pet._play_next_sentence()
        self.assertEqual(self.pet._playback_index, 1)
        self.assertEqual(self.pet.history, [])
        self.complete_sentence()
        self.pet._play_next_sentence()
        self.assertEqual(self.pet._playback_index, 2)

    def test_language_change_flushes_old_display_before_replacing_settings(self):
        self.deliver = False
        candidate = reply("先把这一页看完。")
        candidate.sentences.append(reply("下一页我们明天再看。").sentences[0])
        self.exchange("一起读书。", candidate)
        self.complete_sentence()
        self.pet._play_next_sentence()
        changed = self.pet.settings.model_copy(deep=True)
        changed.ui_language = "en"
        self.pet.apply_settings(changed)
        self.assertIsNone(self.pet._delivery_result)
        self.assertIsNone(self.pet._playback_result)
        self.assertFalse(self.pet.playback_timer.isActive())
        self.assertEqual(self.pet.history[-1]["delivery"], "interrupted")
        self.assertEqual(len(json.loads(self.pet.history[-1]["content"])["sentences"]), 1)

    def test_recording_interrupts_old_reply_and_failure_does_not_strand_delivery(self):
        self.deliver = False
        self.exchange("一起读书。")
        old = self.pet._generation
        self.pet.settings.stt.enabled = True
        configure_voice_trigger(self.pet, self.pet.settings, Mock(), self.runtime)
        callbacks = self.runtime.input.create_voice_trigger.call_args.kwargs
        callbacks["on_record_start"]()
        self.assertGreater(self.pet._generation, old)
        self.assertTrue(self.pet._voice_input_pending)
        self.assertFalse(self.pet.input_mode)
        self.assertFalse(self.pet._try_start_proactive_event("识别时不插话"))
        self.assertIsNone(self.pet._delivery_result)
        self.assertIsNone(self.pet._playback_result)
        callbacks["on_error"]("模拟识别失败")
        self.assertFalse(self.pet._voice_input_pending)
        self.assertFalse(self.pet.playback_timer.isActive())
        self.assertEqual(self.pet.history, [])
        self.assertIn("失败", self.pet.display_text)
        callbacks["on_record_start"]()
        callbacks["on_text_ready"]("本次新的语音输入")
        self.assertEqual(self.workers[-1].user_text, "本次新的语音输入")

    def test_new_request_context_includes_only_the_delivered_prefix(self):
        self.deliver = False
        candidate = reply("先把这一页看完。")
        candidate.sentences.append(reply("下一页我们明天再看。").sentences[0])
        self.exchange("一起读书。", candidate)
        self.complete_sentence()
        self.pet._play_next_sentence()
        self.deferred = True
        self.pet.start_thread("先等等，我想换本书。")
        worker = self.workers[-1]
        content = json.loads(worker.history[-1]["content"])
        self.assertEqual([s["zh"] for s in content["sentences"]], ["先把这一页看完。"])
        self.assertEqual(worker.history[-1]["delivery"], "interrupted")

    def test_english_disappointment_uses_the_important_soft_target(self):
        quote = "今天先不一起读书了。"
        self.pet.continuity_store.apply({"mood": "disappointed", "mood_evidence": quote}, quote, "prior-fixture")
        self.deferred = True
        self.exchange("刚才的事情我想再解释一下。")
        self.assertGreater(self.pet.response_target_timer.interval(), 29000)
        self.assertLessEqual(self.pet.response_target_timer.interval(), 30000)

    def test_avatar_keeps_delivered_displeasure_after_speech_ends(self):
        viewer = self.attach_avatar()
        quote = "你花两个小时整理这些笔记，真是白费力气。"
        candidate = reply("请不要这样轻视我的用心。", {"mood": "生气", "mood_evidence": quote})
        candidate.sentences[0].emotion = "生气"
        self.exchange(quote, candidate)
        self.assertEqual(self.pet._avatar_last_emotion, "生气")
        self.pet._play_next_sentence()
        viewer.finish_speech.assert_called()
        viewer.set_base_emotion.assert_called_with("生气", strength=0.72)
        viewer.set_emotion.assert_not_called()
        self.pet._viewerex = None

    def test_avatar_receives_actual_audio_before_player_starts(self):
        viewer = self.attach_avatar()
        audio = self.root / "synthesized-fixture.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000)
        result = self.result()
        result.audio_paths.append(audio)
        order = Mock()
        order.attach_mock(viewer, "avatar")
        with patch("aipet.ui.pet.QSound") as sound:
            order.attach_mock(sound, "sound")
            self.pet._on_reply(self.pet._generation, result)
            viewer.play_speech.assert_called_once_with("平静", audio, duration_seconds=1.4)
            sound.return_value.play.assert_called_once()
            names = [c[0] for c in order.mock_calls]
            self.assertLess(names.index("avatar.play_speech"), names.index("sound().play"))
        self.pet._stop_playback()
        self.pet._viewerex = None

    def clock_reply(self, count=1, player=None):
        viewer = self.attach_avatar()
        viewer.prepare_timeline_speech.return_value = True
        audio = self.root / "clock-fixture.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 32000)
        candidate = reply("这是用来确认播放进度的文字。")
        if count > 1:
            candidate.sentences.extend([candidate.sentences[0].model_copy() for _ in range(count - 1)])
        result = self.result(candidate)
        result.audio_paths.extend([audio] * count)
        player = player or ClockPlayer()
        self.addCleanup(setattr, self.pet, "_viewerex", None)
        with patch("aipet.ui.pet.AudioPlayback", return_value=player):
            self.pet._on_reply(self.pet._generation, result)
        return player, viewer, result

    def test_audio_clock_advances_caption_only_with_processed_audio(self):
        player, viewer, _ = self.clock_reply()
        self.assertTrue(self.pet._audio_clock_active)
        self.assertEqual(self.pet.typing_index, 0)
        self.pet._typing_step()
        self.assertEqual(self.pet.typing_index, 0)
        player.position_changed.emit(1.0)
        self.assertEqual(self.pet.typing_index, len(self.pet.full_text) // 2)
        viewer.sync_speech_position.assert_called_with(1.0)
        player.position_changed.emit(.5)
        self.assertEqual(self.pet.typing_index, len(self.pet.full_text) // 2)
        self.assertEqual(self.pet.history, [])

    def test_utility_workspace_blocks_proactive_events_without_blocking_body_input(self):
        self.pet._utility_busy = True
        self.assertTrue(self.pet._proactive_event_blocked())
        self.assertFalse(self.pet._try_start_proactive_event("fixture event"))
        started = Mock()
        self.pet.interaction_started.connect(started)
        self.pet.begin_text_input()
        self.assertTrue(self.pet.input_mode)
        started.assert_called_once()

    def test_normal_conversation_signals_workspace_but_dnd_drops_new_interactions(self):
        started = Mock()
        self.pet.interaction_started.connect(started)
        self.deferred = True
        self.pet.start_thread("请继续说。")
        started.assert_called_once()
        self.pet.set_dnd_enabled(True)
        self.pet.begin_text_input()
        self.pet.start_thread("暂停中不发送")
        started.assert_called_once()

    def test_audio_clock_waits_for_backend_end_and_releases_player(self):
        player, viewer, _ = self.clock_reply(count=2)
        player.position_changed.emit(2.0)
        self.pet._play_next_sentence()
        self.assertEqual(self.pet._playback_index, 1)
        self.assertTrue(self.pet._audio_clock_active)
        player.finished.emit()
        self.assertEqual(self.pet.typing_index, len(self.pet.full_text))
        self.assertFalse(self.pet._audio_clock_active)
        self.assertIsNone(self.pet._audio_clock_player)
        self.assertIsNone(self.pet._sound)
        self.assertTrue(player.disposed)
        self.assertEqual(self.pet.playback_timer.interval(), 800)
        viewer.sync_speech_position.assert_called_with(2.0, force=True)

    def test_audio_clock_stop_ignores_late_callbacks_and_stops_resources(self):
        player, viewer, _ = self.clock_reply()
        player.position_changed.emit(.5)
        self.pet._stop_playback()
        old = (self.pet.typing_index, self.pet.display_text)
        viewer.reset_mock()
        player.position_changed.emit(2.0)
        player.finished.emit()
        player.failed.emit("late failure")
        self.assertEqual((self.pet.typing_index, self.pet.display_text), old)
        player.stop.assert_called_once()
        self.assertTrue(player.disposed)
        self.assertFalse(self.pet.playback_timer.isActive())
        viewer.sync_speech_position.assert_not_called()
        self.assertEqual(self.errors, [])

    def test_audio_clock_pause_stops_sound_and_drops_late_completion(self):
        player, viewer, _ = self.clock_reply()
        self.pet.set_dnd_enabled(True)
        viewer.reset_mock()
        player.position_changed.emit(2.0)
        player.finished.emit()
        self.assertEqual(self.pet.display_text, "")
        self.assertEqual(self.pet.history, [])
        self.assertFalse(self.pet.typing_timer.isActive())
        self.assertFalse(self.pet.playback_timer.isActive())
        viewer.sync_speech_position.assert_not_called()

    def test_audio_device_failure_continues_text_without_restarting_sound(self):
        player, viewer, _ = self.clock_reply()
        player.position_changed.emit(.5)
        with patch("aipet.ui.pet.QSound") as fallback:
            player.failed.emit("test device unplugged")
            fallback.assert_not_called()
        self.assertFalse(self.pet._audio_clock_active)
        self.assertTrue(self.pet.typing_timer.isActive())
        self.assertIsNone(self.pet._sound)
        self.assertIsNone(self.pet._audio_clock_player)
        self.assertTrue(player.disposed)
        viewer.finish_speech.assert_called_once()
        self.assertEqual(self.errors, [("语音播放", "音频输出中断，文字将继续显示。")])
        before = self.pet.typing_index
        self.pet._typing_step()
        self.assertEqual(self.pet.typing_index, before + 1)

    def test_synchronous_start_then_failure_restores_fallback_caption_progress(self):
        player = ClockPlayer()
        def failing_play(path):
            player.started.emit()
            player.failed.emit("synchronous output failure")
            return False
        player.play = failing_play
        with patch("aipet.ui.pet.QSound") as fallback:
            _, viewer, _ = self.clock_reply(player=player)
            fallback.return_value.play.assert_called_once()
        self.assertFalse(self.pet._audio_clock_active)
        self.assertTrue(self.pet.typing_timer.isActive())
        self.assertTrue(player.disposed)
        self.pet._typing_step()
        self.assertEqual(self.pet.typing_index, 1)
        self.complete_sentence()
        self.assertEqual(self.pet._delivery_count, 1)
        viewer.finish_speech.assert_called()

    def test_avatar_test_reactions_cannot_leak_back_to_formal_fallback(self):
        viewer = self.attach_avatar()
        self.pet._avatar_last_emotion = "高兴"
        self.pet.set_test_mode(True)
        self.exchange("今天先不一起读了。", reply("我还等着呢。"))
        self.pet._avatar_last_emotion = "生气"
        self.pet.set_test_mode(False)
        viewer.set_base_emotion.assert_called_with("高兴", strength=0.55)
        self.assertEqual(self.pet._avatar_last_emotion, "高兴")
        self.pet._viewerex = None

    def test_avatar_listens_then_thinks_without_talking_and_pause_closes_mouth(self):
        viewer = self.attach_avatar()
        self.pet.begin_text_input()
        viewer.set_activity.assert_called_with("listening", attention=(0.0, 0.22))
        self.deferred = True
        self.exchange("还有一件事。")
        viewer.set_activity.assert_called_with("thinking", attention=(-0.16, 0.10))
        viewer.play_speech.assert_not_called()
        self.pet.set_dnd_enabled(True)
        viewer.finish_speech.assert_called()
        viewer.set_paused.assert_called_with(True)
        self.pet._viewerex = None

    def test_screen_confirmation_preserves_observation_and_requires_literal_user_source(self):
        store = ScreenMemoryStore(self.root / "observations.json")
        entry = ScreenMemoryEntry.now(software="Game", activity="打开游戏", change_summary="标题画面")
        store.remember(entry)
        update = {"observation_id": entry.observation_id, "user_evidence": "我喜欢这个游戏",
                  "interpretation": "用户确认喜欢这个游戏", "status": "confirmed"}
        self.assertFalse(store.confirm([update], user_text="他说‘我喜欢这个游戏’，不是我说的。", episode_id="e1"))
        self.assertFalse(store.confirm([update], user_text="假如我说我喜欢这个游戏呢？", episode_id="e2"))
        self.assertTrue(store.confirm([update], user_text="我喜欢这个游戏。", episode_id="e3"))
        restored = ScreenMemoryStore(store.path).entries[0]
        self.assertEqual(restored.source, "screen_observation")
        self.assertEqual(restored.activity, "打开游戏")
        self.assertEqual(restored.status, "confirmed")
        self.assertEqual(restored.confirmed_by_episode, "e3")

    def test_queued_voice_text_cannot_cross_pause_or_test_boundary(self):
        self.pet.settings.stt.enabled = True
        tray = Mock()
        configure_voice_trigger(self.pet, self.pet.settings, tray, self.runtime)
        callbacks = self.runtime.input.create_voice_trigger.call_args.kwargs
        callbacks["on_record_start"]()
        thread = threading.Thread(target=lambda: callbacks["on_text_ready"]("迟到的识别内容"))
        thread.start()
        thread.join(1)
        self.pet.set_dnd_enabled(True)
        self.pet.set_dnd_enabled(False)
        self.app.processEvents()
        self.assertEqual(self.workers, [])
        self.assertEqual(self.pet.history, [])
        callbacks["on_record_start"]()
        thread = threading.Thread(target=lambda: callbacks["on_text_ready"]("切换前的旧识别"))
        thread.start()
        thread.join(1)
        self.pet.set_test_mode(True)
        self.app.processEvents()
        self.assertEqual(self.workers, [])
        self.assertEqual(self.pet.history, [])


if __name__ == "__main__":
    unittest.main()
