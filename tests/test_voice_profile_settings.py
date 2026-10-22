from __future__ import annotations

import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QDialog

from aipet.core.config import AppSettings, TTSSettings
from aipet.core.tts_assets import TTSAssetState, locate_tts_assets
from aipet.core.voice_profile import PROFILE_BASE_URL, VoiceProfileError
from aipet.ui.settings_dialog import SettingsDialog, TTSCheckWorker, TTSServiceWorker


class VoiceProfileSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"AIPET_DATA_DIR": str(self.root / "data")})
        environment.start()
        self.addCleanup(environment.stop)
        for target, kwargs in (
            ("aipet.ui.settings_dialog.refresh_audio_input_devices", {"return_value": (None, [])}),
            ("aipet.ui.settings_dialog.TTSCheckWorker.start", {}),
            ("aipet.ui.settings_dialog.get_tts_service_manager", {}),
        ):
            mock = patch(target, **kwargs)
            value = mock.start()
            self.addCleanup(mock.stop)
            if target.endswith("get_tts_service_manager"):
                self.manager = value.return_value
                self.manager.owns_running_process.return_value = False
        self.engine = self.root / "engine"
        for relative in (
            "api_v2.py",
            "GPT_SoVITS/pretrained_models/s1v3.ckpt",
            "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
            "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/pytorch_model.bin",
            "GPT_SoVITS/pretrained_models/chinese-hubert-base/pytorch_model.bin",
            "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt",
        ):
            target = self.engine / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"PK-test-fixture")
        with wave.open(str(self.root / "calm.wav"), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\0\0" * (16000 * 4))
        (self.root / "calm.txt").write_text("こんにちは。", encoding="utf-8")
        self.manifest = self.root / "profile.json"
        self.manifest.write_text(json.dumps({
            "schema_version": 1, "version": "v2Pro", "device": "cpu",
            "gpt_weights": "engine/GPT_SoVITS/pretrained_models/s1v3.ckpt",
            "sovits_weights": "engine/GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
            "references": {"平静": {"audio": "calm.wav", "transcript": "calm.txt"}},
        }), encoding="utf-8")
        self.settings = AppSettings(ui_language="zh-CN", mode="ollama")
        self.settings.character.profile_id = "nene"
        self.settings.character.name = "宁宁"
        self.settings.character.name_en = "Nene"
        for language, field in (("zh-CN", "personality_file"), ("en", "personality_file_en")):
            source = self.root / f"source.{language}.txt"
            source.write_text(f"Persona {language}", encoding="utf-8")
            setattr(self.settings.character, field, str(source))
        self.settings.tts = TTSSettings(
            enabled=True, voice_profile=str(self.manifest), advanced_paths=True,
            engine_root=str(self.engine), base_url="http://127.0.0.1:9880/tts",
        )

    def _dialog(self) -> SettingsDialog:
        dialog = SettingsDialog(self.settings)
        self.addCleanup(dialog.close)
        return dialog

    def _state(self) -> TTSAssetState:
        return locate_tts_assets(
            configured_engine_root=str(self.engine),
            configured_voice_profile=str(self.manifest),
        )

    def test_qt_save_to_json_and_reopen_preserves_character_and_voice(self) -> None:
        dialog = self._dialog()
        dialog.user_name.setText("测试主人")
        dialog.tts_timeout.setValue(120)
        # Hidden Murasame controls cannot accidentally overwrite saved values.
        original_outfit = self.settings.character.outfit
        original_portrait = self.settings.character.portrait
        dialog.outfit.setCurrentIndex(0)
        dialog.portrait.setCurrentIndex(0)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        result = dialog.result_settings()
        self.assertEqual(result.character.profile_id, "nene")
        self.assertEqual(result.character.name, "宁宁")
        self.assertEqual(result.character.name_en, "Nene")
        self.assertEqual(result.character.user_name, "测试主人")
        self.assertEqual(result.character.outfit, original_outfit)
        self.assertEqual(result.character.portrait, original_portrait)
        self.assertEqual(result.tts.voice_profile, str(self.manifest))
        self.assertEqual(result.tts.engine_root, str(self.engine))
        self.assertEqual(result.tts.base_url, "http://127.0.0.1:9880/tts")
        self.assertEqual(result.tts.timeout_seconds, 120)
        self.assertTrue(result.personality_path("zh-CN").is_file())
        saved = self.root / "roundtrip.json"
        saved.write_text(result.model_dump_json(), encoding="utf-8")
        reopened = SettingsDialog(AppSettings.model_validate_json(saved.read_text(encoding="utf-8")))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.tts_voice_profile.text(), str(self.manifest))
        self.assertEqual(reopened.tts_url.text(), PROFILE_BASE_URL)
        self.assertEqual(reopened._current_tts_settings().voice_profile, str(self.manifest))

    def test_external_character_hides_murasame_visual_controls(self) -> None:
        dialog = self._dialog()
        for field in (dialog.outfit, dialog.portrait, dialog.portrait_ratio):
            self.assertTrue(field.isHidden())
            self.assertFalse(field.isEnabled())
        self.assertFalse(dialog.external_character_note.isHidden())
        self.assertIn("Live2DViewerEX", dialog.external_character_note.text())

    def test_profile_reports_real_emotions_and_cannot_offer_murasame_download(self) -> None:
        dialog = self._dialog()
        dialog._on_tts_checked(self._state(), False)
        self.assertIn("已配置情绪：平静", dialog.tts_status.text())
        for emotion in ("高兴", "害羞", "生气", "惊讶", "着急"):
            self.assertIn(emotion, dialog.tts_status.text())
        self.assertTrue(dialog.tts_download_button.isHidden())
        self.assertFalse(dialog.tts_download_button.isEnabled())
        self.assertTrue(dialog.tts_backend.isEnabled())
        self.assertFalse(dialog.tts_backend.model().item(dialog.tts_backend.findData("autodl")).isEnabled())
        self.assertTrue(dialog.tts_backend.model().item(dialog.tts_backend.findData("emotional_vits")).isEnabled())
        self.assertEqual(dialog.tts_backend.currentData(), "local")
        with (
            patch.object(dialog.download_manager, "start_tts") as download,
            patch("aipet.ui.settings_dialog.QMessageBox.question") as question,
        ):
            dialog._request_tts_download()
            # A late result from the previous Murasame check must be ignored.
            legacy = TTSAssetState(None, False, None, None, None, False)
            dialog._on_tts_checked(legacy, False)
        download.assert_not_called()
        question.assert_not_called()
        self.assertIn("已配置情绪：平静", dialog.tts_status.text())

    def test_clearing_profile_restores_legacy_endpoint(self) -> None:
        self.settings.character.profile_id = "murasame"
        dialog = self._dialog()
        self.assertEqual(dialog.tts_url.text(), PROFILE_BASE_URL)
        dialog.tts_voice_profile.clear()
        dialog._update_tts_state()
        self.assertEqual(dialog.tts_url.text(), "http://127.0.0.1:9880/tts")
        self.assertTrue(dialog.tts_url.isEnabled())
        self.assertFalse(dialog.outfit.isHidden())
        self.assertEqual(dialog._current_tts_settings().voice_profile, "")

    def test_baseline_ui_is_usable_while_all_six_emotions_remain_uncalibrated(self) -> None:
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["baseline_reference"] = payload["references"].pop("平静")
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")
        dialog = self._dialog()
        dialog._on_tts_checked(self._state(), False)
        self.assertIn("宁宁日常音色已可用；情绪尚未校准", dialog.tts_status.text())
        self.assertEqual(dialog._tts_status_values["configured"], "无")
        for emotion in ("平静", "高兴", "害羞", "生气", "惊讶", "着急"):
            self.assertIn(emotion, dialog._tts_status_values["missing"])
        self.assertTrue(dialog.tts_service_button.isEnabled())
        self.assertFalse(dialog.tts_download_button.isEnabled())
        dialog._set_combo_data(dialog.language_combo, "en")
        dialog._on_tts_checked(self._state(), False)
        self.assertIn("daily voice is available", dialog.tts_status.text())
        self.assertIn("emotions are not yet calibrated", dialog.tts_status.text())

    def test_health_worker_passes_profile_and_probes_actual_profile_endpoint(self) -> None:
        worker = TTSCheckWorker(self.settings.tts)
        results = []
        worker.checked.connect(lambda state, reachable: results.append((state, reachable)))
        with (
            patch("aipet.ui.settings_dialog.locate_tts_assets", return_value=self._state()) as locate,
            patch("aipet.ui.settings_dialog.tts_service_is_reachable", return_value=False) as probe,
        ):
            worker.run()
        self.assertEqual(locate.call_args.kwargs["configured_voice_profile"], str(self.manifest))
        probe.assert_called_once_with(PROFILE_BASE_URL)
        self.assertEqual(len(results), 1)

    def test_invalid_profile_check_emits_failed_signal(self) -> None:
        worker = TTSCheckWorker(self.settings.tts)
        errors = []
        worker.failed.connect(errors.append)
        with (
            patch("aipet.ui.settings_dialog.locate_tts_assets", side_effect=VoiceProfileError("missing reference")),
            patch("aipet.ui.settings_dialog.tts_service_is_reachable") as probe,
        ):
            worker.run()
        self.assertEqual(errors, ["missing reference"])
        probe.assert_not_called()

    def test_service_worker_never_switches_weights_on_legacy_endpoint(self) -> None:
        worker = TTSServiceWorker(self.settings.tts, "start")
        completed = []
        worker.succeeded.connect(completed.append)
        with (
            patch("aipet.ui.settings_dialog.locate_tts_assets", return_value=self._state()),
            patch("aipet.ui.settings_dialog.configure_local_tts_weights") as configure,
        ):
            worker.run()
        self.assertEqual(completed, ["started"])
        self.manager.ensure_running.assert_called_once()
        configure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
