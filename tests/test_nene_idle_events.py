from __future__ import annotations

from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aipet.core.backends import CharacterReply, build_messages
from aipet.core.config import AppSettings
from aipet.core.workers import ConversationWorker
from aipet.ui.pet import Murasame


class NeneIdleEventTests(unittest.TestCase):
    def make_pet(self, profile="nene", language="zh-CN"):
        settings = AppSettings.model_validate({
            "ui_language": language,
            "character": {"profile_id": profile, "name": "宁宁", "name_en": "Nene",
                          "personality_file": "prompt.nene.txt", "personality_file_en": "prompt.nene.txt"},
            "idle": {"thinking_minutes": 1, "away_minutes": 2},
            "tts": {"enabled": False},
        })
        # Exercise the real timer-state methods without constructing a widget,
        # reading history, listening to input, or starting a request/thread.
        pet = SimpleNamespace(
            settings=settings, _closing=False, _dnd_enabled=False,
            input_mode=False, _workers={}, _playback_result=None,
            _proactive_cooldown_until=0, _platform_runtime=Mock(),
            idle_thinking_triggered=False, idle_away_triggered=False,
            away_trigger_time=None, start_thread=Mock(),
        )
        pet._localized = lambda english, chinese: english if language == "en" else chinese
        pet._reset_idle_state = MethodType(Murasame._reset_idle_state, pet)
        pet._proactive_event_blocked = MethodType(Murasame._proactive_event_blocked, pet)
        pet._try_start_proactive_event = MethodType(Murasame._try_start_proactive_event, pet)
        return pet

    def tick(self, pet, idle_seconds, *, wall_time=1000, monotonic=1000):
        with patch("aipet.ui.pet.get_idle_seconds", return_value=idle_seconds), patch(
            "aipet.ui.pet.time.time", return_value=wall_time
        ), patch("aipet.ui.pet.time.monotonic", return_value=monotonic):
            Murasame.check_idle_state(pet)

    def trigger(self, pet, phase):
        if phase == "return":
            pet.idle_away_triggered = True
            pet.away_trigger_time = 900
        self.tick(pet, {"thinking": 90, "away": 180, "return": 0}[phase])
        pet.start_thread.assert_called_once()
        self.assertEqual(pet.start_thread.call_args.kwargs, {"role": "system"})
        return pet.start_thread.call_args.args[0]

    def test_nene_idle_events_report_observation_without_prescribing_a_mood(self):
        for language in ("zh-CN", "en"):
            for phase in ("thinking", "away", "return"):
                with self.subTest(language=language, phase=phase):
                    event = self.trigger(self.make_pet(language=language), phase)
                    # All three branches must carry sensor evidence rather
                    # than an instruction to comfort, greet or gently check in.
                    for required in (
                        ("mouse or keyboard input", "actual conversation", "current feelings")
                        if language == "en" else ("鼠标或键盘输入", "实际对话", "当前心情")
                    ):
                        self.assertIn(required, event.lower() if language == "en" else event)
                    for old_directive in (
                        "主人", "Master", "ご主人", "温柔", "轻声", "提醒", "安慰",
                        "休息", "thinking, daydreaming", "gently", "Gently",
                        "welcome", "welcome back", "take an appropriate break",
                    ):
                        self.assertNotIn(old_directive, event)
                    if phase == "return":
                        self.assertIn(
                            "does not establish that the user left or returned"
                            if language == "en" else "不代表对方离开后返回",
                            event,
                        )
                        self.assertIn(
                            "or that they said anything" if language == "en"
                            else "不代表对方说了话",
                            event,
                        )
                        self.assertIn(
                            "do not invent a prior topic" if language == "en"
                            else "不预设需要继续的话题",
                            event,
                        )
                    else:
                        self.assertIn(
                            "does not establish what the user is doing, how they feel, or whether they are present"
                            if language == "en" else "无法得知对方在做什么、身体状态或是否在电脑前",
                            event,
                        )
                        self.assertIn(
                            "without inventing a prior topic or user utterance"
                            if language == "en" else "不虚构已有话题或对方发言",
                            event,
                        )

    def test_other_profiles_keep_original_words_for_every_idle_phase(self):
        expected = {
            ("zh-CN", "thinking"): "用户一段时间没有输入，可能正在思考、发呆或休息。温柔地关心一下，避免过分打扰。",
            ("zh-CN", "away"): "用户已经离开电脑一段时间。轻声问主人是否还在，并提醒适当休息。",
            ("zh-CN", "return"): "用户刚刚回到电脑前。简短欢迎主人回来，并自然地问是否要继续刚才的事情。",
            ("en", "thinking"): "The user has not provided input for a while and may be thinking, daydreaming, or resting. Check in gently without being intrusive.",
            ("en", "away"): "The user has been away from the computer for a while. Gently ask whether Master is still there and remind them to take an appropriate break.",
            ("en", "return"): "The user has just returned to the computer. Briefly welcome Master back and naturally ask whether they would like to continue what they were doing.",
        }
        for profile in ("murasame", "custom"):
            for (language, phase), event in expected.items():
                with self.subTest(profile=profile, language=language, phase=phase):
                    self.assertEqual(self.trigger(self.make_pet(profile, language), phase), event)

    def test_thinking_threshold_and_single_trigger_are_unchanged(self):
        pet = self.make_pet()
        self.tick(pet, 60)
        pet.start_thread.assert_not_called()
        self.assertFalse(pet.idle_thinking_triggered)
        self.tick(pet, 60.01)
        self.assertTrue(pet.idle_thinking_triggered)
        self.tick(pet, 90, monotonic=1300)
        pet.start_thread.assert_called_once()

    def test_away_threshold_does_not_first_emit_a_thinking_event(self):
        pet = self.make_pet()
        self.tick(pet, 120)
        pet.start_thread.assert_called_once()
        self.assertFalse(pet.idle_thinking_triggered)
        self.assertTrue(pet.idle_away_triggered)
        self.assertEqual(pet.away_trigger_time, 1000)
        self.assertIn("较长时间", pet.start_thread.call_args.args[0])
        self.tick(pet, 180, wall_time=1300, monotonic=1300)
        # Preserve the existing delayed thinking check after the away branch,
        # rather than changing the timer/state machine while fixing wording.
        self.assertTrue(pet.idle_thinking_triggered)
        self.assertEqual(pet.start_thread.call_count, 2)
        self.tick(pet, 190, wall_time=1600, monotonic=1600)
        self.assertEqual(pet.start_thread.call_count, 2)

    def test_return_keeps_existing_thirty_second_guard_and_resets_state(self):
        for elapsed, should_emit in ((29.99, False), (30, True)):
            with self.subTest(elapsed=elapsed):
                pet = self.make_pet()
                pet.idle_thinking_triggered = True
                pet.idle_away_triggered = True
                pet.away_trigger_time = 1000 - elapsed
                self.tick(pet, 60)
                self.assertEqual(pet.start_thread.call_count, int(should_emit))
                self.assertFalse(pet.idle_thinking_triggered)
                self.assertFalse(pet.idle_away_triggered)
                self.assertIsNone(pet.away_trigger_time)

    def test_each_idle_phase_respects_input_worker_playback_and_cooldown(self):
        for phase, idle_seconds in (("thinking", 90), ("away", 180), ("return", 0)):
            for state in ("input", "worker", "playback", "cooldown"):
                with self.subTest(phase=phase, state=state):
                    pet = self.make_pet()
                    if phase == "return":
                        pet.idle_away_triggered = True
                        pet.away_trigger_time = 900
                    if state == "input":
                        pet.input_mode = True
                    elif state == "worker":
                        pet._workers = {1: Mock()}
                    elif state == "playback":
                        pet._playback_result = object()
                    else:
                        pet._proactive_cooldown_until = 1001
                    before_cooldown = pet._proactive_cooldown_until
                    self.tick(pet, idle_seconds)
                    pet.start_thread.assert_not_called()
                    self.assertEqual(pet._proactive_cooldown_until, before_cooldown)

    def test_shutdown_and_do_not_disturb_do_not_even_read_input_state(self):
        for state in ("_closing", "_dnd_enabled"):
            with self.subTest(state=state):
                pet = self.make_pet()
                setattr(pet, state, True)
                with patch("aipet.ui.pet.get_idle_seconds") as get_idle:
                    Murasame.check_idle_state(pet)
                get_idle.assert_not_called()
                pet.start_thread.assert_not_called()
                self.assertFalse(pet.idle_thinking_triggered)
                self.assertFalse(pet.idle_away_triggered)

    def test_nene_event_wrapper_cannot_request_murasame_personality(self):
        for language, name in (("zh-CN", "宁宁"), ("en", "Nene")):
            with self.subTest(language=language):
                pet = self.make_pet(language=language)
                event = self.trigger(pet, "away")
                old_history = [{"role": "assistant", "content": "主人、ご主人、Master。"}]
                wrapper = build_messages(pet.settings, old_history, "", event)[-1]["content"]
                self.assertIn(name, wrapper)
                self.assertNotIn("以丛雨的身份", wrapper)
                self.assertNotIn("Murasame's voice", wrapper)
                self.assertIn("not spoken or typed" if language == "en" else "不是用户刚刚说出或输入的话", wrapper)
                self.assertIn("untrusted" if language == "en" else "不可信", wrapper)
                self.assertIn("master-servant" if language == "en" else "不使用主仆称谓", wrapper)
                self.assertEqual(old_history, [{"role": "assistant", "content": "主人、ご主人、Master。"}])

    def test_worker_keeps_event_metadata_and_emotion_without_fabricating_user_input(self):
        pet = self.make_pet()
        event = self.trigger(pet, "away")
        reply = CharacterReply.model_validate({"sentences": [{
            "zh": "我还没原谅你呢。", "en": "I haven't forgiven you yet.",
            "ja": "まだ許してませんからね。", "emotion": "生气",
        }]})
        worker = ConversationWorker(pet.settings, [], "", event_context=event)
        results = []
        worker.result_ready.connect(results.append)
        with patch("aipet.core.workers.create_backend") as backend:
            backend.return_value.chat.return_value = reply
            worker.run()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].is_user_message)
        self.assertEqual(results[0].user_text, "")
        self.assertEqual(results[0].event_context, event)
        self.assertEqual(results[0].reply, reply)
        self.assertEqual(results[0].reply.sentences[0].emotion, "生气")


if __name__ == "__main__":
    unittest.main()
