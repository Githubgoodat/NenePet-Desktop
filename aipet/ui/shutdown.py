"""Keep Qt owners alive while cancelled background work drains on exit."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PyQt5.QtCore import QObject, QThread, QTimer

from aipet.core.runtime_logging import get_logger


logger = get_logger("shutdown")


def owned_threads(owners: Iterable[QObject]) -> list[QThread]:
    """Include hidden settings dialogs and their worker children exactly once."""
    threads = {}
    for owner in owners:
        try:
            children = owner.findChildren(QThread)
        except RuntimeError:  # A previously closed dialog was deleteLater'd.
            continue
        for thread in children:
            threads[id(thread)] = thread
    return list(threads.values())


def threads_busy(owners: Iterable[QObject]) -> bool:
    for thread in owned_threads(owners):
        try:
            if thread.isRunning() or not thread.wait(0):
                return True
        except RuntimeError:  # Qt already disposed a completed worker.
            continue
    return False


def cancel_owned_threads(owners: Iterable[QObject]) -> None:
    for thread in owned_threads(owners):
        try:
            cancel = getattr(thread, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:
                    logger.exception("请求取消后台任务失败，将等待它自然结束")
            thread.requestInterruption()
        except RuntimeError:
            continue


def voice_busy(trigger) -> bool:
    check = getattr(trigger, "is_busy", None)
    # Older triggers have no query. An unconfigured Mock returns a Mock;
    # accept the documented boolean result rather than treating it as busy.
    return callable(check) and check() is True


def freeze_settings(dialog) -> None:
    """Stop queued follow-up checks without destroying their active workers."""
    dialog._closing = True
    dialog._model_fetch_queue.clear()
    dialog._pending_whisper_model = None
    dialog._pending_tts_check = False
    dialog.setEnabled(False)
    for timer in dialog.findChildren(QTimer):
        timer.stop()
    dialog.hide()


class _CleanupThread(QThread):
    def __init__(self, callbacks: Iterable[Callable[[], None]], parent):
        super().__init__(parent)
        self.callbacks = tuple(callbacks)
        self.errors: list[str] = []

    def run(self) -> None:
        for callback in self.callbacks:
            try:
                callback()
            except Exception as exc:
                self.errors.append(str(exc))
                logger.exception("退出资源清理失败")


class GracefulQuit(QObject):
    """Hide immediately, drain work via timers, then clean up and request Quit.

    No running QThread is destroyed or terminated. The background process can
    remain until an outstanding request reaches its own timeout, but its Qt
    event loop keeps processing completion signals and the desktop stays free.
    """

    def __init__(
        self,
        application,
        *,
        prepare: Callable[[], None],
        busy: Callable[[], bool],
        finalize_ui: Callable[[], None],
        cleanup: Iterable[Callable[[], None]],
        finish: Callable[[], None] | None = None,
        interval_ms: int = 50,
    ):
        super().__init__(application)
        self.application = application
        self.prepare = prepare
        self.busy = busy
        self.finalize_ui = finalize_ui
        self.cleanup = tuple(cleanup)
        self.finish = finish or application.quit
        self.requested = False
        self.completed = False
        self._cleanup_thread: _CleanupThread | None = None
        self._busy_error_logged = False
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

    def request(self) -> None:
        if self.requested:
            return
        self.requested = True
        logger.info("退出已请求，后台任务正在取消")
        try:
            self.prepare()
        except Exception:
            # For example, a device can fail while its stream is being closed.
            # Preserve owners and continue draining instead of escaping a slot.
            logger.exception("退出准备未完全完成，继续等待后台任务")
        self._timer.start()
        # Never run even resource-cleanup setup in the requesting UI handler.
        QTimer.singleShot(0, self._poll)

    def _poll(self) -> None:
        if self.completed:
            return
        if self._cleanup_thread is None:
            try:
                if self.busy():
                    return
            except Exception:
                if not self._busy_error_logged:
                    logger.exception("无法确认后台任务是否已结束，保留退出等待")
                    self._busy_error_logged = True
                return
            try:
                self.finalize_ui()
            except Exception:
                logger.exception("退出时保存状态失败")
            self._cleanup_thread = _CleanupThread(self.cleanup, self)
            self._cleanup_thread.start()
            return
        if self._cleanup_thread.isRunning() or not self._cleanup_thread.wait(0):
            return
        self._timer.stop()
        self._cleanup_thread.deleteLater()
        self.completed = True
        logger.info("后台任务和自有资源已清理，退出 Qt")
        self.finish()
