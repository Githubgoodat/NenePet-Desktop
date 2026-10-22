"""Explicit, one-shot Qt region selection; no image is written to disk."""
from __future__ import annotations

from PyQt5.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QWidget


MAX_DESKTOP_PIXELS = 40_000_000
MAX_REGION_PIXELS = 16_000_000


def desktop_snapshot() -> tuple[QPixmap, QRect]:
    screens = QGuiApplication.screens()
    if not screens:
        raise ValueError("未找到可截图的显示器。")
    bounds = QRect()
    for screen in screens:
        bounds = bounds.united(screen.geometry())
    if bounds.width() * bounds.height() > MAX_DESKTOP_PIXELS:
        raise ValueError("桌面范围过大，请改用剪贴板图片。")
    snapshot = QPixmap(bounds.size())
    snapshot.fill(Qt.black)
    painter = QPainter(snapshot)
    try:
        for screen in screens:
            frame = screen.grabWindow(0)
            if frame.isNull():
                raise ValueError("当前显示器无法截图，请改用剪贴板文字或图片。")
            target = QRect(screen.geometry())
            target.translate(-bounds.topLeft())
            painter.drawPixmap(target, frame, frame.rect())
    finally:
        painter.end()
    return snapshot, bounds


class RegionCapture(QWidget):
    captured = pyqtSignal(QImage)
    cancelled = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, snapshot: QPixmap, geometry: QRect, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._snapshot = snapshot
        self._origin = None
        self._selection = QRect()
        self._completed = False
        self.setGeometry(geometry)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setWindowModality(Qt.ApplicationModal)
        self.setAttribute(Qt.WA_DeleteOnClose)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._snapshot)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if not self._selection.isEmpty():
            painter.drawPixmap(self._selection, self._snapshot, self._selection)
            painter.setPen(QPen(QColor("#b6dcff"), 2))
            painter.drawRect(self._selection)
        painter.setPen(Qt.white)
        painter.drawText(QPoint(24, 36), "拖动框选文字或报错区域 · Esc 取消 · 选区仅暂存在内存")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._origin = event.pos()
            self._selection = QRect()

    def mouseMoveEvent(self, event):
        if self._origin is not None:
            self._selection = QRect(self._origin, event.pos()).normalized().intersected(self.rect())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._origin is None:
            return
        self._selection = QRect(self._origin, event.pos()).normalized().intersected(self.rect())
        self._origin = None
        if self._selection.width() < 8 or self._selection.height() < 8:
            self.update()
            return
        if self._selection.width() * self._selection.height() > MAX_REGION_PIXELS:
            self._completed = True
            self.failed.emit("选区过大，请选择较小的文字区域。")
        else:
            self._completed = True
            image = self._snapshot.copy(self._selection).toImage()
            self.captured.emit(image)
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        if not self._completed:
            self.cancelled.emit()
        self._completed = True
        self._snapshot = QPixmap()
        super().closeEvent(event)
