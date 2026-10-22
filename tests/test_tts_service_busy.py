"""Owned GPT API backpressure, without HTTP, child processes or model imports."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aipet.core.config import TTSSettings
from aipet.core.tts_assets import TTSAssetState, TTSHealthCheck
from aipet.core.tts_service import LocalTTSServiceManager, TTSServiceCancelled, TTSServiceError


class OwnedTTSBusyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = SimpleNamespace(manifest_path=(self.root / "voice.json").resolve(),
                                       engine_root=self.root.resolve(), signature="profile-a")
        self.settings = TTSSettings(voice_profile=str(self.profile.manifest_path),
                                    engine_root=str(self.root), timeout_seconds=10)
        self.state = TTSAssetState(self.root.resolve(), True, self.root / "gpt.ckpt",
                                   self.root / "sovits.pth", self.root, True, self.profile)
        self.manager = LocalTTSServiceManager(platform_runtime=Mock())
        self.addCleanup(self.manager.shutdown)
        self.process = Mock(spec=subprocess.Popen)
        self.process.pid = 24680
        self.process.poll.return_value = None
        self.process.wait.return_value = 0
        self.manager._process = self.process
        self.manager._server_address = ("127.0.0.1", 9881)
        self.manager._profile_signature = self.profile.signature
        self.manager._last_stage = "ready"
        self.addCleanup(lambda: setattr(self.manager, "_process", None))
        popen_patch = patch("aipet.core.tts_service.subprocess.Popen")
        self.popen = popen_patch.start()
        self.addCleanup(popen_patch.stop)
        recheck_patch = patch("aipet.core.tts_service.tts_service_is_reachable", return_value=False)
        self.recheck = recheck_patch.start()
        self.addCleanup(recheck_patch.stop)
        self.offline = TTSHealthCheck(False, "ReadTimeout while previous inference is running")
        self.online = TTSHealthCheck(True, "OpenAPI confirmed POST /tts", 200)

    def test_busy_owned_api_recovers_and_reuses_same_process_without_holding_lock_during_http(self):
        replies = iter([self.offline, self.offline, self.online])
        def probe(_url, **_kwargs):
            self.assertFalse(self.manager._condition._is_owned())
            return next(replies)
        with (
            patch("aipet.core.tts_service.probe_tts_service", side_effect=probe),
            patch.object(self.manager._condition, "wait"),
        ):
            self.assertFalse(self.manager.ensure_running(self.settings, state=self.state))
        self.assertIs(self.manager._process, self.process)
        self.assertEqual(self.manager._profile_signature, "profile-a")
        self.assertFalse(self.manager.is_starting())
        self.popen.assert_not_called()
        self.process.terminate.assert_not_called()

    def test_cancellation_after_busy_probe_keeps_the_shared_process_alive(self):
        cancelled = threading.Event()
        calls = 0
        def probe(_url, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                cancelled.set()
                return self.online  # Cancellation still wins over a late success.
            return self.offline
        with patch("aipet.core.tts_service.probe_tts_service", side_effect=probe):
            with self.assertRaises(TTSServiceCancelled):
                self.manager.ensure_running(self.settings, state=self.state, cancelled=cancelled.is_set)
        self.assertEqual(calls, 2)
        self.assertIs(self.manager._process, self.process)
        self.process.terminate.assert_not_called()
        self.popen.assert_not_called()

    def test_persistent_busy_state_reaches_real_short_deadline_without_stopping_process(self):
        # Bypass the production minimum only in this fixture: exercise the real
        # monotonic clock and condition wait without making a ten-second test.
        settings = self.settings.model_copy(update={"timeout_seconds": 0.04})
        started = time.monotonic()
        with patch("aipet.core.tts_service.probe_tts_service", return_value=self.offline):
            with self.assertRaisesRegex(TTSServiceError, "Timed out.*owned.*API"):
                self.manager.ensure_running(settings, state=self.state)
        self.assertGreaterEqual(time.monotonic() - started, 0.035)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIs(self.manager._process, self.process)
        self.process.terminate.assert_not_called()
        self.popen.assert_not_called()

    def test_busy_wait_rejects_another_address_signature_or_foreign_online_process(self):
        cases = (
            (("127.0.0.1", 9999), "profile-a", self.process, self.offline),
            (("127.0.0.1", 9881), "different-profile", self.process, self.offline),
            (("127.0.0.1", 9881), "different-profile", self.process, self.online),
            (None, None, None, self.online),
        )
        for address, signature, process, health in cases:
            with self.subTest(address=address, signature=signature, owned=process is not None):
                self.manager._server_address = address
                self.manager._profile_signature = signature
                self.manager._process = process
                with (
                    patch("aipet.core.tts_service.probe_tts_service", return_value=health),
                    patch.object(self.manager, "_wait_for_owned_api") as wait,
                    self.assertRaises(TTSServiceError),
                ):
                    self.manager.ensure_running(self.settings, state=self.state)
                wait.assert_not_called()
        self.process.terminate.assert_not_called()
        self.popen.assert_not_called()

    def test_child_exit_or_replacement_during_probe_cannot_become_a_foreign_success(self):
        for change in ("exit", "replace", "signature"):
            with self.subTest(change=change):
                self.manager._process = self.process
                self.manager._profile_signature = "profile-a"
                self.process.poll.return_value = None
                count = 0
                def probe(_url, **_kwargs):
                    nonlocal count
                    count += 1
                    if count == 2:
                        if change == "exit":
                            self.process.poll.return_value = 7
                        elif change == "replace":
                            self.manager._process = Mock()
                        else:
                            self.manager._profile_signature = "changed"
                        return self.online
                    return self.offline
                with (
                    patch("aipet.core.tts_service.probe_tts_service", side_effect=probe),
                    self.assertRaises(TTSServiceError),
                ):
                    self.manager.ensure_running(self.settings, state=self.state)
                if change == "exit":
                    self.assertIsNone(self.manager._process)
                    self.assertEqual(self.manager._last_process_exit_code, 7)
        self.process.terminate.assert_not_called()
        self.popen.assert_not_called()

    def test_explicit_stop_interrupts_wait_without_any_automatic_restart(self):
        count = 0
        def probe(_url, **_kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.manager.stop()  # User's explicit stop, on a fake process.
            return self.offline
        with patch("aipet.core.tts_service.probe_tts_service", side_effect=probe):
            with self.assertRaises(TTSServiceCancelled):
                self.manager.ensure_running(self.settings, state=self.state)
        self.assertIsNone(self.manager._process)
        self.process.terminate.assert_called_once()
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
