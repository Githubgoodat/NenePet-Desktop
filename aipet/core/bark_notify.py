"""Opt-in text notices to Bark's fixed public HTTPS endpoint.

Protocol: https://github.com/Finb/Bark/blob/master/docs/tutorial.md
Acceptance: https://github.com/Finb/bark-server/blob/master/router.go
Server code 200 is acceptance, not proof that an iPhone displayed the notice.
No URL actions, clipboard actions, critical alerts, retries or incoming commands.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import time

import requests


__all__ = ["BarkConfig", "BarkNotifyError", "BarkSendResult", "BarkClient",
           "normalize_device_key", "load_config", "save_config", "logout"]

ENDPOINT = "https://api.day.app/push"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 64 * 1024
_CONFIG_FORMAT = "nenepet-bark-notify-v1"
_KEY = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_KEY_URL = re.compile(r"https://api\.day\.app/([A-Za-z0-9_-]{1,256})/?\Z")
_ERROR_CODES = frozenset({"key_missing", "invalid_key", "invalid_notice", "redirect_blocked",
                          "http_error", "network_error", "response_too_large", "bad_response",
                          "api_rejected", "acceptance_unknown", "config_error"})


class BarkNotifyError(Exception):
    """Only fixed codes are exposed; never keep raw exceptions or responses."""
    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "bad_response"
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class BarkConfig:
    enabled: bool = False
    device_key: str = ""


@dataclass(frozen=True)
class BarkSendResult:
    accepted: bool


def normalize_device_key(value: str) -> str:
    """Accept an ordinary key or its exact api.day.app share URL.

    ASCII letters, digits, underscores and hyphens, length 1..256, are a
    conservative adapter constraint covering ordinary Bark keys; this does not
    claim that every custom Bark server uses that complete character policy.
    """
    if not isinstance(value, str):
        raise BarkNotifyError("invalid_key")
    value = value.strip()
    if not value:
        raise BarkNotifyError("key_missing")
    if _KEY.fullmatch(value):
        return value
    match = _KEY_URL.fullmatch(value)
    if match:
        return match.group(1)
    raise BarkNotifyError("invalid_key")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate object field")
        result[key] = value
    return result


class BarkClient:
    """Synchronous bounded requests; the caller owns cancellation and workers.

    An eight-second socket timeout and streamed deadline are used. A blocking
    socket read still has its own timeout; this is not a hard wall-clock cap.
    """
    def __init__(self, session=None):
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def send_notice(self, device_key: str, text: str) -> BarkSendResult:
        key = normalize_device_key(device_key)
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise BarkNotifyError("invalid_notice")
        valid_text = True
        try:
            text.encode("utf-8")
        except UnicodeError:
            valid_text = False
        if not valid_text:
            raise BarkNotifyError("invalid_notice")
        response = None
        failure = None
        deadline = time.monotonic() + 8
        try:
            response = self.session.request(
                "POST", ENDPOINT,
                headers={"Content-Type": "application/json; charset=utf-8"},
                json={"device_key": key, "title": "宁宁·AI任务提醒", "body": text, "group": "NenePet"},
                timeout=8, allow_redirects=False, stream=True,
            )
            if 300 <= response.status_code < 400:
                raise BarkNotifyError("redirect_blocked")
            if not 200 <= response.status_code < 300:
                raise BarkNotifyError("http_error")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=1):
                if time.monotonic() > deadline:
                    raise BarkNotifyError("network_error")
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise BarkNotifyError("response_too_large")
            data = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(data, dict):
                raise BarkNotifyError("bad_response")
            if "code" not in data:
                raise BarkNotifyError("acceptance_unknown")
            if type(data["code"]) is not int:
                raise BarkNotifyError("bad_response")
            if data["code"] != 200:
                raise BarkNotifyError("api_rejected")
        except BarkNotifyError as error:
            failure = error.code
        except (ValueError, TypeError, UnicodeError, RecursionError):
            failure = "bad_response"
        except Exception:
            failure = "network_error"
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
        # Raise outside the except block so the fixed exception does not retain
        # a raw requests exception (which can include its prepared request/key).
        if failure is not None:
            raise BarkNotifyError(failure)
        return BarkSendResult(accepted=True)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _config_path(path: Path) -> Path:
    if (not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
            or path.drive.startswith("\\\\")
            or any(":" in part for part in path.parts[1:])
            or any(_is_link(part) for part in (path, *path.parents))):
        raise BarkNotifyError("config_error")
    return path


def _read_envelope(path: Path) -> dict:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_CONFIG_BYTES:
        raise BarkNotifyError("config_error")
    with path.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise BarkNotifyError("config_error")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(value, dict) or set(value) != {"format", "encrypted_state"}
            or value["format"] != _CONFIG_FORMAT
            or not isinstance(value["encrypted_state"], str) or not value["encrypted_state"]):
        raise BarkNotifyError("config_error")
    return value


def _decode_state(value: object) -> BarkConfig:
    if (not isinstance(value, dict) or set(value) != {"enabled", "device_key"}
            or type(value["enabled"]) is not bool or not isinstance(value["device_key"], str)):
        raise BarkNotifyError("config_error")
    if not value["device_key"] and not value["enabled"]:
        return BarkConfig()
    key = normalize_device_key(value["device_key"])
    return BarkConfig(enabled=value["enabled"], device_key=key)


def load_config(path: Path) -> BarkConfig:
    """Read only the supplied own file; absence stays off and creates nothing."""
    try:
        path = _config_path(path)
        if not path.exists():
            return BarkConfig()
        encrypted = _read_envelope(path)["encrypted_state"]
        from aipet.core.credentials import unprotect_secret
        return _decode_state(json.loads(unprotect_secret(encrypted), object_pairs_hook=_unique_object))
    except Exception:
        pass
    raise BarkNotifyError("config_error")


def save_config(path: Path, config: BarkConfig) -> None:
    """Save DPAPI-protected state via the credential facade, never plaintext."""
    temporary = None
    failed = False
    try:
        path = _config_path(path)
        if path.exists():
            _read_envelope(path)
        if not isinstance(config, BarkConfig):
            raise BarkNotifyError("config_error")
        checked = _decode_state({"enabled": config.enabled, "device_key": config.device_key})
        from aipet.core.credentials import protect_secret
        encrypted = protect_secret(json.dumps({"enabled": checked.enabled, "device_key": checked.device_key},
                                             ensure_ascii=False, separators=(",", ":")))
        if not isinstance(encrypted, str) or not encrypted:
            raise BarkNotifyError("config_error")
        encoded = json.dumps({"format": _CONFIG_FORMAT, "encrypted_state": encrypted}).encode("utf-8")
        if len(encoded) > MAX_CONFIG_BYTES:
            raise BarkNotifyError("config_error")
        path.parent.mkdir(parents=True, exist_ok=True)
        _config_path(path)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".nene-bark-", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except Exception:
        failed = True
    finally:
        if temporary is not None:
            try:
                if not _is_link(temporary) and temporary.resolve().parent == path.parent.resolve():
                    temporary.unlink(missing_ok=True)
            except Exception:
                pass
    if failed:
        raise BarkNotifyError("config_error")


def logout(path: Path) -> None:
    """Delete only an explicitly supplied file with this adapter's marker."""
    try:
        path = _config_path(path)
        if path.exists():
            _read_envelope(path)
            path.unlink()
        return
    except Exception:
        pass
    raise BarkNotifyError("config_error")
