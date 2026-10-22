"""Live2D interaction checks without opening a window or contacting ViewerEX."""
from __future__ import annotations

import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from PyQt5.QtCore import QPoint, QRect, Qt

from aipet.core.backends import CharacterReply
from aipet.core.workers import ConversationResult
from aipet.ui.pet import Murasame


class Live2DInputTests(unittest.TestCase):
    def make_pet(self, *, viewer=True, layout=True):
        pet = SimpleNamespace(
            _closing=False, _viewerex=Mock() if viewer else None,
            _viewer_layout=SimpleNamespace(body_rect=QRect(20, 40, 150, 520)) if viewer and layout else None,
            input_mode=False, input_buffer="旧草稿", preedit_text="组合输入",
            touch_head=False, head_press_x=None, drag_offset=None,
            full_text="旧内容", display_text="旧内容", typing_prefix="【宁宁】\n", typing_index=4,
            typing_timer=Mock(), update=Mock(), raise_=Mock(), activateWindow=Mock(),
            setFocus=Mock(), setCursor=Mock(), _cancel_active_jobs=Mock(),
            _input_display=Mock(side_effect=lambda text: "输入:" + text),
            show_text=Mock(), start_thread=Mock(),
        )
        for name in ("begin_text_input", "mousePressEvent", "keyPressEvent", "_clear_dialogue"):
            setattr(pet, name, MethodType(getattr(Murasame, name), pet))
        return pet

    def click(self, pet, x, y):
        event = Mock()
        event.button.return_value = Qt.LeftButton
        event.x.return_value = x
        event.y.return_value = y
        event.pos.return_value = QPoint(x, y)
        pet.mousePressEvent(event)

    def test_live2d_head_below_area_including_former_dead_zone_enters_input(self):
        # body_rect starts at y=40 and has height=520, mapping to 640 units.
        for offset in (122, 180, 225, 350, 500):
            with self.subTest(offset=offset):
                pet = self.make_pet()
                self.click(pet, 50, 40 + offset)
                self.assertTrue(pet.input_mode)
                self.assertEqual(pet.display_text, "输入:...")
                pet._cancel_active_jobs.assert_called_once_with(include_vision=True)
                pet.raise_.assert_called_once_with()
                pet.activateWindow.assert_called_once_with()
                pet.setFocus.assert_called_once_with(Qt.MouseFocusReason)

    def test_live2d_head_still_arms_head_pat_without_entering_editor(self):
        pet = self.make_pet()
        self.click(pet, 50, 80)
        self.assertTrue(pet.touch_head)
        self.assertEqual(pet.head_press_x, 50)
        self.assertFalse(pet.input_mode)
        pet._cancel_active_jobs.assert_not_called()

    def test_overlay_click_outside_body_is_ignored(self):
        pet = self.make_pet()
        self.click(pet, 10, 230)
        self.assertFalse(pet.input_mode)
        pet._cancel_active_jobs.assert_not_called()

    def test_png_keeps_original_head_dead_zone_and_lower_body_input(self):
        for y, expect_head, expect_input in ((80, True, False), (200, False, False), (350, False, True)):
            with self.subTest(y=y):
                pet = self.make_pet(viewer=False)
                self.click(pet, 50, y)
                self.assertEqual(pet.touch_head, expect_head)
                self.assertEqual(pet.input_mode, expect_input)
                pet.raise_.assert_not_called()
                pet.activateWindow.assert_not_called()

    def test_model_tap_entry_does_not_require_overlay_layout_or_a_coordinate(self):
        pet = self.make_pet(layout=False)
        pet.touch_head, pet.head_press_x = True, 20
        pet.begin_text_input()
        self.assertTrue(pet.input_mode)
        self.assertEqual(pet.input_buffer, "")
        self.assertEqual(pet.preedit_text, "")
        self.assertFalse(pet.touch_head)
        self.assertIsNone(pet.head_press_x)
        self.assertEqual(pet.display_text, "输入:...")
        pet._cancel_active_jobs.assert_called_once_with(include_vision=True)
        pet.setFocus.assert_called_once_with(Qt.MouseFocusReason)
        pet.update.assert_called_once_with()

    def test_closing_rejects_late_model_tap_without_activating_or_cancelling(self):
        pet = self.make_pet()
        pet._closing = True
        pet.begin_text_input()
        self.assertFalse(pet.input_mode)
        pet._cancel_active_jobs.assert_not_called()
        pet.raise_.assert_not_called()
        pet.activateWindow.assert_not_called()
        pet.setFocus.assert_not_called()
        pet.update.assert_not_called()

    def test_repeated_live2d_taps_preserve_chinese_draft_and_ime_composition(self):
        pet = self.make_pet(layout=False)
        pet.input_mode = True
        pet.input_buffer = "你好，宁宁"
        pet.preedit_text = "今天"
        pet.display_text = "输入:你好，宁宁今天"
        for _ in range(2):
            pet.begin_text_input()
        self.assertTrue(pet.input_mode)
        self.assertEqual(pet.input_buffer, "你好，宁宁")
        self.assertEqual(pet.preedit_text, "今天")
        self.assertEqual(pet.display_text, "输入:你好，宁宁今天")
        pet._cancel_active_jobs.assert_not_called()
        pet._input_display.assert_not_called()
        self.assertEqual(pet.raise_.call_count, 2)
        self.assertEqual(pet.activateWindow.call_count, 2)
        self.assertEqual(pet.setFocus.call_count, 2)
        pet.setFocus.assert_called_with(Qt.MouseFocusReason)

    def test_repeated_png_body_click_preserves_original_new_draft_behavior(self):
        pet = self.make_pet(viewer=False)
        pet.input_mode = True
        self.click(pet, 50, 350)
        self.assertEqual(pet.input_buffer, "")
        self.assertEqual(pet.preedit_text, "")
        self.assertEqual(pet.display_text, "输入:...")
        pet._cancel_active_jobs.assert_called_once_with(include_vision=True)
        pet.raise_.assert_not_called()
        pet.activateWindow.assert_not_called()

    def test_live2d_escape_clears_text_and_preedit_without_question_reply(self):
        pet = self.make_pet()
        pet.input_mode = True
        event = Mock()
        event.key.return_value = Qt.Key_Escape
        pet.keyPressEvent(event)
        self.assertFalse(pet.input_mode)
        self.assertEqual(pet.preedit_text, "")
        self.assertEqual((pet.full_text, pet.display_text, pet.typing_prefix), ("", "", ""))
        self.assertEqual(pet.typing_index, 0)
        pet.typing_timer.stop.assert_called_once_with()
        pet.show_text.assert_not_called()

    def test_png_escape_keeps_original_question_reply(self):
        pet = self.make_pet(viewer=False)
        pet.input_mode = True
        event = Mock()
        event.key.return_value = Qt.Key_Escape
        pet.keyPressEvent(event)
        self.assertFalse(pet.input_mode)
        pet.show_text.assert_called_once_with("……？", typing=False)
        self.assertEqual(pet.display_text, "旧内容")


class Live2DTextLifetimeTests(unittest.TestCase):
    def make_pet(self, *, viewer=True):
        reply = CharacterReply.model_validate({"sentences": [{
            "zh": "下午好。", "en": "Good afternoon.", "ja": "こんにちは。", "emotion": "平静",
        }]})
        result = ConversationResult(reply, [], "你好", True)
        pet = SimpleNamespace(
            _closing=False, input_mode=False, _viewerex=Mock() if viewer else None,
            _playback_result=result, _playback_index=1, _sound=Mock(),
            _remove_audio_files=Mock(),
            full_text="下午好。", display_text="【宁宁】\n下午好。", typing_prefix="【宁宁】\n", typing_index=4,
            typing_timer=Mock(), update=Mock(),
        )
        pet._clear_dialogue = MethodType(Murasame._clear_dialogue, pet)
        return pet, result

    def test_last_live2d_sentence_clears_visual_text_but_keeps_existing_result_cleanup(self):
        pet, result = self.make_pet()
        Murasame._play_next_sentence(pet)
        self.assertEqual((pet.full_text, pet.display_text, pet.typing_prefix), ("", "", ""))
        self.assertEqual(pet.typing_index, 0)
        pet.typing_timer.stop.assert_called_once_with()
        pet.update.assert_called_once_with()
        pet._remove_audio_files.assert_called_once_with(result)
        pet._viewerex.set_emotion.assert_called_once_with("平静")
        self.assertIsNone(pet._playback_result)

    def test_png_keeps_last_sentence_visible(self):
        pet, result = self.make_pet(viewer=False)
        Murasame._play_next_sentence(pet)
        self.assertEqual(pet.display_text, "【宁宁】\n下午好。")
        pet.typing_timer.stop.assert_not_called()
        pet.update.assert_not_called()
        pet._remove_audio_files.assert_called_once_with(result)

    def test_empty_or_stale_playback_tick_does_not_erase_new_input(self):
        for input_mode, has_old_result in ((True, True), (True, False), (False, False)):
            with self.subTest(input_mode=input_mode, has_old_result=has_old_result):
                pet, _ = self.make_pet()
                pet.input_mode = input_mode
                if not has_old_result:
                    pet._playback_result = None
                pet.display_text = "输入中的草稿或新请求提示"
                Murasame._play_next_sentence(pet)
                self.assertEqual(pet.display_text, "输入中的草稿或新请求提示")
                pet.typing_timer.stop.assert_not_called()
                pet.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
