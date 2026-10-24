"""Entry point for a friend-distributable PyInstaller onedir Windows build."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

# Required when running this file as a script from its packaging directory.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from share_support import (
    apply_viewer_settings, apply_voice_choice, configure_environment, discover_viewers,
    fresh_settings, interactive_process_options, launch_viewer, normalize_viewer_root,
    parse_coordinates, portable_roots, read_viewer_settings, self_test,
    write_viewer_settings, ExternalSubprocessProxy,
)


def _write_report(report: dict, path: str | None) -> int:
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if path:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
    elif sys.stdout is not None:
        print(encoded)
    return 0 if report.get("success") else 2


def _setup(base: Path, resources: Path, profile: Path) -> int:
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
        QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
        QMessageBox, QPushButton, QVBoxLayout,
    )
    from aipet.core.config import load_settings, save_settings
    app = QApplication(sys.argv)
    app.setApplicationName("宁宁桌宠 · 分享版首次设置")
    app.setQuitOnLastWindowClosed(True)
    path = profile / "config.json"
    try:
        settings = load_settings(path) if path.is_file() else fresh_settings(base, resources)
        viewer_config = read_viewer_settings(profile)
    except Exception as exc:
        QMessageBox.critical(None, "配置无法读取", "现有配置未被覆盖。请先备份 data/profile，再排查损坏的配置文件。\n"
                             + type(exc).__name__)
        return 2

    dialog = QDialog()
    dialog.setWindowTitle("宁宁桌宠 · 首次设置")
    dialog.setMinimumWidth(640)
    layout = QVBoxLayout(dialog)
    heading = QLabel("请先通过 Steam 安装并拥有 Live2DViewerEX，开启桌面模式与 ExAPI。\n"
                     "分享包不包含 Steam、Viewer 授权或对话 API。API 请求会产生你自己账号的费用。")
    heading.setWordWrap(True)
    layout.addWidget(heading)
    form = QFormLayout()
    viewer_row = QHBoxLayout()
    discovered = discover_viewers(base)
    viewer_path = QLineEdit(str(viewer_config.get("viewer_root") or (discovered[0] if discovered else "")))
    browse = QPushButton("浏览…")
    def choose_viewer():
        selected = QFileDialog.getExistingDirectory(dialog, "选择含 launcher.exe 的 Live2DViewerEX 安装目录", viewer_path.text())
        if selected:
            viewer_path.setText(selected)
    browse.clicked.connect(choose_viewer)
    viewer_row.addWidget(viewer_path)
    viewer_row.addWidget(browse)
    form.addRow("Viewer 安装目录", viewer_row)
    provider = QComboBox()
    for text, code in (("DeepSeek", "deepseek"), ("OpenAI", "openai"), ("阿里云百炼", "aliyun")):
        provider.addItem(text, code)
    provider.setCurrentIndex(max(0, provider.findData(settings.api.provider)))
    form.addRow("对话服务", provider)
    api_key = QLineEdit()
    api_key.setEchoMode(QLineEdit.Password)
    form.addRow("自己的 API Key", api_key)
    endpoint = QLineEdit()
    model = QLineEdit()
    previous = {"provider": settings.api.provider}
    values = {name: {"key": getattr(settings.api, name + "_api_key"),
                     "endpoint": getattr(settings.api, name + "_base_url"),
                     "model": getattr(settings.api, name + "_chat_model")}
              for name in ("deepseek", "openai", "aliyun")}
    def load_provider():
        value = values[provider.currentData()]
        api_key.setText(value["key"])
        endpoint.setText(value["endpoint"])
        model.setText(value["model"])
    def change_provider():
        values[previous["provider"]] = {"key": api_key.text(), "endpoint": endpoint.text(), "model": model.text()}
        previous["provider"] = provider.currentData()
        load_provider()
    load_provider()
    provider.currentIndexChanged.connect(change_provider)
    form.addRow("API 地址", endpoint)
    form.addRow("模型名称", model)
    voice = QComboBox()
    voice.addItem("关闭语音（默认，可直接文字聊天）", "")
    for name, label in (("nene-cpu.json", "宁宁日语 · CPU（较慢）"),
                        ("nene-cuda.json", "宁宁日语 · NVIDIA CUDA（需兼容显卡）")):
        candidate = base / "voices" / name
        if candidate.is_file():
            voice.addItem(label, str(candidate))
            if settings.tts.enabled and Path(settings.tts.voice_profile).name == name:
                voice.setCurrentIndex(voice.count() - 1)
    form.addRow("可选本地语音", voice)
    layout.addLayout(form)
    note = QLabel("本分享版不包含麦克风识别组件；屏幕视觉默认关闭。API Key 会保存在本机 data/profile/config.json，"
                  "再次分享时请只发原始压缩包。请勿把使用后的 data 目录发给别人。")
    note.setWordWrap(True)
    layout.addWidget(note)
    advanced = QGroupBox("人物范围校准（首次可留空；不会修改其他 Viewer 槽位）")
    advanced.setCheckable(True)
    advanced.setChecked(False)
    advanced_form = QFormLayout(advanced)
    bbox = QLineEdit(str(viewer_config.get("bbox") or ""))
    bbox.setPlaceholderText("Qt 逻辑像素：x,y,宽,高；空=按屏幕缩放的右下默认范围")
    position = QLineEdit(str(viewer_config.get("position") or ""))
    position.setPlaceholderText("经 Viewer 校准的 ExAPI X,Y；不能直接填人物范围的坐标")
    managed = QCheckBox("按上述 ExAPI 坐标管理位置（支持人物与字幕中键同步拖动）")
    managed.setChecked(viewer_config.get("managed_position") is True)
    advanced_form.addRow("身体点击范围", bbox)
    advanced_form.addRow("模型位置", position)
    advanced_form.addRow(managed)
    layout.addWidget(advanced)
    buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Save).setText("保存设置")
    buttons.button(QDialogButtonBox.Cancel).setText("取消")
    buttons.rejected.connect(dialog.reject)
    def save():
        try:
            root = normalize_viewer_root(Path(viewer_path.text().strip()))
            if root is None:
                raise ValueError("请选择已安装的 Live2DViewerEX 目录，其中应有 launcher.exe 和 bin 文件夹。")
            if not api_key.text().strip():
                raise ValueError("请输入你自己的 API Key；需要由该账号提供可用额度。")
            if not endpoint.text().strip().lower().startswith(("https://", "http://")):
                raise ValueError("API 地址应以 https:// 或 http:// 开头。")
            if not model.text().strip():
                raise ValueError("请填写服务商提供的模型名称。")
            parse_coordinates(bbox.text(), 4)
            parse_coordinates(position.text(), 2)
            selection = {"schema_version": 1, "viewer_root": str(root),
                         "bbox": bbox.text().strip(), "position": position.text().strip(),
                         "managed_position": managed.isChecked()}
            apply_viewer_settings(selection)
            candidate = settings.model_copy(deep=True)
            candidate.mode = "api"
            selected_provider = provider.currentData()
            candidate.api.provider = selected_provider
            setattr(candidate.api, selected_provider + "_api_key", api_key.text().strip())
            setattr(candidate.api, selected_provider + "_base_url", endpoint.text().strip())
            setattr(candidate.api, selected_provider + "_chat_model", model.text().strip())
            selected_voice = voice.currentData()
            apply_voice_choice(candidate, base, selected_voice)
            if not path.is_file() and app.primaryScreen() is not None:
                candidate.display.screen_name = app.primaryScreen().name()
            # Explicitly accepted user inputs are the only configuration writes.
            save_settings(candidate, path)
            write_viewer_settings(profile, selection)
            dialog.accept()
        except Exception as exc:
            # Never display a credential or the API request body in an error.
            message = str(exc) if isinstance(exc, (ValueError, OSError)) else type(exc).__name__
            QMessageBox.warning(dialog, "设置未完成", message)
    buttons.accepted.connect(save)
    layout.addWidget(buttons)
    return 0 if dialog.exec_() == QDialog.Accepted else 1


def _lock(profile: Path):
    from PyQt5.QtCore import QLockFile
    lock = QLockFile(str(profile / "nene.lock"))
    if lock.tryLock(100):
        return lock
    from PyQt5.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.information(None, "宁宁桌宠", "这个分享版已经运行或正在退出。请从托盘打开设置，或等待退出后再启动。")
    return None


def _ui_smoke(base: Path, resources: Path, report_path: str | None) -> int:
    """Finite, explicit Qt/tray test. Does not start the pet, Viewer or a backend."""
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication, QLabel, QSystemTrayIcon
    app = QApplication(sys.argv)
    window = QLabel("宁宁分享版 · 界面组件检查（不会调用对话或 Viewer）")
    window.setWindowTitle("宁宁分享版 · 界面检查")
    window.resize(520, 100)
    window.show()
    QTimer.singleShot(500, app.quit)
    app.exec_()
    return _write_report({"success": True, "scope": "qt_components_only",
                          "tray_available": QSystemTrayIcon.isSystemTrayAvailable(),
                          "profile_read": False, "profile_written": False,
                          "viewer_started": False, "network_used": False}, report_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doctor", action="store_true", help="仅离线检查源码准备状态，不读取用户配置或启动程序")
    parser.add_argument("--viewer-root", type=Path, help="仅与 --doctor 配合，检查显式指定的 Viewer 安装结构")
    parser.add_argument("--share-setup", action="store_true")
    parser.add_argument("--share-setup-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--share-self-test", action="store_true")
    parser.add_argument("--share-ui-smoke", action="store_true")
    parser.add_argument("--share-voice-check", action="store_true")
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--report")
    # Preserve the app's bundled log viewer special entry point.
    args, rest = parser.parse_known_args(argv)
    if args.viewer_root is not None and not args.doctor:
        parser.error("--viewer-root 只能与 --doctor 配合使用")
    if args.doctor:
        if (rest or args.share_setup or args.share_setup_child or args.share_self_test
                or args.share_ui_smoke or args.share_voice_check or args.engine_root is not None):
            parser.error("--doctor 只能与 --viewer-root 和 --report 配合使用")
        from share_doctor import diagnose, write_report
        base, resources = portable_roots()
        try:
            return write_report(diagnose(resources, base=base, viewer_root=args.viewer_root),
                                Path(args.report) if args.report else None)
        except (ValueError, OSError):
            if sys.stderr is not None:
                print("无法写入诊断报告：请使用已有普通目录内的新 .json 文件，并避开配置、素材与引擎目录。", file=sys.stderr)
            return 2
    if args.share_setup_child and not args.share_setup:
        parser.error("内部设置参数只能用于首次设置")
    if args.share_voice_check and (not args.engine_root or not args.report):
        parser.error("语音验收需要 --engine-root DIR 和一个未存在的 --report JSON")
    base, resources = portable_roots()
    profile = configure_environment(base, resources, create=False)
    if args.share_voice_check:
        from share_voice_check import run_voice_check
        try:
            result = run_voice_check(base, resources, args.engine_root, Path(args.report))
        except Exception as exc:
            # CLI validation failures must not open a fallback Qt dialog or
            # create the normal application's data directories.
            if sys.stderr is not None:
                print("独立语音验收无法开始: " + type(exc).__name__ + ":" + str(exc)[:500], file=sys.stderr)
            return 2
        return 0 if result["success"] else 2
    if args.share_self_test:
        return _write_report(self_test(base, resources), args.report)
    if args.share_ui_smoke:
        return _ui_smoke(base, resources, args.report)
    if rest and rest[0] == "--log-viewer":
        from aipet.application import run_special_mode
        return run_special_mode([sys.argv[0], *rest]) or 0
    if rest:
        parser.error("不支持的启动参数: " + " ".join(rest))
    configure_environment(base, resources, create=True)
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    QApplication.setAttribute(Qt.AA_DisableHighDpiScaling, True)
    # The parent owns the lock across setup and the main app. A setup child
    # never creates another app in this process or competes for that same lock.
    lock = None if args.share_setup_child else _lock(profile)
    if lock is None and not args.share_setup_child:
        return 0
    if args.share_setup:
        return _setup(base, resources, profile)
    try:
        viewer = read_viewer_settings(profile)
        root = apply_viewer_settings(viewer)
        needs_setup = not (profile / "config.json").is_file()
    except (ValueError, OSError, TypeError):
        needs_setup = True
    if needs_setup:
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).resolve()))
        command.extend(["--share-setup", "--share-setup-child"])
        completed = subprocess.run(command, cwd=str(base), **interactive_process_options())
        if completed.returncode != 0:
            return completed.returncode
        root = apply_viewer_settings(read_viewer_settings(profile))
    launch_viewer(root)
    import aipet.application as application
    import aipet.core.tts_service as tts_service
    original_tts_subprocess = tts_service.subprocess
    tts_service.subprocess = ExternalSubprocessProxy(original_tts_subprocess)
    original_voice_trigger = application.configure_voice_trigger
    def packaged_voice_trigger(pet, settings, tray_icon, platform_runtime=None):
        if settings.stt.enabled:
            tray_icon.showMessage("分享版语音输入不可用", "此包不包含语音识别模型及组件，请使用键盘输入。")
        return None
    application.configure_voice_trigger = packaged_voice_trigger
    try:
        return application.main()
    finally:
        application.configure_voice_trigger = original_voice_trigger
        tts_service.subprocess = original_tts_subprocess


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        base, _ = portable_roots()
        error_path = base / "data" / "share-startup-error.txt"
        error_path.parent.mkdir(parents=True, exist_ok=True)
        # No traceback locals or configuration values are included.
        error_path.write_text("宁宁桌宠启动失败。错误类型: " + type(exc).__name__ + "\n"
                              "请检查解压是否完整，运行首次设置并核对 Viewer 安装目录。\n", encoding="utf-8")
        from PyQt5.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "宁宁桌宠启动失败", "启动未完成；未修改你的对话记录。\n详情: " + str(error_path))
        raise SystemExit(2)
