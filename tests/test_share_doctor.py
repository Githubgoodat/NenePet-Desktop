"""Source setup diagnostics: no application imports, private state or services."""
from __future__ import annotations

import builtins
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doctor = load_module("share_doctor_test_target", "packaging/share_doctor.py")
support = load_module("share_doctor_support_target", "packaging/share_support.py")


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="nene-doctor-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "runtime"
        self.resources = self.root / "source"
        self.resources.mkdir()
        (self.resources / "prompt.nene.txt").write_text("synthetic persona", encoding="utf-8")
        self.model = self.resources / doctor.MODEL_LABEL
        self.model.parent.mkdir(parents=True)
        self.manifest = {"Version": 3, "FileReferences": {"Moc": "nene.moc3", "Textures": ["texture.png"]}}
        self.write_model()
        for name in ("nene.moc3", "texture.png"):
            (self.model.parent / name).write_bytes(b"synthetic, not a real model")
        self.addCleanup(patch.stopall)
        patch.object(doctor.sys, "platform", "win32").start()
        patch.object(doctor.struct, "calcsize", return_value=8).start()
        patch.object(doctor.sys, "version_info", sys.version_info).start()
        self.metadata = patch.object(doctor.metadata, "version", return_value="1.0").start()

    def write_model(self):
        self.model.write_text(json.dumps(self.manifest), encoding="utf-8")

    def diagnose(self, **kwargs):
        return doctor.diagnose(self.resources, base=self.base, **kwargs)

    def by_name(self, report, name):
        return next(item for item in report["checks"] if item["name"] == name)

    def test_optional_assets_do_not_block_text_mode_and_report_is_path_free(self):
        report = self.diagnose()
        self.assertTrue(report["success"])
        self.assertEqual("missing", self.by_name(report, "scene_evidence")["status"])
        self.assertEqual("missing", self.by_name(report, "voice")["status"])
        self.assertEqual("not_checked", self.by_name(report, "viewer")["status"])
        self.assertFalse(report["profile_read"])
        self.assertFalse(report["profile_written"])
        self.assertFalse(report["network_used"])
        self.assertFalse(report["viewer_started"])
        self.assertNotIn(str(self.root), json.dumps(report))
        self.assertFalse(self.base.exists())

    def test_missing_model_is_actionable_required_blocker(self):
        self.model.unlink()
        report = self.diagnose()
        self.assertIn(doctor.MODEL_LABEL, report["blockers"])
        self.assertIn("docs/LOCAL_ASSETS.md", self.by_name(report, doctor.MODEL_LABEL)["detail"])
        self.assertFalse(report["success"])

    def test_missing_declared_file_invalid_json_and_malformed_motion_are_reported(self):
        (self.model.parent / "texture.png").unlink()
        self.manifest["FileReferences"]["Motions"] = {"untrusted private label": [1]}
        self.write_model()
        report = self.diagnose()
        issues = self.by_name(report, doctor.MODEL_LABEL)["issues"]
        self.assertIn("Textures[0]:missing_empty_or_outside_model_directory", issues)
        self.assertIn("Motions[0][0].File:invalid_local_reference", issues)
        self.assertNotIn("untrusted private label", json.dumps(report))
        self.model.write_text("{broken", encoding="utf-8")
        self.assertEqual(["model_entry_unreadable_or_invalid_json"], self.by_name(self.diagnose(), doctor.MODEL_LABEL)["issues"])

    def test_external_model_references_are_rejected_without_echoing_private_paths(self):
        self.manifest["FileReferences"]["Moc"] = "../private-secret.moc3"
        self.manifest["FileReferences"]["Textures"] = ["C:\\private-user\\secret.png", "/secret.png"]
        self.write_model()
        report = self.diagnose()
        self.assertFalse(report["success"])
        self.assertNotIn("private-secret", json.dumps(report))
        self.assertNotIn("private-user", json.dumps(report))
        self.assertEqual(3, len(self.by_name(report, doctor.MODEL_LABEL)["issues"]))

    def test_all_declared_auxiliary_files_are_checked(self):
        self.manifest["FileReferences"].update({"Physics": "physics.json", "Expressions": [{"File": "face.json"}],
            "Motions": {"Tap": [{"File": "tap.json", "Sound": "native.wav"}]}})
        self.write_model()
        issues = self.by_name(self.diagnose(), doctor.MODEL_LABEL)["issues"]
        self.assertEqual(4, len(issues))
        for name in ("physics.json", "face.json", "tap.json", "native.wav"):
            (self.model.parent / name).write_bytes(b"synthetic")
        self.assertTrue(self.diagnose()["success"])

    def test_invalid_path_bytes_are_model_or_viewer_issues_not_report_failures(self):
        self.manifest["FileReferences"]["Moc"] = "invalid\0model.moc3"
        self.write_model()
        report = self.diagnose(viewer_root=Path("invalid\0viewer"))
        self.assertEqual("invalid", self.by_name(report, doctor.MODEL_LABEL)["status"])
        self.assertEqual("invalid", self.by_name(report, "viewer")["status"])
        self.assertNotIn("invalid\\u0000", json.dumps(report))

    def test_escaping_reference_is_not_opened_or_statted(self):
        self.manifest["FileReferences"]["Moc"] = "../private-secret.moc3"
        self.write_model()
        original_is_file = Path.is_file
        original_stat = Path.stat
        def guarded_is_file(path, *args, **kwargs):
            if path.name == "private-secret.moc3":
                raise AssertionError("escaping reference inspected")
            return original_is_file(path, *args, **kwargs)
        def guarded_stat(path, *args, **kwargs):
            if path.name == "private-secret.moc3":
                raise AssertionError("escaping reference inspected")
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, "is_file", guarded_is_file), patch.object(Path, "stat", guarded_stat):
            self.assertFalse(self.diagnose()["success"])

    def test_missing_required_dependency_blocks_but_optional_does_not(self):
        def version(name):
            if name in {"PyQt5", "pypdf"}:
                raise doctor.metadata.PackageNotFoundError(name)
            return "1.0"
        self.metadata.side_effect = version
        report = self.diagnose()
        self.assertEqual(["dependency:PyQt5"], report["blockers"])
        self.assertEqual("missing", self.by_name(report, "dependency:pypdf")["status"])

    def test_explicit_viewer_only_checks_structure_and_reports_no_authorization_claim(self):
        viewer = self.root / "viewer"
        report = self.diagnose(viewer_root=viewer)
        self.assertIn("viewer", report["blockers"])
        viewer.mkdir()
        (viewer / "launcher.exe").write_bytes(b"synthetic")
        (viewer / "bin").mkdir()
        report = self.diagnose(viewer_root=viewer)
        self.assertTrue(report["success"])
        self.assertEqual("present_unverified", self.by_name(report, "viewer")["status"])
        self.assertNotIn(str(viewer), json.dumps(report))

    def test_unsupported_platform_or_architecture_is_clear(self):
        with patch.object(doctor.sys, "platform", "linux"):
            self.assertIn("platform", self.diagnose()["blockers"])
        with patch.object(doctor.struct, "calcsize", return_value=4):
            self.assertIn("platform", self.diagnose()["blockers"])

    def test_frozen_bundle_is_unsupported_without_misdiagnosing_absent_dist_info(self):
        with patch.object(doctor.sys, "frozen", True, create=True):
            report = self.diagnose()
        self.assertIn("source_mode", report["blockers"])
        self.metadata.assert_not_called()

    def test_no_application_imports_config_reads_network_or_processes(self):
        original = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name.startswith(("aipet", "PyQt5", "pynput")):
                raise AssertionError("application import forbidden")
            return original(name, *args, **kwargs)
        before = dict(os.environ)
        with patch("builtins.__import__", side_effect=guarded_import), patch("socket.create_connection", side_effect=AssertionError), patch("subprocess.Popen", side_effect=AssertionError):
            self.assertTrue(self.diagnose()["success"])
        self.assertEqual(before, dict(os.environ))
        self.assertFalse(self.base.exists())

    def test_report_creates_only_new_json_and_never_overwrites_or_makes_directories(self):
        target = self.root / "doctor-result.json"
        report = self.diagnose()
        self.assertEqual(0, doctor.write_report(report, target))
        original = target.read_bytes()
        with self.assertRaises(ValueError):
            doctor.write_report(report, target)
        self.assertEqual(original, target.read_bytes())
        for path in (self.root / "absent" / "report.json", self.root / "result.txt"):
            with self.subTest(name=path.name), self.assertRaises(ValueError):
                doctor.write_report(report, path)
        self.assertFalse((self.root / "absent").exists())

    def test_report_refuses_profile_material_and_engine_directories(self):
        for name in ("data", "profile", "assets", "voice-sources", "voices", "engine", "models"):
            directory = self.root / name
            directory.mkdir()
            with self.subTest(name=name), self.assertRaises(ValueError):
                doctor.write_report(self.diagnose(), directory / "new.json")
            self.assertFalse((directory / "new.json").exists())


class DoctorEntryTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(sys.modules, {"share_support": support}):
            self.entry = load_module("share_doctor_entry_target", "packaging/share_main.py")

    def test_branch_precedes_environment_setup_profile_lock_gui_or_viewer(self):
        before = dict(os.environ)
        with patch.dict(sys.modules, {"share_doctor": doctor}), patch.object(doctor, "diagnose", return_value={"success": True}) as diagnose, patch.object(doctor, "write_report", return_value=0), patch.object(self.entry, "configure_environment", side_effect=AssertionError), patch.object(self.entry, "_lock", side_effect=AssertionError), patch.object(self.entry, "_setup", side_effect=AssertionError), patch.object(self.entry, "launch_viewer", side_effect=AssertionError):
            self.assertEqual(0, self.entry.main(["--doctor"]))
        diagnose.assert_called_once()
        self.assertEqual(before, dict(os.environ))

    def test_real_diagnostic_entry_never_opens_existing_profile_or_application(self):
        with TemporaryDirectory(prefix="nene-doctor-entry-") as temporary:
            resources = Path(temporary) / "source"
            resources.mkdir()
            (resources / "prompt.nene.txt").write_text("synthetic persona", encoding="utf-8")
            base = Path(temporary) / "runtime"
            profile = base / "data/profile"
            profile.mkdir(parents=True)
            config = profile / "config.json"
            config.write_text('{"api_key":"synthetic-private-canary"}', encoding="utf-8")
            original = config.read_bytes()
            original_open = Path.open
            original_import = builtins.__import__
            def guarded_open(path, *args, **kwargs):
                if path.is_relative_to(profile):
                    raise AssertionError("diagnostic opened profile")
                return original_open(path, *args, **kwargs)
            def guarded_import(name, *args, **kwargs):
                if name.startswith(("aipet", "PyQt5", "pynput")):
                    raise AssertionError("diagnostic imported application")
                return original_import(name, *args, **kwargs)
            with patch.dict(sys.modules, {"share_doctor": doctor}), patch.object(self.entry, "portable_roots", return_value=(base, resources)), patch.object(doctor.metadata, "version", return_value="1.0"), patch.object(Path, "open", guarded_open), patch("builtins.__import__", side_effect=guarded_import), patch("socket.create_connection", side_effect=AssertionError), patch("subprocess.Popen", side_effect=AssertionError), patch.object(sys, "stdout", io.StringIO()) as stdout:
                self.assertEqual(2, self.entry.main(["--doctor"]))
            report = json.loads(stdout.getvalue())
            self.assertIn(doctor.MODEL_LABEL, report["blockers"])
            self.assertFalse(report["profile_read"])
            self.assertNotIn("synthetic-private-canary", stdout.getvalue())
            self.assertNotIn(str(resources), stdout.getvalue())
            self.assertEqual(original, config.read_bytes())

    def test_explicit_viewer_root_passed_without_discovery(self):
        with patch.dict(sys.modules, {"share_doctor": doctor}), patch.object(doctor, "diagnose", return_value={"success": True}) as diagnose, patch.object(doctor, "write_report", return_value=0), patch.object(self.entry, "discover_viewers", side_effect=AssertionError):
            self.assertEqual(0, self.entry.main(["--doctor", "--viewer-root", "viewer"]))
        self.assertEqual(Path("viewer"), diagnose.call_args.kwargs["viewer_root"])

    def test_unsupported_combinations_fail_before_config_or_profile_access(self):
        invalid = (["--viewer-root", "viewer"], ["--doctor", "--share-setup"],
                   ["--doctor", "--share-self-test"], ["--doctor", "--share-ui-smoke"],
                   ["--doctor", "--engine-root", "engine"], ["--doctor", "--log-viewer"])
        for arguments in invalid:
            with self.subTest(arguments=arguments), patch.object(self.entry, "configure_environment", side_effect=AssertionError), patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit) as failure:
                self.entry.main(arguments)
            self.assertEqual(2, failure.exception.code)

    def test_invalid_report_error_is_cli_only_and_does_not_echo_input(self):
        with patch.dict(sys.modules, {"share_doctor": doctor}), patch.object(doctor, "diagnose", return_value={"success": True}), patch.object(doctor, "write_report", side_effect=OSError("secret-path")), patch.object(sys, "stderr", io.StringIO()) as stderr:
            self.assertEqual(2, self.entry.main(["--doctor", "--report", "bad.json"]))
        self.assertNotIn("secret-path", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
