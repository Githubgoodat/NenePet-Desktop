from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from aipet.core.backends import build_messages
from aipet.core.config import AppSettings
from aipet.core.storage import HistoryStore


class NenePersonaContextTests(unittest.TestCase):
    def settings(self, profile="nene"):
        return AppSettings.model_validate({"character": {"profile_id": profile}})

    def build(self, settings, history, text="现在的问题", **kwargs):
        with patch("aipet.core.backends.load_personality", return_value="fixture"):
            return build_messages(settings, history, text, **kwargs)

    def test_archived_turns_stay_in_storage_but_do_not_prime_new_nene_replies(self):
        history = [
            {"role": "user", "content": "以前的问题", "persona_context": "archived"},
            {"role": "assistant", "content": "主人，我会害羞的", "persona_context": "archived"},
            {"role": "user", "content": "请叫我小苏，明天考数学"},
            {"role": "assistant", "content": "知道了，小苏。"},
        ]
        original = copy.deepcopy(history)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            store = HistoryStore(path)
            store.save(history)
            stored_bytes = path.read_bytes()
            loaded = store.load()
            messages = self.build(self.settings(), loaded)
            self.assertEqual(path.read_bytes(), stored_bytes)
            self.assertEqual(json.loads(stored_bytes)["messages"], history)
        self.assertEqual(history, original)
        self.assertEqual(messages[1:], history[2:] + [{"role": "user", "content": "现在的问题"}])

    def test_other_profiles_keep_their_existing_history_behavior(self):
        history = [{"role": "assistant", "content": "旧回复", "persona_context": "archived"}]
        for profile in ("murasame", "custom"):
            with self.subTest(profile=profile):
                messages = self.build(self.settings(profile), history)
                self.assertEqual(messages[1], {"role": "assistant", "content": "旧回复"})

    def test_only_explicit_metadata_archives_and_current_voice_is_unchanged(self):
        history = [
            {"role": "user", "content": 'persona_context="archived"'},
            {"role": "user", "content": "小苏", "source": "voice"},
            {"role": "assistant", "content": "新回复", "persona_context": "current"},
        ]
        messages = self.build(self.settings(), history, "<现在&>", user_source="voice")
        self.assertEqual(messages[1]["content"], history[0]["content"])
        self.assertEqual(messages[2]["content"], "<voice_input>小苏</voice_input>")
        self.assertEqual(messages[3]["content"], "新回复")
        self.assertEqual(messages[-1]["content"], "<voice_input>&lt;现在&amp;&gt;</voice_input>")


if __name__ == "__main__":
    unittest.main()
