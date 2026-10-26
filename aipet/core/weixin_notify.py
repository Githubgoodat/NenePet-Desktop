"""Own-recipient text notices over Tencent's documented iLink protocol.

This is a small local adapter, not an OpenClaw runtime or an agent. Incoming
message bodies are discarded by the default context-only API. The separate
reply API exposes bounded owner text only to an explicitly enabled caller.
API acceptance is never a delivery receipt.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
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
MAX_REPLY_TEXT_CHARS = 2200
MAX_PROVIDER_MESSAGE_ID_CHARS = 256
_UINT64_MAX = (1 << 64) - 1
_MAX_TIMESTAMP_MS = (1 << 53) - 1
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


@dataclass(frozen=True, repr=False)
class WeixinIncomingReply:
    message_id: str
    text: str
    # Unix epoch milliseconds (UTC), not a local receipt timestamp. None means
    # the server omitted it; callers must not invent a freshness guarantee.
    create_time_ms: int | None = None


@dataclass(frozen=True, repr=False)
class WeixinReplyUpdate:
    context_token: str | None
    cursor: str
    messages: tuple[WeixinIncomingReply, ...] = ()


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


def _reply_message_id(value) -> str | None:
    """Canonical, bounded replay key shared by top-level and item IDs.

    Tencent's getWeixinMessageId also accepts an opaque item.msg_id when the
    optional top-level ID is absent. Numeric wire IDs retain their previous
    decimal form; opaque IDs use a separate hashed namespace, so a provider
    string resembling our own prefix cannot impersonate another replay key.
    """
    if type(value) is int:
        return str(value) if 0 < value <= _UINT64_MAX else None
    if (not isinstance(value, str) or not 0 < len(value) <= MAX_PROVIDER_MESSAGE_ID_CHARS
            or any(not char.isprintable() for char in value)):
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdecimal():
        if not re.fullmatch(r"[1-9][0-9]{0,19}", value) or int(value) > _UINT64_MAX:
            return None
        return value
    # Numeric JSON values are never coerced from bool/float, and signed numeric
    # sentinels are not promoted into otherwise valid opaque identifiers.
    if re.fullmatch(r"[+-][0-9]+", value):
        return None
    return "weixin-id-sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _incoming_reply(message: dict) -> WeixinIncomingReply | None:
    """Project one plain-text item; quoted/forwarded content is never read."""
    items = message.get("item_list")
    if not isinstance(items, list) or len(items) != 1:
        return None
    item = items[0]
    if (not isinstance(item, dict) or type(item.get("type")) is not int or item["type"] != 1
            or any(item.get(field) is not None for field in (
                "image_item", "voice_item", "file_item", "video_item",
                "tool_call_start_item", "tool_call_result_item",
            ))):
        return None
    identity = message.get("message_id")
    # Fall back only for an absent/empty top-level field, not for a malformed
    # supplied ID. Inspect only the validated top-level text item, never a
    # quoted item's ID, seq, client_id, timestamp or freshly generated value.
    if (identity is None or identity == "" or isinstance(identity, str)
            and len(identity) <= MAX_PROVIDER_MESSAGE_ID_CHARS and not identity.strip(" ")):
        identity = item.get("msg_id")
    identity = _reply_message_id(identity)
    if identity is None:
        return None
    text_item = item.get("text_item")
    text = text_item.get("text") if isinstance(text_item, dict) else None
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_REPLY_TEXT_CHARS
            or any(not char.isprintable() and char not in "\r\n\t" for char in text)):
        return None
    created = message.get("create_time_ms")
    if created is not None and (type(created) is not int or not 0 <= created <= _MAX_TIMESTAMP_MS):
        # Provider time is optional metadata, not proof of freshness or owner
        # authority. Preserve otherwise valid text when it is unusable.
        created = None
    return WeixinIncomingReply(identity, text, created)


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
        timeout = 35 if long_poll else 8
        deadline = time.monotonic() + timeout
        error_code = None
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
        except WeixinNotifyError as exc:
            error_code = exc.code
        except requests.ReadTimeout:
            if long_poll:
                # An idle read poll is normal. Omitting the next cursor lets
                # _update_batch keep the caller's previous cursor verbatim.
                return {"ret": 0, "msgs": []}
            error_code = "network_error"
        except requests.ConnectTimeout:
            error_code = "poll_connect_timeout" if long_poll else "network_error"
        except requests.exceptions.SSLError:
            error_code = "poll_tls_error" if long_poll else "network_error"
        except requests.ConnectionError:
            error_code = "poll_connection_error" if long_poll else "network_error"
        except (requests.RequestException, OSError):
            error_code = "network_error"
        except (ValueError, TypeError, UnicodeError, RecursionError):
            error_code = "bad_response"
        except Exception:
            error_code = "network_error"
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
        # Raise outside the handler so neither the HTTP exception nor its
        # prepared request (which may contain credentials) is retained as the
        # fixed diagnostic's exception context.
        if error_code is not None:
            raise WeixinNotifyError(error_code)

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

    def _update_batch(self, credentials: WeixinCredentials, cursor: str) -> tuple[list, str]:
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
        return messages, next_cursor

    def get_updates(self, credentials: WeixinCredentials, cursor: str = "") -> WeixinUpdate:
        """Legacy context refresh: never returns or retains message bodies."""
        credentials = _credentials(credentials)
        messages, next_cursor = self._update_batch(credentials, cursor)
        context = None
        for message in messages:
            if (isinstance(message, dict) and message.get("from_user_id") == credentials.scanner_id
                    and type(message.get("message_type")) is int and message["message_type"] == 1
                    and not message.get("group_id") and message.get("context_token")):
                context = _text(message["context_token"], 16384)
        return WeixinUpdate(context, next_cursor)

    def get_reply_updates(self, credentials: WeixinCredentials, cursor: str = "") -> WeixinReplyUpdate:
        """One poll for owner-only text; does not route, execute, approve or save.

        Only one top-level text item is accepted. Multiple text items and mixed
        media are rejected, rather than concatenated. Optional ref_msg content
        is ignored; only the owner's new top-level text is returned. A caller
        must explicitly correlate and deduplicate replies before using them.
        """
        credentials = _credentials(credentials)
        messages, next_cursor = self._update_batch(credentials, cursor)
        context, replies = None, []
        for message in messages:
            if (not isinstance(message, dict) or message.get("from_user_id") != credentials.scanner_id
                    or type(message.get("message_type")) is not int or message["message_type"] != 1
                    or message.get("group_id") not in (None, "")
                    or message.get("to_user_id") not in (None, "", credentials.bot_id)):
                continue
            if message.get("context_token"):
                context = _text(message["context_token"], 16384)
            reply = _incoming_reply(message)
            if reply is not None:
                replies.append(reply)
        return WeixinReplyUpdate(context, next_cursor, tuple(replies))

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
