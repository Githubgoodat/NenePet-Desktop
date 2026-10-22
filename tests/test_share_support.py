"""Portable distribution isolation and discovery checks; no GUI or network."""
from __future__ import annotations

import importlib.util
import array
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
import wave
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "packaging" / "share_support.py"
SPEC = importlib.util.spec_from_file_location("share_support_tests_target", MODULE_PATH)
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)


class ShareSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bundle = self.base / "_internal"
        self.bundle.mkdir()

    def viewer(self, root):
        root.mkdir(parents=True)
        (root / "launcher.exe").write_bytes(b"dummy")
        (root / "bin").mkdir()
        return root

    def test_import_has_no_mutable_profile_side_effect(self):
        self.assertFalse((self.base / "data").exists())

    def test_configure_replaces_inherited_local_machine_paths_without_writing(self):
        with patch.dict(os.environ, {"AIPET_DATA_DIR": "C:/somebody/profile",
                                    "AIPET_VIEWER_POSITION": "123,456",
                                    "AIPET_VIEWER_BBOX": "10,10,100,200"}):
            profile = support.configure_environment(self.base, self.bundle)
            self.assertEqual(str(profile), os.environ["AIPET_DATA_DIR"])
            self.assertEqual(str(profile / "config.json"), os.environ["AIPET_CONFIG_PATH"])
            self.assertEqual("", os.environ["AIPET_VIEWER_POSITION"])
            self.assertEqual("", os.environ["AIPET_VIEWER_BBOX"])
            self.assertEqual("0", os.environ["AIPET_VIEWER_MANAGED_POSITION"])
            self.assertEqual(str(self.bundle / "models"), os.environ["AIPET_MODEL_DIR"])
            self.assertFalse(profile.exists())

    def test_create_directories_does_not_overwrite_existing_configuration(self):
        profile = self.base / "data" / "profile"
        profile.mkdir(parents=True)
        config = profile / "config.json"
        config.write_bytes(b"private-key-placeholder")
        with patch.dict(os.environ):
            support.configure_environment(self.base, self.bundle, create=True)
        self.assertEqual(b"private-key-placeholder", config.read_bytes())

    def test_source_entry_profile_is_separate_from_production(self):
        with patch.object(sys, "frozen", False, create=True):
            base, resources = support.portable_roots()
        self.assertEqual(resources / "data" / "share-development", base)
        self.assertNotEqual(resources, base)

    def test_frozen_resources_and_writable_executable_directory_are_separate(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(self.bundle), create=True), patch.object(sys, "executable", str(self.base / "Nene.exe")):
            base, resources = support.portable_roots()
        self.assertEqual(self.base.resolve(), base)
        self.assertEqual(self.bundle.resolve(), resources)

    def test_fresh_profile_has_no_credentials_or_unavailable_services(self):
        with patch.dict(os.environ):
            support.configure_environment(self.base, self.bundle)
            settings = support.fresh_settings(self.base, self.bundle)
        self.assertEqual("nene", settings.character.profile_id)
        self.assertEqual("api", settings.mode)
        self.assertEqual("deepseek", settings.api.provider)
        self.assertEqual("", settings.api.deepseek_api_key)
        self.assertEqual("", settings.api.openai_api_key)
        self.assertEqual("", settings.api.aliyun_api_key)
        self.assertFalse(settings.tts.enabled)
        self.assertFalse(settings.stt.enabled)
        self.assertFalse(settings.vision.enabled)
        self.assertFalse(settings.display.show_log_console)
        self.assertEqual("prompt.nene.txt", settings.character.personality_file)
        self.assertFalse((self.base / "data").exists())

    def test_modern_library_paths_support_unicode_without_numeric_app_sizes(self):
        text = r'''"libraryfolders" { "0" { "path" "C:\\Steam" "apps" { "616720" "123456789" } }
                    "1" { "path" "D:\\游戏库" } }'''
        self.assertEqual([Path(r"C:\Steam"), Path(r"D:\游戏库")], support.library_paths(text))

    def test_local_voice_reconfiguration_replaces_previous_remote_or_vits_backend(self):
        for previous_backend, device in (("autodl", "cpu"), ("emotional_vits", "cuda")):
            with self.subTest(previous_backend=previous_backend), patch.dict(os.environ):
                support.configure_environment(self.base, self.bundle)
                settings = support.fresh_settings(self.base, self.bundle)
                settings.tts.backend = previous_backend
                settings.tts.base_url = "http://old-server.invalid:9880/tts"
                selected = str(self.base / "voices" / ("nene-" + device + ".json"))
                with patch("aipet.core.voice_profile.load_voice_profile") as validate:
                    support.apply_voice_choice(settings, self.base, selected)
                validate.assert_called_once_with(selected, self.base / "engine" / "GPT-SoVITS")
                self.assertTrue(settings.tts.enabled)
                self.assertEqual("local", settings.tts.backend)
                self.assertEqual("http://127.0.0.1:9881/tts", settings.tts.base_url)
                self.assertEqual(selected, settings.tts.voice_profile)
                self.assertEqual(str(self.base / "engine" / "GPT-SoVITS"), settings.tts.engine_root)
                self.assertTrue(settings.tts.advanced_paths)

    def test_invalid_voice_reconfiguration_keeps_previous_candidate_settings(self):
        with patch.dict(os.environ):
            support.configure_environment(self.base, self.bundle)
            settings = support.fresh_settings(self.base, self.bundle)
        before = settings.tts.model_dump()
        with patch("aipet.core.voice_profile.load_voice_profile", side_effect=ValueError("missing weights")), self.assertRaises(ValueError):
            support.apply_voice_choice(settings, self.base, "unavailable-profile.json")
        self.assertEqual(before, settings.tts.model_dump())

    def test_legacy_library_paths(self):
        text = r'''"LibraryFolders" { "TimeNextStatsReport" "123" "1" "E:\\SteamLibrary" }'''
        self.assertEqual([Path(r"E:\SteamLibrary")], support.library_paths(text))

    def test_discovery_searches_current_steam_and_secondary_library(self):
        steam = self.base / "Steam"
        secondary = self.base / "游戏库"
        first = self.viewer(steam / "steamapps" / "common" / "Live2DViewerEX")
        second = self.viewer(secondary / "steamapps" / "common" / "Live2DViewerEX")
        (steam / "steamapps" / "libraryfolders.vdf").write_text('"libraryfolders" { "1" { "path" '
            + json.dumps(str(secondary), ensure_ascii=False) + ' } }', encoding="utf-8")
        self.assertEqual([first.resolve(), second.resolve()], support.discover_viewers(self.base, roots=[steam]))

    def test_manual_launcher_or_bin_directory_normalizes(self):
        viewer = self.viewer(self.base / "Live2DViewerEX")
        self.assertEqual(viewer.resolve(), support.normalize_viewer_root(viewer / "launcher.exe"))
        self.assertEqual(viewer.resolve(), support.normalize_viewer_root(viewer / "bin"))
        self.assertIsNone(support.normalize_viewer_root(self.base / "missing"))

    def test_local_sidecar_is_detected_without_registry(self):
        viewer = self.viewer(self.base / "Live2DViewerEX")
        self.assertEqual([viewer.resolve()], support.discover_viewers(self.base, roots=[]))

    def test_parse_coordinates_rejects_nonfinite_wrong_count_and_small_boxes(self):
        self.assertIsNone(support.parse_coordinates(" ", 4))
        self.assertEqual((1.0, -3.0), support.parse_coordinates("1,-3", 2))
        for value, count in (("nan,1", 2), ("inf,2", 2), ("1", 2), ("0,0,39,80", 4), ("0,0,40,79", 4)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                support.parse_coordinates(value, count)

    def test_viewer_settings_never_implicitly_manage_an_unproven_position(self):
        viewer = self.viewer(self.base / "Live2DViewerEX")
        with patch.dict(os.environ):
            support.apply_viewer_settings({"viewer_root": str(viewer)})
            self.assertEqual("0", os.environ["AIPET_VIEWER_MANAGED_POSITION"])
            self.assertEqual("", os.environ["AIPET_VIEWER_POSITION"])
            self.assertEqual(str(viewer / "bin" / "app_port"), os.environ["AIPET_VIEWER_PORT_FILE"])
        with self.assertRaises(ValueError):
            support.apply_viewer_settings({"viewer_root": str(viewer), "managed_position": True})

    def test_viewer_settings_save_only_explicit_selection_and_round_trip(self):
        viewer = self.viewer(self.base / "Live2DViewerEX")
        profile = self.base / "profile"
        selection = {"schema_version": 1, "viewer_root": str(viewer),
                     "bbox": "10,20,300,600", "position": "500,-200", "managed_position": True}
        with patch.dict(os.environ):
            support.write_viewer_settings(profile, selection)
            self.assertEqual(selection, support.read_viewer_settings(profile))
        self.assertEqual(["share-viewer.json"], [item.name for item in profile.iterdir()])

    def test_invalid_viewer_config_preserves_existing_file_bytes(self):
        profile = self.base / "profile"
        profile.mkdir()
        path = profile / "share-viewer.json"
        path.write_bytes(b"old-config")
        with self.assertRaises(ValueError):
            support.write_viewer_settings(profile, {"viewer_root": str(self.base / "absent")})
        self.assertEqual(b"old-config", path.read_bytes())

    def test_launcher_runs_only_installed_executable_without_mode_arguments(self):
        viewer = self.viewer(self.base / "Live2DViewerEX")
        with patch.object(support.subprocess, "Popen") as process, patch.object(support, "hidden_process_options", return_value={"hidden": True}):
            self.assertTrue(support.launch_viewer(viewer))
        args, kwargs = process.call_args
        self.assertEqual([str(viewer / "launcher.exe")], args[0])
        self.assertEqual(str(viewer), kwargs["cwd"])
        self.assertTrue(kwargs["hidden"])

    def test_interactive_setup_child_does_not_hide_first_window(self):
        with patch.object(support.os, "name", "nt"):
            options = support.interactive_process_options()
        self.assertEqual({"creationflags": support.subprocess.CREATE_NO_WINDOW}, options)
        self.assertNotIn("startupinfo", options)

    def test_external_environment_removes_bundle_and_python_qt_paths_only(self):
        original = {"PATH": os.pathsep.join((str(self.bundle), str(self.bundle / "Qt5"),
                    str(self.base / "engine/runtime"), "C:/Windows/System32")),
                    "PYTHONHOME": "bad-python", "PYTHONPATH": "bad-path",
                    "QT_PLUGIN_PATH": "bad-plugins", "QT_QPA_PLATFORM_PLUGIN_PATH": "bad-qt",
                    "_PYI_PARENT_PROCESS_LEVEL": "1", "KEEP_SETTING": "retain"}
        with patch.object(sys, "_MEIPASS", str(self.bundle), create=True):
            cleaned = support.external_environment(original)
        self.assertEqual(os.pathsep.join((str(self.base / "engine/runtime"), "C:/Windows/System32")), cleaned["PATH"])
        self.assertEqual("retain", cleaned["KEEP_SETTING"])
        self.assertNotIn("PYTHONHOME", cleaned)
        self.assertNotIn("PYTHONPATH", cleaned)
        self.assertNotIn("QT_PLUGIN_PATH", cleaned)
        self.assertNotIn("QT_QPA_PLATFORM_PLUGIN_PATH", cleaned)
        self.assertNotIn("_PYI_PARENT_PROCESS_LEVEL", cleaned)
        self.assertEqual("bad-python", original["PYTHONHOME"])

    def test_external_launch_restores_dll_directory_even_when_creation_fails(self):
        kernel = Mock()
        def previous_directory(size, buffer):
            buffer.value = str(self.bundle)
            return len(str(self.bundle))
        kernel.GetDllDirectoryW.side_effect = previous_directory
        kernel.SetDllDirectoryW.return_value = True
        with patch.object(sys, "frozen", True, create=True), patch.object(support, "_dll_directory_api", return_value=kernel), patch.object(support.subprocess, "Popen", side_effect=OSError("test creation failure")), self.assertRaises(OSError):
            support.safe_external_popen(["external-python.exe"], env={"PATH": "runtime"})
        self.assertEqual([None, str(self.bundle)], [call.args[0] for call in kernel.SetDllDirectoryW.call_args_list])

    def test_subprocess_proxy_forwards_constants_and_keeps_global_popen_untouched(self):
        original = support.subprocess.Popen
        proxy = support.ExternalSubprocessProxy(support.subprocess)
        with patch.object(support, "safe_external_popen", return_value="owned-child") as launch:
            self.assertEqual("owned-child", proxy.Popen(["runtime/python.exe"], stdout=support.subprocess.PIPE))
        launch.assert_called_once_with(["runtime/python.exe"], stdout=support.subprocess.PIPE)
        self.assertIs(support.subprocess.Popen, original)
        self.assertIs(proxy.TimeoutExpired, support.subprocess.TimeoutExpired)
        self.assertEqual(proxy.STDOUT, support.subprocess.STDOUT)

    def test_successful_child_is_cleaned_if_dll_restore_fails(self):
        kernel = Mock()
        def previous_directory(size, buffer):
            buffer.value = str(self.bundle)
            return len(str(self.bundle))
        kernel.GetDllDirectoryW.side_effect = previous_directory
        kernel.SetDllDirectoryW.side_effect = [True, False]
        owned = Mock()
        owned.poll.return_value = None
        with patch.object(sys, "frozen", True, create=True), patch.object(support, "_dll_directory_api", return_value=kernel), patch.object(support.subprocess, "Popen", return_value=owned), self.assertRaises(OSError):
            support.safe_external_popen(["runtime/python.exe"])
        owned.terminate.assert_called_once()
        owned.wait.assert_called_once_with(timeout=5)
        owned.kill.assert_not_called()

    @unittest.skip('Full distribution self-test requires private model and screenshot evidence absent from the public repository.')
    def test_offline_report_does_not_inspect_installed_configuration(self):
        resources = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ):
            profile = support.configure_environment(self.base, resources)
            with patch.object(support, "launch_viewer", side_effect=AssertionError("unexpected Viewer start")), patch.object(support, "discover_viewers", side_effect=AssertionError("unexpected discovery")):
                report = support.self_test(self.base, resources)
        self.assertTrue(report["success"], report["errors"])
        self.assertFalse(report["profile_read"])
        self.assertFalse(report["profile_written"])
        self.assertFalse(report["network_used"])
        self.assertFalse(profile.exists())
        self.assertTrue(report["checks"]["persona_all_sources_match"])
        self.assertEqual(12, report["checks"]["persona_screenshot_count"])
        self.assertTrue(report["checks"]["pdf_dynamic_import_and_memory_roundtrip"])
        self.assertTrue(report["checks"]["audio_output_enumeration"])
        self.assertTrue(report["checks"]["import:pynput.keyboard._win32"])
        self.assertTrue(report["checks"]["external_process_environment_isolated"])


class ShareMainLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.resources = Path(__file__).resolve().parents[1]
        main_spec = importlib.util.spec_from_file_location("share_main_tests_target", self.resources / "packaging" / "share_main.py")
        self.entry = importlib.util.module_from_spec(main_spec)
        with patch.dict(sys.modules, {"share_support": support}):
            main_spec.loader.exec_module(self.entry)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        roots = patch.object(self.entry, "portable_roots", return_value=(self.base, self.resources))
        roots.start()
        self.addCleanup(roots.stop)
        qt_attribute = patch("PyQt5.QtWidgets.QApplication.setAttribute")
        qt_attribute.start()
        self.addCleanup(qt_attribute.stop)
        self.lock = patch.object(self.entry, "_lock", return_value=object())
        self.lock_mock = self.lock.start()
        self.addCleanup(self.lock.stop)
        self.profile = self.base / "data" / "profile"

    def configured_viewer(self):
        viewer = self.base / "Viewer"
        viewer.mkdir()
        (viewer / "bin").mkdir()
        (viewer / "launcher.exe").write_bytes(b"dummy")
        self.profile.mkdir(parents=True, exist_ok=True)
        (self.profile / "config.json").write_text("{}", encoding="utf-8")
        support.write_viewer_settings(self.profile, {"schema_version": 1, "viewer_root": str(viewer)})
        return viewer

    def test_self_test_branch_creates_no_profile_or_lock_and_opens_no_gui(self):
        with patch.object(self.entry, "self_test", return_value={"success": True}) as check, patch.object(self.entry, "_write_report", return_value=0), patch.object(self.entry, "launch_viewer") as launch:
            self.assertEqual(0, self.entry.main(["--share-self-test"]))
        check.assert_called_once_with(self.base, self.resources)
        self.lock_mock.assert_not_called()
        launch.assert_not_called()
        self.assertFalse(self.profile.exists())

    def test_log_viewer_special_mode_does_not_enter_first_setup_or_write_profile(self):
        with patch("aipet.application.run_special_mode", return_value=0) as special, patch.object(self.entry, "_setup") as setup:
            self.assertEqual(0, self.entry.main(["--log-viewer", "test-log-dir", "123"]))
        self.assertEqual(["--log-viewer", "test-log-dir", "123"], special.call_args.args[0][1:])
        setup.assert_not_called()
        self.lock_mock.assert_not_called()
        self.assertFalse(self.profile.exists())

    def test_setup_child_does_not_contend_for_parent_lock(self):
        with patch.object(self.entry, "_setup", return_value=1) as setup, patch.object(self.entry, "launch_viewer") as launch:
            self.assertEqual(1, self.entry.main(["--share-setup", "--share-setup-child"]))
        setup.assert_called_once()
        self.lock_mock.assert_not_called()
        launch.assert_not_called()

    def test_voice_check_validation_failure_does_not_fall_through_to_normal_startup(self):
        checker = Mock()
        checker.run_voice_check.side_effect = ValueError("existing report")
        with patch.dict(sys.modules, {"share_voice_check": checker}), patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(2, self.entry.main(["--share-voice-check", "--engine-root", str(self.base / "engine"),
                                               "--report", str(self.base / "previous.json")]))
        self.lock_mock.assert_not_called()
        self.assertFalse(self.profile.exists())

    def test_cancelling_first_setup_never_launches_viewer_or_pet(self):
        with patch.object(self.entry.subprocess, "run", return_value=Mock(returncode=1)) as run, patch.object(self.entry, "launch_viewer") as launch, patch("aipet.application.main") as pet:
            self.assertEqual(1, self.entry.main([]))
        command = run.call_args.args[0]
        self.assertEqual(["--share-setup", "--share-setup-child"], command[-2:])
        self.assertNotIn("startupinfo", run.call_args.kwargs)
        launch.assert_not_called()
        pet.assert_not_called()
        self.assertFalse((self.profile / "config.json").exists())

    def test_existing_profile_launches_installed_viewer_then_single_application(self):
        viewer = self.configured_viewer()
        sequence = []
        original = __import__("aipet.application", fromlist=["configure_voice_trigger"]).configure_voice_trigger
        def pet_main():
            sequence.append("pet")
            from aipet import application
            tray = Mock()
            settings = Mock()
            settings.stt.enabled = True
            self.assertIsNone(application.configure_voice_trigger(None, settings, tray))
            tray.showMessage.assert_called_once()
            return 0
        with patch.object(self.entry, "launch_viewer", side_effect=lambda path: sequence.append("viewer")) as launch, patch("aipet.application.main", side_effect=pet_main) as pet, patch.object(self.entry.subprocess, "run") as setup_child:
            self.assertEqual(0, self.entry.main([]))
        launch.assert_called_once_with(viewer.resolve())
        pet.assert_called_once()
        setup_child.assert_not_called()
        self.assertEqual(["viewer", "pet"], sequence)
        from aipet import application
        self.assertIs(original, application.configure_voice_trigger)
        self.assertEqual("{}", (self.profile / "config.json").read_text(encoding="utf-8"))


class ShareVoiceCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.resources = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("share_voice_check_tests_target", self.resources / "packaging" / "share_voice_check.py")
        self.check = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"share_support": support}):
            spec.loader.exec_module(self.check)
        # Public CI substitutes a generated tone and synthetic transcript for
        # the unavailable game reference. This tests routing and cleanup only,
        # and never claims to reproduce the character's timbre.
        self.resources = self.base / "synthetic-resources"
        fixture_dir = self.resources / "voice-sources"
        fixture_dir.mkdir(parents=True)
        with wave.open(str(fixture_dir / "c2-2-nene-01-clause.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(array.array("h", [0, 12000, -12000, 0] * 16000).tobytes())
        (fixture_dir / "c2-2-nene-01-clause.txt").write_text("synthetic reference fixture", encoding="utf-8")
        self.engine = self.base / "external-engine"
        self.engine.mkdir()

    @staticmethod
    def sample_wav(silent=False):
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(array.array("h", ([0] if silent else [0, 12000, -12000]) * 4800).tobytes())
        return output.getvalue()

    def test_wav_acceptance_checks_real_pcm_amplitude_and_silence(self):
        result = self.check.wav_metrics(self.sample_wav())
        self.assertGreater(result["rms"], .1)
        self.assertGreater(result["duration_seconds"], .2)
        with self.assertRaises(ValueError):
            self.check.wav_metrics(self.sample_wav(silent=True))
        with self.assertRaises((ValueError, wave.Error)):
            self.check.wav_metrics(b"not-a-WAV")

    def test_existing_report_and_engine_contained_report_are_never_overwritten(self):
        report = self.base / "existing.json"
        report.write_bytes(b"previous-report")
        with self.assertRaises(ValueError):
            self.check.run_voice_check(self.base, self.resources, self.engine, report)
        self.assertEqual(b"previous-report", report.read_bytes())
        with self.assertRaises(ValueError):
            self.check.run_voice_check(self.base, self.resources, self.engine, self.engine / "report.json")
        self.assertEqual([], list(self.engine.iterdir()))

    def test_busy_acceptance_port_does_not_inspect_or_start_existing_services(self):
        (self.engine / "old" / "__pycache__").mkdir(parents=True)
        report_path = self.base / "busy.json"
        environment = dict(os.environ)
        with patch.object(self.check, "_check_port_free", side_effect=RuntimeError("19883 is busy")):
            report = self.check.run_voice_check(self.base, self.resources, self.engine, report_path)
        self.assertFalse(report["success"])
        self.assertEqual("19883 is busy", report["error"])
        self.assertNotIn("engine_new_bytecode_directories", report)
        self.assertIsNone(report["owned_child_stopped"])
        self.assertEqual(environment, dict(os.environ))
        self.assertFalse((self.base / "data/profile").exists())

    def test_runtime_caches_are_redirected_and_bytecode_is_disabled(self):
        with patch.dict(os.environ):
            self.check._isolated_environment(self.base / "check")
            self.assertEqual("1", os.environ["PYTHONDONTWRITEBYTECODE"])
            self.assertEqual("", os.environ["CUDA_VISIBLE_DEVICES"])
            self.assertEqual("1", os.environ["HF_HUB_OFFLINE"])
            for variable in ("HF_HOME", "TORCH_HOME", "NUMBA_CACHE_DIR", "TEMP", "TMP", "AIPET_CACHE_DIR"):
                self.assertTrue(Path(os.environ[variable]).is_relative_to(self.base / "check"))

    def _mocked_standard_launch(self, *, fail_cleanup=False):
        for relative in ("api_v2.py", "runtime/python.exe", "GPT_SoVITS/pretrained_models/s1v3.ckpt",
                         "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth"):
            path = self.engine / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"dummy-model")
        import aipet.core.tts_service as service
        import requests
        original_proxy = service.subprocess
        owned = Mock()
        owned.pid = 123456
        owned.poll.return_value = None
        manager = Mock()
        manager._launch.return_value = owned
        manager.shutdown.side_effect = lambda: setattr(owned.poll, "return_value", 0)
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"paths": {"/tts": {"post": {"requestBody": {"required": True}}}}}
        response = Mock()
        response.status_code = 200
        response.iter_content.return_value = [self.sample_wav()]
        session.post.return_value.__enter__ = Mock(return_value=response)
        session.post.return_value.__exit__ = Mock(return_value=False)
        if fail_cleanup:
            session.close.side_effect = OSError("synthetic close failure")
        def command(engine, address, runtime, *, config_path):
            self.assertEqual(("127.0.0.1", 19883), address)
            return ["runtime/python.exe", "api_v2.py"], dict(os.environ)
        environment = dict(os.environ)
        with patch.object(self.check, "_check_port_free"), patch.object(service, "_build_start_command", side_effect=command) as build, patch.object(service, "LocalTTSServiceManager", return_value=manager), patch.object(requests, "Session", return_value=session):
            report = self.check.run_voice_check(self.base, self.resources, self.engine, self.base / "success.json")
        self.assertEqual(not fail_cleanup, report["success"], report)
        self.assertTrue(report["owned_child_stopped"])
        self.assertFalse(report["profile_read"])
        self.assertFalse(report["audio_played"])
        self.assertTrue(Path(report["wav_path"]).is_file())
        self.assertEqual("http://127.0.0.1:19883/tts", session.post.call_args.args[0])
        self.assertFalse(session.trust_env)
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(1, session.post.call_count)
        manager.shutdown.assert_called_once()
        self.assertIs(original_proxy, service.subprocess)
        self.assertEqual(environment, dict(os.environ))
        return report

    def test_mocked_standard_launch_synthesizes_only_dedicated_port_and_cleans_owner(self):
        self._mocked_standard_launch()

    def test_cleanup_failure_still_stops_owner_and_restores_environment_and_proxy(self):
        report = self._mocked_standard_launch(fail_cleanup=True)
        self.assertIn("session:OSError", report["cleanup_errors"])


if __name__ == "__main__":
    unittest.main()
