from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pydantic import ValidationError
from PyQt5.QtWidgets import QApplication, QDialog

from aipet.core.config import (
    AppSettings, InteractionSettings, load_settings, save_settings,
)
from aipet.ui.settings_dialog import SettingsDialog, TRANSLATIONS


class InteractionSettingsTests(unittest.TestCase):
    def test_legacy_settings_get_safe_non_repeating_defaults(self):
        settings = AppSettings.model_validate({
            "ui_language": "zh-CN",
            "character": {"profile_id": "nene"},
            "api": {"provider": "deepseek", "deepseek_chat_model": "deepseek-v4-pro"},
        })
        self.assertEqual(settings.interaction.response_target_seconds, 10)
        self.assertEqual(settings.interaction.important_response_target_seconds, 30)
        self.assertEqual(settings.interaction.proactive_cooldown_seconds, 180)
        self.assertFalse(settings.interaction.proactive_after_unanswered)
        self.assertTrue(settings.interaction.continuity_enabled)
        self.assertNotIn("test_mode", settings.model_dump())
        self.assertNotIn("test_mode", settings.interaction.model_dump())

    def test_target_and_cooldown_boundaries_are_validated(self):
        for field in ("response_target_seconds", "important_response_target_seconds"):
            for value in (0, 5, 600):
                with self.subTest(field=field, value=value):
                    self.assertEqual(getattr(InteractionSettings(**{field: value}), field), value)
            for value in (-1, 601, "never", 3.5):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        InteractionSettings(**{field: value})
        for value in (29, 86401):
            with self.assertRaises(ValidationError):
                InteractionSettings(proactive_cooldown_seconds=value)

    def test_zero_targets_roundtrip_without_changing_service_timeouts(self):
        settings = AppSettings()
        settings.interaction.response_target_seconds = 0
        settings.interaction.important_response_target_seconds = 600
        service_timeouts = (settings.api.timeout_seconds, settings.ollama.timeout_seconds,
                            settings.tts.timeout_seconds)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_settings(settings, path)
            reopened = load_settings(path)
        self.assertEqual(reopened.interaction, settings.interaction)
        self.assertEqual((reopened.api.timeout_seconds, reopened.ollama.timeout_seconds,
                          reopened.tts.timeout_seconds), service_timeouts)


class HumanizationSettingsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"AIPET_DATA_DIR": str(self.root / "data")})
        environment.start()
        self.addCleanup(environment.stop)
        for target, kwargs in (
            ("aipet.ui.settings_dialog.refresh_audio_input_devices", {"return_value": (None, [])}),
            ("aipet.ui.settings_dialog.TTSCheckWorker.start", {}),
            ("aipet.ui.settings_dialog.WhisperModelCheckWorker.start", {}),
            ("aipet.ui.settings_dialog.ModelListWorker.start", {}),
            ("aipet.ui.settings_dialog.get_tts_service_manager", {}),
            ("aipet.ui.settings_dialog.load_personality", {"return_value": "Test-only persona."}),
        ):
            mocked = patch(target, **kwargs)
            value = mocked.start()
            self.addCleanup(mocked.stop)
            if target.endswith("get_tts_service_manager"):
                value.return_value.owns_running_process.return_value = False

    def dialog(self, settings=None):
        dialog = SettingsDialog(settings or AppSettings(ui_language="zh-CN"))
        self.addCleanup(dialog.close)
        return dialog

    def test_presets_custom_and_unlimited_save_and_reopen(self):
        settings = AppSettings(ui_language="zh-CN")
        settings.interaction.proactive_cooldown_seconds = 555
        settings.interaction.continuity_enabled = False
        dialog = self.dialog(settings)
        self.assertEqual([dialog.response_target.itemData(i) for i in range(7)],
                         [5, 10, 20, 30, 60, 0, None])
        dialog.response_target.setEditText("47")
        dialog._set_combo_data(dialog.important_response_target, 0)
        updated = dialog._form_settings()
        self.assertEqual(updated.interaction.response_target_seconds, 47)
        self.assertEqual(updated.interaction.important_response_target_seconds, 0)
        self.assertEqual(updated.interaction.proactive_cooldown_seconds, 555)
        self.assertFalse(updated.interaction.proactive_after_unanswered)
        self.assertFalse(updated.interaction.continuity_enabled)
        self.assertEqual(settings.interaction.response_target_seconds, 10)
        reopened = self.dialog(AppSettings.model_validate_json(updated.model_dump_json()))
        self.assertEqual(reopened.response_target.currentText(), "47")
        self.assertEqual(reopened.important_response_target.currentText(), "不限")

    def test_custom_selection_has_no_stale_preset_and_rejects_invalid_input(self):
        dialog = self.dialog()
        custom_index = dialog.response_target.count() - 1
        dialog.response_target.setCurrentIndex(custom_index)
        dialog._activate_response_target(dialog.response_target, custom_index)
        self.assertEqual(dialog.response_target.currentText(), "")
        for text in ("", "custom", "-1", "601", "1.5"):
            with self.subTest(text=text):
                dialog.response_target.setEditText(text)
                with self.assertRaises(ValidationError):
                    dialog._form_settings()
        dialog.response_target.setEditText("600")
        self.assertEqual(dialog._form_settings().interaction.response_target_seconds, 600)

    def test_custom_number_survives_translation_even_with_unlimited_item_selected(self):
        dialog = self.dialog()
        dialog._set_combo_data(dialog.response_target, 0)
        dialog.response_target.setEditText("125")
        dialog._set_combo_data(dialog.important_response_target, 0)
        for language in ("en", "zh-CN", "en"):
            dialog._set_combo_data(dialog.language_combo, language)
            self.assertEqual(dialog.response_target.currentText(), "125")
            self.assertEqual(dialog._form_settings().interaction.response_target_seconds, 125)
            self.assertEqual(dialog._form_settings().interaction.important_response_target_seconds, 0)
            self.assertEqual(dialog.important_response_target.currentText(),
                             TRANSLATIONS[language]["response_unlimited"])

    def test_target_edits_preserve_other_backend_voice_display_and_profile_settings(self):
        settings = AppSettings(ui_language="en")
        settings.character.profile_id = "nene"
        settings.character.name = "宁宁"
        settings.character.name_en = "Nene"
        settings.api.provider = "deepseek"
        settings.api.deepseek_api_key = "fixture-not-a-real-key"
        settings.api.deepseek_chat_model = "deepseek-v4-pro"
        settings.api.deepseek_thinking = True
        settings.vision.openai_api_key = "fixture-vision-not-a-real-key"
        settings.vision.openai_model = "fixture-vision-model"
        settings.tts.advanced_paths = True
        settings.tts.engine_root = str(self.root / "no-engine")
        settings.tts.model_dir = str(self.root / "no-model")
        settings.display.window_x = 123
        settings.display.window_y = 456
        settings.display.screen_name = "fixture-screen"
        settings.interaction.proactive_cooldown_seconds = 600
        dialog = self.dialog(settings)
        dialog.response_target.setEditText("33")
        updated = dialog._form_settings()
        for field in ("api", "ollama", "vision", "tts", "stt", "display", "idle"):
            with self.subTest(field=field):
                self.assertEqual(getattr(updated, field), getattr(settings, field))
        for field in ("profile_id", "name", "name_en", "portrait", "outfit", "user_name"):
            self.assertEqual(getattr(updated.character, field), getattr(settings.character, field))
        self.assertEqual(updated.interaction.proactive_cooldown_seconds, 600)
        self.assertFalse(updated.interaction.proactive_after_unanswered)

    def test_cancelling_dialog_does_not_persist_test_mode_or_target_edits(self):
        settings = AppSettings(ui_language="zh-CN")
        dialog = self.dialog(settings)
        dialog.response_target.setEditText("600")
        dialog.reject()
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertEqual(dialog.result_settings(), settings)
        self.assertFalse((self.root / "data" / "config.json").exists())

    def test_bilingual_help_explains_soft_targets_pause_and_continuity_clear(self):
        dialog = self.dialog()
        for language in ("en", "zh-CN"):
            dialog._set_combo_data(dialog.language_combo, language)
            self.assertEqual(dialog.response_target.toolTip(),
                             TRANSLATIONS[language]["response_target_help"])
            self.assertEqual(dialog.do_not_disturb.toolTip(),
                             TRANSLATIONS[language]["do_not_disturb_help"])
            texts = TRANSLATIONS[language]
            self.assertIn("soft target" if language == "en" else "软目标",
                          texts["automation_help_body"])
            self.assertIn("relationship state" if language == "en" else "关系状态",
                          texts["clear_history_confirm_body"])
            self.assertNotIn("manual conversation still works", texts["automation_help_body"])
            self.assertNotIn("手动对话仍可正常使用", texts["automation_help_body"])


if __name__ == "__main__":
    unittest.main()
