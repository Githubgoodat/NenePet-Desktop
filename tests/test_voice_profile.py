from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from aipet.core.config import AppSettings, TTSSettings
from aipet.core.tts import TTSClient, TTSError
from aipet.core.tts_assets import TTSHealthCheck, locate_tts_assets
from aipet.core.tts_service import LocalTTSServiceManager, TTSServiceError
from aipet.core.voice_profile import (
    BASE_MODELS,
    PROFILE_BASE_URL,
    VoiceProfileError,
    load_voice_profile,
    tts_base_url,
    write_profile_engine_config,
)


class VoiceProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = self.root / "engine"
        self.pretrained = self.engine / "GPT_SoVITS" / "pretrained_models"
        for relative in (
            "api_v2.py", "runtime/python.exe",
            "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/pytorch_model.bin",
            "GPT_SoVITS/pretrained_models/chinese-hubert-base/pytorch_model.bin",
            "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt",
        ):
            target = self.engine / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"test fixture only")
        for relative in BASE_MODELS["v2Pro"]:
            target = self.pretrained / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"PK-test-fixture-only")
        self.original_config = self.engine / "GPT_SoVITS/configs/tts_infer.yaml"
        self.original_config.parent.mkdir(parents=True)
        self.original_bytes = b"custom:\n  vits_weights_path: murasame-sovits.pth\n"
        self.original_config.write_bytes(self.original_bytes)
        self.audio = self.root / "calm.wav"
        self._write_wave(4.0)
        (self.root / "calm.txt").write_text("こんにちは。", encoding="utf-8")
        self.manifest = self.root / "voice.json"
        self.payload = {
            "schema_version": 1,
            "version": "v2Pro",
            "gpt_weights": "engine/GPT_SoVITS/pretrained_models/s1v3.ckpt",
            "sovits_weights": "engine/GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
            "text_lang": "ja",
            "prompt_lang": "ja",
            "device": "cpu",
            "references": {"平静": {"audio": "calm.wav", "transcript": "calm.txt"}},
        }
        self._save()

    def _save(self) -> None:
        self.manifest.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")

    def _write_wave(self, duration: float) -> None:
        with wave.open(str(self.audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\0\0" * round(16000 * duration))

    def _settings(self) -> TTSSettings:
        return TTSSettings(
            enabled=True, advanced_paths=True, engine_root=str(self.engine),
            voice_profile=str(self.manifest), timeout_seconds=10,
        )

    def _state(self):
        return locate_tts_assets(
            configured_engine_root=str(self.engine),
            configured_model_dir=str(self.root / "nonexistent-murasame-model"),
            configured_voice_profile=str(self.manifest),
        )

    def test_relative_references_are_paired_with_transcript(self) -> None:
        profile = load_voice_profile(str(self.manifest), self.engine)
        reference = profile.reference("平静")
        self.assertEqual(reference.audio, self.audio.resolve())
        self.assertEqual(reference.transcript, "こんにちは。")
        self.assertEqual(reference.duration_seconds, 4.0)
        self.assertFalse(profile.is_half)
        self.assertTrue(self._state().engine_ready)

    def test_missing_emotion_fails_before_service_or_http(self) -> None:
        settings = AppSettings(tts=self._settings())
        client = TTSClient(settings)
        client.session = Mock()
        with patch("aipet.core.tts.get_tts_service_manager") as manager:
            with self.assertRaisesRegex(TTSError, "缺少情绪参考语音: 生气"):
                client.synthesize("こんにちは", "生气")
        manager.assert_not_called()
        client.session.post.assert_not_called()

    def test_explicit_baseline_is_ready_without_claiming_any_emotion_reference(self) -> None:
        self.payload["baseline_reference"] = self.payload["references"].pop("平静")
        self._save()
        profile = load_voice_profile(str(self.manifest), self.engine)
        self.assertEqual(profile.references, {})
        for emotion in ("平静", "高兴", "害羞", "生气", "惊讶", "着急"):
            self.assertIs(profile.reference(emotion), profile.baseline_reference)
        self.assertTrue(self._state().reference_voices_ready)
        for unknown in ("neutral", "开心", "", None, []):
            with self.subTest(unknown=unknown), self.assertRaisesRegex(VoiceProfileError, "未知情绪"):
                profile.reference(unknown)
        del self.payload["baseline_reference"]
        self._save()
        with self.assertRaisesRegex(VoiceProfileError, "非空 references"):
            load_voice_profile(str(self.manifest), self.engine)

    def test_exact_emotion_reference_takes_priority_over_explicit_baseline(self) -> None:
        original_signature = load_voice_profile(str(self.manifest), self.engine).signature
        baseline = self.root / "baseline.wav"
        baseline.write_bytes(self.audio.read_bytes())
        self.payload["baseline_reference"] = {"audio": baseline.name, "transcript": "calm.txt"}
        self._save()
        profile = load_voice_profile(str(self.manifest), self.engine)
        self.assertEqual(set(profile.references), {"平静"})
        self.assertEqual(profile.reference("平静").audio, self.audio.resolve())
        self.assertEqual(profile.reference("生气").audio, baseline.resolve())
        self.assertNotEqual(profile.signature, original_signature)

    def test_baseline_uses_same_duration_transcript_and_file_validation(self) -> None:
        self.payload["baseline_reference"] = self.payload["references"].pop("平静")
        self._save()
        for duration in (2.999, 10.001):
            self._write_wave(duration)
            with self.subTest(duration=duration), self.assertRaisesRegex(VoiceProfileError, "3–10 秒"):
                load_voice_profile(str(self.manifest), self.engine)
        self._write_wave(4.)
        (self.root / "calm.txt").write_text(" ", encoding="utf-8")
        with self.assertRaisesRegex(VoiceProfileError, "参考文本为空"):
            load_voice_profile(str(self.manifest), self.engine)
        (self.root / "calm.txt").write_text("こんにちは。", encoding="utf-8")
        self.audio.write_bytes(self.audio.read_bytes()[:100])
        with self.assertRaisesRegex(VoiceProfileError, "WAV 文件不完整"):
            load_voice_profile(str(self.manifest), self.engine)

    def test_original_tts_client_routes_known_labels_to_baseline_without_network(self) -> None:
        self.payload["baseline_reference"] = self.payload["references"].pop("平静")
        self._save()
        client = TTSClient(AppSettings(tts=self._settings()))
        self.addCleanup(client.close)
        with patch("aipet.core.tts.get_tts_service_manager"), patch.object(client, "_request_audio") as request:
            for emotion in ("平静", "高兴", "害羞", "生气", "惊讶", "着急"):
                client.synthesize("こんにちは。", emotion)
                params = request.call_args.args[2]
                self.assertEqual(params["ref_audio_path"], str(self.audio.resolve()))
                self.assertEqual(params["prompt_text"], "こんにちは。")
            self.assertEqual(request.call_count, 6)
            with self.assertRaisesRegex(TTSError, "未知情绪"):
                client.synthesize("こんにちは。", "unknown")
            self.assertEqual(request.call_count, 6)

    def test_audio_duration_boundaries_and_out_of_range(self) -> None:
        for duration in (3.0, 10.0):
            with self.subTest(accepted=duration):
                self._write_wave(duration)
                load_voice_profile(str(self.manifest), self.engine)
        for duration in (0.0, 2.999, 10.001):
            with self.subTest(rejected=duration):
                self._write_wave(duration)
                with self.assertRaisesRegex(VoiceProfileError, "3–10 秒"):
                    load_voice_profile(str(self.manifest), self.engine)

    def test_truncated_wav_is_not_accepted_by_header_duration(self) -> None:
        self.audio.write_bytes(self.audio.read_bytes()[:100])
        with self.assertRaisesRegex(VoiceProfileError, "WAV 文件不完整"):
            load_voice_profile(str(self.manifest), self.engine)

    def test_empty_or_missing_transcript_is_explicit_error(self) -> None:
        (self.root / "calm.txt").write_text("\n", encoding="utf-8")
        with self.assertRaisesRegex(VoiceProfileError, "参考文本为空"):
            load_voice_profile(str(self.manifest), self.engine)
        (self.root / "calm.txt").unlink()
        with self.assertRaisesRegex(VoiceProfileError, "平静.transcript"):
            load_voice_profile(str(self.manifest), self.engine)

    def test_murasame_or_mismatched_weights_are_rejected(self) -> None:
        other = self.root / "murasame-sovits.pth"
        other.write_bytes(b"04-character-lora")
        self.payload["sovits_weights"] = str(other)
        self._save()
        with self.assertRaisesRegex(VoiceProfileError, "不能继承丛雨权重"):
            load_voice_profile(str(self.manifest), self.engine)

    def test_lora_cannot_be_renamed_over_a_base_weight(self) -> None:
        (self.pretrained / BASE_MODELS["v2Pro"][1]).write_bytes(b"04-character-lora")
        with self.assertRaisesRegex(VoiceProfileError, "不能使用角色 SoVITS LoRA"):
            load_voice_profile(str(self.manifest), self.engine)

    def test_manifest_and_device_validation(self) -> None:
        for field, invalid in (("schema_version", True), ("version", []), ("device", [])):
            with self.subTest(field=field):
                original = self.payload[field]
                self.payload[field] = invalid
                self._save()
                with self.assertRaises(VoiceProfileError):
                    load_voice_profile(str(self.manifest), self.engine)
                self.payload[field] = original
        self.manifest.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(VoiceProfileError, "无法读取语音配置"):
            load_voice_profile(str(self.manifest), self.engine)

    def test_compressed_audio_uses_ffprobe_with_timeout(self) -> None:
        compressed = self.root / "calm.mp3"
        compressed.write_bytes(b"test fixture")
        self.payload["references"]["平静"]["audio"] = "calm.mp3"
        self._save()
        with (
            patch("aipet.core.voice_profile.shutil.which", return_value="ffprobe"),
            patch("aipet.core.voice_profile.subprocess.run") as run,
        ):
            run.return_value.stdout = '{"format":{"duration":"4.2"}}'
            profile = load_voice_profile(str(self.manifest), self.engine)
            self.assertEqual(profile.reference("平静").duration_seconds, 4.2)
            self.assertEqual(run.call_args.args[0][-1], str(compressed.resolve()))
            self.assertEqual(run.call_args.kwargs["timeout"], 15)
            run.return_value.stdout = '{"format":{"duration":"NaN"}}'
            with self.assertRaisesRegex(VoiceProfileError, "3–10 秒"):
                load_voice_profile(str(self.manifest), self.engine)

    def test_config_is_separate_and_never_inherits_murasame_yaml(self) -> None:
        profile = load_voice_profile(str(self.manifest), self.engine)
        config_path = write_profile_engine_config(profile, self.root / "cache")
        content = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertNotEqual(config_path, self.original_config)
        self.assertEqual(self.original_config.read_bytes(), self.original_bytes)
        self.assertNotIn("murasame", json.dumps(content))
        self.assertEqual(content["custom"]["version"], "v2Pro")
        self.assertEqual(Path(content["custom"]["vits_weights_path"]), profile.sovits_weight)
        # Profiles do not need the original custom YAML even to locate assets.
        self.original_config.unlink()
        self.assertTrue(self._state().engine_ready)

    def test_client_uses_profile_endpoint_reference_and_language(self) -> None:
        self.payload["text_lang"] = "zh"
        self._save()
        client = TTSClient(AppSettings(tts=self._settings()))
        response = Mock(spec=requests.Response)
        response.headers = {"Content-Type": "audio/wav"}
        response.status_code = 200
        response.content = b"RIFF-test-audio"
        client.session = Mock()
        client.session.post.return_value = response
        with (
            patch("aipet.core.tts.get_tts_service_manager") as manager,
            patch("aipet.core.tts.configure_local_tts_weights") as configure,
            patch("aipet.core.tts.get_cache_dir", return_value=self.root / "cache"),
        ):
            output = client.synthesize("你好", "平静")
        self.assertEqual(output.read_bytes(), response.content)
        self.assertEqual(client.session.post.call_args.args[0], PROFILE_BASE_URL)
        params = client.session.post.call_args.kwargs["json"]
        self.assertEqual(params["ref_audio_path"], str(self.audio.resolve()))
        self.assertEqual(params["prompt_text"], "こんにちは。")
        self.assertEqual(params["text_lang"], "zh")
        self.assertEqual(params["prompt_lang"], "ja")
        manager.return_value.ensure_running.assert_called_once()
        configure.assert_not_called()

    def test_profile_rejects_autodl_before_network(self) -> None:
        settings = self._settings()
        settings.backend = "autodl"
        client = TTSClient(AppSettings(tts=settings))
        with patch("aipet.core.tts.get_tts_service_manager") as manager:
            with self.assertRaisesRegex(TTSError, "仅支持本地"):
                client.synthesize("こんにちは", "平静")
        manager.assert_not_called()

    def test_profile_does_not_reuse_an_unowned_online_api(self) -> None:
        manager = LocalTTSServiceManager()
        self.addCleanup(manager.shutdown)
        with (
            patch("aipet.core.tts_service.probe_tts_service", return_value=TTSHealthCheck(True, "openapi")),
            patch("aipet.core.tts_service.subprocess.Popen") as popen,
        ):
            with self.assertRaisesRegex(TTSServiceError, "9881 已被其他服务占用"):
                manager.ensure_running(self._settings(), state=self._state())
        popen.assert_not_called()
        self.assertEqual(self.original_config.read_bytes(), self.original_bytes)

    def test_profile_launch_and_owned_reuse_use_only_9881_and_own_config(self) -> None:
        manager = LocalTTSServiceManager()
        self.addCleanup(manager.shutdown)
        process = Mock(spec=subprocess.Popen)
        process.poll.return_value = None
        process.wait.return_value = 0
        with (
            patch("aipet.core.tts_service.get_cache_dir", return_value=self.root / "cache"),
            patch("aipet.core.tts_service.probe_tts_service", side_effect=[TTSHealthCheck(False, "offline"), TTSHealthCheck(True, "openapi")]),
            patch("aipet.core.tts_service.tts_service_is_reachable", side_effect=[False, True]),
            patch.object(manager._process_job, "assign"),
            patch("aipet.core.tts_service.subprocess.Popen", return_value=process) as popen,
        ):
            self.assertTrue(manager.ensure_running(self._settings(), state=self._state()))
            self.assertFalse(manager.ensure_running(self._settings(), state=self._state()))
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("-p") + 1], "9881")
        config_path = Path(command[command.index("-c") + 1])
        self.assertIn("voice-profiles", config_path.parts)
        self.assertEqual(self.original_config.read_bytes(), self.original_bytes)
        self.assertEqual(popen.call_count, 1)
        # Editing a manifest must not silently reuse the old model process.
        self.payload["text_lang"] = "zh"
        self._save()
        with patch("aipet.core.tts_service.probe_tts_service", return_value=TTSHealthCheck(True, "openapi")):
            with self.assertRaisesRegex(TTSServiceError, "配置已改变"):
                manager.ensure_running(self._settings(), state=self._state())

    def test_empty_profile_preserves_original_endpoint(self) -> None:
        settings = TTSSettings()
        self.assertEqual(settings.voice_profile, "")
        self.assertEqual(tts_base_url(settings), "http://127.0.0.1:9880/tts")


if __name__ == "__main__":
    unittest.main()
