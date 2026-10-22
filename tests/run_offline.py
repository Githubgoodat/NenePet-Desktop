"""Run the public source suite with isolated state and no external services.

This tests software behavior using generated or mocked fixtures. It does not
validate the absent character model, game recordings, API service, or hardware.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _offline_audit(event, _arguments):
    if event in {
        "socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo",
        "socket.gethostbyname", "socket.gethostbyaddr", "subprocess.Popen",
        "os.system", "os.posix_spawn", "os.posix_spawnp",
    }:
        raise RuntimeError("Public offline suite forbids real network and subprocess activity: " + event)


def main() -> int:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    with TemporaryDirectory(prefix="nenepet-public-tests-") as temporary:
        scratch = Path(temporary)
        for name in list(os.environ):
            if name.startswith("AIPET_") or name in {
                "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY",
            }:
                os.environ.pop(name, None)
        os.environ.update({
            "QT_QPA_PLATFORM": "offscreen",
            "AIPET_DATA_DIR": str(scratch / "profile"),
            "AIPET_CONFIG_PATH": str(scratch / "profile" / "config.json"),
            "AIPET_CACHE_DIR": str(scratch / "cache"),
            "AIPET_MODEL_DIR": str(scratch / "models"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        })
        sys.addaudithook(_offline_audit)
        started = time.monotonic()
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        print(json.dumps({
            "scope": "public source offline regression suite",
            "tests_run": result.testsRun,
            "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped),
            "skipped_cases": [{"test": case.id(), "reason": reason} for case, reason in result.skipped],
            "seconds": round(time.monotonic() - started, 2),
            "external_services_used": False,
            "private_asset_acceptance": False,
        }, ensure_ascii=False))
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
