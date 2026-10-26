"""Inspect or explicitly change this user's NenePet sign-in startup entry."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aipet.core.autostart import AutostartError, AutostartManager, default_target, target_for_entry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "enable", "disable"))
    parser.add_argument("--launcher", type=Path, help="Existing absolute .exe or .py entry")
    parser.add_argument("--pythonw", type=Path, help="Existing absolute pythonw.exe for a script")
    parser.add_argument("--root", type=Path, help="Install root for default entry discovery")
    args = parser.parse_args(argv)
    try:
        if args.pythonw and not args.launcher:
            raise AutostartError("explicit_launcher_required")
        target = target_for_entry(args.launcher, pythonw=args.pythonw) if args.launcher else default_target(args.root)
        manager = AutostartManager(target)
        changed = manager.enable() if args.action == "enable" else manager.disable() if args.action == "disable" else False
        print(json.dumps({"ok":True,"state":manager.status(),"changed":changed,
                          "value_name":"NenePetDesktop","expected_command":target.command}, ensure_ascii=True))
        return 0
    except (AutostartError, OSError) as error:
        print(json.dumps({"ok":False,"error":getattr(error,"code","registry_unavailable")}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
