"""Explicit, expiring cooperation questions; never a remote approval client."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from PyQt5.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt5.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from aipet.core.config import get_cache_dir, get_user_data_dir
from aipet.core.workflow_questions import QuestionStore, QuestionStoreError
from aipet.core.workflow_question_status import read_channel_status, write_channel_status
from aipet.ui.weixin_notify import _project_name


_FORMAT = "nenepet-weixin-questions-settings-v1"
_REPLY = re.compile(r"(?:回复\s+)?([0-9a-fA-F]{8})\s+(.+)\Z", re.DOTALL)
_SOURCE = {"codex": "Codex", "grok_bot": "grok bot"}


def _utc_now():
    return datetime.now(timezone.utc)


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _safe_path(path):
    if (not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
            or str(path).startswith(("\\\\", "//"))
            or any(":" in part for part in path.parts if part != path.anchor)):
        return False
    try:
        for part in (path, *path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
        return True
    except OSError:
        return False


def load_question_enabled(path):
    if not _safe_path(path):
        raise ValueError("question_config_error")
    if not path.exists():
        return False
    if not path.is_file() or path.stat().st_size > 2048:
        raise ValueError("question_config_error")
    if path.stat().st_nlink != 1:
        raise ValueError("question_config_error")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or set(value) != {"format", "enabled"}
            or value["format"] != _FORMAT or type(value["enabled"]) is not bool):
        raise ValueError("question_config_error")
    return value["enabled"]


def save_question_enabled(path, enabled):
    if not _safe_path(path) or type(enabled) is not bool:
        raise ValueError("question_config_error")
    if path.exists():
        load_question_enabled(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".nene-question-settings-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"format": _FORMAT, "enabled": enabled}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def question_text(record, now):
    if record.source not in _SOURCE or record.status != "pending" or _time(record.expires_at) <= now:
        return None
    remaining = max(1, int((_time(record.expires_at) - now).total_seconds()))
    lines = ["宁宁 · 微信提问", "〈" + _project_name(record) + "〉 · " + _SOURCE[record.source],
             "那个，这件事想听听你的意见：", record.question]
    lines.extend(chr(65 + index) + ". " + option for index, option in enumerate(record.options))
    lines.extend(["问题编号：" + record.code + "（剩余约 " + str(remaining) + " 秒）",
                  "微信回复：回复 " + record.code + " " + ("A" if record.options else "你的回答"),
                  "也可简写：" + record.code + " " + ("A" if record.options else "你的回答"),
                  "执行者仍在等待时，领取回答后继续。这是普通问答，不是操作授权。"])
    text = "\n".join(lines)
    return text if len(text) <= 2000 else None


class QuestionDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("宁宁 · 微信问答")
        self.resize(600, 340)
        layout = QVBoxLayout(self)
        info = QLabel(
            "让 Codex 或 grok bot 主动发起带编号的普通提问，在微信回复后由原执行者领取答案。\n"
            "问题与回答会经过微信服务，只在本机独立加密文件中短期保存，不加入角色记忆。\n"
            "只接收绑定本人的“问题编号 答案”，也兼容“回复 问题编号 答案”。不接管原生问题或操作审批。\n"
            "问题最多有效 15 分钟；等待进程已结束时，收到答案也不会自动唤醒它。"
        )
        info.setTextFormat(Qt.PlainText)
        info.setWordWrap(True)
        layout.addWidget(info)
        self.enabled = QCheckBox("启用本人微信问答")
        layout.addWidget(self.enabled)
        row = QHBoxLayout()
        self.save = QPushButton("保存")
        self.retry = QPushButton("重试接收 / 刷新上下文")
        row.addWidget(self.save)
        row.addWidget(self.retry)
        row.addStretch()
        layout.addLayout(row)
        self.inbox = QLabel()
        self.inbox.setTextFormat(Qt.PlainText)
        self.inbox.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.inbox.setWordWrap(True)
        layout.addWidget(self.inbox)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)


class QuestionChannelController(QObject):
    status_changed = pyqtSignal(str)

    def __init__(self, pet, weixin, parent=None, *, inbox=None, config_path=None,
                 store=None, dialog=None, clock=_utc_now):
        super().__init__(parent)
        self.pet, self.weixin, self._clock = pet, weixin, clock
        self.inbox = Path(inbox) if inbox is not None else get_cache_dir() / "workflow-questions"
        self.config_path = Path(config_path) if config_path is not None else get_user_data_dir() / "workflow-questions.json"
        self.store = store or QuestionStore(self.inbox, clock=clock)
        self.dialog = dialog or QuestionDialog()
        self._closed = False
        self._paused = pet.is_dnd_enabled()
        self._test = bool(getattr(pet, "is_test_mode", lambda: False)())
        self._tracking = True
        self._watch_started = clock()
        self._heartbeat = None
        self._was_ready = None
        self._receipts = deque()
        self._seen_replies = {}
        self._sending = None
        self._sending_record = None
        self._busy = False
        self._storage_fault = False
        self.status_text = "微信问答未启用。"
        try:
            self._enabled = load_question_enabled(self.config_path)
        except Exception:
            self._enabled = False
            self.status_text = "问答设置无法读取，保持关闭。"
        self.dialog.enabled.setChecked(self._enabled)
        if self._enabled:
            self.status_text = ("微信问答已启用，仅处理执行者新注册的普通问题。"
                                if weixin.question_channel_available()
                                else "微信问答已启用，正在建立本人接收连接。")
        self._set_status(self.status_text)
        self.dialog.inbox.setText("合作脚本的专用 --inbox：\n" + str(self.inbox))
        self.dialog.save.clicked.connect(self.save_settings)
        self.dialog.retry.clicked.connect(self.retry)
        weixin.question_replies.connect(self._replies)
        weixin.question_send_result.connect(self._sent)
        weixin.question_transport_error.connect(self._receive_error)
        weixin.slot_available.connect(self.poll)
        pet.pause_changed.connect(self.set_paused)
        pet.test_mode_changed.connect(self.set_test_mode)
        self.timer = QTimer(self)
        self.timer.setInterval(2_000)
        self.timer.timeout.connect(self.poll)
        if self._enabled and not self._paused and not self._test:
            self.timer.start()
        self.poll()

    def _allowed(self):
        return bool(self._enabled and not self._closed and not self._paused and not self._test and not self._storage_fault
                    and self._tracking and not getattr(self.pet, "_closing", False)
                    and self.pet.settings.character.profile_id == "nene")

    def _set_status(self, text):
        self.status_text = text
        self.dialog.status.setText(text)
        self.status_changed.emit(text)

    def _write_status(self, force=False):
        now = self._clock()
        if not self.inbox.exists() and not self._enabled:
            return
        if not _safe_path(self.inbox):
            raise ValueError("question_inbox_error")
        self.inbox.mkdir(parents=True, exist_ok=True)
        if force or self._heartbeat is None or now - self._heartbeat >= timedelta(seconds=5):
            accepting = self._allowed() and not self._storage_fault and self.weixin.question_channel_available()
            write_channel_status(self.inbox, enabled=self._enabled,
                                 accepting=accepting, now=now)
            expected = "disabled" if not self._enabled else "ready" if accepting else "paused"
            if read_channel_status(self.inbox, now=now) != expected:
                raise ValueError("question_status_error")
            self._heartbeat = now

    def open(self):
        if self._closed or self._paused:
            return
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def save_settings(self):
        if self._closed:
            return
        enabled = self.dialog.enabled.isChecked()
        try:
            save_question_enabled(self.config_path, enabled)
        except Exception:
            self._set_status("问答设置保存失败；没有启用新配置。")
            return
        self._enabled = enabled
        self._storage_fault = False
        self._reset()
        self._set_status("微信问答设置已保存；仅处理之后的新问题。" if enabled else "微信问答已关闭。")

    def poll(self):
        if self._busy:
            return
        self._busy = True
        try:
            ready = self._allowed() and self.weixin.question_channel_available()
            if ready != self._was_ready:
                if ready and self._was_ready is not None:
                    self._set_status("微信问答接收已就绪，可以由执行者创建新问题。")
                self._heartbeat = None
                self._was_ready = ready
            self._write_status()
            if not self._allowed():
                self.weixin.set_question_channel(False)
                return
            records = self.store.list_pending()
            eligible = [record for record in records if _time(record.created_at) > self._watch_started]
            # An opted-in mobile channel must hear the owner's next message
            # even between questions, so it can refresh its communication
            # context without requiring a click on this desktop.
            self.weixin.set_question_channel(True)
            if not self.weixin.can_send_question():
                return
            while self._receipts and self._receipts[0][0] != self.weixin.question_owner_key():
                self._receipts.popleft()
            if self._receipts:
                receipt = self._receipts.popleft()
                text = receipt[1]
                self._sending = "receipt"
                self._sending_record = None
                if not self.weixin.send_question_notice(text):
                    self._receipts.appendleft(receipt)
                    self._sending = None
                return
            for record in sorted(eligible, key=lambda item: (item.created_at, item.request_id)):
                if record.delivery_attempted:
                    continue
                text = question_text(record, self._clock())
                if text is None:
                    continue
                if self.store.claim_delivery(record.request_id, self.weixin.question_owner_key()):
                    self._sending = record.code
                    self._sending_record = record
                    # A claim precedes submission. An ambiguous outcome cannot
                    # trigger a duplicate question on a later timer tick.
                    if self.weixin.send_question_notice(text):
                        self._set_status("正在发送问题 " + record.code + "；等待本人微信回答。")
                    else:
                        self._sending = None
                        self._set_status("问题未提交，未自动重发；请执行者重新发起问题。")
                    break
        except (OSError, ValueError, QuestionStoreError):
            self._storage_fault = True
            self._heartbeat = None
            try:
                self._write_status(force=True)
            except (OSError, ValueError):
                pass
            self.weixin.set_question_channel(False)
            self._set_status("问答通道暂不可用；没有转交任何新回答。")
        finally:
            self._busy = False

    def _replies(self, messages, owner_key):
        if not self._allowed():
            return
        now = self._clock()
        try:
            eligible_codes = {record.code for record in self.store.list_pending()
                              if _time(record.created_at) > self._watch_started}
        except (OSError, ValueError, QuestionStoreError):
            self.weixin.defer_question_update()
            self._set_status("问题目录暂不可读，保留接收游标，没有转交回答。")
            return
        for message in messages:
            # Only an explicit code routes an answer. Quote context, project
            # names and the most recent active task never select a recipient.
            match = _REPLY.fullmatch(message.text.strip())
            message_key = (owner_key, message.message_id)
            if match is None or message_key in self._seen_replies:
                continue
            if match[1].upper() not in eligible_codes:
                self._set_status("问题不存在、已过期或已暂停，没有转交回答。")
                continue
            # Source timestamps are optional and may be zero or clock-skewed.
            # The eligible request epoch, owner, nonce, TTL and message id are
            # the routing boundaries; an unrelated clock cannot discard an answer.
            result = self.store.submit_reply(code=match[1].upper(), answer=match[2].strip(),
                                             message_id=message.message_id, owner_key=owner_key)
            if result in {"busy", "storage_error"}:
                self.weixin.defer_question_update()
                self._set_status("回答暂未安全存入，保留接收游标稍后重查；没有交给其他任务。")
                continue
            self._seen_replies[message_key] = now
            if len(self._seen_replies) > 256:
                self._seen_replies.pop(min(self._seen_replies, key=self._seen_replies.get))
            if result == "accepted":
                self._set_status("问题 " + match[1].upper() + " 的回答已收到，等待原执行者领取。")
                if len(self._receipts) < 4:
                    self._receipts.append((owner_key, "宁宁 · 回答已收到\n问题 " + match[1].upper()
                                           + " 的回答已经交到本机了。原执行者仍在等待时，领取后会继续；这还不代表执行完成。"))
            else:
                self._set_status("回答未转交（" + result + "）；没有改变其他任务。")

    def _sent(self, result):
        record = self._sending_record
        self._sending = None
        self._sending_record = None
        if result == "accepted":
            self._set_status("微信服务已受理，请以手机收到及原执行者取答为准。")
        elif result == "acceptance_unknown":
            self._set_status("服务受理结果不明；请核对微信，没有自动重发问题。")
        else:
            if record is not None and result in {
                    "api_rejected_minus2", "api_rejected", "session_expired", "context_missing",
                    "credentials_missing", "context_refresh_required"}:
                try:
                    self.store.cancel(record.request_id, source=record.source,
                                      task_id=record.task_id, turn_id=record.turn_id)
                except (OSError, ValueError, QuestionStoreError):
                    pass
            self._set_status("微信未明确受理本次问答消息，没有自动重发；可刷新上下文后重新发起。")

    def _receive_error(self, code):
        transient = {"network_error": "微信接收暂不可用", "poll_connect_timeout": "微信接收连接超时",
                     "poll_tls_error": "微信接收的安全连接失败", "poll_connection_error": "微信接收网络连接失败"}
        self._set_status(transient[code] + "，稍后只读重查；不会重发问题。" if code in transient
                         else "微信接收已暂停，请检查绑定后点击“重试接收”。")
        self._heartbeat = None

    def retry(self):
        if (not self._enabled or self._closed or self._paused or self._test or not self._tracking
                or getattr(self.pet, "_closing", False) or self.pet.settings.character.profile_id != "nene"):
            return
        self._storage_fault = False
        self.weixin.retry_question_channel()
        self._heartbeat = None
        self._set_status("正在建立本人微信接收连接，新的普通消息会自动刷新通信上下文。")
        self.poll()
        self.weixin.establish_context()

    def _cancel_pending(self):
        try:
            for record in self.store.list_pending():
                self.store.cancel(record.request_id, source=record.source,
                                  task_id=record.task_id, turn_id=record.turn_id)
        except (OSError, ValueError, QuestionStoreError):
            pass

    def _reset(self):
        self.timer.stop()
        self._watch_started = self._clock()
        self._receipts.clear()
        self._sending = None
        self._sending_record = None
        self.weixin.set_question_channel(False)
        self._cancel_pending()
        self._heartbeat = None
        self.poll()
        if self._allowed():
            self.timer.start()

    def set_paused(self, value):
        self._paused = bool(value)
        self._reset()

    def set_test_mode(self, value):
        self._test = bool(value)
        self._reset()

    def set_tracking(self, value):
        self._tracking = bool(value)
        self._reset()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._reset()
