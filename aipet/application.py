"""Shared Qt application bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PyQt5.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QDialog,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QStyle,
)

from aipet.core.config import (
    AppSettings,
    PROJECT_ROOT,
    load_settings,
    save_settings,
    settings_file_exists,
)
from aipet.core.download_manager import DownloadManager
from aipet.core.runtime_logging import (
    configure_console_logging,
    get_logger,
    shutdown_console_logging,
)
from aipet.core.tts_service import (
    get_tts_service_manager,
    shutdown_tts_service,
)
from aipet.platforms import PlatformRuntime, get_platform_runtime
from aipet.ui.pet import Murasame
from aipet.ui.settings_dialog import SettingsDialog
from aipet.ui.shutdown import (
    GracefulQuit,
    cancel_owned_threads,
    freeze_settings,
    threads_busy,
    voice_busy,
)


logger = get_logger("main")


UI_TEXT = {
    "en": {
        "speech_unavailable": "Speech input unavailable",
        "install_voice": "Install requirements-voice.txt to enable it: {error}",
        "speech_failed": "Speech input failed",
        "recording": "Recording…",
        "recognizing": "Recognizing speech…",
        "type_message": "Type a message",
        "settings": "Settings…",
        "dnd": "Do Not Disturb",
        "vision": "Screen Vision",
        "clear": "Clear Conversation Memory",
        "exit": "Exit",
        "save_failed": "Settings save failed",
        "test_mode": "Test mode (separate memories)",
        "cancel_reply": "Cancel current reply",
    },
    "zh-CN": {
        "speech_unavailable": "语音输入不可用",
        "install_voice": "请安装 requirements-voice.txt：{error}",
        "speech_failed": "语音输入失败",
        "recording": "正在录音……",
        "recognizing": "正在识别……",
        "type_message": "输入对话",
        "settings": "设置…",
        "dnd": "勿扰模式",
        "vision": "屏幕视觉",
        "clear": "清除对话记忆",
        "exit": "退出",
        "save_failed": "设置保存失败",
        "test_mode": "测试模式（独立记忆）",
        "cancel_reply": "取消当前回复",
    },
}


def run_special_mode(argv: list[str] | None = None) -> int | None:
    args = sys.argv if argv is None else argv
    if len(args) < 2 or args[1] != "--log-viewer":
        return None
    if len(args) != 4:
        return 2
    try:
        parent_process_id = int(args[3])
    except ValueError:
        return 2

    get_platform_runtime().processes.follow_log_viewer(
        Path(args[2]),
        parent_process_id,
    )
    return 0


def ui_text(settings: AppSettings, key: str, **values: object) -> str:
    language = (
        settings.ui_language
        if settings.ui_language in UI_TEXT
        else "en"
    )
    text = UI_TEXT[language][key]
    return text.format(**values) if values else text


class VoiceBridge(QObject):
    text_ready = pyqtSignal(str, int)
    record_start = pyqtSignal(int)
    record_end = pyqtSignal(int)
    error = pyqtSignal(str, int)


def add_text_input_action(menu: QMenu, pet: Murasame, settings: AppSettings) -> QAction:
    """Reach the existing inline editor even when the Viewer hit area differs."""
    action = QAction(ui_text(settings, "type_message"), menu)
    action.triggered.connect(pet.begin_text_input)
    existing = menu.actions()
    menu.insertAction(existing[0] if existing else None, action)
    return action


def load_settings_safely() -> tuple[AppSettings, str | None]:
    try:
        return load_settings(), None
    except Exception as exc:
        return AppSettings(), str(exc)


def move_pet_to_configured_screen(
    app: QApplication,
    pet: Murasame,
    settings: AppSettings,
) -> None:
    screens = app.screens()
    display = settings.display
    screen = next(
        (
            candidate
            for candidate in screens
            if display.screen_name
            and candidate.name() == display.screen_name
        ),
        None,
    )
    index = display.screen_index
    screen = (
        screen
        or (
            screens[index]
            if 0 <= index < len(screens)
            else app.primaryScreen()
        )
    )
    if screen is not None:
        geometry = screen.availableGeometry()
        x = geometry.x()
        y = geometry.y()
        if display.window_x is not None and display.window_y is not None:
            x += display.window_x
            y += display.window_y
        pet.move(x, y)
        pet.remember_window_position()


def configure_voice_trigger(
    pet: Murasame,
    settings: AppSettings,
    tray_icon: QSystemTrayIcon,
    platform_runtime: PlatformRuntime | None = None,
) -> Any | None:
    if not settings.stt.enabled:
        return None

    bridge = VoiceBridge(pet)
    capture_epoch = {"value": getattr(pet, "_voice_input_epoch", 0)}
    def current(epoch):
        return (not getattr(pet, "_closing", False) and not pet.is_dnd_enabled()
                and epoch == getattr(pet, "_voice_input_epoch", 0))
    def recording_started():
        capture_epoch["value"] = getattr(pet, "_voice_input_epoch", 0)
        bridge.record_start.emit(capture_epoch["value"])
    def begin_recording(epoch):
        if not current(epoch):
            return
        # Recording is explicit user input. Flush the displayed reply prefix
        # before replacing it with status text; retain this accepted recording.
        pet._cancel_active_jobs(include_vision=True, preserve_voice_input=True)
        pet._reset_screen_observation()
        pet._voice_input_pending = True
        pet.input_mode = False
        pet.preedit_text = ""
        avatar = getattr(pet, "_avatar_activity", None)
        if callable(avatar):
            avatar("listening")
        pet.show_text(ui_text(settings, "recording"), typing=False,
                      speaker_name=settings.character.user_name)
    def accept_voice_text(text, epoch):
        if not current(epoch):
            return
        pet._voice_input_pending = False
        pet.start_thread(text, role="user", source="voice")
    def finish_failed_voice(_message, epoch):
        if not current(epoch):
            return
        pet._voice_input_pending = False
        restore = getattr(pet, "_restore_avatar_face", None)
        if callable(restore):
            restore()
    bridge.text_ready.connect(accept_voice_text)
    bridge.record_start.connect(begin_recording)
    bridge.record_end.connect(
        lambda epoch: pet.show_text(
            ui_text(settings, "recognizing"),
            typing=False,
            speaker_name=settings.character.user_name,
        ) if current(epoch) else None
    )
    bridge.record_end.connect(
        lambda epoch: pet._avatar_activity("thinking")
        if current(epoch) and callable(getattr(pet, "_avatar_activity", None)) else None
    )
    bridge.error.connect(
        lambda message, epoch: tray_icon.showMessage(
            ui_text(settings, "speech_failed"),
            message,
            QSystemTrayIcon.Warning,
        ) if current(epoch) else None
    )
    bridge.error.connect(finish_failed_voice)
    bridge.error.connect(
        lambda _message, epoch: pet.show_text(
            ui_text(settings, "speech_failed"),
            typing=False,
            speaker_name=settings.character.user_name,
        ) if current(epoch) else None
    )

    runtime = platform_runtime or get_platform_runtime()
    try:
        trigger = runtime.input.create_voice_trigger(
            on_text_ready=lambda text: bridge.text_ready.emit(text, capture_epoch["value"]),
            hold_seconds=2.0,
            on_record_start=recording_started,
            on_record_end=lambda: bridge.record_end.emit(capture_epoch["value"]),
            model_name=settings.stt.model,
            model_directory=settings.stt.model_dir,
            device=settings.stt.device,
            input_device=settings.stt.input_device,
            language=settings.stt.resolved_language(settings.ui_language),
            vocabulary_profile=settings.character.profile_id,
            on_error=lambda message: bridge.error.emit(message, capture_epoch["value"]),
        )
    except (ImportError, OSError) as exc:
        logger.exception("语音输入模块加载失败")
        tray_icon.showMessage(
            ui_text(settings, "speech_unavailable"),
            ui_text(settings, "install_voice", error=exc),
            QSystemTrayIcon.Warning,
        )
        return None

    try:
        if not pet.is_dnd_enabled():
            trigger.start()
    except Exception as exc:
        logger.exception("语音触发器启动失败")
        try:
            trigger.stop()
        except Exception:
            logger.exception("清理启动失败的语音触发器时出错")
        tray_icon.showMessage(
            ui_text(settings, "speech_failed"),
            str(exc),
            QSystemTrayIcon.Warning,
        )
        return None
    trigger._qt_bridge = bridge
    return trigger


def _clear_idle_speech_models() -> None:
    try:
        from aipet.core.stt import clear_model_cache
    except ImportError:
        return
    clear_model_cache()


def _run_until_shutdown(app: QApplication, coordinator: GracefulQuit) -> int:
    result = app.exec_()
    # Keep owners alive even if an external caller invokes quit()/exit()
    # directly, resuming events until the same safe drain completes.
    while not coordinator.completed:
        coordinator.request()
        app.exec_()
    return result


def _finish_startup_exit(app: QApplication, downloads: DownloadManager) -> None:
    """A cancelled setup can still own a model download or a settings check."""
    def owners():
        return [app, *app.topLevelWidgets()]

    def prepare():
        for window in app.topLevelWidgets():
            if isinstance(window, SettingsDialog):
                freeze_settings(window)
            if isinstance(window, Murasame):
                window._closing = True
            for timer in window.findChildren(QTimer):
                timer.stop()
            window.hide()
        cancel_owned_threads(owners())

    coordinator = GracefulQuit(
        app,
        prepare=prepare,
        busy=lambda: threads_busy(owners()),
        finalize_ui=downloads.shutdown,
        cleanup=(shutdown_tts_service,),
    )
    coordinator.request()
    _run_until_shutdown(app, coordinator)
    shutdown_console_logging()


def main() -> int:
    platform_runtime = get_platform_runtime()
    get_tts_service_manager(platform_runtime)
    app = QApplication(sys.argv)
    import os
    app.setApplicationName(os.getenv("AIPET_APPLICATION_NAME", "AIpet Murasame"))
    app.setFont(QFont("Segoe UI", 10))
    app.setQuitOnLastWindowClosed(False)
    download_manager = DownloadManager(
        app,
        platform_runtime=platform_runtime,
    )
    icon = QIcon(str(PROJECT_ROOT / "icon.png"))
    if icon.isNull():
        icon = app.style().standardIcon(QStyle.SP_ComputerIcon)
    app.setWindowIcon(icon)

    settings, load_error = load_settings_safely()
    configure_console_logging(settings.display.show_log_console)
    logger.info(
        "配置已加载 | 对话后端=%s | 视觉后端=%s",
        settings.mode,
        settings.vision.provider,
    )
    first_run = not settings_file_exists()
    if load_error:
        logger.error("读取配置失败：%s", load_error)
        QMessageBox.warning(
            None,
            "Invalid configuration",
            "The saved configuration could not be read. "
            f"Defaults will be shown instead.\n\n{load_error}",
        )
        first_run = True

    if first_run:
        setup = SettingsDialog(
            settings,
            first_run=True,
            download_manager=download_manager,
            platform_runtime=platform_runtime,
        )
        if setup.exec_() != QDialog.Accepted:
            _finish_startup_exit(app, download_manager)
            return 0
        settings = setup.result_settings()
        save_settings(settings)
        configure_console_logging(settings.display.show_log_console)

    try:
        pet = Murasame(settings, platform_runtime)
    except Exception as exc:
        logger.exception("AIpet 启动失败")
        QMessageBox.critical(
            None,
            "AIpet startup failed",
            str(exc),
        )
        _finish_startup_exit(app, download_manager)
        return 1
    pet.show()
    move_pet_to_configured_screen(app, pet, settings)
    if hasattr(pet, "place_live2d_overlay"):
        pet.place_live2d_overlay()

    tray_icon = QSystemTrayIcon(icon, app)
    tray_menu = QMenu()
    settings_action = QAction(tray_menu)
    dnd_action = QAction(tray_menu)
    dnd_action.setCheckable(True)
    dnd_action.setChecked(pet.is_dnd_enabled())
    screenshot_action = QAction(tray_menu)
    screenshot_action.setCheckable(True)
    screenshot_action.setChecked(pet.is_screenshot_enabled())
    clear_action = QAction(tray_menu)
    test_action = QAction(tray_menu)
    test_action.setCheckable(True)
    test_action.setVisible(settings.character.profile_id == "nene")
    cancel_action = QAction(tray_menu)
    exit_action = QAction(tray_menu)
    input_action = add_text_input_action(tray_menu, pet, settings)
    utility_action = QAction(tray_menu)
    utility_action.setVisible(settings.character.profile_id == "nene")
    workflow_action = QAction(tray_menu)
    workflow_action.setVisible(settings.character.profile_id == "nene")
    utility = utility_hotkey = None
    workflow = None
    workflow_weixin = None
    workflow_bark = None
    phone_router = None
    if settings.character.profile_id == "nene":
        from aipet.ui.utility_assistant import UtilityController
        from aipet.ui.utility_hotkey import UtilityHotkey
        from aipet.ui.workflow_assistant import WorkflowController
        from aipet.ui.weixin_notify import WeixinNotifyController
        from aipet.ui.bark_notify import BarkNotifyController
        from aipet.ui.phone_router import PhoneNotificationRouter
        utility = UtilityController(pet, parent=app)
        workflow = WorkflowController(pet, parent=app)
        workflow_weixin = WeixinNotifyController(pet, parent=app)
        workflow_bark = BarkNotifyController(pet, parent=app)
        phone_router = PhoneNotificationRouter(workflow_weixin, workflow_bark)
        workflow.notification_ready.connect(phone_router.enqueue)
        workflow.tracking_changed.connect(workflow_weixin.set_tracking)
        workflow.tracking_changed.connect(workflow_bark.set_tracking)
        workflow.dialog.weixin.clicked.connect(workflow_weixin.open)
        workflow.dialog.bark.clicked.connect(workflow_bark.open)
        workflow_weixin.status_changed.connect(workflow.dialog.weixin_status.setText)
        workflow.dialog.weixin_status.setText(workflow_weixin.status_text)
        workflow_bark.status_changed.connect(workflow.dialog.bark_status.setText)
        workflow.dialog.bark_status.setText(workflow_bark.status_text)
        # Controllers start in tracking mode. Preserve their useful loaded
        # binding/disabled status rather than replacing it with "resumed" on
        # ordinary startup. An initially paused workflow still gates both.
        if not workflow._sync_tracking():
            workflow_weixin.set_tracking(False)
            workflow_bark.set_tracking(False)
        workflow_action.triggered.connect(lambda: workflow.open())
        utility_hotkey = UtilityHotkey(parent=app)
        utility_action.triggered.connect(lambda: utility.open())
        utility_hotkey.activated.connect(lambda: utility.open(clipboard=True))
        utility_hotkey.unavailable.connect(lambda message: utility.set_hotkey_status(False, message))
        registered = utility_hotkey.start()
        utility.set_hotkey_status(registered)
        logger.info("Utility workspace initialized | hotkey_registered=%s | shortcut=Ctrl+Alt+N", registered)

    def apply_tray_language(current_settings: AppSettings) -> None:
        tray_icon.setToolTip(pet.windowTitle() or app.applicationName())
        input_action.setText(ui_text(current_settings, "type_message"))
        shortcut_hint = "  Ctrl+Alt+N" if utility_hotkey is not None and utility_hotkey.is_registered else ""
        utility_action.setText(("Ask / Read together…" if current_settings.ui_language == "en"
                               else "随手问与共同阅读…") + shortcut_hint)
        utility_action.setVisible(current_settings.character.profile_id == "nene")
        workflow_action.setText("AI task tracking…" if current_settings.ui_language == "en" else "AI任务跟进…")
        workflow_action.setVisible(current_settings.character.profile_id == "nene")
        settings_action.setText(ui_text(current_settings, "settings"))
        dnd_action.setText(ui_text(current_settings, "dnd"))
        screenshot_action.setText(ui_text(current_settings, "vision"))
        clear_action.setText(ui_text(current_settings, "clear"))
        test_action.setText(ui_text(current_settings, "test_mode"))
        test_action.setVisible(current_settings.character.profile_id == "nene")
        cancel_action.setText(ui_text(current_settings, "cancel_reply"))
        exit_action.setText(ui_text(current_settings, "exit"))

    apply_tray_language(settings)

    tray_menu.addAction(utility_action)
    tray_menu.addAction(workflow_action)
    tray_menu.addAction(settings_action)
    tray_menu.addSeparator()
    tray_menu.addAction(dnd_action)
    tray_menu.addAction(test_action)
    tray_menu.addAction(cancel_action)
    tray_menu.addAction(screenshot_action)
    tray_menu.addAction(clear_action)
    tray_menu.addSeparator()
    tray_menu.addAction(exit_action)
    tray_icon.setContextMenu(tray_menu)
    tray_icon.show()
    pet.setContextMenuPolicy(Qt.CustomContextMenu)
    pet.customContextMenuRequested.connect(
        lambda pos: tray_menu.popup(pet.mapToGlobal(pos))
    )
    logger.info(
        "Tray initialized | available=%s | icon_null=%s | tooltip=%s | visible=%s",
        QSystemTrayIcon.isSystemTrayAvailable(),
        tray_icon.icon().isNull(),
        tray_icon.toolTip(),
        tray_icon.isVisible(),
    )
    quitting = False
    pet.notification.connect(
        lambda title, message: tray_icon.showMessage(
            title,
            message,
            QSystemTrayIcon.Warning,
        ) if not quitting and not pet.is_dnd_enabled() else None
    )

    voice_trigger = configure_voice_trigger(
        pet,
        settings,
        tray_icon,
        platform_runtime,
    )
    retired_voice_triggers = []

    def pause_voice_input(paused: bool) -> None:
        if voice_trigger is None or quitting:
            return
        try:
            if paused:
                voice_trigger.stop()
            else:
                voice_trigger.start()
        except Exception:
            logger.warning("切换语音输入暂停状态失败", exc_info=False)
            if not paused:
                pet.notification.emit(ui_text(pet.settings, "speech_failed"),
                                      "语音输入未能恢复，可继续使用文字输入。")

    pet.pause_changed.connect(pause_voice_input)
    test_action.toggled.connect(pet.set_test_mode)
    cancel_action.triggered.connect(pet.cancel_conversation)
    def update_test_indicator(enabled: bool) -> None:
        test_action.blockSignals(True)
        test_action.setChecked(enabled)
        test_action.blockSignals(False)
        tray_icon.setToolTip(pet.windowTitle() or app.applicationName())
    pet.test_mode_changed.connect(update_test_indicator)

    def persist_pet_settings() -> None:
        pet.remember_window_position()
        try:
            save_settings(pet.settings_for_persistence())
        except OSError as exc:
            if quitting:
                logger.exception("退出时保存设置失败")
                return
            tray_icon.showMessage(
                ui_text(pet.settings, "save_failed"),
                str(exc),
                QSystemTrayIcon.Warning,
            )

    def set_screen_vision(enabled: bool) -> None:
        if quitting:
            return
        pet.set_screenshot_enabled(enabled)
        screenshot_action.blockSignals(True)
        screenshot_action.setChecked(pet.is_screenshot_enabled())
        screenshot_action.blockSignals(False)
        persist_pet_settings()

    def set_do_not_disturb(enabled: bool) -> None:
        if quitting:
            return
        pet.set_dnd_enabled(enabled)
        dnd_action.blockSignals(True)
        dnd_action.setChecked(pet.is_dnd_enabled())
        dnd_action.blockSignals(False)
        persist_pet_settings()

    screenshot_action.toggled.connect(set_screen_vision)
    dnd_action.toggled.connect(set_do_not_disturb)
    clear_action.triggered.connect(pet.clear_history)

    settings_dialog: SettingsDialog | None = None

    def apply_settings_dialog(dialog: SettingsDialog) -> None:
        nonlocal settings, voice_trigger
        if quitting:
            return
        if utility is not None:
            utility.cancel()
        settings = dialog.result_settings()
        try:
            save_settings(settings)
        except OSError as exc:
            QMessageBox.warning(
                None,
                ui_text(settings, "save_failed"),
                str(exc),
            )
            return

        configure_console_logging(settings.display.show_log_console)
        logger.info(
            "设置已应用 | 对话后端=%s | 视觉后端=%s",
            settings.mode,
            settings.vision.provider,
        )
        if voice_trigger is not None:
            voice_trigger.stop()
            if voice_busy(voice_trigger):
                retired_voice_triggers.append(voice_trigger)
        voice_trigger = configure_voice_trigger(
            pet,
            settings,
            tray_icon,
            platform_runtime,
        )
        pet.apply_settings(settings)
        apply_tray_language(settings)
        dnd_action.blockSignals(True)
        dnd_action.setChecked(pet.is_dnd_enabled())
        dnd_action.blockSignals(False)
        screenshot_action.blockSignals(True)
        screenshot_action.setChecked(settings.vision.enabled)
        screenshot_action.blockSignals(False)
        move_pet_to_configured_screen(app, pet, settings)
        if hasattr(pet, "place_live2d_overlay"):
            pet.place_live2d_overlay()

    def finish_settings_dialog(
        result: int,
        dialog: SettingsDialog,
    ) -> None:
        nonlocal settings_dialog
        if result == QDialog.Accepted:
            apply_settings_dialog(dialog)
        if settings_dialog is dialog:
            settings_dialog = None
        dispose_settings_dialog_when_idle(dialog)

    def dispose_settings_dialog_when_idle(
        dialog: SettingsDialog,
    ) -> None:
        if dialog._background_check_is_running():
            QTimer.singleShot(
                100,
                lambda current=dialog: (
                    dispose_settings_dialog_when_idle(current)
                ),
            )
            return
        dialog.deleteLater()

    def open_settings() -> None:
        nonlocal settings_dialog
        if quitting:
            return
        if settings_dialog is not None:
            settings_dialog.showNormal()
            settings_dialog.raise_()
            settings_dialog.activateWindow()
            return

        dialog = SettingsDialog(
            pet.settings,
            download_manager=download_manager,
            parent=None,
            platform_runtime=platform_runtime,
        )
        dialog.clear_history_requested.connect(pet.clear_history)
        settings_dialog = dialog
        dialog.finished.connect(
            lambda result, current=dialog: finish_settings_dialog(
                result,
                current,
            )
        )
        dialog.open()

    settings_action.triggered.connect(open_settings)

    def exit_owners():
        # topLevelWidgets includes hidden, previously closed settings dialogs
        # whose outstanding workers prevent their deferred deletion.
        return [app, pet, *app.topLevelWidgets()]

    def voice_triggers():
        return [*retired_voice_triggers, *([voice_trigger] if voice_trigger is not None else [])]

    def prepare_exit() -> None:
        nonlocal quitting
        quitting = True
        if utility_hotkey is not None:
            utility_hotkey.stop()
        if utility is not None:
            utility.shutdown()
        if phone_router is not None:
            phone_router.shutdown()
        if workflow is not None:
            workflow.shutdown()
        if workflow_weixin is not None:
            workflow_weixin.shutdown()
        if workflow_bark is not None:
            workflow_bark.shutdown()
        tray_icon.hide()
        tray_menu.setEnabled(False)
        pet.begin_shutdown()
        for window in app.topLevelWidgets():
            if isinstance(window, SettingsDialog):
                freeze_settings(window)
            window.setEnabled(False)
            window.hide()
        for trigger in voice_triggers():
            trigger.stop()
        cancel_owned_threads(exit_owners())

    def exit_busy() -> bool:
        return threads_busy(exit_owners()) or any(voice_busy(trigger) for trigger in voice_triggers())

    def finalize_exit_ui() -> None:
        persist_pet_settings()
        pet.shutdown()
        # Every download QThread has joined already; the legacy fallback's
        # wait/terminate branch is unreachable on this application exit path.
        download_manager.shutdown()

    coordinator = GracefulQuit(
        app,
        prepare=prepare_exit,
        busy=exit_busy,
        finalize_ui=finalize_exit_ui,
        cleanup=(shutdown_tts_service, _clear_idle_speech_models),
    )
    app.aboutToQuit.connect(
        lambda: shutdown_console_logging() if coordinator.completed else None
    )
    exit_action.triggered.connect(coordinator.request)
    if utility is not None and "--nene-open-tools" in sys.argv:
        QTimer.singleShot(0, lambda: utility.open())
    if workflow is not None and "--nene-open-workflow" in sys.argv:
        QTimer.singleShot(0, workflow.open)
    if workflow_weixin is not None and "--nene-open-weixin" in sys.argv:
        QTimer.singleShot(0, workflow_weixin.open)
    if workflow_bark is not None and "--nene-open-bark" in sys.argv:
        QTimer.singleShot(0, workflow_bark.open)
    if "--nene-smoke-ui" in sys.argv:
        pet.show_text("点击人物输入，回车发送。", typing=False)
        QTimer.singleShot(60_000, coordinator.request)
    return _run_until_shutdown(app, coordinator)

