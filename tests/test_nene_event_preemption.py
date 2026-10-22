"""Non-user events must not discard a requested reply during synthesis/playback."""
from __future__ import annotations

import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from PyQt5.QtCore import Qt

from aipet.core.config import AppSettings
from aipet.core.backends import CharacterReply
from aipet.core.workers import ConversationResult, ConversationWorker
from aipet.core.storage import HistoryStore
from aipet.ui.pet import Murasame


class EventPreemptionTests(unittest.TestCase):
    def make_pet(self):
        settings = AppSettings.model_validate({
            "character": {"profile_id": "nene", "name": "宁宁"},
            "vision": {"enabled": False},
        })
        pet = SimpleNamespace(
            _closing=False, _dnd_enabled=False, input_mode=False,
            _generation=7, _workers={}, _vision_worker=Mock(),
            _playback_result=None, _sound=None, _viewerex=None,
            _proactive_cooldown_until=0.0, settings=settings,
            history=[], history_store=HistoryStore(separate_proactive=True),
            screen_memory_store=Mock(),
            playback_timer=Mock(), typing_timer=Mock(),
            _stop_thinking_animation=Mock(), _start_thinking_animation=Mock(),
            _remove_audio_files=Mock(), _on_reply=Mock(), _on_worker_error=Mock(),
            _on_worker_stage=Mock(),
            notification=Mock(), touch_head=False, head_press_x=None,
            drag_offset=None, pet_name="宁宁",
        )
        for name in (
            "_proactive_event_blocked", "_try_start_proactive_event",
            "start_thread", "_cancel_active_jobs", "_stop_playback",
            "_finish_worker", "mouseMoveEvent",
        ):
            setattr(pet, name, MethodType(getattr(Murasame, name), pet))
        pet._localized = lambda english, chinese: chinese
        return pet

    def assert_untouched(self, pet, generation, active_worker=None, sound=None):
        self.assertEqual(pet._generation, generation)
        pet.playback_timer.stop.assert_not_called()
        pet._start_thinking_animation.assert_not_called()
        if active_worker is not None:
            active_worker.cancel.assert_not_called()
        if sound is not None:
            sound.stop.assert_not_called()

    def test_system_event_cannot_cancel_chat_or_tts_worker(self):
        # A ConversationWorker stays owned through chat, cold start and TTS.
        for state in ("chat", "tts_startup", "tts_synthesis", "cancel_draining"):
            with self.subTest(state=state):
                pet = self.make_pet()
                active = pet._workers[7] = Mock()
                with patch("aipet.ui.pet.ConversationWorker") as factory:
                    for _ in range(3):
                        pet.start_thread("摸头事件", role="system")
                factory.assert_not_called()
                self.assertEqual(pet._workers, {7: active})
                self.assert_untouched(pet, 7, active_worker=active)

    def test_system_event_cannot_stop_audio_after_worker_finished(self):
        pet = self.make_pet()
        result = pet._playback_result = object()
        sound = pet._sound = Mock()
        with patch("aipet.ui.pet.ConversationWorker") as factory:
            pet.start_thread("摸头事件", role="system")
        factory.assert_not_called()
        self.assertIs(pet._playback_result, result)
        self.assert_untouched(pet, 7, sound=sound)
        pet._remove_audio_files.assert_not_called()

    def test_system_event_respects_input_dnd_and_shutdown(self):
        for field in ("input_mode", "_dnd_enabled", "_closing"):
            with self.subTest(field=field):
                pet = self.make_pet()
                setattr(pet, field, True)
                with patch("aipet.ui.pet.ConversationWorker") as factory:
                    pet.start_thread("摸头事件", role="system")
                factory.assert_not_called()
                self.assert_untouched(pet, 7)

    def test_explicit_typed_and_voice_messages_still_replace_old_reply(self):
        for source in ("typed", "voice"):
            with self.subTest(source=source):
                pet = self.make_pet()
                active = pet._workers[7] = Mock()
                result = pet._playback_result = object()
                sound = pet._sound = Mock()
                # While active, explicit input still replaces older work.
                # Nene's new full pause is checked separately below.
                pet._dnd_enabled = False
                with patch("aipet.ui.pet.ConversationWorker") as factory:
                    worker = factory.return_value
                    pet.start_thread("你好", role="user", source=source)
                active.cancel.assert_called_once_with()
                pet._vision_worker.cancel.assert_called_once_with()
                sound.stop.assert_called_once_with()
                pet._remove_audio_files.assert_called_once_with(result)
                factory.assert_called_once()
                self.assertEqual(factory.call_args.args[2], "你好")
                self.assertIsNone(factory.call_args.kwargs["event_context"])
                self.assertEqual(factory.call_args.kwargs["user_source"], source)
                worker.start.assert_called_once_with()
                self.assertEqual(pet._generation, 9)
                self.assertIs(pet._workers[9], worker)

    def test_nene_full_pause_rejects_typed_and_voice_requests(self):
        for source in ("typed", "voice"):
            with self.subTest(source=source):
                pet = self.make_pet()
                pet._dnd_enabled = True
                with patch("aipet.ui.pet.ConversationWorker") as factory:
                    pet.start_thread("暂停中的输入", source=source)
                factory.assert_not_called()
                self.assertEqual(pet._generation, 7)

    def test_head_pat_is_consumed_while_busy_without_scheduling_a_late_reply(self):
        pet = self.make_pet()
        active = pet._workers[7] = Mock()
        pet.touch_head, pet.head_press_x = True, 10
        event = Mock()
        event.x.return_value = 80
        event.buttons.return_value = Qt.LeftButton
        with patch("aipet.ui.pet.ConversationWorker") as factory:
            pet.mouseMoveEvent(event)
            self.assertFalse(pet.touch_head)
            self.assert_untouched(pet, 7, active_worker=active)
            # Finishing the old worker does not replay a discarded head pat.
            pet._finish_worker(7, active)
            pet.mouseMoveEvent(event)
        active.deleteLater.assert_called_once_with()
        factory.assert_not_called()
        self.assertEqual(pet._workers, {})

    def test_head_pat_when_idle_keeps_original_interaction_and_no_idle_cooldown(self):
        pet = self.make_pet()
        pet._proactive_cooldown_until = float("inf")
        pet.touch_head, pet.head_press_x = True, 10
        event = Mock()
        event.x.return_value = 80
        event.buttons.return_value = Qt.LeftButton
        with patch("aipet.ui.pet.ConversationWorker") as factory:
            pet.mouseMoveEvent(event)
        self.assertFalse(pet.touch_head)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[2], "")
        self.assertEqual(factory.call_args.kwargs["event_context"], "用户轻轻摸了摸宁宁的头。")
        factory.return_value.start.assert_called_once_with()

    def test_idle_events_skip_busy_reply_without_spending_cooldown_or_queueing(self):
        for state in ("worker", "playback", "input"):
            with self.subTest(state=state):
                pet = self.make_pet()
                if state == "worker":
                    pet._workers[7] = Mock()
                elif state == "playback":
                    pet._playback_result = object()
                else:
                    pet.input_mode = True
                with patch("aipet.ui.pet.ConversationWorker") as factory:
                    self.assertFalse(pet._try_start_proactive_event("空闲提醒"))
                self.assertEqual(pet._proactive_cooldown_until, 0.0)
                factory.assert_not_called()
                self.assert_untouched(pet, 7)

    def test_idle_event_still_runs_once_when_available_then_obeys_cooldown(self):
        pet = self.make_pet()
        with patch("aipet.ui.pet.ConversationWorker") as factory, patch(
            "aipet.ui.pet.time.monotonic", return_value=1000.0,
        ):
            self.assertTrue(pet._try_start_proactive_event("空闲提醒"))
            worker = factory.return_value
            pet._finish_worker(pet._generation, worker)
            self.assertFalse(pet._try_start_proactive_event("另一个提醒"))
        factory.assert_called_once()
        self.assertEqual(pet._proactive_cooldown_until, 1180.0)


class VoiceWaitingHintTests(unittest.TestCase):
    def make_pet(self, language="zh-CN"):
        pet = SimpleNamespace(
            _closing=False, _generation=7, _thinking_label="",
            _thinking_dot_count=0, typing_timer=Mock(), thinking_timer=Mock(),
            update=Mock(), _speaker_prefix=lambda: "宁宁：",
        )
        pet._localized = lambda english, chinese: english if language == "en" else chinese
        for name in ("_on_worker_stage", "_start_thinking_animation", "_thinking_step"):
            setattr(pet, name, MethodType(getattr(Murasame, name), pet))
        return pet

    def test_current_voice_stage_stays_visible_during_thinking_ticks_in_both_languages(self):
        for language, label in (("zh-CN", "正在生成语音"), ("en", "Generating voice")):
            with self.subTest(language=language):
                pet = self.make_pet(language)
                pet._on_worker_stage(7, "voice")
                self.assertEqual(pet.display_text, "宁宁：" + label + ".")
                pet._thinking_step()
                self.assertEqual(pet.display_text, "宁宁：" + label + "..")

    def test_stale_stage_cannot_overwrite_new_request_and_closing_is_inert(self):
        for closing, generation, stage in ((False, 6, "voice"), (True, 7, "voice"), (False, 7, "other")):
            with self.subTest(closing=closing, generation=generation, stage=stage):
                pet = self.make_pet()
                pet._closing = closing
                pet._thinking_label = "existing"
                pet.display_text = "new request"
                pet._on_worker_stage(generation, stage)
                self.assertEqual(pet._thinking_label, "existing")
                self.assertEqual(pet.display_text, "new request")
                pet.update.assert_not_called()

    def test_new_request_restores_original_thinking_dots(self):
        pet = self.make_pet()
        pet._on_worker_stage(7, "voice")
        pet._generation += 1
        pet._start_thinking_animation()
        self.assertEqual(pet._thinking_label, "")
        self.assertEqual(pet.display_text, "宁宁：.")
        pet.thinking_timer.start.assert_called_once_with()

    def test_worker_announces_voice_before_tts_but_not_when_disabled(self):
        reply = CharacterReply.model_validate({"sentences": [{
            "zh": "你好。", "en": "Hello.", "ja": "こんにちは。", "emotion": "平静",
        }]})
        for enabled in (False, True):
            with self.subTest(tts_enabled=enabled):
                settings = AppSettings()
                settings.tts.enabled = enabled
                worker = ConversationWorker(settings, [], "你好")
                events, results, errors = [], [], []
                worker.stage_changed.connect(lambda stage: events.append(stage))
                worker.result_ready.connect(results.append)
                worker.error.connect(errors.append)
                with patch("aipet.core.workers.create_backend") as backend, patch(
                    "aipet.core.workers.TTSClient",
                ) as tts:
                    backend.return_value.chat.side_effect = lambda *_: (events.append("chat") or reply)
                    tts.side_effect = lambda *_args, **_kwargs: (events.append("tts") or Mock(synthesize=Mock(return_value=None)))
                    worker.run()
                self.assertEqual(events, ["chat", "voice", "tts"] if enabled else ["chat"])
                self.assertFalse(errors)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].reply, reply)


class PlaybackLoggingTests(unittest.TestCase):
    def test_playback_log_distinguishes_player_call_from_missing_audio(self):
        reply = CharacterReply.model_validate({"sentences": [{
            "zh": "你好。", "en": "Hello.", "ja": "こんにちは。", "emotion": "平静",
        }]})
        for available in (False, True):
            with self.subTest(available=available):
                audio_path = Mock()
                audio_path.exists.return_value = available
                pet = SimpleNamespace(
                    _closing=False, _viewerex=None, _playback_index=0, _sound=None,
                    _playback_result=ConversationResult(
                        reply=reply, audio_paths=[audio_path if available else None],
                        user_text="你好", is_user_message=True,
                    ),
                    settings=AppSettings(ui_language="zh-CN"), update_portrait=Mock(), show_text=Mock(),
                    playback_timer=Mock(),
                    _audio_duration_milliseconds=Mock(return_value=1200 if available else 0),
                )
                with patch("aipet.ui.pet.QSound") as sound, patch("aipet.ui.pet.logger") as logger:
                    Murasame._play_next_sentence(pet)
                if available:
                    sound.return_value.play.assert_called_once_with()
                    logger.info.assert_called_once_with(
                        "语音播放开始 | duration_ms=%d | 音频=%s", 1200, audio_path,
                    )
                    pet.playback_timer.start.assert_called_once_with(1600)
                else:
                    sound.assert_not_called()
                    logger.info.assert_called_once_with("此句无可播放语音 | sentence_index=%d", 0)
                    pet.playback_timer.start.assert_called_once_with(920)


if __name__ == "__main__":
    unittest.main()
