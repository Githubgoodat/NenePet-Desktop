"""Local task-event display; never submits jobs, calls models or stores chat.

Events are evidence of a lifecycle transition, not proof that work succeeded.
The producer is installed separately. This reader never creates its inbox.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from aipet.core.config import get_cache_dir
from aipet.core.workflow_events import load_snapshots


_STATUS_TEXT = {
    "running": "最近事件：正在处理",
    "needs_attention": "需要你回任务来源确认",
    "turn_ended": "本轮结束，待查看",
    "phase_ended": "阶段结束，待查看",
    "interrupted": "本轮已中断",
    "failed": "来源上报失败，待查看",
    "session_ended": "会话已结束",
}
_NOTICE_TEXT = {
    "needs_attention": "需要你的确认，请回任务来源查看。",
    "turn_ended": "本轮结束了，请回任务来源查看结果；这还不能证明任务完成。",
    "phase_ended": "阶段结束了，请回任务来源查看结果；这还不能证明任务完成。",
    "interrupted": "本轮已中断，请回任务来源确认是否继续。",
    "failed": "协作方上报失败，请回任务来源核对。",
}
_SOURCE_TEXT = {"codex": "Codex", "grok_bot": "grok bot（协作上报）"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _observed(snapshot) -> datetime | None:
    try:
        value = datetime.fromisoformat(snapshot.observed_at.replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo is not None else None
    except (AttributeError, ValueError, TypeError):
        return None


def _display(value, limit=70) -> str:
    return " ".join(str(value).split())[:limit]


class WorkflowDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("宁宁 · AI任务跟进")
        self.resize(880, 420)
        layout = QVBoxLayout(self)
        self.description = QLabel(
            "跟进本机已接入的任务状态，不读取聊天正文或执行命令；微信提醒需单独扫码启用。\n"
            "“本轮结束”表示来源停止了这一轮，不代表测试通过或整个任务完成。"
        )
        self.description.setTextFormat(Qt.PlainText)
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        row = QHBoxLayout()
        self.enabled = QCheckBox("跟进本机任务状态")
        self.enabled.setChecked(True)
        self.refresh = QPushButton("刷新状态")
        self.weixin = QPushButton("微信提醒…")
        row.addWidget(self.enabled)
        row.addStretch()
        row.addWidget(self.refresh)
        row.addWidget(self.weixin)
        layout.addLayout(row)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("来源", "项目", "会话", "最近状态", "事件时间"))
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for column, width in ((0, 64), (1, 170), (2, 130), (4, 145)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)
        self.sources = QLabel()
        self.sources.setTextFormat(Qt.PlainText)
        self.sources.setWordWrap(True)
        layout.addWidget(self.sources)
        self.inbox_path = QLabel()
        self.inbox_path.setTextFormat(Qt.PlainText)
        self.inbox_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.inbox_path.setWordWrap(True)
        layout.addWidget(self.inbox_path)
        self.weixin_status = QLabel("微信提醒：未启用。")
        self.weixin_status.setTextFormat(Qt.PlainText)
        self.weixin_status.setWordWrap(True)
        layout.addWidget(self.weixin_status)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)


class WorkflowController(QObject):
    """Read bounded local snapshots and deduplicate notices only in RAM."""

    notification_ready = pyqtSignal(object)
    tracking_changed = pyqtSignal(bool)

    def __init__(self, pet, parent=None, *, inbox=None, clock=_utc_now, loader=load_snapshots, dialog=None):
        super().__init__(parent)
        self.pet = pet
        self.inbox = Path(inbox) if inbox is not None else get_cache_dir() / "workflow-inbox"
        self.dialog = dialog or WorkflowDialog()
        self._clock, self._loader = clock, loader
        self._enabled = True
        self._paused = pet.is_dnd_enabled()
        self._closed = False
        self._snapshots = {}
        self._baseline_required = True
        self._watch_started = self._clock()
        self._read_error = False
        self._last_tracking = None
        self.timer = QTimer(self)
        self.timer.setInterval(3_000)
        self.timer.timeout.connect(self.poll)
        self.dialog.enabled.toggled.connect(self.set_enabled)
        self.dialog.refresh.clicked.connect(self.poll)
        pet.pause_changed.connect(self.set_paused)
        pet.test_mode_changed.connect(self._reset_baseline)
        if not self._paused:
            self.poll()
            self.timer.start()
        else:
            self._render()
        self._sync_tracking()

    def _allowed(self):
        return (not self._closed and self._enabled and not self._paused
                and not getattr(self.pet, "_closing", False)
                and self.pet.settings.character.profile_id == "nene")

    def _sync_tracking(self):
        active = bool(self._allowed())
        if active != self._last_tracking:
            self._last_tracking = active
            self.tracking_changed.emit(active)
        return active

    def open(self):
        if self._closed or self._paused or getattr(self.pet, "_closing", False):
            return
        self.poll()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def poll(self):
        if not self._sync_tracking():
            self._baseline_required = True
            return
        now = self._clock()
        if self._baseline_required:
            self._watch_started = now
        try:
            records = tuple(self._loader(self.inbox, now=now))[:128]
        except (OSError, ValueError):
            self._read_error = True
            self._baseline_required = True
            self._render()
            return
        self._read_error = False
        current = {}
        for record in records:
            if record.source not in _SOURCE_TEXT or record.status not in _STATUS_TEXT:
                continue
            observed = _observed(record)
            if observed is None or observed > now:
                continue
            key = (record.source, record.task_id)
            previous = self._snapshots.get(key)
            # Do not turn a newer displayed state back into an older one if a
            # delayed producer or filesystem view delivers events out of order.
            if previous is not None and observed <= _observed(previous):
                current[key] = previous
                continue
            current[key] = record
            if (not self._baseline_required and observed > self._watch_started
                    and record.status in _NOTICE_TEXT and not record.is_stale(now)):
                self.pet.notification.emit(
                    "宁宁 · AI任务跟进",
                    _SOURCE_TEXT[record.source] + " · " + (_display(record.project) or "未命名项目") + "：" + _NOTICE_TEXT[record.status],
                )
                if self._allowed():
                    self.notification_ready.emit(record)
        self._snapshots = current
        self._baseline_required = False
        self._render(now)

    def _render(self, now=None):
        now = now or self._clock()
        d = self.dialog
        records = sorted(self._snapshots.values(), key=lambda item: item.observed_at, reverse=True)
        d.table.setRowCount(len(records))
        for row, record in enumerate(records):
            observed = _observed(record)
            state = _STATUS_TEXT[record.status]
            if self._read_error:
                state = "来源暂不可读；" + state
            elif record.is_stale(now):
                state = "状态已过期，请回来源确认"
            values = (_SOURCE_TEXT[record.source], _display(record.project) or "未命名项目", _display(record.task_id, 14),
                      state, observed.astimezone().strftime("%m-%d %H:%M:%S") if observed else "未知")
            for column, text in enumerate(values):
                d.table.setItem(row, column, QTableWidgetItem(text))
        codex = ("已收到本机状态事件（不等于完整任务验收）"
                 if any(record.source == "codex" for record in records) else "尚未连接或尚无事件")
        grok = ("已收到协作上报（非原生订阅；以事件时间为准）"
                if any(record.source == "grok_bot" for record in records)
                else "未连接或尚无协作上报（非原生订阅）")
        d.sources.setText("Codex：" + codex + "\ngrok bot：" + grok)
        d.inbox_path.setText("本机收件目录：" + str(self.inbox))
        d.refresh.setEnabled(self._allowed())
        if self._closed:
            message = "已停止任务跟进。"
        elif self._paused:
            message = "免打扰中：暂停读取和通知，恢复后不补发旧事件。"
        elif not self._enabled:
            message = "任务跟进已暂停；恢复后从当前状态重新开始。"
        elif self._read_error:
            message = "无法读取本机状态收件箱；当前来源状态未知。"
        else:
            message = "每 3 秒只读刷新。接入步骤见 docs/WORKFLOW.md；Codex hooks 需先审阅并信任。"
        d.status.setText(message)

    def _reset_baseline(self, *_args):
        if self._closed:
            return
        self._baseline_required = True
        self._watch_started = self._clock()
        self._sync_tracking()
        if self._allowed():
            self.poll()
        else:
            self._render()

    def set_paused(self, paused):
        if self._closed:
            return
        self._paused = bool(paused)
        self.timer.stop()
        self._reset_baseline()
        if self._allowed():
            self.timer.start()

    def set_enabled(self, enabled):
        if self._closed:
            return
        self._enabled = bool(enabled)
        self.dialog.enabled.blockSignals(True)
        self.dialog.enabled.setChecked(self._enabled)
        self.dialog.enabled.blockSignals(False)
        self.timer.stop()
        self._reset_baseline()
        if self._allowed():
            self.timer.start()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._sync_tracking()
        self.timer.stop()
        self._snapshots.clear()
        self.dialog.hide()
        self.dialog.setEnabled(False)
