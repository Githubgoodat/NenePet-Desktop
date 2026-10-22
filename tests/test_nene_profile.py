from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from aipet.core.backends import APIBackend, OllamaBackend, ScreenAnalysis, build_screen_analysis_prompt, build_system_prompt
from aipet.core.config import AppSettings, PROJECT_ROOT, get_cache_dir, get_user_data_dir


class NeneProfileTests(unittest.TestCase):
    def test_nene_prompt_uses_original_schema_without_murasame_rules(self):
        settings = AppSettings.model_validate({
            "ui_language": "zh-CN",
            "character": {
                "profile_id": "nene", "name": "宁宁", "name_en": "Nene",
                "personality_file": "prompt.nene.txt",
                "personality_file_en": "prompt.nene.txt", "outfit": "casual",
            },
        })
        for language in ("zh-CN", "en"):
            settings.ui_language = language
            prompt = build_system_prompt(settings)
            self.assertIn("sentences", prompt)
            self.assertIn("綾地寧々", prompt)
            self.assertIn("平静、高兴、害羞、生气、惊讶、着急", prompt)
            self.assertNotIn("日语中的自称使用“吾輩”", prompt)
            self.assertNotIn("Murasame refers to herself", prompt)
            self.assertNotIn("紫色和服", prompt)
            self.assertIn("Live2D", prompt)

    def test_nene_identity_is_explicit_in_each_language_and_not_injected_into_other_profiles(self):
        settings = AppSettings.model_validate({"character": {"profile_id": "nene"}})
        for language in ("zh-CN", "en"):
            settings.ui_language = language
            with patch("aipet.core.backends.load_personality", return_value="character fixture"):
                prompt = build_system_prompt(settings)
            for canonical in ("宁宁", "Ayachi Nene", "Nene", "綾地寧々", "寧々"):
                self.assertIn(canonical, prompt)
            self.assertIn("历史回复中的错误姓名", prompt)
        for profile in ("murasame", "another_character"):
            settings.character.profile_id = profile
            with patch("aipet.core.backends.load_personality", return_value="character fixture"):
                self.assertNotIn("Ayachi Nene", build_system_prompt(settings))

    def test_cache_and_data_have_separate_explicit_overrides(self):
        with patch.dict(os.environ, {
            "AIPET_DATA_DIR": str(PROJECT_ROOT / "data" / "isolated-profile"),
            "AIPET_CACHE_DIR": str(PROJECT_ROOT / "data" / "isolated-cache"),
        }):
            self.assertEqual(get_user_data_dir(), (PROJECT_ROOT / "data" / "isolated-profile").resolve())
            self.assertEqual(get_cache_dir(), (PROJECT_ROOT / "data" / "isolated-cache").resolve())
            self.assertNotEqual(get_cache_dir(), get_user_data_dir())

    def test_custom_fields_survive_json_roundtrip(self):
        original = AppSettings.model_validate({
            "character": {"profile_id": "nene", "name": "宁宁", "name_en": "Nene"},
            "tts": {"voice_profile": "voices/nene.json"},
        })
        result = AppSettings.model_validate_json(original.model_dump_json())
        self.assertEqual(result.character.profile_id, "nene")
        self.assertEqual(result.character.name, "宁宁")
        self.assertEqual(result.tts.voice_profile, "voices/nene.json")

    def test_vision_role_is_character_specific_without_rewriting_previous_scene(self):
        previous = ScreenAnalysis(activity="网页中介绍丛雨和Murasame。")
        for language, name in (("zh-CN", "宁宁"), ("en", "Nene")):
            with self.subTest(language=language):
                prompt = build_screen_analysis_prompt(previous, language, character_name=name)
                self.assertIn(name, prompt)
                self.assertIn("网页中介绍丛雨和Murasame。", prompt)
                self.assertNotIn("常驻桌宠丛雨", prompt)
                self.assertNotIn("AIpet character Murasame", prompt)
        self.assertIn("常驻桌宠丛雨", build_screen_analysis_prompt(None))
        self.assertIn("AIpet character Murasame", build_screen_analysis_prompt(None, "en"))

    def test_both_vision_backends_send_nene_identity(self):
        # No screenshot or service is used; test only the actual request payloads.
        response = ScreenAnalysis(activity="查看文档").model_dump_json()
        for backend_class in (APIBackend, OllamaBackend):
            for language, name in (("zh-CN", "宁宁"), ("en", "Nene")):
                with self.subTest(backend=backend_class.__name__, language=language):
                    settings = AppSettings.model_validate({
                        "ui_language": language,
                        "character": {"profile_id": "nene", "name": "宁宁", "name_en": "Nene"},
                        "vision": {"provider": "aliyun" if backend_class is APIBackend else "ollama",
                                   "aliyun_api_key": "test-fixture-key"},
                    })
                    backend = backend_class(settings)
                    payload = ({"message": {"content": response}} if backend_class is OllamaBackend
                               else {"choices": [{"message": {"content": response}}]})
                    with patch.object(Path, "read_bytes", return_value=b"fixture"), patch.object(
                        backend, "_json_response", return_value=payload
                    ) as request:
                        backend.describe_image(Path("fixture.png"))
                    content = request.call_args.kwargs["json"]["messages"][0]["content"]
                    prompt = content if isinstance(content, str) else content[1]["text"]
                    self.assertIn(name, prompt)
                    self.assertNotIn("常驻桌宠丛雨", prompt)
                    self.assertNotIn("AIpet character Murasame", prompt)


if __name__ == "__main__":
    unittest.main()
