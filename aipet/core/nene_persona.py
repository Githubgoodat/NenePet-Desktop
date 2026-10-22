"""Local, source-linked Nene scene evidence and read-only acceptance fixtures.

This module never calls a model, starts TTS, or changes user state. Screenshots
support limited behavioral observations, not verified Japanese transcripts or
the desktop user's shared history. Acceptance cases are synthetic review aids;
passing structural tests does not certify character fidelity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENE_CATALOG = PROJECT_ROOT / "assets" / "persona" / "nene-scenes.json"
MAX_SCENES = 2
MAX_CONTEXT_CHARACTERS = 5_200


def _catalog() -> dict:
    try:
        data = json.loads(SCENE_CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return {}
    if not isinstance(data.get("scenes"), list):
        return {}
    required_text = ("id", "title", "chapter", "relationship_stage", "observation", "behavior_pattern", "limits",
                     "antecedent", "actual_reaction", "subsequent_turn", "inference_boundary")
    # A damaged or partially edited entry cannot break the conversation path.
    data["scenes"] = [
        scene for scene in data["scenes"]
        if isinstance(scene, dict)
        and all(isinstance(scene.get(field), str) and scene[field].strip()
                for field in required_text)
        and isinstance(scene.get("keywords"), list)
        and isinstance(scene.get("sources"), list)
    ]
    return data


def _source_is_valid(source: dict) -> bool:
    """Use evidence only while the visually reviewed source bytes still match."""
    if not isinstance(source, dict):
        return False
    for kind in ("screenshot", "metadata"):
        relative = source.get(kind)
        expected = source.get(f"{kind}_sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            return False
        try:
            path = (PROJECT_ROOT / relative).resolve()
            if not path.is_relative_to((PROJECT_ROOT / "voice-sources").resolve()):
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
        except (OSError, ValueError):
            return False
    try:
        metadata = json.loads((PROJECT_ROOT / source["metadata"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(metadata, dict)
            and metadata.get("subtitle_zh") == source.get("subtitle_zh")
            and metadata.get("speaker", metadata.get("speaker_label")) == source.get("speaker")
            and metadata.get("status") not in {"excluded_scene_context", "aborted_before_replay"}
            and source.get("evidence_kind") == "visually_checked_chinese_game_translation"
            and source.get("japanese_transcript_verified") is False)


def verify_scene_sources() -> dict:
    """Report source integrity without loading audio or contacting services."""
    catalog = _catalog()
    statuses = []
    for scene in catalog.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        sources = scene.get("sources", [])
        statuses.append({
            "id": scene.get("id"),
            "source_count": len(sources) if isinstance(sources, list) else 0,
            "verified": bool(sources) and isinstance(sources, list)
            and all(_source_is_valid(source) for source in sources),
        })
    return {
        "catalog_valid": bool(catalog),
        "all_sources_match": bool(statuses) and all(item["verified"] for item in statuses),
        "scenes": statuses,
        "unique_screenshots": len({source.get("screenshot")
            for scene in catalog.get("scenes", []) for source in scene.get("sources", [])}),
        "japanese_transcript_verified": False,
        "excluded_sources": catalog.get("excluded_sources", []),
        "limits": catalog.get("global_limits", []),
    }


def _keyword_matches(query: str, keyword: str) -> bool:
    if not keyword:
        return False
    if keyword.isascii():
        # Avoid matching e.g. "exam" inside "example".
        return re.search(r"(?<![a-z0-9_])" + re.escape(keyword) + r"(?![a-z0-9_])", query) is not None
    return keyword in query


def scene_context(query: str, *, limit: int = MAX_SCENES) -> str:
    """Return at most two relevant scenes, or empty text when none is supported.

    The caller should use this only for the Nene profile and combine it with
    ongoing state as ``character_context``. The query is never copied into the
    evidence block, so retrieved screen/user text cannot become instructions.
    """
    if not isinstance(query, str) or not query.strip() or limit <= 0:
        return ""
    query = query[:12_000].casefold()
    catalog = _catalog()
    ranked = []
    for order, scene in enumerate(catalog.get("scenes", [])):
        if not isinstance(scene, dict):
            continue
        keywords = scene.get("keywords", [])
        if not isinstance(keywords, list):
            continue
        matched = [keyword for keyword in keywords if isinstance(keyword, str)
                   and _keyword_matches(query, keyword.casefold())]
        sources = scene.get("sources", [])
        if not matched or not isinstance(sources, list) or not sources:
            continue
        if not all(_source_is_valid(source) for source in sources):
            continue
        score = sum(min(len(keyword), 8) for keyword in matched)
        ranked.append((-score, order, scene))
    ranked.sort(key=lambda item: (item[0], item[1]))
    selected = ranked[:min(int(limit), MAX_SCENES)]
    if not selected:
        return ""
    lines = [
        "相关原作场景依据（不是当前用户的共同回忆，也不是要求复述的台词）：",
        "这些是普通校园截图，恋人阶段未核实；只作有限行为线索。按当前真实关系和事件延伸，不强行套用。",
        "下列中文是截图可见游戏译文，日语原文未核实；不把未核实 ASR 当原文，也不照译成当前回复。",
    ]
    for _, _, scene in selected:
        block = [
            f"场景 {scene['id']}：{scene['title']}（Chapter {scene['chapter']}）。",
            f"前因：{scene['antecedent']}",
            f"实际反应：{scene['actual_reaction']}",
            f"后续转折：{scene['subsequent_turn']}",
            f"阶段：{scene['relationship_stage']}；推断边界：{scene['inference_boundary']}",
            f"可见事实：{scene['observation']}",
            f"有限行为线索：{scene['behavior_pattern']}",
            f"局限：{scene['limits']}",
        ]
        for source in scene["sources"]:
            block.append(
                f"来源：{source['screenshot']}；记录：{source['metadata']}；"
                f"可见译文【{source['speaker']}】：{source['subtitle_zh']}"
            )
        if len("\n".join(lines + block)) > MAX_CONTEXT_CHARACTERS:
            break
        lines.extend(block)
    return "\n".join(lines)


def acceptance_cases() -> list[dict]:
    """Synthetic scene rubrics for explicit review; no automatic model grading."""
    cases = _catalog().get("acceptance_cases", [])
    if not isinstance(cases, list):
        return []
    return [case for case in cases if isinstance(case, dict)
            and case.get("kind") == "synthetic_dialogue_not_game_quote"]


def consistency_cases() -> list[dict]:
    """Fixed multi-turn fixtures, never a record of actual user interaction.

    Each paraphrase is a separate run. Generated replies are threaded only in
    that run's in-memory list; there is no production memory/store dependency.
    Rubrics are human review criteria, not an automatic personality score.
    """
    cases = _catalog().get("consistency_cases", [])
    if not isinstance(cases, list):
        return []
    result = []
    for case in cases:
        if (not isinstance(case, dict) or case.get("kind") != "synthetic_multiturn_not_game_quote"
                or not isinstance(case.get("id"), str) or not case["id"].strip()):
            continue
        history, variants = case.get("history"), case.get("variants")
        if not isinstance(history, list) or not isinstance(variants, list) or not variants:
            continue
        if any(not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}
               or not isinstance(item.get("content"), str) for item in history):
            continue
        if any(not isinstance(variant, dict) or not isinstance(variant.get("id"), str)
               or not isinstance(variant.get("turns"), list) or not variant["turns"]
               or any(not isinstance(turn, str) or not turn.strip() for turn in variant["turns"])
               for variant in variants):
            continue
        if not isinstance(case.get("checks"), list) or len(case["checks"]) < 3:
            continue
        result.append(case)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--query", help="Print relevant source-linked scene evidence.")
    group.add_argument("--cases", action="store_true", help="Print synthetic review cases; no model calls.")
    group.add_argument("--multiturn-cases", action="store_true", help="Print synthetic multi-turn review fixtures.")
    group.add_argument("--verify", action="store_true", help="Check local reviewed-source hashes and metadata.")
    args = parser.parse_args()
    if args.multiturn_cases:
        print(json.dumps(consistency_cases(), ensure_ascii=False, indent=2))
    elif args.cases:
        print(json.dumps(acceptance_cases(), ensure_ascii=False, indent=2))
    elif args.query is not None:
        print(scene_context(args.query))
    else:
        report = verify_scene_sources()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["all_sources_match"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
