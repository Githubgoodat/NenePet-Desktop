"""Opt-in direct WeChat task notices; QR login and every test are explicit.

This client accepts only the scanner's context from the separate core adapter.
It never turns phone messages into commands, approvals, or model prompts.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
from pathlib import Path
import sys
import threading

from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt5.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from aipet.core.config import PROJECT_ROOT, get_user_data_dir
from aipet.core.weixin_notify import (
    DEFAULT_BASE_URL, WeixinClient, WeixinConfig, WeixinNotifyError,
    load_config, logout, save_config,
)


_SOURCES = {"codex": "Codex", "grok_bot": "grok bot（协作上报）"}
_STATES = {
    "needs_attention": "需要你的确认，请回任务来源查看。",
    "turn_ended": "本轮结束，待查看；这不代表整个任务完成。",
    "phase_ended": "阶段结束，待查看；这不代表整个任务完成。",
    "interrupted": "本轮已中断，请回任务来源确认。",
    "failed": "协作方上报失败，请回任务来源核对。",
    "input_requested": "发起输入请求，请回 Codex 查看。",
    "question_opened": "发起提问，任务可能仍在继续，请回 Codex 查看。",
}
_INPUT_STATES = {"input_requested", "question_opened"}
_DEFINITIVE_FAILURES = {
    "api_rejected_minus2", "api_rejected", "session_expired", "context_missing",
    "credentials_missing", "context_refresh_required",
}
_ERRORS = {
    "credentials_missing": "请先通过本人微信扫码确认登录。",
    "context_missing": "缺少本人上下文。请本人先给机器人发一句文字，再点“建立本人上下文”。",
    "session_expired": "微信登录已过期，请重新扫码登录；没有自动重试或补发。",
    "context_refresh_required": "请本人先给机器人再发一句文字，然后重新建立上下文。",
    "api_rejected": "微信服务未受理；请核对登录和本人上下文，不会自动重试。",
    "api_rejected_minus2": "微信服务拒绝请求（-2），通知已暂停；原因尚不能确定。请先给机器人发一句话、重新建立本人上下文，再显式测试并启用。没有自动重试。",
    "network_error": "连接微信服务失败，请稍后显式重试；没有自动补发。",
    "bad_response": "微信服务返回了无法验证的结果，未认定发送成功。",
    "unsafe_url": "微信服务返回的地址未通过校验，已停止本次操作。",
    "config_error": "本机微信设置无法读取或安全保存，提醒保持关闭。",
    "acceptance_unknown": "服务未返回明确受理凭据，不能确认发送成功；请先自行核对手机，不自动重试。",
}


def _utc_now():
    return datetime.now(timezone.utc)


def notice_text(snapshot):
    source, state = _SOURCES.get(snapshot.source), _STATES.get(snapshot.status)
    if source is None or state is None or not isinstance(snapshot.task_id, str):
        return None
    if snapshot.status in _INPUT_STATES and snapshot.source != "codex":
        return None
    short_id = hashlib.sha256((snapshot.source + "\0" + snapshot.task_id).encode("utf-8")).hexdigest()[:10]
    return "宁宁 · AI任务提醒\n来源：" + source + "\n状态：" + state + "\n任务代号：" + short_id


def _load_qrcode():
    try:
        return importlib.import_module("qrcode")
    except ImportError:
        # Only the administrator-managed dependency directory in this project.
        vendor = PROJECT_ROOT / "vendor" / "weixin"
        if not (vendor / "qrcode" / "__init__.py").is_file():
            raise
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        return importlib.import_module("qrcode")


def qr_available():
    try:
        return callable(_load_qrcode().QRCode)
    except (ImportError, AttributeError, OSError):
        return False


def render_qr(content):
    """Encode login content locally; never fetch it or use an image website."""
    if not isinstance(content, str) or not 1 <= len(content) <= 8192:
        raise ValueError("Invalid QR content")
    qr = _load_qrcode().QRCode(border=4, box_size=1)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    if not 21 <= size <= 185 or any(len(row) != size for row in matrix):
        raise ValueError("Unsupported QR size")
    scale = max(1, min(6, 330 // size))
    image = QImage(size * scale, size * scale, QImage.Format_RGB32)
    image.fill(Qt.white)
    painter = QPainter(image)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.fillRect(x * scale, y * scale, scale, scale, QColor(Qt.black))
    painter.end()
    return QPixmap.fromImage(image)


class WeixinWorker(QThread):
    result_ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, operation, arguments=(), parent=None, *, client_factory=WeixinClient):
        super().__init__(parent)
        self.operation, self.arguments = operation, arguments
        self._factory = client_factory
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    def run(self):
        client = None
        try:
            if self._cancelled.is_set():
                return
            client = self._factory()
            if self._cancelled.is_set():
                return
            if self.operation == "qr":
                result = client.request_qr()
            elif self.operation == "poll":
                result = client.poll_qr(self.arguments[0], base_url=self.arguments[1])
            elif self.operation == "context":
                result = client.get_updates(self.arguments[0], cursor=self.arguments[1])
            elif self.operation == "send":
                result = client.send_notice(*self.arguments)
            else:
                return
            if not self._cancelled.is_set():
                self.result_ready.emit(result)
        except WeixinNotifyError as exc:
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


class WeixinDialog(QDialog):
    closing = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("宁宁 · 微信任务提醒")
        self.resize(580, 580)
        layout = QVBoxLayout(self)
        self.info = QLabel(
            "使用开源微信渠道直接发给扫码本人，不经过公众号推送。\n"
            "只发送任务来源、状态和哈希短代号，不发送项目名、路径或聊天正文，不执行手机消息。\n"
            "登录保存在本机时使用当前系统账户的安全存储。服务受理不等于手机已收到。"
        )
        self.info.setTextFormat(Qt.PlainText)
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        row = QHBoxLayout()
        self.login = QPushButton("扫码登录微信")
        self.check = QPushButton("检查扫码状态")
        self.stop = QPushButton("停止当前请求")
        for button in (self.login, self.check, self.stop):
            row.addWidget(button)
        layout.addLayout(row)
        self.qr = QLabel("仅点击“扫码登录微信”后请求二维码。")
        self.qr.setTextFormat(Qt.PlainText)
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setMinimumHeight(240)
        layout.addWidget(self.qr)
        self.context = QPushButton("已给机器人发消息，建立本人上下文")
        layout.addWidget(self.context)
        self.enabled = QCheckBox("启用微信任务状态提醒")
        layout.addWidget(self.enabled)
        row = QHBoxLayout()
        self.save = QPushButton("保存设置")
        self.test = QPushButton("发送一条测试提醒")
        self.logout = QPushButton("退出并清除本机微信登录")
        for button in (self.save, self.test, self.logout):
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.limit = QLabel("免打扰、停止任务跟进或退出后不再提交新提醒；已经在途的请求无法撤回。恢复后不补发。")
        self.limit.setTextFormat(Qt.PlainText)
        self.limit.setWordWrap(True)
        layout.addWidget(self.limit)

    def closeEvent(self, event):
        self.closing.emit()
        super().closeEvent(event)


class WeixinNotifyController(QObject):
    status_changed = pyqtSignal(str)
    automatic_notice_failed = pyqtSignal(object)

    def __init__(self, pet, parent=None, *, config_path=None, worker_factory=WeixinWorker,
                 clock=_utc_now, qr_renderer=render_qr, qr_check=qr_available, dialog=None):
        super().__init__(parent)
        self.pet = pet
        self.config_path = Path(config_path) if config_path is not None else get_user_data_dir() / "workflow-weixin.json"
        self.dialog = dialog or WeixinDialog()
        self._factory, self._clock = worker_factory, clock
        self._qr_renderer, self._qr_check = qr_renderer, qr_check
        self._closed = False
        self._paused = pet.is_dnd_enabled()
        self._test_mode = bool(getattr(pet, "is_test_mode", lambda: False)())
        self._tracking = True
        self._enabled = False
        self._credentials = None
        self._context = self._cursor = ""
        self._epoch = 0
        self._active = None
        self._active_auto_notice = None
        self._workers = set()
        self._queue = deque()
        self._last_sent = {}
        self._input_seen = {}
        self._input_seen_before = None
        self._input_watch_started = self._clock()
        self._qrcode = None
        self._qr_base = DEFAULT_BASE_URL
        self._qr_deadline = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2_000)
        self._poll_timer.timeout.connect(self.check_login)
        self.status_text = "微信提醒未启用；仅显式扫码登录或发送测试时访问服务。"
        try:
            config = load_config(self.config_path)
            self._credentials, self._context, self._cursor = config.credentials, config.context_token, config.cursor
            self._enabled = bool(config.enabled and self._credentials and self._context)
            if self._enabled:
                self.status_text = "微信提醒已启用，仅等待后续新的任务状态事件。"
        except Exception:
            self.status_text = "本机微信设置无法读取或解密，提醒保持关闭，请重新扫码设置。"
        d = self.dialog
        d.enabled.setChecked(self._enabled)
        d.login.clicked.connect(self.login)
        d.check.clicked.connect(self.check_login)
        d.stop.clicked.connect(self.cancel)
        d.context.clicked.connect(self.establish_context)
        d.save.clicked.connect(self.save_settings)
        d.test.clicked.connect(self.send_test)
        d.logout.clicked.connect(self.clear_login)
        d.closing.connect(self._dialog_closed)
        pet.pause_changed.connect(self.set_paused)
        pet.test_mode_changed.connect(self.set_test_mode)
        self._set_status(self.status_text)

    def _interactive_allowed(self):
        return (not self._closed and not self._paused and not self._test_mode and self._tracking
                and not getattr(self.pet, "_closing", False)
                and self.pet.settings.character.profile_id == "nene")

    def can_send_automatic(self):
        return bool(self._enabled and self._interactive_allowed()
                    and self._credentials is not None and self._context)

    def _set_status(self, text):
        self.status_text = text
        self.dialog.status.setText(text)
        self.status_changed.emit(text)

    def open(self):
        if self._closed or self._paused or getattr(self.pet, "_closing", False):
            return
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _start(self, operation, arguments, callback, *, auto_notice=None):
        if not self._interactive_allowed() or self._active is not None:
            return False
        epoch = self._epoch
        worker = self._factory(operation, arguments, parent=self)
        self._workers.add(worker)
        self._active = worker
        self._active_auto_notice = auto_notice
        worker.result_ready.connect(lambda value: callback(value) if epoch == self._epoch
                                    and self._interactive_allowed() else None)
        worker.error.connect(lambda code: self._job_error(operation, code)
                             if epoch == self._epoch and self._interactive_allowed() else None)
        worker.finished.connect(lambda: self._retire(worker))
        worker.start()
        return True

    def _job_error(self, operation, code):
        failed = []
        if operation in {"qr", "poll"}:
            self._poll_timer.stop()  # A failed poll requires an explicit retry.
        if code in _DEFINITIVE_FAILURES:
            if operation == "send" and self._active_auto_notice is not None:
                failed.append(self._active_auto_notice)
            failed.extend(record for _, _, record in self._queue)
            # Pause this runtime before routing failed automatic notices. Do
            # not guess that a timeout/unknown response was never delivered.
            self._enabled = False
            self._queue.clear()
            self._active_auto_notice = None
            self._epoch += 1
            self.dialog.enabled.setChecked(False)
        self._set_status(_ERRORS.get(code, _ERRORS["bad_response"]))
        if self._interactive_allowed():
            failed.sort(key=lambda record: record.status not in _INPUT_STATES
                        and record.status != "needs_attention")
            for record in failed:
                if not self._interactive_allowed():
                    break
                self.automatic_notice_failed.emit(record)

    def _retire(self, worker):
        self._workers.discard(worker)
        if self._active is worker:
            self._active = None
            self._active_auto_notice = None
        worker.deleteLater()
        self._drain()

    def _invalidate(self):
        self._epoch += 1
        self._input_watch_started = self._clock()
        self._queue.clear()
        self._active_auto_notice = None
        self._poll_timer.stop()
        self._qrcode = None
        self.dialog.qr.clear()
        if self._active is not None:
            self._active.cancel()

    def _dialog_closed(self):
        if self._closed:
            return
        interactive = self._active is not None and self._active.operation in {"qr", "poll", "context"}
        if self._qrcode or interactive:
            self._invalidate()
            self._set_status("设置窗口已关闭，扫码和上下文请求已停止；没有保存或启用新登录。")

    def cancel(self, *_args):
        if not self._closed:
            self._invalidate()
            self._set_status("当前请求已停止；已经在途的请求无法撤回，后续不补发。")

    def login(self):
        if not self._interactive_allowed():
            return
        if not self._qr_check():
            self._set_status("缺少二维码组件，请安装 requirements-weixin.txt；基础桌宠和任务跟进仍可使用。")
            return
        if self._active is not None:
            self._set_status("上一个请求尚未结束，请稍后重新点击扫码登录。")
            return
        self._invalidate()
        self._enabled = False
        self.dialog.enabled.setChecked(False)
        self._set_status("正在请求本次登录二维码……")
        self._start("qr", (), self._qr_ready)

    def _qr_ready(self, value):
        try:
            pixmap = self._qr_renderer(value.qrcode_img_content)
            self._qrcode = value.qrcode
            self._qr_base = DEFAULT_BASE_URL
            self._qr_deadline = self._clock() + timedelta(minutes=2)
            self.dialog.qr.setPixmap(pixmap)
        except Exception:
            self._set_status("二维码无法在本机绘制，请核对 requirements-weixin.txt 后显式重试。")
            return
        self._set_status("请用本人微信扫描并确认登录。二维码仅在本机绘制，不发送到外部生成网站。")
        self._poll_timer.start()

    def check_login(self):
        if not self._interactive_allowed() or not self._qrcode or self._active is not None:
            return
        if self._clock() >= self._qr_deadline:
            self._poll_timer.stop()
            self._qrcode = None
            self.dialog.qr.clear()
            self._set_status("本次扫码等待已结束，请重新点击扫码登录。")
            return
        self._start("poll", (self._qrcode, self._qr_base), self._login_checked)

    def _login_checked(self, value):
        if value.status in {"pending", "scanned"}:
            self._qr_base = value.poll_base_url
            self._set_status("已扫码，请在本人微信确认登录。" if value.status == "scanned" else "等待本人微信扫码确认……")
        elif value.status == "confirmed" and value.credentials is not None:
            self._credentials = value.credentials
            self._context = self._cursor = ""
            self._qrcode = None
            self._poll_timer.stop()
            self.dialog.qr.clear()
            self._set_status("已确认扫码。请本人给刚登录的机器人发一句文字，再点“建立本人上下文”。尚未启用提醒。")
        else:
            self._qrcode = None
            self._poll_timer.stop()
            self.dialog.qr.clear()
            self._set_status("本次登录需要重新验证或二维码已过期，请重新扫码。")

    def establish_context(self):
        if not self._interactive_allowed():
            return
        if self._credentials is None:
            self._set_status("请先通过本人微信扫码确认登录。")
            return
        self._set_status("仅核对扫码本人发来的消息上下文，不执行消息内容……")
        self._start("context", (self._credentials, self._cursor), self._context_ready)

    def _context_ready(self, value):
        self._cursor = value.cursor
        if value.context_token:
            self._context = value.context_token
            self._set_status("本人上下文已建立。可显式发送测试，或勾选启用后保存设置。")
        else:
            self._set_status("尚未收到扫码本人的新上下文。请本人给机器人发一句文字，再点击建立。")

    def save_settings(self):
        if self._closed:
            return
        enabled = self.dialog.enabled.isChecked()
        if enabled and (self._credentials is None or not self._context):
            self._set_status(_ERRORS["context_missing"])
            return
        self._invalidate()
        self._enabled = False
        try:
            save_config(self.config_path, WeixinConfig(enabled=enabled, credentials=self._credentials,
                                                      context_token=self._context, cursor=self._cursor))
        except Exception:
            self.dialog.enabled.setChecked(False)
            self._set_status("本机微信设置未能安全保存，提醒保持关闭；没有发送任何测试。")
            return
        self._enabled = enabled
        self._set_status("微信提醒设置已保存；只处理之后的新事件，不补发旧提醒。" if enabled else "微信提醒已关闭并保存。")

    def clear_login(self):
        if self._closed:
            return
        self._invalidate()
        self._enabled = False
        self._credentials = None
        self._context = self._cursor = ""
        self.dialog.enabled.setChecked(False)
        try:
            logout(self.config_path)
        except Exception:
            self._set_status("本次运行已关闭微信提醒，但本机登录文件未能移除，请检查目录权限。")
            return
        self._set_status("已清除本机微信登录和上下文。")

    def send_test(self):
        if not self._interactive_allowed():
            return
        if self._credentials is None or not self._context:
            self._set_status(_ERRORS["context_missing"])
            return
        if self._active is not None:
            self._set_status("上一个请求尚未结束，请稍后再次显式发送测试。")
            return
        self._set_status("正在提交你主动要求的测试提醒……")
        self._start("send", (self._credentials, self._context,
                             "宁宁 · 微信任务提醒测试\n这是你主动发送的测试消息，不包含任务正文。请确认本人手机是否收到。"),
                    self._sent)

    def _sent(self, value):
        self._set_status("微信服务已受理，请自行确认本人手机是否收到；受理不等于送达。" if value.accepted
                         else "微信服务未受理本次提醒，没有自动重试或补发。")

    def enqueue(self, snapshot):
        if not self._interactive_allowed() or not self._enabled:
            return
        now = self._clock()
        text = notice_text(snapshot)
        if text is None or snapshot.is_stale(now):
            return
        is_input = snapshot.status in _INPUT_STATES
        if is_input:
            notice_id = getattr(snapshot, "notice_id", None)
            if snapshot.source != "codex" or not isinstance(notice_id, str) or not notice_id:
                return
            try:
                observed = datetime.fromisoformat(snapshot.observed_at.replace("Z", "+00:00"))
                if observed.tzinfo is None or observed > now:
                    return
            except (AttributeError, TypeError, ValueError):
                return
            if notice_id in self._input_seen or (
                    observed <= self._input_watch_started or
                    self._input_seen_before is not None and observed <= self._input_seen_before):
                return
            # Remember even a queue-full drop: this event must not be replayed
            # later merely because a slot becomes available.
            self._input_seen[notice_id] = observed
            while len(self._input_seen) > 256:
                oldest = min(self._input_seen, key=lambda value: (self._input_seen[value], value))
                removed = self._input_seen.pop(oldest)
                self._input_seen_before = max(self._input_seen_before or removed, removed)
        else:
            key = (snapshot.source, snapshot.task_id, snapshot.status)
            last = self._last_sent.get(key)
            if last is not None and now - last < timedelta(seconds=30):
                return
        critical = is_input or snapshot.status == "needs_attention"
        ordinary_waiting = sum(not urgent for urgent, _, _ in self._queue)
        if critical and len(self._queue) >= 4:
            # A later input request may replace an ordinary pending update,
            # but never an already submitted request or another input request.
            ordinary = next((index for index in range(len(self._queue) - 1, -1, -1)
                             if not self._queue[index][0]), None)
            if ordinary is not None:
                del self._queue[ordinary]
        if len(self._queue) >= 4 or not critical and ordinary_waiting >= 3:
            self._set_status("微信提醒待发队列已满，本条已丢弃；不会之后补发。")
            return
        if not is_input:
            self._last_sent[key] = now
            if len(self._last_sent) > 256:
                self._last_sent.pop(min(self._last_sent, key=self._last_sent.get))
        if critical:
            # Keep FIFO among input/approval requests while placing them ahead
            # of pending lifecycle endings. Four waiting items remains the cap.
            position = sum(urgent for urgent, _, _ in self._queue)
            self._queue.insert(position, (True, text, snapshot))
        else:
            self._queue.append((False, text, snapshot))
        self._drain()

    def _drain(self):
        if (not self._interactive_allowed() or not self._enabled or self._active is not None
                or not self._queue or self._credentials is None or not self._context):
            return
        _, text, record = self._queue.popleft()
        self._set_status("正在提交新的任务状态提醒……")
        self._start("send", (self._credentials, self._context, text), self._sent, auto_notice=record)

    def set_tracking(self, enabled):
        if self._closed:
            return
        self._tracking = bool(enabled)
        self._input_watch_started = self._clock()
        if not self._tracking:
            self._invalidate()
            self._set_status("任务跟进已暂停，微信待发提醒已清空；已经在途的请求无法撤回。")
        elif self._interactive_allowed():
            self._set_status("任务跟进已恢复；只等待之后的新事件，不补发旧提醒。")

    def set_paused(self, paused):
        if self._closed:
            return
        self._paused = bool(paused)
        self._input_watch_started = self._clock()
        if self._paused:
            self._invalidate()
            self._set_status("免打扰中：暂停微信请求并清空待发提醒；已经在途的请求无法撤回。")
        elif self._interactive_allowed():
            self._set_status("免打扰已解除；只等待之后的新事件，不补发旧提醒。")

    def set_test_mode(self, enabled):
        if self._closed:
            return
        self._test_mode = bool(enabled)
        self._invalidate()
        self._set_status("测试模式中：暂停微信任务提醒，恢复后不补发。" if self._test_mode
                         else "已退出测试模式；只等待之后的新事件，不补发旧提醒。")

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._invalidate()
        self._credentials = None
        self._context = self._cursor = ""
        self._input_seen.clear()
        self.dialog.hide()
        self.dialog.setEnabled(False)
