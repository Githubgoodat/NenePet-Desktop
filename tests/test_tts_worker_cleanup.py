"""Cancellation and ownership checks with no model, service or audio device."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from aipet.core.backends import CharacterReply
from aipet.core.config import AppSettings
from aipet.core.tts import TTSClient, TTSCancelled, TTSError
from aipet.core.tts_assets import TTSAssetState, configure_local_tts_weights, probe_tts_service
from aipet.core.workers import ConversationWorker


class TTSOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        reference = self.root / "refs" / "平静"
        reference.mkdir(parents=True)
        (reference / "ref.wav").write_bytes(b"fixture only")
        (reference / "asr.txt").write_text("こんにちは。", encoding="utf-8")
        self.state = TTSAssetState(self.root, True, self.root / "gpt.ckpt",
                                   self.root / "sovits.pth", reference.parent, True)
        self.settings = AppSettings()
        self.settings.tts.enabled = True
        self.client = TTSClient(self.settings)
        self.addCleanup(self.client.close)
        self.client.session.close()
        self.client.session = Mock()
        self.response = Mock(spec=requests.Response)
        self.response.headers = {"Content-Type": "audio/wav"}
        self.response.status_code = 200
        self.response.content = b"RIFF-test"
        self.client.session.post.return_value = self.response
        self.locate = self.stack.enter_context(patch("aipet.core.tts.locate_tts_assets", return_value=self.state))
        self.manager = self.stack.enter_context(patch("aipet.core.tts.get_tts_service_manager")).return_value
        self.weights = self.stack.enter_context(patch("aipet.core.tts.configure_local_tts_weights"))
        self.stack.enter_context(patch("aipet.core.tts.get_cache_dir", return_value=self.root / "cache"))

    def test_cancel_during_asset_check_never_starts_service(self):
        def locate(**_kwargs):
            self.client.cancel()
            return self.state
        self.locate.side_effect = locate
        with self.assertRaises(TTSCancelled):
            self.client.synthesize("こんにちは", "平静")
        self.manager.ensure_running.assert_not_called()
        self.weights.assert_not_called()
        self.client.session.post.assert_not_called()

    def test_service_receives_cancellation_and_no_later_request(self):
        def start(*_args, **kwargs):
            self.assertFalse(kwargs["cancelled"]())
            self.client.cancel()
            self.assertTrue(kwargs["cancelled"]())
        self.manager.ensure_running.side_effect = start
        with self.assertRaises(TTSCancelled):
            self.client.synthesize("こんにちは", "平静")
        self.weights.assert_not_called()
        self.client.session.post.assert_not_called()

    def test_missing_emotion_and_mismatched_profile_fail_before_service(self):
        with self.assertRaisesRegex(TTSError, "缺少情绪参考"):
            self.client.synthesize("こんにちは", "害羞")
        self.settings.tts.voice_profile = str(self.root / "other.json")
        with self.assertRaisesRegex(TTSError, "不一致"):
            self.client.synthesize("こんにちは", "平静")
        self.manager.ensure_running.assert_not_called()
        self.client.session.post.assert_not_called()

    def test_cancel_after_http_closes_response_without_audio_cache(self):
        def post(*_args, **_kwargs):
            self.client.cancel()
            return self.response
        self.client.session.post.side_effect = post
        with self.assertRaises(TTSCancelled):
            self.client.synthesize("こんにちは", "平静")
        self.response.close.assert_called_once()
        self.assertFalse((self.root / "cache" / "voices").exists())

    def test_partial_cache_write_is_removed_and_is_recoverable_tts_error(self):
        original = Path.write_bytes
        def fail(path, content):
            original(path, content[:4])
            raise OSError("disk full")
        with patch.object(Path, "write_bytes", fail), self.assertRaisesRegex(TTSError, "缓存写入失败"):
            self.client.synthesize("こんにちは", "平静")
        self.response.close.assert_called_once()
        self.assertEqual(list((self.root / "cache" / "voices").iterdir()), [])

    def test_cancel_at_cache_commit_removes_completed_and_temporary_files(self):
        original = Path.replace
        def cancel(path, destination):
            result = original(path, destination)
            self.client.cancel()
            return result
        with patch.object(Path, "replace", cancel), self.assertRaises(TTSCancelled):
            self.client.synthesize("こんにちは", "平静")
        self.response.close.assert_called_once()
        self.assertEqual(list((self.root / "cache" / "voices").iterdir()), [])

    def test_http_error_closes_response_and_client_close_is_idempotent(self):
        self.response.raise_for_status.side_effect = requests.HTTPError("failed", response=self.response)
        self.response.headers = {"Content-Type": "text/plain"}
        self.response.text = "fixture error"
        with self.assertRaises(TTSError):
            self.client.synthesize("こんにちは", "平静")
        self.response.close.assert_called_once()
        self.client.close()
        self.client.close()
        self.client.session.close.assert_called_once()

    def test_legacy_weight_cache_is_scoped_to_endpoint(self):
        self.client.synthesize("こんにちは", "平静").unlink()
        self.settings.tts.base_url = "http://127.0.0.1:9882/tts"
        self.client.synthesize("こんにちは", "平静").unlink()
        self.assertEqual(self.weights.call_count, 2)


class ConversationCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = AppSettings()
        self.settings.tts.enabled = True
        self.reply = CharacterReply.model_validate({"sentences": [
            {"zh": "你好", "en": "hello", "ja": "こんにちは", "emotion": "平静"},
            {"zh": "再见", "en": "bye", "ja": "さようなら", "emotion": "平静"},
        ]})

    def test_cancel_before_run_does_not_create_clients(self):
        worker = ConversationWorker(self.settings, [], "hello")
        worker.cancel()
        with patch("aipet.core.workers.create_backend") as backend, patch("aipet.core.workers.TTSClient") as tts:
            worker.run()
        backend.assert_not_called()
        tts.assert_not_called()

    def test_cancel_and_unexpected_error_remove_earlier_audio_and_close_sessions(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                worker = ConversationWorker(self.settings, [], "hello")
                results, errors, warnings = [], [], []
                worker.result_ready.connect(results.append)
                worker.error.connect(errors.append)
                worker.warning.connect(warnings.append)
                audio = self.root / "first.wav"
                audio.write_bytes(b"test")
                def second(*_args):
                    if cancelled:
                        worker.cancel()
                    raise RuntimeError("injected failure")
                with patch("aipet.core.workers.create_backend") as backend, patch("aipet.core.workers.TTSClient") as tts:
                    backend.return_value.chat.return_value = self.reply
                    calls = iter((lambda: audio, lambda: second()))
                    tts.return_value.synthesize.side_effect = lambda *_: next(calls)()
                    worker.run()
                    backend.return_value.session.close.assert_called_once()
                    tts.return_value.close.assert_called_once()
                    if cancelled:
                        tts.return_value.cancel.assert_called_once()
                self.assertFalse(audio.exists())
                self.assertFalse(results)
                self.assertFalse(warnings)
                self.assertEqual(len(errors), 0 if cancelled else 1)

    def test_success_hands_audio_to_ui_and_freezes_character_settings(self):
        worker = ConversationWorker(self.settings, [], "hello")
        self.settings.tts.voice_profile = "voices/other.json"
        results = []
        worker.result_ready.connect(results.append)
        audio = self.root / "delivered.wav"
        audio.write_bytes(b"test")
        with patch("aipet.core.workers.create_backend") as backend, patch("aipet.core.workers.TTSClient") as tts:
            backend.return_value.chat.return_value = self.reply
            tts.return_value.synthesize.return_value = audio
            worker.run()
            self.assertEqual(tts.call_args.args[0].tts.voice_profile, "")
            self.assertTrue(callable(tts.call_args.kwargs["cancelled"]))
            tts.return_value.close.assert_called_once()
            backend.return_value.session.close.assert_called_once()
        self.assertTrue(audio.exists())
        self.assertEqual(results[0].audio_paths, [audio, audio])


class AssetSessionCleanupTests(unittest.TestCase):
    def test_probe_and_weight_utility_close_temporary_sessions(self):
        with patch("aipet.core.tts_assets.requests.Session") as session:
            response = session.return_value.get.return_value
            response.status_code = 200
            response.json.return_value = {"paths": {"/tts": {"post": {}}}}
            self.assertTrue(probe_tts_service("http://127.0.0.1:9880/tts").reachable)
            response.close.assert_called_once()
            session.return_value.close.assert_called_once()
        state = TTSAssetState(Path("engine"), True, Path("gpt"), Path("sovits"), None, False)
        with patch("aipet.core.tts_assets.requests.Session") as session:
            configure_local_tts_weights("http://127.0.0.1:9880/tts", state, 10)
            self.assertEqual(session.return_value.get.return_value.close.call_count, 2)
            session.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
