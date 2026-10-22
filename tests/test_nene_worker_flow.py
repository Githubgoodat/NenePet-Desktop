from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aipet.core.backends import CharacterReply
from aipet.core.config import AppSettings, PROJECT_ROOT
from aipet.core.workers import ConversationWorker


class NeneWorkerFlowTests(unittest.TestCase):
    def settings(self):
        return AppSettings.model_validate({
            "character": {"profile_id": "nene", "name": "宁宁", "name_en": "Nene",
                          "personality_file": "prompt.nene.txt"},
            "tts": {"enabled": True, "voice_profile": "voices/nene.json"},
        })

    def reply(self, emotion="平静"):
        return CharacterReply.model_validate({"outfit": "casual", "sentences": [{
            "zh": "很高兴和你说话。", "en": "I'm happy to talk with you.",
            "ja": "お話しできてうれしいです。", "emotion": emotion, "portrait": "b",
        }]})

    def run_worker(self, settings, reply):
        worker = ConversationWorker(settings, [], "你好", user_source="voice")
        results, errors, warnings = [], [], []
        worker.result_ready.connect(results.append)
        worker.error.connect(errors.append)
        worker.warning.connect(warnings.append)
        with patch("aipet.core.workers.create_backend") as factory:
            factory.return_value.chat.return_value = reply
            worker.run()
        return results, errors, warnings

    def test_all_six_emotions_route_japanese_to_tts_and_keep_chinese_display(self):
        for emotion in ("平静", "高兴", "害羞", "生气", "惊讶", "着急"):
            with self.subTest(emotion=emotion), patch("aipet.core.workers.TTSClient") as tts:
                audio = PROJECT_ROOT / "data" / f"mock-{emotion}.wav"
                tts.return_value.synthesize.return_value = audio
                results, errors, warnings = self.run_worker(self.settings(), self.reply(emotion))
                tts.return_value.synthesize.assert_called_once_with("お話しできてうれしいです。", emotion)
                self.assertFalse(errors)
                self.assertFalse(warnings)
                self.assertEqual(results[0].audio_paths, [audio])
                self.assertEqual(results[0].reply.localized_text("zh-CN"), "很高兴和你说话。")
                self.assertEqual(results[0].reply.sentences[0].emotion, emotion)
                self.assertEqual(results[0].user_source, "voice")

    def test_missing_real_profile_references_preserve_text_and_never_start_tts(self):
        # Exercise the actual manifest validation and TTSClient, with only tiny
        # resource placeholders. Never import or run the synthesis engine.
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as directory:
            root = Path(directory)
            engine = root / "engine"
            weights = engine / "GPT_SoVITS" / "pretrained_models"
            (weights / "v2Pro").mkdir(parents=True)
            (engine / "api_v2.py").write_text("", encoding="utf-8")
            (weights / "s1v3.ckpt").write_bytes(b"fixture")
            (weights / "v2Pro/s2Gv2Pro.pth").write_bytes(b"fixture")
            manifest = root / "nene.json"
            manifest.write_text(json.dumps({
                "schema_version": 1, "version": "v2Pro", "references": {},
                "gpt_weights": str(weights / "s1v3.ckpt"),
                "sovits_weights": str(weights / "v2Pro/s2Gv2Pro.pth"),
            }), encoding="utf-8")
            settings = self.settings()
            settings.tts.advanced_paths = True
            settings.tts.engine_root = str(engine)
            settings.tts.voice_profile = str(manifest)
            with patch("aipet.core.tts.get_tts_service_manager") as manager, patch(
                "requests.Session.request", side_effect=AssertionError("No network")
            ):
                results, errors, warnings = self.run_worker(settings, self.reply())
            manager.assert_not_called()
            self.assertFalse(errors)
            self.assertEqual(len(warnings), 1)
            self.assertIn("references", warnings[0])
            self.assertEqual(results[0].audio_paths, [None])
            self.assertEqual(results[0].reply.chinese_text(), "很高兴和你说话。")


if __name__ == "__main__":
    unittest.main()
