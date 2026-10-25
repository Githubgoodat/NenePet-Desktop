"""Own-recipient text notices over Tencent's documented iLink protocol.

This is a small local adapter, not an OpenClaw runtime or an agent. Incoming
message bodies are discarded. API acceptance is never a delivery receipt.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import requests


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.10"
CLIENT_VERSION = str((2 << 16) | (4 << 8) | 10)
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 256 * 1024
_CONFIG_FORMAT = "nenepet-weixin-notify-v1"
_IDENTITY = re.compile(r"[A-Za-z0-9_.:@-]{1,256}\Z")


class WeixinNotifyError(Exception):
    """Fixed codes only: never retain a response, URL, token or HTTP exception."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class WeixinCredentials:
    bot_token: str
    bot_id: str
    scanner_id: str
    base_url: str


@dataclass(frozen=True, repr=False)
class WeixinQRCode:
    qrcode: str
    qrcode_img_content: str


@dataclass(frozen=True, repr=False)
class WeixinQRResult:
    status: str
    poll_base_url: str
    credentials: WeixinCredentials | None = None


@dataclass(frozen=True, repr=False)
class WeixinUpdate:
    context_token: str | None
    cursor: str


@dataclass(frozen=True)
class WeixinSendResult:
    accepted: bool


@dataclass(frozen=True, repr=False)
class WeixinConfig:
    enabled: bool = False
    credentials: WeixinCredentials | None = None
    context_token: str = ""
    cursor: str = ""


def _text(value, maximum: int, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or not empty and not value or any(not c.isprintable() for c in value)):
        raise WeixinNotifyError("bad_response")
    return value


def _base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if (parsed.scheme != "https" or not hostname.endswith(".weixin.qq.com")
                or not re.fullmatch(r"[a-z0-9.-]+", hostname)
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError
        return "https://" + hostname
    except (ValueError, TypeError, AttributeError):
        raise WeixinNotifyError("unsafe_url") from None


def _credentials(value: WeixinCredentials) -> WeixinCredentials:
    if not isinstance(value, WeixinCredentials):
        raise WeixinNotifyError("credentials_missing")
    token = _text(value.bot_token, 8192)
    if any(c.isspace() for c in token) or not token.isascii():
        raise WeixinNotifyError("bad_response")
    if (not isinstance(value.bot_id, str) or not _IDENTITY.fullmatch(value.bot_id)
            or not isinstance(value.scanner_id, str) or not _IDENTITY.fullmatch(value.scanner_id)):
        raise WeixinNotifyError("bad_response")
    _base_url(value.base_url)
    return value


def _business(data: dict) -> None:
    for field in ("ret", "errcode"):
        if field in data and type(data[field]) is not int:
            # Malformed/ambiguous replies are not explicit service rejection.
            # In particular, they must not cause a second-channel resend.
            raise WeixinNotifyError("bad_response")
        if field in data and data[field] != 0:
            if type(data[field]) is int and data[field] == -14:
                code = "session_expired"
            elif type(data[field]) is int and data[field] == -2:
                # Preserve only the fixed service code, never arbitrary errmsg.
                # Tencent does not document a unique cause for this rejection.
                code = "api_rejected_minus2"
            else:
                code = "api_rejected"
            raise WeixinNotifyError(code)


def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": "NenePet/1.0"}


class WeixinClient:
    """Synchronous bounded requests; callers own worker/cancellation lifecycle."""
    def __init__(self, session=None):
        self.session = session if session is not None else requests.Session()
        # Never pick up another application's netrc authentication or cookies.
        self.session.trust_env = False

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def _request(self, method: str, base_url: str, endpoint: str, *, body=None,
                 params=None, token: str | None = None, long_poll: bool = False) -> dict:
        base_url = _base_url(base_url)
        headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": CLIENT_VERSION}
        if method == "POST":
            headers.update({"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
                            "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode("ascii")).decode("ascii")})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        response = None
        timeout = 5 if long_poll else 8
        deadline = time.monotonic() + timeout
        try:
            response = self.session.request(method, base_url + "/" + endpoint, headers=headers,
                params=params, json=body, timeout=timeout, allow_redirects=False, stream=True)
            if 300 <= response.status_code < 400:
                raise WeixinNotifyError("redirect_blocked")
            if not 200 <= response.status_code < 300:
                raise WeixinNotifyError("http_error")
            content = bytearray()
            # A byte-sized iterator lets us check the elapsed deadline even
            # when a peer slowly drips data. requests' blocking socket reads
            # still have their own timeout; this is not a hard wall-clock cap.
            for chunk in response.iter_content(chunk_size=1):
                if time.monotonic() > deadline:
                    raise WeixinNotifyError("network_error")
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise WeixinNotifyError("response_too_large")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise WeixinNotifyError("bad_response")
            _business(data)
            return data
        except WeixinNotifyError:
            raise
        except (requests.RequestException, OSError):
            raise WeixinNotifyError("network_error") from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise WeixinNotifyError("bad_response") from None
        except Exception:
            raise WeixinNotifyError("network_error") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def request_qr(self) -> WeixinQRCode:
        data = self._request("POST", DEFAULT_BASE_URL, "ilink/bot/get_bot_qrcode",
                             params={"bot_type": "3"}, body={"local_token_list": []})
        return WeixinQRCode(_text(data.get("qrcode"), 8192), _text(data.get("qrcode_img_content"), 16384))

    def poll_qr(self, qrcode: str, *, base_url: str = DEFAULT_BASE_URL) -> WeixinQRResult:
        base_url = _base_url(base_url)
        data = self._request("GET", base_url, "ilink/bot/get_qrcode_status",
                             params={"qrcode": _text(qrcode, 8192)})
        status = data.get("status")
        if not isinstance(status, str):
            raise WeixinNotifyError("bad_response")
        if status in {"scaned_but_redirect", "binded_redirect"}:
            if status == "binded_redirect":
                raise WeixinNotifyError("already_bound")
            host = _text(data.get("redirect_host"), 256)
            redirected = _base_url(host if "://" in host else "https://" + host)
            return WeixinQRResult("pending", redirected)
        if status == "confirmed":
            credentials = _credentials(WeixinCredentials(data.get("bot_token"), data.get("ilink_bot_id"),
                data.get("ilink_user_id"), _base_url(data.get("baseurl") or base_url)))
            return WeixinQRResult("confirmed", credentials.base_url, credentials)
        statuses = {"wait": "pending", "scaned": "scanned", "expired": "expired",
                    "need_verifycode": "needs_verification", "verify_code_blocked": "needs_verification"}
        if status not in statuses:
            raise WeixinNotifyError("bad_response")
        return WeixinQRResult(statuses[status], base_url)

    def get_updates(self, credentials: WeixinCredentials, cursor: str = "") -> WeixinUpdate:
        credentials = _credentials(credentials)
        cursor = _text(cursor, 65536, empty=True)
        data = self._request("POST", credentials.base_url, "ilink/bot/getupdates", token=credentials.bot_token,
                             body={"get_updates_buf": cursor, "base_info": _base_info()}, long_poll=True)
        next_cursor = data.get("get_updates_buf")
        if next_cursor is None or next_cursor == "":
            next_cursor = cursor
        next_cursor = _text(next_cursor, 65536, empty=True)
        messages = data.get("msgs", [])
        if not isinstance(messages, list):
            raise WeixinNotifyError("bad_response")
        context = None
        for message in messages:
            if (isinstance(message, dict) and message.get("from_user_id") == credentials.scanner_id
                    and type(message.get("message_type")) is int and message["message_type"] == 1
                    and not message.get("group_id") and message.get("context_token")):
                context = _text(message["context_token"], 16384)
        return WeixinUpdate(context, next_cursor)

    def send_notice(self, credentials: WeixinCredentials, context_token: str, text: str) -> WeixinSendResult:
        credentials = _credentials(credentials)
        if not context_token:
            raise WeixinNotifyError("context_missing")
        context_token = _text(context_token, 16384)
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise WeixinNotifyError("invalid_notice")
        message = {"from_user_id": "", "to_user_id": credentials.scanner_id,
                   "client_id": "nenepet-" + uuid.uuid4().hex, "message_type": 2, "message_state": 2,
                   "context_token": context_token, "item_list": [{"type": 1, "text_item": {"text": text}}]}
        data = self._request("POST", credentials.base_url, "ilink/bot/sendmessage", token=credentials.bot_token,
                             body={"msg": message, "base_info": _base_info()})
        if type(data.get("ret")) is not int or data["ret"] != 0:
            raise WeixinNotifyError("acceptance_unknown")
        return WeixinSendResult(accepted=True)


def _config_path(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise WeixinNotifyError("config_error")
    return path


def _read_envelope(path: Path) -> dict:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_CONFIG_BYTES:
        raise WeixinNotifyError("config_error")
    with path.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"format", "encrypted_state"}
            or value.get("format") != _CONFIG_FORMAT or not isinstance(value["encrypted_state"], str)):
        raise WeixinNotifyError("config_error")
    return value


def _decode_state(value: dict) -> WeixinConfig:
    if (not isinstance(value, dict) or set(value) != {"enabled", "credentials", "context_token", "cursor"}
            or type(value["enabled"]) is not bool):
        raise WeixinNotifyError("config_error")
    credentials = value["credentials"]
    if credentials is not None:
        if not isinstance(credentials, dict) or set(credentials) != {"bot_token", "bot_id", "scanner_id", "base_url"}:
            raise WeixinNotifyError("config_error")
        credentials = _credentials(WeixinCredentials(**credentials))
    if value["enabled"] and credentials is None:
        raise WeixinNotifyError("config_error")
    return WeixinConfig(value["enabled"], credentials, _text(value["context_token"], 16384, empty=True),
                        _text(value["cursor"], 65536, empty=True))


def load_config(path: Path) -> WeixinConfig:
    """Only an explicitly supplied own configuration; absent means disabled."""
    try:
        path = _config_path(path)
        if not path.exists():
            return WeixinConfig()
        encrypted = _read_envelope(path)["encrypted_state"]
        from aipet.core.credentials import unprotect_secret
        return _decode_state(json.loads(unprotect_secret(encrypted)))
    except Exception:
        raise WeixinNotifyError("config_error") from None


def save_config(path: Path, config: WeixinConfig) -> None:
    """Explicit owner action only; all identity, token, context and cursor encrypted."""
    temporary = None
    try:
        path = _config_path(path)
        if path.exists():
            _read_envelope(path)  # Refuse to replace another application's file.
        if not isinstance(config, WeixinConfig):
            raise WeixinNotifyError("config_error")
        credentials = config.credentials
        value = {"enabled": config.enabled, "credentials": None if credentials is None else {
            "bot_token": credentials.bot_token, "bot_id": credentials.bot_id,
            "scanner_id": credentials.scanner_id, "base_url": credentials.base_url},
            "context_token": config.context_token, "cursor": config.cursor}
        _decode_state(value)
        from aipet.core.credentials import protect_secret
        encrypted = protect_secret(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        if not encrypted:
            raise WeixinNotifyError("config_error")
        encoded = json.dumps({"format": _CONFIG_FORMAT, "encrypted_state": encrypted}).encode("utf-8")
        if len(encoded) > MAX_CONFIG_BYTES:
            raise WeixinNotifyError("config_error")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".nene-weixin-", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except Exception:
        raise WeixinNotifyError("config_error") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def logout(path: Path) -> None:
    """Remove only this adapter's own encrypted configuration file."""
    try:
        path = _config_path(path)
        if path.exists():
            _read_envelope(path)
            path.unlink()
    except Exception:
        raise WeixinNotifyError("config_error") from None
