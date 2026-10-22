from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pydantic import ValidationError
from PyQt5.QtWidgets import QApplication, QDialog

from aipet.core.backends import APIBackend
from aipet.core.config import APISettings, AppSettings, load_settings, save_settings
from aipet.ui.settings_dialog import SettingsDialog


VALID_REPLY = (
    '{"sentences":[{"zh":"好。","en":"Okay.",'
    '"ja":"はい。","emotion":"平静"}]}'
)


class DeepSeekThinkingBudgetTests(unittest.TestCase):
    def test_old_settings_receive_default_reasoning_budget(self) -> None:
        settings = APISettings.model_validate({"deepseek_thinking": True})
        self.assertEqual(settings.deepseek_thinking_max_tokens, 8192)

    def test_reasoning_budget_boundaries(self) -> None:
        for value in (1024, 8192, 32768, 393216):
            with self.subTest(value=value):
                self.assertEqual(
                    APISettings(deepseek_thinking_max_tokens=value)
                    .deepseek_thinking_max_tokens,
                    value,
                )
        for value in (1023, 393217):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                APISettings(deepseek_thinking_max_tokens=value)

    def test_reasoning_budget_survives_settings_file_roundtrip(self) -> None:
        settings = AppSettings(
            api=APISettings(
                deepseek_thinking=True,
                deepseek_thinking_max_tokens=16384,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_settings(settings, path)
            self.assertEqual(load_settings(path), settings)

    @patch("requests.Session.request")
    def test_budget_only_changes_deepseek_thinking_requests(self, request: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": VALID_REPLY}}]
        }
        request.return_value = response
        cases = (
            ("deepseek", True, 8192, "max_tokens", 8192),
            ("deepseek", True, 24576, "max_tokens", 24576),
            ("deepseek", False, 24576, "max_tokens", 1200),
            ("aliyun", True, 24576, "max_tokens", 1200),
            ("aliyun", False, 24576, "max_tokens", 1200),
            ("openai", True, 24576, "max_completion_tokens", 1200),
            ("openai", False, 24576, "max_completion_tokens", 1200),
        )
        for provider, thinking, budget, parameter, expected in cases:
            with self.subTest(provider=provider, thinking=thinking, budget=budget):
                config = APISettings(
                    provider=provider,
                    deepseek_api_key="fake-test-key",
                    aliyun_api_key="fake-test-key",
                    openai_api_key="fake-test-key",
                    deepseek_chat_model="deepseek-v4-pro",
                    deepseek_thinking=thinking,
                    deepseek_thinking_max_tokens=budget,
                )
                APIBackend(AppSettings(mode="api", api=config)).chat([], "你好")
                payload = request.call_args.kwargs["json"]
                self.assertEqual(payload[parameter], expected)
                self.assertNotIn("reasoning_effort", payload)
                if provider == "deepseek":
                    self.assertEqual(payload["model"], "deepseek-v4-pro")
                    self.assertEqual(
                        payload["thinking"],
                        {"type": "enabled" if thinking else "disabled"},
                    )
                else:
                    self.assertNotIn("thinking", payload)
                other = "max_tokens" if provider == "openai" else "max_completion_tokens"
                self.assertNotIn(other, payload)

    @patch("requests.Session.request")
    def test_retry_uses_same_configured_reasoning_budget(self, request: Mock) -> None:
        truncated = Mock()
        truncated.json.return_value = {
            "choices": [{"message": {"content": '{"sentences":[{'},
                         "finish_reason": "length"}]
        }
        valid = Mock()
        valid.json.return_value = {
            "choices": [{"message": {"content": VALID_REPLY},
                         "finish_reason": "stop"}]
        }
        request.side_effect = [truncated, valid]
        settings = AppSettings(
            mode="api",
            api=APISettings(
                deepseek_api_key="fake-test-key",
                deepseek_thinking=True,
                deepseek_thinking_max_tokens=16384,
            ),
        )
        reply = APIBackend(settings).chat([], "你好")
        self.assertEqual(reply.chinese_text(), "好。")
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertEqual(call.kwargs["json"]["max_tokens"], 16384)


class DeepSeekThinkingBudgetUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"AIPET_DATA_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        for method in (
            "_update_whisper_state", "_update_tts_state",
            "_refresh_audio_input_devices", "_auto_fetch_models",
        ):
            mock = patch.object(SettingsDialog, method)
            mock.start()
            self.addCleanup(mock.stop)
        network = patch("requests.Session.request", side_effect=AssertionError("No network in UI tests"))
        network.start()
        self.addCleanup(network.stop)

    def dialog(self, settings: AppSettings) -> SettingsDialog:
        dialog = SettingsDialog(settings)
        self.addCleanup(dialog.close)
        return dialog

    def test_default_budget_and_toggle_enablement(self) -> None:
        dialog = self.dialog(AppSettings(ui_language="zh-CN"))
        field = dialog.deepseek_thinking_max_tokens
        self.assertEqual(field.minimum(), 1024)
        self.assertEqual(field.maximum(), 393216)
        self.assertEqual(field.value(), 8192)
        self.assertFalse(field.isEnabled())
        label = dialog._form_labels["deepseek_thinking_max_tokens"][0]
        self.assertIn("含推理和最终台词", label.text())
        self.assertFalse(label.isEnabled())
        dialog.deepseek_thinking.setChecked(True)
        self.assertTrue(field.isEnabled())
        self.assertTrue(label.isEnabled())
        field.setValue(24576)
        dialog.deepseek_thinking.setChecked(False)
        self.assertFalse(field.isEnabled())
        self.assertEqual(field.value(), 24576)
        self.assertEqual(dialog._form_settings().api.deepseek_thinking_max_tokens, 24576)

    def test_bilingual_save_and_reopen_preserves_budget_and_persona(self) -> None:
        for language in ("zh-CN", "en"):
            with self.subTest(language=language):
                settings = AppSettings(mode="api", ui_language=language)
                settings.api.deepseek_api_key = "fake-test-key"
                settings.api.deepseek_chat_model = "deepseek-v4-pro"
                settings.api.deepseek_thinking = True
                settings.api.deepseek_thinking_max_tokens = 16384
                for locale, field in (
                    ("zh-CN", "personality_file"), ("en", "personality_file_en")
                ):
                    source = self.root / f"original.{locale}.txt"
                    source.write_text(f"Test persona {locale}", encoding="utf-8")
                    setattr(settings.character, field, str(source))
                dialog = self.dialog(settings)
                field = dialog.deepseek_thinking_max_tokens
                self.assertTrue(field.isEnabled())
                self.assertEqual(field.value(), 16384)
                label = dialog._form_labels["deepseek_thinking_max_tokens"][0]
                self.assertIn("reasoning and dialogue" if language == "en" else "推理和最终台词", label.text())
                field.setValue(32768)
                dialog.accept()
                self.assertEqual(dialog.result(), QDialog.Accepted)
                saved = dialog.result_settings()
                self.assertEqual(saved.api.deepseek_thinking_max_tokens, 32768)
                self.assertEqual(saved.api.deepseek_api_key, "fake-test-key")
                self.assertEqual(saved.api.deepseek_chat_model, "deepseek-v4-pro")
                for locale in ("zh-CN", "en"):
                    self.assertEqual(saved.personality_path(locale).read_text(encoding="utf-8").strip(), f"Test persona {locale}")
                path = self.root / "roundtrip.json"
                save_settings(saved, path)
                reopened = self.dialog(load_settings(path))
                self.assertEqual(reopened.deepseek_thinking_max_tokens.value(), 32768)
                self.assertTrue(reopened.deepseek_thinking.isChecked())
                self.assertTrue(reopened.deepseek_thinking_max_tokens.isEnabled())


if __name__ == "__main__":
    unittest.main()
