from __future__ import annotations

import os
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aipet.core.backends import CharacterReply, CharacterSentence
from aipet.core.workers import ConversationResult
from aipet.ui.pet import Murasame


class PlaybackFaceResetTests(unittest.TestCase):
    """Exercise the real playback/close methods without Qt windows or devices."""

    def make_pet(self, *, viewer=True):
        result = ConversationResult(
            reply=CharacterReply(sentences=[CharacterSentence(
                zh="下午好。", en="Good afternoon.", ja="こんにちは。", emotion="高兴",
            )]),
            audio_paths=[], user_text="测试", is_user_message=True,
        )
        pet = SimpleNamespace(
            _closing=False,
            input_mode=False,
            _generation=3,
            _viewerex=Mock() if viewer else None,
            _playback_result=result,
            _playback_index=0,
            _sound=None,
            _workers={"active": Mock()},
            _vision_worker=Mock(),
            settings=SimpleNamespace(
                ui_language="zh-CN",
                character=SimpleNamespace(portrait="01", outfit="校服"),
            ),
            playback_timer=Mock(),
            typing_timer=Mock(),
            _topmost_timer=Mock(),
            _screen_resize_timer=Mock(),
            screenshot_timer=Mock(),
            idle_timer=Mock(),
            _stop_thinking_animation=Mock(),
            _remove_audio_files=Mock(),
            update_portrait=Mock(),
            show_text=Mock(),
            update=Mock(),
        )
        for name in (
            "_play_next_sentence", "_stop_playback", "_cancel_active_jobs", "_clear_dialogue",
            "begin_shutdown",
        ):
            setattr(pet, name, MethodType(getattr(Murasame, name), pet))
        pet._audio_duration_milliseconds = Murasame._audio_duration_milliseconds
        return pet, result

    def test_normal_queue_completion_resets_face_after_last_sentence(self):
        pet, result = self.make_pet()
        with patch("aipet.ui.pet.QSound") as sound:
            pet._play_next_sentence()
            pet._viewerex.set_emotion.assert_called_once_with("高兴")
            pet.playback_timer.start.assert_called_once_with(960)
            self.assertIs(pet._playback_result, result)

            pet._play_next_sentence()
            sound.assert_not_called()

        self.assertEqual(
            pet._viewerex.set_emotion.call_args_list,
            [call("高兴"), call("平静")],
        )
        pet._remove_audio_files.assert_called_once_with(result)
        self.assertIsNone(pet._playback_result)
        self.assertIsNone(pet._sound)

    def test_cancel_stops_audio_and_resets_face(self):
        pet, result = self.make_pet()
        sound = pet._sound = Mock()

        pet._cancel_active_jobs(include_vision=True)

        self.assertEqual(pet._generation, 4)
        pet._workers["active"].cancel.assert_called_once_with()
        pet._vision_worker.cancel.assert_called_once_with()
        pet.playback_timer.stop.assert_called_once_with()
        pet.typing_timer.stop.assert_called_once_with()
        sound.stop.assert_called_once_with()
        pet._viewerex.set_emotion.assert_called_once_with("平静")
        pet._remove_audio_files.assert_called_once_with(result)
        self.assertIsNone(pet._sound)
        self.assertIsNone(pet._playback_result)

    def test_without_viewer_existing_cleanup_paths_still_work(self):
        for action in ("completed", "cancelled", "closing"):
            with self.subTest(action=action):
                pet, result = self.make_pet(viewer=False)
                sound = pet._sound = Mock()
                if action == "completed":
                    pet._playback_index = len(result.reply.sentences)
                    pet._play_next_sentence()
                elif action == "cancelled":
                    pet._cancel_active_jobs()
                    sound.stop.assert_called_once_with()
                else:
                    pet.begin_shutdown()
                    sound.stop.assert_called_once_with()
                    self.assertTrue(pet._closing)
                pet._remove_audio_files.assert_called_once_with(result)
                self.assertIsNone(pet._playback_result)
                self.assertIsNone(pet._sound)

    def test_shutdown_resets_before_disconnecting_viewer_and_is_idempotent(self):
        pet, _ = self.make_pet()
        sound = pet._sound = Mock()
        events = Mock()
        events.attach_mock(sound, "sound")
        events.attach_mock(pet._viewerex, "viewer")

        pet.begin_shutdown()
        pet.begin_shutdown()

        self.assertTrue(pet._closing)
        self.assertEqual(events.mock_calls, [
            call.sound.stop(), call.viewer.set_emotion("平静"), call.viewer.stop(),
        ])
        for timer in (
            pet._topmost_timer, pet._screen_resize_timer,
            pet.screenshot_timer, pet.idle_timer,
        ):
            timer.stop.assert_called_once_with()
        pet._vision_worker.cancel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
