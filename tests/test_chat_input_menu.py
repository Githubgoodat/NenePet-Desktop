"""Exercise the real shared menu action without showing UI or starting jobs."""
from __future__ import annotations

import os
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QPoint, QRect, Qt
from PyQt5.QtWidgets import QApplication, QMenu

from aipet.application import add_text_input_action
from aipet.core.config import AppSettings
from aipet.ui.pet import Murasame


class PetState(SimpleNamespace):
    """Allow Qt to weak-reference the bound slot receiver, as for a real widget."""


class ChatInputMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def pet(self, *, viewer=True):
        pet = PetState(
            _closing=False, _viewerex=Mock() if viewer else None,
            _viewer_layout=SimpleNamespace(body_rect=QRect(20, 40, 150, 520)) if viewer else None,
            input_mode=False, input_buffer="秘密草稿", preedit_text="秘密组合文字",
            touch_head=False, head_press_x=None,
            _cancel_active_jobs=Mock(), raise_=Mock(), activateWindow=Mock(),
            setFocus=Mock(), setCursor=Mock(), update=Mock(), start_thread=Mock(),
            _input_display=lambda text: "输入:" + text,
        )
        pet.begin_text_input = MethodType(Murasame.begin_text_input, pet)
        pet.mousePressEvent = MethodType(Murasame.mousePressEvent, pet)
        return pet

    def menu(self, pet, language="zh-CN"):
        menu = QMenu()
        self.addCleanup(menu.close)
        settings_action = menu.addAction("Existing settings action")
        action = add_text_input_action(menu, pet, AppSettings(ui_language=language))
        self.assertEqual(menu.actions(), [action, settings_action])
        self.assertIs(action.parent(), menu)
        self.assertTrue(action.shortcut().isEmpty())
        return menu, action

    def test_real_menu_action_opens_original_editor_for_both_renderers_and_languages(self):
        for viewer in (True, False):
            for language, label in (("zh-CN", "输入对话"), ("en", "Type a message")):
                with self.subTest(viewer=viewer, language=language):
                    pet = self.pet(viewer=viewer)
                    menu, action = self.menu(pet, language)
                    self.assertEqual(action.text(), label)
                    self.assertFalse(menu.isVisible())
                    self.assertFalse(pet.input_mode)
                    action.trigger()  # Actual QAction signal, not a direct method call.
                    self.assertTrue(pet.input_mode)
                    self.assertEqual(pet.display_text, "输入:...")
                    self.assertEqual((pet.input_buffer, pet.preedit_text), ("", ""))
                    pet._cancel_active_jobs.assert_called_once_with(include_vision=True)
                    pet.setFocus.assert_called_once_with(Qt.MouseFocusReason)
                    pet.start_thread.assert_not_called()  # Enter sends; opening the editor does not.
                    self.assertEqual(pet.raise_.called, viewer)

    def test_initially_empty_menu_keeps_input_before_later_settings_action(self):
        pet = self.pet()
        menu = QMenu()
        self.addCleanup(menu.close)
        action = add_text_input_action(menu, pet, AppSettings(ui_language="zh-CN"))
        settings_action = menu.addAction("Settings")
        self.assertEqual(menu.actions(), [action, settings_action])
        action.trigger()
        self.assertTrue(pet.input_mode)

    def test_reopening_live2d_editor_from_menu_preserves_draft_and_composition(self):
        pet = self.pet()
        pet.input_mode = True
        pet.display_text = "原编辑内容"
        _, action = self.menu(pet)
        action.trigger()
        self.assertEqual((pet.input_buffer, pet.preedit_text), ("秘密草稿", "秘密组合文字"))
        self.assertEqual(pet.display_text, "原编辑内容")
        pet._cancel_active_jobs.assert_not_called()
        pet.setFocus.assert_called_once_with(Qt.MouseFocusReason)

    def test_closing_rejects_menu_entry_and_diagnostic_log(self):
        pet = self.pet()
        pet._closing = True
        _, action = self.menu(pet)
        with patch("aipet.ui.pet.logger.info") as info:
            action.trigger()
        self.assertFalse(pet.input_mode)
        pet._cancel_active_jobs.assert_not_called()
        pet.setFocus.assert_not_called()
        info.assert_not_called()

    def test_mouse_hit_and_entry_logs_describe_regions_without_draft_content(self):
        for y, area in ((60, "head"), (400, "body")):
            with self.subTest(area=area):
                pet = self.pet()
                event = Mock()
                event.button.return_value = Qt.LeftButton
                event.pos.return_value = QPoint(50, y)
                event.x.return_value, event.y.return_value = 50, y
                with patch("aipet.ui.pet.logger.info") as info:
                    pet.mousePressEvent(event)
                messages = [args[0] % args[1:] for args, _ in info.call_args_list]
                self.assertTrue(any("area=" + area in text for text in messages))
                self.assertEqual(any("输入对话入口" in text for text in messages), area == "body")
                self.assertFalse(any("秘密" in text for text in messages))

    def test_placement_log_reports_actual_global_body_and_window_geometry(self):
        settings = AppSettings()
        settings.display.window_x, settings.display.window_y = 1964, 540
        geometry = QRect(1964, 540, 560, 780)
        pet = SimpleNamespace(
            settings=settings,
            _viewerex=SimpleNamespace(config=SimpleNamespace(managed_position=False, position=None)),
            _viewer_layout=SimpleNamespace(body_rect=QRect(206, 260, 150, 520)),
            _viewer_position_restored=True, _viewer_had_saved_position=True,
            _prepare_live2d_overlay=Mock(),
            _configured_screen=lambda: SimpleNamespace(geometry=lambda: QRect(0, 0, 2560, 1440)),
            geometry=lambda: geometry, mapToGlobal=lambda point: geometry.topLeft() + point,
            remember_window_position=Mock(), update=Mock(),
            isVisible=lambda: True, isEnabled=lambda: True,
        )
        with patch("aipet.ui.pet.logger.info") as info:
            Murasame.place_live2d_overlay(pet)
        message = info.call_args.args[0] % info.call_args.args[1:]
        self.assertIn("geometry=(1964, 540, 560, 780)", message)
        self.assertIn("body_global=(2170, 800, 150, 520)", message)
        self.assertIn("visible=True | enabled=True", message)


if __name__ == "__main__":
    unittest.main()
