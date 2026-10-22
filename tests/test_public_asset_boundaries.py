"""Generated fixtures check missing-resource handling, never game fidelity."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from aipet.core import nene_persona


class PublicPersonaBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(self.temporary)
        self.catalog = self.root / "assets/persona/nene-scenes.json"
        self.catalog.parent.mkdir(parents=True)
        self.enterContext(patch.object(nene_persona, "PROJECT_ROOT", self.root))
        self.enterContext(patch.object(nene_persona, "SCENE_CATALOG", self.catalog))

    def fixture(self):
        source_root = self.root / "voice-sources"
        source_root.mkdir(exist_ok=True)
        screenshot = source_root / "synthetic.source.png"
        screenshot.write_bytes(b"synthetic non-rendered source-integrity fixture")
        metadata = source_root / "synthetic.source.json"
        metadata.write_text(json.dumps({"speaker": "Fixture", "subtitle_zh": "Synthetic fixture sentence", "status": "fixture"}), encoding="utf-8")
        source = {
            "screenshot": screenshot.relative_to(self.root).as_posix(),
            "screenshot_sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
            "metadata": metadata.relative_to(self.root).as_posix(),
            "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
            "speaker": "Fixture", "subtitle_zh": "Synthetic fixture sentence",
            "evidence_kind": "visually_checked_chinese_game_translation",
            "japanese_transcript_verified": False,
        }
        scene = {field: "Synthetic fixture; no canon claim" for field in (
            "id", "title", "chapter", "relationship_stage", "observation", "behavior_pattern",
            "limits", "antecedent", "actual_reaction", "subsequent_turn", "inference_boundary",
        )}
        scene.update(id="synthetic_fixture", keywords=["synthetic-topic"], sources=[source])
        self.catalog.write_text(json.dumps({"schema_version": 1, "scenes": [scene]}), encoding="utf-8")
        return source, screenshot, metadata

    def test_missing_catalog_cannot_claim_verified_evidence(self):
        report = nene_persona.verify_scene_sources()
        self.assertFalse(report["catalog_valid"])
        self.assertFalse(report["all_sources_match"])
        self.assertEqual("", nene_persona.scene_context("synthetic-topic"))

    def test_missing_source_disables_context_instead_of_fabricating_evidence(self):
        _, screenshot, _ = self.fixture()
        screenshot.unlink()  # Only this test's generated temporary fixture.
        report = nene_persona.verify_scene_sources()
        self.assertFalse(report["all_sources_match"])
        self.assertEqual("", nene_persona.scene_context("synthetic-topic"))

    def test_changed_bytes_disable_previously_valid_structural_fixture(self):
        _, screenshot, _ = self.fixture()
        self.assertTrue(nene_persona.verify_scene_sources()["all_sources_match"])
        self.assertIn("synthetic_fixture", nene_persona.scene_context("synthetic-topic"))
        screenshot.write_bytes(b"changed synthetic bytes")
        self.assertFalse(nene_persona.verify_scene_sources()["all_sources_match"])
        self.assertEqual("", nene_persona.scene_context("synthetic-topic"))

    def test_matching_hash_does_not_override_mismatched_speaker_or_transcript(self):
        source, _, metadata = self.fixture()
        metadata.write_text(json.dumps({"speaker": "Different fixture", "subtitle_zh": "Synthetic fixture sentence"}), encoding="utf-8")
        altered = copy.deepcopy(source)
        altered["metadata_sha256"] = hashlib.sha256(metadata.read_bytes()).hexdigest()
        self.assertFalse(nene_persona._source_is_valid(altered))

    def test_source_reference_cannot_escape_the_allowed_directory(self):
        source, _, _ = self.fixture()
        source["screenshot"] = "../outside-private-file.png"
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Outside file must never be opened")):
            self.assertFalse(nene_persona._source_is_valid(source))


if __name__ == "__main__":
    unittest.main()
