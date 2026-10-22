"""Collect local distribution notices without importing the desktop application.

Run with the Python used to build the Windows release, for example::

    python packaging/collect_share_licenses.py \
        --output release-notices --site-packages build/pyinstaller-target

This is an inventory of locally available evidence, not a declaration that a
binary release has satisfied every licence obligation. Missing texts remain
explicitly missing. No user configuration, dialogue, cache or log is inspected.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import struct
import sys
from typing import Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CORE_DISTRIBUTIONS = (
    "PyQt5", "PyQt5-Qt5", "PyQt5-sip", "numpy", "opencv-python", "pydantic",
    "paramiko", "requests", "pynput", "Pillow", "sounddevice", "cryptography",
    "cffi", "pypdf",
)
BUILD_DISTRIBUTIONS = ("pyinstaller", "pyinstaller-hooks-contrib")
MAX_LICENCE_BYTES = 16 * 1024 * 1024
LICENCE_NAME = re.compile(
    r"^(?:licen[cs]e|copying|copyright|notice|authors|ofl|third[-_. ]?party)"
    r"(?:$|[-_. ])", re.IGNORECASE,
)
FONT_LICENSE_NAMES = (
    "OFL.txt", "OFL-1.1.txt", "LICENSE_FONT.txt", "FONT-LICENSE.txt",
    "fonts/OFL.txt", "fonts/LICENSE", "fonts/LICENSE.txt",
)
FONT_APACHE_URL = "https://www.apache.org/licenses/LICENSE-2.0.txt"
PYQT5_GPL_SOURCES = (
    ("https://www.gnu.org/licenses/gpl-3.0.txt",
     "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"),
    ("https://raw.githubusercontent.com/spdx/license-list-data/main/text/GPL-3.0-only.txt",
     "fb981668c18a279e285fc4d83fba1e836cc84dd4daa73c9697d3cfd2d8aca6e0"),
)
NO_LICENCE_TEXT = "No standalone licence/notice text found; metadata declarations are not a substitute."
EXTRA_PACKAGE_NOTICES = {
    "opencv-python": ("cv2/LICENSE.txt", "cv2/LICENSE-3RD-PARTY.txt"),
    "sounddevice": (
        "_sounddevice_data/LICENSE", "_sounddevice_data/LICENSE.txt",
        "_sounddevice_data/portaudio-binaries/LICENSE.txt",
        "_sounddevice_data/portaudio-binaries/LICENSE",
    ),
}


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _relative(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def copy_evidence(source: Path, destination: Path) -> dict:
    """Copy an actual bounded file, preserving bytes and recording its digest."""
    if not source.is_file():
        raise ValueError("licence source is not a file")
    if source.stat().st_size > MAX_LICENCE_BYTES:
        raise ValueError("licence file exceeds the collector's 16 MiB limit")
    content = source.read_bytes()
    if not content:
        raise ValueError("licence file is empty")
    if len(content) > MAX_LICENCE_BYTES:
        raise ValueError("licence file changed beyond the collector's limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() != content:
        raise ValueError("refusing to overwrite different existing licence evidence")
    destination.write_bytes(content)
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def discover_distributions(paths: Iterable[Path]) -> tuple[dict, list[dict]]:
    """Prefer explicit private build targets, then the selected Python and vendor."""
    selected = {}
    duplicates = []
    for index, path in enumerate(paths):
        if not path.is_dir():
            continue
        for dist in metadata.distributions(path=[str(path)]):
            name = dist.metadata.get("Name", "")
            if not name:
                continue
            key = canonical_name(name)
            item = (dist, path.resolve(), f"package-source-{index + 1}")
            if key in selected:
                duplicates.append({
                    "name": name, "version": dist.version,
                    "ignored_source": item[2],
                    "preferred_source": selected[key][2],
                })
            else:
                selected[key] = item
    return selected, duplicates


def _dependency_requests(dist, warnings: list[str]) -> list[str]:
    """Follow installed non-extra requirements for the actual build interpreter."""
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError:
        warnings.append(
            "packaging is unavailable: dependency markers were not evaluated; "
            "only directly requested components are collected."
        )
        return []
    result = []
    for raw in dist.requires or []:
        try:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                result.append(requirement.name)
        except (InvalidRequirement, ValueError):
            warnings.append(f"Could not evaluate one Requires-Dist entry for {dist.metadata['Name']}.")
    return result


def select_components(available: dict, extra: list[str], warnings: list[str]) -> list[dict]:
    pending = deque((name, "runtime", "explicit") for name in (*CORE_DISTRIBUTIONS, *extra))
    pending.extend((name, "build", "explicit") for name in BUILD_DISTRIBUTIONS)
    selected = {}
    while pending:
        name, scope, requested_by = pending.popleft()
        key = canonical_name(name)
        if key in selected:
            if requested_by not in selected[key]["requested_by"]:
                selected[key]["requested_by"].append(requested_by)
            continue
        item = {"key": key, "name": name, "scope": scope, "requested_by": [requested_by]}
        selected[key] = item
        if key not in available:
            item["status"] = "distribution_not_found"
            continue
        dist, _, source_label = available[key]
        item.update({
            "name": dist.metadata.get("Name", name), "version": dist.version,
            "source": source_label, "status": "found",
        })
        for dependency in _dependency_requests(dist, warnings):
            pending.append((dependency, scope, item["name"]))
    return sorted(selected.values(), key=lambda item: (item["scope"] != "runtime", item["key"]))


def collect_distribution(item: dict, available: dict, output: Path) -> dict:
    result = dict(item)
    result["files"] = []
    result["issues"] = []
    if item["status"] != "found":
        result["issues"].append("Distribution metadata and licence text were not found locally.")
        return result
    dist, site_root, _ = available[item["key"]]
    info = dist.metadata
    # Only selected public package headers, never full metadata descriptions,
    # application settings, installation direct_url.json or RECORD contents.
    extracted = {
        key: info.get_all(key, []) for key in (
            "Name", "Version", "Summary", "Home-page", "Project-URL", "License",
            "License-Expression", "License-File", "Classifier", "Requires-Dist",
        )
    }
    declared = info.get("License-Expression") or info.get("License")
    if not declared:
        declared = "; ".join(value for value in info.get_all("Classifier", [])
                             if value.startswith("License ::"))
    result["declared_license"] = declared or "Not declared in available metadata"
    component_dir = output / "licenses" / item["key"]
    component_dir.mkdir(parents=True, exist_ok=True)
    (component_dir / "METADATA-EXTRACT.json").write_text(
        json.dumps(extracted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    candidates = {}
    declared_files = set(info.get_all("License-File", []))
    for package_file in dist.files or []:
        parts = package_file.parts
        # Keep only this wheel's distribution notice tree, not arbitrary files
        # advertised in RECORD (which may legitimately include ../Scripts).
        if (len(parts) < 2 or not parts[0].lower().endswith(".dist-info")
                or any(part in {".", ".."} for part in parts)):
            continue
        inside_info = "/".join(parts[1:])
        if not ("licenses" in [part.lower() for part in parts[1:-1]]
                or LICENCE_NAME.match(package_file.name)
                or inside_info in declared_files
                or inside_info.removeprefix("licenses/") in declared_files):
            continue
        candidate = Path(dist.locate_file(package_file))
        if _relative(candidate, site_root) is not None:
            candidates[candidate.resolve()] = "dist-info/" + inside_info
    # Some installations omit notice entries in RECORD. Inspect only the exact
    # metadata directory, never recursively scan package roots or user folders.
    metadata_dir = getattr(dist, "_path", None)
    if metadata_dir is not None:
        metadata_dir = Path(metadata_dir)
        if metadata_dir.is_dir() and _relative(metadata_dir, site_root) is not None:
            for candidate in metadata_dir.rglob("*"):
                if not candidate.is_file():
                    continue
                relative = candidate.relative_to(metadata_dir)
                if ("licenses" in [part.lower() for part in relative.parts[:-1]]
                        or LICENCE_NAME.match(candidate.name)
                        or relative.as_posix() in declared_files):
                    if _relative(candidate, metadata_dir) is not None:
                        candidates[candidate.resolve()] = "dist-info/" + relative.as_posix()
    for relative in EXTRA_PACKAGE_NOTICES.get(item["key"], ()):
        candidate = site_root / relative
        if candidate.is_file() and _relative(candidate, site_root) is not None:
            candidates[candidate.resolve()] = "package/" + relative
    for candidate, relative in sorted(candidates.items(), key=lambda pair: pair[1]):
        destination = component_dir / relative
        try:
            evidence = copy_evidence(candidate, destination)
            result["files"].append({
                "path": destination.relative_to(output).as_posix(), **evidence,
            })
        except (OSError, ValueError) as exc:
            result["issues"].append(f"Could not collect {relative}: {type(exc).__name__}.")
    if not result["files"]:
        result["issues"].append(NO_LICENCE_TEXT)
    if item["key"] == "pyqt5" and not result["files"]:
        result = supplement_pyqt5_gpl(result, output)
    return result


def supplement_pyqt5_gpl(item: dict, output: Path) -> dict:
    """Supply actual GPL 3 text only for PyQt5's explicit GPL-v3 declaration.

    GNU and SPDX are official licence-text sources. This does not decide
    between GPL-only/or-later/commercial licensing for a user or audit whether
    every distribution condition has been satisfied.
    """
    result = dict(item)
    result["files"] = list(item.get("files", []))
    result["issues"] = list(item.get("issues", []))
    if (item.get("key") != "pyqt5"
            or not re.search(r"\bGPL\s*v?\s*3\b", item.get("declared_license", ""), re.I)):
        return result
    content = None
    source_url = ""
    for url, expected_hash in PYQT5_GPL_SOURCES:
        try:
            request = Request(url, headers={"User-Agent": "NenePet-License-Collector/1.0"})
            with urlopen(request, timeout=15) as response:
                if urlparse(response.url).hostname != urlparse(url).hostname:
                    raise ValueError("licence request redirected to an unexpected host")
                candidate = response.read(256 * 1024 + 1)
            if hashlib.sha256(candidate).hexdigest() != expected_hash:
                raise ValueError("official licence bytes differ from the recorded source hash")
            content, source_url = candidate, url
            break
        except (OSError, ValueError):
            continue
    try:
        if content is None:
            raise ValueError("official sources unavailable or bytes changed")
        if (len(content) > 256 * 1024
                or b"GNU GENERAL PUBLIC LICENSE" not in content[:150]
                or b"Version 3, 29 June 2007" not in content[:200]
                or b"END OF TERMS AND CONDITIONS" not in content):
            raise ValueError("response is not the expected complete GPL version 3 text")
        destination = output / "licenses" / "pyqt5" / "GPL-3.0.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != content:
            raise ValueError("existing GPL evidence differs from downloaded text")
        destination.write_bytes(content)
        record = {
            "path": destination.relative_to(output).as_posix(), "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(), "source_url": source_url,
            "note": "Official GNU/SPDX GPL version 3 text supplement; original wheel metadata unchanged.",
        }
        result["files"] = [entry for entry in result["files"] if entry["path"] != record["path"]]
        result["files"].append(record)
        result["issues"] = [issue for issue in result["issues"] if issue != NO_LICENCE_TEXT]
    except (OSError, ValueError):
        issue = "Could not obtain PyQt5's missing full GPL v3 text from the recorded official GNU/SPDX sources."
        if issue not in result["issues"]:
            result["issues"].append(issue)
    return result


def collect_named_files(output: Path, component: str, paths: Iterable[Path]) -> list[dict]:
    evidence = []
    for path in paths:
        if not path.is_file():
            continue
        destination = output / "licenses" / component / path.name
        try:
            details = copy_evidence(path, destination)
        except (OSError, ValueError):
            continue
        if not any(item["sha256"] == details["sha256"] for item in evidence):
            evidence.append({"path": destination.relative_to(output).as_posix(), **details})
    return evidence


def read_font_names(path: Path) -> dict:
    """Read bounded OpenType name records as evidence, without loading the font."""
    if path.stat().st_size > MAX_LICENCE_BYTES:
        raise ValueError("font exceeds the collector's input limit")
    raw = path.read_bytes()
    if len(raw) < 12:
        raise ValueError("incomplete OpenType header")
    count = struct.unpack_from(">H", raw, 4)[0]
    if 12 + count * 16 > len(raw):
        raise ValueError("incomplete OpenType table directory")
    table = None
    for index in range(count):
        tag, _, offset, length = struct.unpack_from(">4sIII", raw, 12 + index * 16)
        if tag == b"name":
            if offset + length > len(raw) or length < 6:
                raise ValueError("invalid OpenType name table")
            table = raw[offset:offset + length]
            break
    if table is None:
        raise ValueError("missing OpenType name table")
    _, records, string_offset = struct.unpack_from(">HHH", table, 0)
    if 6 + records * 12 > len(table):
        raise ValueError("incomplete OpenType name records")
    names = {}
    for index in range(records):
        platform_id, _, language, name_id, length, offset = struct.unpack_from(
            ">HHHHHH", table, 6 + index * 12,
        )
        if name_id not in {0, 1, 2, 5, 13, 14}:
            continue
        start = string_offset + offset
        if start + length > len(table):
            raise ValueError("invalid OpenType name string")
        encoding = "utf-16-be" if platform_id in {0, 3} else "mac_roman"
        value = table[start:start + length].decode(encoding, errors="replace").strip()
        if value:
            record = {"text": value, "platform": platform_id, "language": language}
            names.setdefault(str(name_id), [])
            if not any(item["text"] == value for item in names[str(name_id)]):
                names[str(name_id)].append(record)
    return {"filename": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "names": names}


def collect_font(output: Path, supplied: list[Path]) -> dict:
    """Use this exact font's embedded terms rather than current-family assumptions."""
    result = {
        "name": "思源黑体Bold.otf font", "description":
        "No licence is inferred from the font filename or a newer release of the same family.",
        "files": [], "issues": [],
    }
    try:
        evidence = read_font_names(REPOSITORY_ROOT / "思源黑体Bold.otf")
    except (OSError, ValueError, struct.error):
        result["issues"].append("Could not inspect the shipped font's embedded OpenType licence/provenance records.")
        evidence = {"names": {}}
    if "sha256" in evidence:
        embedded = output / "licenses" / "font" / "font-name-table.json"
        embedded.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        embedded.write_bytes(content)
        result["files"].append({
            "path": embedded.relative_to(output).as_posix(), "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    values = evidence["names"]
    joined = lambda name_id: " / ".join(item["text"] for item in values.get(str(name_id), []))
    license_text = joined(13)
    license_url = joined(14)
    apache_embedded = bool(re.search(r"Apache.{0,60}(?:Version|License|V)[\s,]*2\.0", license_text, re.I)
                           or ("apache.org/licenses/LICENSE-2.0" in license_text
                               or "apache.org/licenses/LICENSE-2.0" in license_url))
    result["description"] = (
        f"Embedded name-table evidence for this exact font: family {joined(1) or 'unavailable'}; "
        f"style {joined(2) or 'unavailable'}; version {joined(5) or 'unavailable'}; "
        f"copyright {joined(0) or 'unavailable'}. Licence name ID 13: {license_text or 'unavailable'}. "
        f"Licence URL name ID 14: {license_url or 'unavailable'}. "
        "These embedded records, not the licence of a current family release, determine the local evidence."
    )
    local_candidates = [*(REPOSITORY_ROOT / name for name in FONT_LICENSE_NAMES), *supplied]
    if apache_embedded:
        # Do not attach a current OFL release's terms to the older Apache font.
        matching = []
        for candidate in local_candidates:
            if not candidate.is_file() or candidate.stat().st_size > MAX_LICENCE_BYTES:
                continue
            sample = candidate.read_text(encoding="utf-8-sig", errors="replace")
            if "Apache License" in sample and "Version 2.0" in sample:
                matching.append(candidate)
        local_candidates = matching
    local_files = collect_named_files(output, "font", local_candidates)
    result["files"].extend(local_files)
    if apache_embedded and not local_files:
        try:
            with urlopen(FONT_APACHE_URL, timeout=15) as response:
                if urlparse(response.url).hostname != "www.apache.org":
                    raise ValueError("official licence request redirected to an unexpected host")
                content = response.read(256 * 1024 + 1)
            if (len(content) > 256 * 1024 or b"Apache License" not in content[:100]
                    or b"Version 2.0" not in content[:200]):
                raise ValueError("response is not the expected bounded Apache 2.0 licence text")
            destination = output / "licenses" / "font" / "LICENSE-Apache-2.0.txt"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            result["files"].append({
                "path": destination.relative_to(output).as_posix(), "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "source_url": FONT_APACHE_URL,
            })
            result["description"] += f" Full Apache 2.0 text obtained from the official source: {FONT_APACHE_URL}."
        except (OSError, ValueError):
            result["issues"].append(
                "Embedded font terms identify Apache 2.0, but its full text could not be downloaded from "
                + FONT_APACHE_URL + "; supply the verified text with --font-license."
            )
    elif not local_files:
        result["issues"].append(
            "No matching local font licence text was found; verify its embedded terms and supply the actual licence."
        )
    return result


def write_notices(output: Path, inventory: dict) -> None:
    lines = [
        "# Third-party notices / 第三方许可清单", "",
        "This document inventories locally available package metadata and actual licence files. "
        "It does not grant rights to character assets, voices, Steam products, fonts, or model weights; "
        "it does not certify that all binary redistribution obligations have been met.", "",
        "本清单记录实际找到的许可和依赖。缺失项明确列出，未以许可证名称、推测或其他组件的文本代替。"
        "字体、人物模型与语音素材的来源及使用范围须在分发说明中另行列明。", "",
        f"Collected (UTC): {inventory['collected_at_utc']}",
        f"Build Python: {inventory['python_version']} ({inventory['platform']})", "",
        "## Application, Python and font", "",
    ]
    for item in inventory["resources"]:
        lines.extend([f"### {item['name']}", "", item["description"], ""])
        for file in item["files"]:
            lines.append(f"- [{file['path']}]({file['path']}) — SHA256 `{file['sha256']}`")
        for issue in item["issues"]:
            lines.append(f"- **MISSING / 缺失：** {issue}")
        lines.append("")
    lines.extend([
        "## Package inventory / 依赖清单", "",
        "Runtime rows are the requested core packages and their installed, active, non-extra metadata dependencies. "
        "Build rows describe the freezer toolchain; their presence does not imply all tool modules ship in the executable. "
        "Dynamic imports and native DLL dependencies still require the final build manifest to be checked.", "",
        "| Component | Version | Scope | Declared licence (metadata) | Collected text files |",
        "|---|---|---|---|---|",
    ])
    for item in inventory["components"]:
        texts = ", ".join(f"[{Path(file['path']).name}]({file['path']})" for file in item["files"])
        if not texts:
            texts = "**MISSING**"
        license_label = item.get("declared_license", "Unavailable")
        if len(license_label) > 140:
            license_label = license_label[:137] + "... (see metadata extract)"
        lines.append("| " + " | ".join((
            _markdown(item["name"]), _markdown(item.get("version", "NOT FOUND")),
            item["scope"], _markdown(license_label), texts,
        )) + " |")
    supplements = [(item, file) for item in inventory["components"]
                   for file in item["files"] if file.get("source_url")]
    if supplements:
        lines.extend(["", "### Official text supplements / 官方许可文本补充", ""])
        for item, file in supplements:
            lines.append(
                f"- {_markdown(item['name'])}: [{file['path']}]({file['path']}); "
                f"source [{file['source_url']}]({file['source_url']}); SHA256 `{file['sha256']}`. "
                "This supplies an actual licence text; it does not replace the component's original "
                "metadata, establish a commercial licence, or certify redistribution compliance."
            )
    lines.extend(["", "## Missing material and review boundaries / 缺失与核对范围", ""])
    for item in inventory["components"]:
        for issue in item["issues"]:
            lines.append(f"- **{_markdown(item['name'])}:** {issue}")
    for warning in inventory["warnings"]:
        lines.append(f"- {warning}")
    lines.extend([
        "- Native libraries bundled in Qt, OpenCV, NumPy, Pillow, cryptography, and PortAudio "
        "may have their own notices. Existing package notice bundles are included where found; "
        "this metadata inventory cannot prove coverage of every DLL in the final executable.",
        "- sounddevice's own MIT licence is not a replacement for a PortAudio or ASIO binary notice. "
        "If those binaries ship, inspect their exact source/build and licence material separately.",
        "- PyInstaller's bootloader exception and any component-specific conditions must be read in "
        "the collected text; do not infer an exception merely from a package's licence label.",
        "- No personal configuration, conversation, cache, or logs were used as collection sources.", "",
        "Hashes, dependency reasons and selected metadata headers are in "
        "[licenses/dependency-inventory.json](licenses/dependency-inventory.json).", "",
    ])
    (output / "THIRD-PARTY-NOTICES.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Empty output directory; never the source tree")
    parser.add_argument("--python-root", type=Path, default=Path(sys.executable).resolve().parent)
    parser.add_argument("--site-packages", type=Path, action="append", default=[],
                        help="Additional private build targets, preferred before the selected Python packages")
    parser.add_argument("--include-distribution", action="append", default=[], help="Additional wheel/distribution name")
    parser.add_argument("--font-license", type=Path, action="append", default=[], help="Actual, independently verified font licence file")
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if output == REPOSITORY_ROOT or output == REPOSITORY_ROOT / "licenses":
        parser.error("Choose a separate output directory, not the application source or licence tree.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("The output directory must be empty to avoid overwriting existing evidence.")
    output.mkdir(parents=True, exist_ok=True)
    python_root = args.python_root.expanduser().resolve()
    paths = [*(path.expanduser().resolve() for path in args.site_packages),
             python_root / "Lib" / "site-packages", REPOSITORY_ROOT / "vendor" / "reading"]
    available, duplicates = discover_distributions(dict.fromkeys(paths))
    warnings = []
    items = select_components(available, args.include_distribution, warnings)
    components = [collect_distribution(item, available, output) for item in items]
    resources = []
    app_files = collect_named_files(output, "application", [REPOSITORY_ROOT / "LICENSE"])
    resources.append({
        "name": "NenePet application / upstream AIpet", "description":
        "The repository's own licence text is copied verbatim. Corresponding application source "
        "and changes must be provided separately by the release assembler.",
        "files": app_files, "issues": [] if app_files else ["Repository LICENSE was not found or readable."],
    })
    python_files = collect_named_files(output, "python", [
        python_root / "LICENSE_PYTHON.txt", python_root / "LICENSE.txt",
        python_root / "LICENSE", python_root / "Doc" / "license.txt",
    ])
    resources.append({
        "name": "Python", "description":
        "Licence files from the selected Python installation. The reported Python version is "
        "the interpreter executing this collector; run it with the actual build interpreter.",
        "files": python_files, "issues": [] if python_files else ["Python licence text was not found locally."],
    })
    resources.append(collect_font(output, args.font_license))
    if duplicates:
        warnings.append("Duplicate distribution metadata was found. The first explicit build target or package source wins; "
                        "the inventory records ignored versions for review against the freezer manifest.")
    if not any(item["key"] == "pyinstaller" and item.get("version") == "6.22.3" for item in components):
        warnings.append("The intended PyInstaller 6.22.3 distribution was not found at that exact version. "
                        "Pass its private installation directory with --site-packages before release.")
    inventory = {
        "schema_version": 1, "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(), "platform": sys.platform,
        "resources": resources, "components": components,
        "duplicates": duplicates, "warnings": sorted(set(warnings)),
    }
    licenses = output / "licenses"
    licenses.mkdir(exist_ok=True)
    (licenses / "dependency-inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    write_notices(output, inventory)
    missing = sum(bool(item["issues"]) for item in [*resources, *components])
    print(json.dumps({
        "components": len(components), "components_or_resources_with_issues": missing,
        "warnings": len(inventory["warnings"]), "notice": "THIRD-PARTY-NOTICES.md",
    }, ensure_ascii=False))
    # Missing evidence remains reportable output, not an invented licence or a
    # misleading successful compliance assertion. The assembler reviews issues.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
