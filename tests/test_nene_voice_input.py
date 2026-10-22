from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aipet.core.config import PROJECT_ROOT
from aipet.core.stt import clear_model_cache, transcribe_full, transcription_prompt
from aipet.core.voice_input import HoldToTalkSession


class NeneVoiceInputTests(unittest.TestCase):
    def setUp(self):
        clear_model_cache()
        self.addCleanup(clear_model_cache)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=PROJECT_ROOT)))
        self.enterContext(patch("aipet.core.voice_input.get_cache_dir", return_value=self.root))

    def test_nene_prompts_and_unknown_profile_do_not_inherit_murasame(self):
        for language, name in (("zh", "宁宁"), ("en", "Nene"), ("ja", "寧々")):
            prompt = transcription_prompt(language, "nene")
            self.assertIn(name, prompt)
            for old_name in ("丛雨", "Murasame", "ムラサメ"):
                self.assertNotIn(old_name, prompt)
                self.assertNotIn(old_name, transcription_prompt(language, "another-character"))
        self.assertIn("丛雨", transcription_prompt("zh"))

    def test_nene_transcription_uses_hint_without_rewriting_transcript(self):
        model = Mock()
        model.transcribe.return_value = (iter([SimpleNamespace(text="请保持原话。")]), None)
        constructor = Mock(return_value=model)
        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=constructor)}), patch(
            "aipet.core.stt.find_local_model", return_value="fake-local-model"
        ):
            text = transcribe_full("fake.wav", device="cpu", language="zh", vocabulary_profile="nene")
        self.assertEqual(text, "请保持原话。")
        self.assertIn("宁宁", model.transcribe.call_args.kwargs["initial_prompt"])

    def test_language_detection_still_has_no_forced_language_or_hint(self):
        model = Mock()
        model.transcribe.return_value = (iter([SimpleNamespace(text="hello")]), None)
        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=Mock(return_value=model))}), patch(
            "aipet.core.stt.find_local_model", return_value="fake-local-model"
        ):
            transcribe_full("fake.wav", device="cpu", language=None, vocabulary_profile="nene")
        self.assertNotIn("initial_prompt", model.transcribe.call_args.kwargs)
        self.assertNotIn("language", model.transcribe.call_args.kwargs)

    def session(self):
        session = HoldToTalkSession(on_text_ready=Mock(), on_error=Mock(), vocabulary_profile="nene")
        session._recorder = Mock()
        self.addCleanup(session.stop_session)
        return session

    def test_stopped_recognition_never_delivers_late_text_or_error_and_deletes_wav(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                session = self.session()
                audio = self.root / f"fake-{fail}.wav"
                audio.write_bytes(b"mock-audio-never-played")
                session._recorder.stop_and_save.return_value = str(audio)
                entered, release = threading.Event(), threading.Event()

                def recognize(*args, **kwargs):
                    self.assertEqual(kwargs["vocabulary_profile"], "nene")
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError("test release was not signaled")
                    if fail:
                        raise RuntimeError("mock ASR failure after stop")
                    return "迟到结果"

                with patch("aipet.core.voice_input.transcribe_full", side_effect=recognize):
                    try:
                        session._handle_record_done()
                        self.assertTrue(entered.wait(1))
                        self.assertTrue(session.is_busy())
                        session.stop_session()
                        self.assertTrue(session.is_busy())
                        # Restarting does not revive a previous session's result.
                        session.start_session()
                    finally:
                        release.set()
                        if session._recognition_thread:
                            session._recognition_thread.join(2)
                session.on_text_ready.assert_not_called()
                session.on_error.assert_not_called()
                self.assertFalse(session.is_busy())
                self.assertFalse(audio.exists())

    def test_recording_opened_after_stop_is_closed_without_callback(self):
        session = self.session()
        session.on_record_start = Mock()
        session._pressed = True
        entered, release = threading.Event(), threading.Event()

        def open_stream():
            entered.set()
            release.wait(2)

        session._recorder.start.side_effect = open_stream
        thread = threading.Thread(target=session._maybe_start_record)
        try:
            thread.start()
            self.assertTrue(entered.wait(1))
            session.stop_session()
            self.assertTrue(session.is_busy())
        finally:
            release.set()
            thread.join(2)
        session.on_record_start.assert_not_called()
        self.assertGreaterEqual(session._recorder.close.call_count, 2)
        self.assertFalse(session.is_busy())

    def test_stop_blocks_new_timer_until_explicit_restart(self):
        session = self.session()
        session.stop_session()
        with patch("aipet.core.voice_input.threading.Timer") as timer:
            session.press()
            timer.assert_not_called()
            session.start_session()
            session.press()
            timer.assert_called_once()

    def test_failed_save_clears_busy_state(self):
        session = self.session()
        session._recorder.stop_and_save.side_effect = OSError("mock capture failure")
        session._handle_record_done()
        self.assertFalse(session.is_busy())
        session.on_error.assert_called_once_with("mock capture failure")


if __name__ == "__main__":
    unittest.main()
