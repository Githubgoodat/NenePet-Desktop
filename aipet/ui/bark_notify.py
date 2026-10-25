"""Opt-in iPhone fallback notices with fixed metadata and no phone commands."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import threading

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from aipet.core.bark_notify import (
    BarkClient, BarkConfig, BarkNotifyError, load_config, logout,
    normalize_device_key, save_config,
)
from aipet.core.config import get_user_data_dir
from aipet.ui.weixin_notify import notice_text


_INPUT_STATES = {"input_requested", "question_opened"}
_ERRORS = {
    "key_missing": "请先填写并保存本人 iPhone 的 Bark 设备密钥。",
    "invalid_key": "设备密钥格式无效；只接受裸密钥或 api.day.app 的完整设备地址。",
    "invalid_notice": "提醒内容未通过校验，没有发送。",
    "redirect_blocked": "推送服务返回了重定向，本次请求已停止。",
    "http_error": "推送服务未正常受理，没有自动重试。",
    "network_error": "连接推送服务失败；结果可能不明，没有自动重试。",
    "response_too_large": "推送服务返回了无法验证的结果，没有自动重试。",
    "bad_response": "推送服务结果无法验证，请先核对手机；没有自动重试。",
    "api_rejected": "推送服务明确拒绝，本次运行的备用提醒已暂停；请核对绑定后重新启用。",
    "acceptance_unknown": "服务未明确确认受理，请先核对手机；没有自动重试。",
    "config_error": "本机备用提醒设置无法读取或安全保存，提醒保持关闭。",
}


def _utc_now():
    return datetime.now(timezone.utc)


class BarkWorker(QThread):
    result_ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, operation, arguments=(), parent=None, *, client_factory=BarkClient):
        super().__init__(parent)
        self.operation, self.arguments = operation, arguments
        self._factory = client_factory
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    def run(self):
        client = None
        try:
            if self._cancelled.is_set() or self.operation != "send":
                return
            client = self._factory()
            if self._cancelled.is_set():
                return
            result = client.send_notice(*self.arguments)
            if not self._cancelled.is_set():
                self.result_ready.emit(result)
        except BarkNotifyError as exc:
            if not self._cancelled.is_set():
                self.error.emit(exc.code if exc.code in _ERRORS else "bad_response")
        except Exception:
            if not self._cancelled.is_set():
                self.error.emit("bad_response")
        finally:
            self.arguments = ()
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass


class BarkDialog(QDialog):
    closing = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("宁宁 · iPhone备用提醒")
        self.resize(620, 360)
        layout = QVBoxLayout(self)
        self.info = QLabel(
            "在本人的 iPhone 安装 Bark 并允许通知后，填写它提供的设备密钥或地址。\n"
            "启用后，微信不可用或明确拒绝的任务提醒可交给 Bark 备用通道。\n"
            "按你的选择，第三方推送服务会收到项目名、来源和状态；不含完整路径、任务编号或聊天正文。"
        )
        self.info.setTextFormat(Qt.PlainText)
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("Bark 设备密钥或 https://api.day.app/设备密钥")
        layout.addWidget(self.key)
        self.enabled = QCheckBox("启用 iPhone 备用提醒")
        layout.addWidget(self.enabled)
        row = QHBoxLayout()
        self.save = QPushButton("保存设置")
        self.test = QPushButton("发送一条测试提醒")
        self.logout = QPushButton("清除本机绑定")
        self.stop = QPushButton("停止当前请求")
        for button in (self.save, self.test, self.logout, self.stop):
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.limit = QLabel(
            "密钥仅加密保存在本机。免打扰、测试模式、停止跟进或退出会清空待发提醒；"
            "在途请求不能撤回，恢复后不补发。服务受理仍需本人核对手机送达。"
        )
        self.limit.setTextFormat(Qt.PlainText)
        self.limit.setWordWrap(True)
        layout.addWidget(self.limit)

    def closeEvent(self, event):
        self.closing.emit()
        super().closeEvent(event)


class BarkNotifyController(QObject):
    status_changed = pyqtSignal(str)

    def __init__(self, pet, parent=None, *, config_path=None, worker_factory=BarkWorker,
                 clock=_utc_now, dialog=None):
        super().__init__(parent)
        self.pet = pet
        self.config_path = Path(config_path) if config_path is not None else get_user_data_dir() / "workflow-bark.json"
        self.dialog = dialog or BarkDialog()
        self._factory, self._clock = worker_factory, clock
        self._closed = False
        self._paused = pet.is_dnd_enabled()
        self._test_mode = bool(getattr(pet, "is_test_mode", lambda: False)())
        self._tracking = True
        self._enabled = False
        self._device_key = ""
        self._epoch = 0
        self._active = None
        self._active_is_test = False
        self._workers = set()
        self._queue = deque()
        self._seen = {}
        self._seen_before = self._watch_started = self._clock()
        self.status_text = "iPhone 备用提醒未启用；保存设置不会发送测试。"
        try:
            config = load_config(self.config_path)
            self._device_key = config.device_key
            self._enabled = bool(config.enabled and self._device_key)
            if self._enabled:
                self.status_text = "iPhone 备用提醒已启用，等待之后需要转交的新事件。"
        except Exception:
            self.status_text = _ERRORS["config_error"]
        self.dialog.enabled.setChecked(self._enabled)
        self._update_placeholder()
        self.dialog.save.clicked.connect(self.save_settings)
        self.dialog.test.clicked.connect(self.send_test)
        self.dialog.logout.clicked.connect(self.clear_login)
        self.dialog.stop.clicked.connect(self.cancel)
        self.dialog.closing.connect(self._dialog_closed)
        pet.pause_changed.connect(self.set_paused)
        pet.test_mode_changed.connect(self.set_test_mode)
        self._set_status(self.status_text)

    def _allowed(self):
        return (not self._closed and not self._paused and not self._test_mode and self._tracking
                and not getattr(self.pet, "_closing", False)
                and self.pet.settings.character.profile_id == "nene")

    def _set_status(self, text):
        self.status_text = text
        self.dialog.status.setText(text)
        self.status_changed.emit(text)

    def _update_placeholder(self):
        self.dialog.key.setPlaceholderText(
            "本机已有绑定；留空沿用，填写则替换" if self._device_key
            else "Bark 设备密钥或 https://api.day.app/设备密钥"
        )

    def open(self):
        if self._closed or self._paused or getattr(self.pet, "_closing", False):
            return
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _start(self, text, *, is_test=False):
        if not self._allowed() or self._active is not None or not self._device_key:
            return False
        epoch = self._epoch
        worker = self._factory("send", (self._device_key, text), parent=self)
        self._active, self._active_is_test = worker, is_test
        self._workers.add(worker)
        worker.result_ready.connect(lambda value: self._sent(value)
                                    if epoch == self._epoch and self._allowed() else None)
        worker.error.connect(lambda code: self._job_error(code)
                             if epoch == self._epoch and self._allowed() else None)
        worker.finished.connect(lambda: self._retire(worker))
        worker.start()
        return True

    def _sent(self, value):
        self._set_status(
            "Bark 服务已受理，请核对本人 iPhone 是否收到；受理不等于送达。"
            if value.accepted else "Bark 未明确确认受理，请核对手机；没有自动重试。"
        )

    def _job_error(self, code):
        if code in {"key_missing", "invalid_key", "api_rejected"}:
            self._enabled = False
            self.dialog.enabled.setChecked(False)
            self._queue.clear()
            self._epoch += 1
        self._set_status(_ERRORS.get(code, _ERRORS["bad_response"]))

    def _retire(self, worker):
        self._workers.discard(worker)
        if self._active is worker:
            self._active, self._active_is_test = None, False
        worker.deleteLater()
        self._drain()

    def _invalidate(self):
        self._epoch += 1
        self._watch_started = self._clock()
        self._queue.clear()
        if self._active is not None:
            self._active.cancel()

    def _dialog_closed(self):
        if self._active_is_test and not self._closed:
            self.cancel()

    def cancel(self, *_args):
        if not self._closed:
            self._invalidate()
            self._set_status("备用请求已停止；在途请求不能撤回，后续不补发。")

    def save_settings(self):
        if self._closed:
            return
        try:
            entered = self.dialog.key.text().strip()
            key = normalize_device_key(entered) if entered else self._device_key
            enabled = self.dialog.enabled.isChecked()
            if enabled and not key:
                self._set_status(_ERRORS["key_missing"])
                return
        except BarkNotifyError as exc:
            self._set_status(_ERRORS.get(exc.code, _ERRORS["invalid_key"]))
            return
        self._invalidate()
        self._enabled = False
        try:
            save_config(self.config_path, BarkConfig(enabled, key))
        except Exception:
            self.dialog.enabled.setChecked(False)
            self._set_status(_ERRORS["config_error"])
            return
        self._enabled, self._device_key = bool(enabled and key), key
        self.dialog.key.clear()
        self._update_placeholder()
        self._set_status("备用设置已保存并启用，只处理之后的新事件。" if self._enabled
                         else "备用设置已保存，当前未启用自动提醒。")

    def send_test(self):
        if not self._allowed():
            return
        if not self._device_key:
            self._set_status(_ERRORS["key_missing"])
            return
        if self._active is not None:
            self._set_status("上一个请求尚未结束，请稍后显式测试。")
            return
        self._set_status("正在提交你主动要求的一条 iPhone 测试提醒……")
        self._start("宁宁 · iPhone备用提醒测试\n这是你主动发送的测试，不包含任务正文。请核对本人手机是否收到。",
                    is_test=True)

    def clear_login(self):
        if self._closed:
            return
        self._invalidate()
        self._enabled, self._device_key = False, ""
        self.dialog.key.clear()
        self.dialog.enabled.setChecked(False)
        self._update_placeholder()
        try:
            logout(self.config_path)
        except Exception:
            self._set_status("本次运行已关闭备用提醒，但本机绑定文件未能清除。")
            return
        self._set_status("已清除本机 Bark 绑定。")

    def enqueue(self, snapshot):
        if not self._allowed() or not self._enabled or not self._device_key:
            return
        now = self._clock()
        text = notice_text(snapshot)
        if text is None:
            return
        try:
            observed = datetime.fromisoformat(snapshot.observed_at.replace("Z", "+00:00"))
            if (observed.tzinfo is None or observed > now or observed <= self._watch_started
                    or observed <= self._seen_before or snapshot.is_stale(now)):
                return
        except (AttributeError, TypeError, ValueError):
            return
        is_input = snapshot.status in _INPUT_STATES
        if is_input:
            notice_id = getattr(snapshot, "notice_id", None)
            if not isinstance(notice_id, str) or not notice_id:
                return
            key = ("input", notice_id)
        else:
            key = ("state", snapshot.source, snapshot.task_id, snapshot.turn_id, snapshot.status)
            if snapshot.status not in {"turn_ended", "phase_ended", "interrupted", "failed"}:
                key += (snapshot.observed_at,)
        if key in self._seen:
            return
        self._seen[key] = observed
        while len(self._seen) > 256:
            oldest = min(self._seen, key=lambda value: (self._seen[value], value))
            self._seen_before = max(self._seen_before, self._seen.pop(oldest))
        critical = is_input or snapshot.status == "needs_attention"
        if critical and len(self._queue) >= 4:
            ordinary = next((index for index in range(len(self._queue) - 1, -1, -1)
                             if not self._queue[index][0]), None)
            if ordinary is not None:
                del self._queue[ordinary]
        ordinary_waiting = sum(not urgent for urgent, _ in self._queue)
        if len(self._queue) >= 4 or not critical and ordinary_waiting >= 3:
            self._set_status("备用提醒待发队列已满，本条已丢弃，不会补发。")
            return
        if critical:
            self._queue.insert(sum(urgent for urgent, _ in self._queue), (True, text))
        else:
            self._queue.append((False, text))
        self._drain()

    def _drain(self):
        if not self._allowed() or not self._enabled or self._active is not None or not self._queue:
            return
        _, text = self._queue.popleft()
        self._set_status("正在提交新的 iPhone 备用提醒……")
        self._start(text)

    def set_tracking(self, enabled):
        if self._closed:
            return
        self._tracking = bool(enabled)
        self._watch_started = self._clock()
        if not self._tracking:
            self._invalidate()
            self._set_status("任务跟进已暂停，备用待发提醒已清空；恢复后不补发。")
        elif self._allowed():
            self._set_status("任务跟进已恢复，只等待之后的新事件。")

    def set_paused(self, paused):
        if self._closed:
            return
        self._paused = bool(paused)
        self._watch_started = self._clock()
        if self._paused:
            self._invalidate()
            self._set_status("免打扰中：备用请求暂停，恢复后不补发。")
        elif self._allowed():
            self._set_status("免打扰已解除，只等待之后的新事件。")

    def set_test_mode(self, enabled):
        if self._closed:
            return
        self._test_mode = bool(enabled)
        self._invalidate()
        self._set_status("测试模式中：备用提醒暂停。" if self._test_mode
                         else "已退出测试模式，只等待之后的新事件。")

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._invalidate()
        self._enabled, self._device_key = False, ""
        self.dialog.key.clear()
        self.dialog.hide()
        self.dialog.setEnabled(False)
        # Workers stay in the application's QObject tree. GracefulQuit waits
        # for them asynchronously; joining here would block its GUI timers.
