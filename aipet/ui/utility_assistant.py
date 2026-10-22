"""Modeless, RAM-only reading workspace and explicitly invoked speech.

Formal conversation delivery and memory are owned by the existing pet. This
workspace never calls its reply/history APIs or changes its relationship state.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PyQt5.QtCore import QByteArray, QBuffer, QIODevice, QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QImage, QKeySequence
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QPlainTextEdit, QPushButton, QShortcut, QSplitter,
    QTabWidget, QVBoxLayout, QWidget,
)

from aipet.core.reading_materials import (
    MAX_MATERIALS, MAX_TOTAL_CHARACTERS, MaterialError,
    load_local_material, material_from_text, select_context,
)
from aipet.core.utility_assistant import ImageReadWorker, UtilityVoiceWorker, UtilityWorker
from aipet.ui.audio_playback import AudioPlayback
from aipet.ui.utility_capture import RegionCapture, desktop_snapshot


class MaterialLoadWorker(QThread):
    result_ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, path, material_id, encoding, parent=None):
        super().__init__(parent)
        self.path, self.material_id, self.encoding = path, material_id, encoding
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()
        self.requestInterruption()

    def run(self):
        try:
            if self._cancelled.is_set():
                return
            material = load_local_material(self.path, material_id=self.material_id, encoding=self.encoding)
            if not self._cancelled.is_set():
                self.result_ready.emit(material)
        except MaterialError as exc:
            if not self._cancelled.is_set():
                self.error.emit(str(exc))
        except Exception:
            if not self._cancelled.is_set():
                self.error.emit("资料读取失败，请检查格式、编码或文件是否可用。")


class UtilityDialog(QDialog):
    """Plain-text presentation avoids executing document or model markup."""
    closing = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("宁宁 · 随手问与共同阅读")
        self.resize(920, 780)
        self.setMinimumSize(720, 580)
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        self.hint = QLabel("复制文字后按 Ctrl+Alt+N；文字先返回，朗读按需触发。关闭窗口会清空本次资料与工具对话。")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        controls = QHBoxLayout()
        self.paste = QPushButton("读取剪贴板文字")
        self.capture = QPushButton("框选屏幕…")
        self.image = QPushButton("识别剪贴板图片…")
        for button in (self.paste, self.capture, self.image):
            controls.addWidget(button)
        layout.addLayout(controls)

        tabs = QTabWidget()
        self.tabs = tabs
        input_tab = QWidget()
        source_layout = QVBoxLayout(input_tab)
        self.source = QPlainTextEdit()
        self.source.setAcceptDrops(False)
        self.source.setPlaceholderText("粘贴要解释、翻译或共读的正文。图片识别结果也会放在这里，可先修正再提问。")
        source_layout.addWidget(self.source)
        source_actions = QHBoxLayout()
        self.add_material = QPushButton("加入共读材料")
        self.import_file = QPushButton("导入 TXT / Markdown / PDF…")
        self.encoding = QComboBox()
        self.encoding.addItem("UTF-8 / PDF", None)
        self.encoding.addItem("GB18030 文本", "gb18030")
        source_actions.addWidget(self.add_material)
        source_actions.addWidget(self.import_file)
        source_actions.addWidget(self.encoding)
        source_layout.addLayout(source_actions)
        tabs.addTab(input_tab, "本次文字")

        materials_tab = QWidget()
        materials_layout = QVBoxLayout(materials_tab)
        self.materials = QListWidget()
        self.materials.setMaximumHeight(100)
        self.source_view = QPlainTextEdit()
        self.source_view.setAcceptDrops(False)
        self.source_view.setReadOnly(True)
        self.source_view.setPlaceholderText("点击材料查看原文，点击下方回答引用查看本次实际提交的摘录。")
        self.clear_materials = QPushButton("清空本次材料与工具对话")
        materials_layout.addWidget(self.materials)
        materials_layout.addWidget(self.source_view)
        materials_layout.addWidget(self.clear_materials)
        tabs.addTab(materials_tab, "共同阅读")

        answer_tab = QWidget()
        answer_layout = QVBoxLayout(answer_tab)
        self.answer = QPlainTextEdit()
        self.answer.setAcceptDrops(False)
        self.answer.setReadOnly(True)
        self.answer.setPlaceholderText("宁宁的文字回答会先显示在这里。引用只核对原文是否存在，不保证推断正确。")
        self.references = QListWidget()
        self.references.setMaximumHeight(90)
        answer_layout.addWidget(self.answer)
        answer_layout.addWidget(self.references)
        split = QSplitter(Qt.Vertical)
        split.addWidget(tabs)
        split.addWidget(answer_tab)
        split.setSizes([330, 280])
        layout.addWidget(split)
        row = QHBoxLayout()
        self.action = QComboBox()
        for label, value in (("解释", "explain"), ("翻译", "translate"), ("摘重点", "summarize"), ("共同讨论", "discuss")):
            self.action.addItem(label, value)
        self.question = QPlainTextEdit()
        self.question.setAcceptDrops(False)
        self.question.setMaximumHeight(76)
        self.question.setPlaceholderText("说说你想问什么；有材料时可留空。Ctrl+Enter 发送。")
        self.send = QPushButton("发给宁宁")
        row.addWidget(self.action)
        row.addWidget(self.question, 1)
        row.addWidget(self.send)
        layout.addLayout(row)
        buttons = QHBoxLayout()
        self.stop = QPushButton("停止")
        self.retry = QPushButton("重试上一问")
        self.copy = QPushButton("复制回答")
        self.speak = QPushButton("朗读日语要点")
        for button in (self.stop, self.retry, self.copy, self.speak):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.status = QLabel("就绪 · 资料与工具对话仅保留在本次窗口中，不写入桌宠记忆。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.send_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.drop_paths = None

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if self.drop_paths and urls and all(url.isLocalFile() for url in urls):
            # One at a time keeps ownership, errors and context changes clear.
            if len(urls) != 1:
                self.status.setText("请一次拖入一个文件。")
            else:
                self.drop_paths(urls[0].toLocalFile())
            event.acceptProposedAction()

    def closeEvent(self, event):
        self.closing.emit()
        super().closeEvent(event)


class UtilityController(QObject):
    def __init__(self, pet, parent=None, *, dialog=None, worker_factory=UtilityWorker,
                 voice_factory=UtilityVoiceWorker, image_factory=ImageReadWorker,
                 player_factory=AudioPlayback, material_factory=MaterialLoadWorker):
        super().__init__(parent)
        self.pet = pet
        self.dialog = dialog or UtilityDialog()
        self._worker_factory, self._voice_factory = worker_factory, voice_factory
        self._image_factory, self._player_factory = image_factory, player_factory
        self._material_factory = material_factory
        self._epoch = 0
        self._workers = set()
        self._next_id = 1
        self._materials = []
        self._draft_material = None
        self._draft_origin = "pasted"
        self._history = []
        self._last_request = None
        self._answer = self._selection = None
        self._player = self._audio_path = None
        self._renderer = None
        self._capture = None
        self._closed = False
        self._changing_source = False
        self._last_context_note = ""
        d = self.dialog
        d.paste.clicked.connect(self.paste_clipboard)
        d.capture.clicked.connect(self.capture_region)
        d.image.clicked.connect(self.read_clipboard_image)
        d.add_material.clicked.connect(self.add_pasted_material)
        d.import_file.clicked.connect(self.choose_file)
        d.clear_materials.clicked.connect(self.clear_session)
        d.send.clicked.connect(self.ask)
        d.send_shortcut.activated.connect(self.ask)
        d.stop.clicked.connect(self.cancel)
        d.retry.clicked.connect(self.retry)
        d.copy.clicked.connect(self.copy_answer)
        d.speak.clicked.connect(self.speak)
        d.source.textChanged.connect(self._source_changed)
        d.materials.currentRowChanged.connect(self._view_material)
        d.references.currentRowChanged.connect(self._view_reference)
        d.closing.connect(self.clear_session)
        d.drop_paths = self.import_path
        pet.pause_changed.connect(self.set_paused)
        pet.test_mode_changed.connect(lambda _value: self.clear_session())
        interaction = getattr(pet, "interaction_started", None)
        if interaction is not None:
            interaction.connect(self._pet_interaction)
        self._refresh_buttons()

    def _allowed(self):
        return not self._closed and not self.pet._closing and not self.pet.is_dnd_enabled()

    def _id(self):
        result = f"M{self._next_id}"
        self._next_id += 1
        return result

    def _status(self, message):
        self.dialog.status.setText(message)

    def open(self, *, clipboard=False):
        if not self._allowed():
            return
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        self.pet._utility_busy = True
        if clipboard:
            self.paste_clipboard()
        self.dialog.question.setFocus()

    def set_hotkey_status(self, registered, message=""):
        text = "Ctrl+Alt+N 已启用" if registered else "快捷键不可用，可从人物右键菜单打开"
        self.dialog.hint.setText(text + "；文字先返回，朗读按需触发。关闭窗口清空本次资料与工具对话。")

    def _refresh_buttons(self):
        allowed = self._allowed()
        d = self.dialog
        for widget in (d.send, d.paste, d.capture, d.image, d.add_material, d.import_file,
                       d.source, d.question, d.action, d.encoding, d.clear_materials):
            widget.setEnabled(allowed)
        d.retry.setEnabled(allowed and self._last_request is not None)
        d.copy.setEnabled(allowed and self._answer is not None)
        d.speak.setEnabled(allowed and self._answer is not None and bool(self._answer.speech_ja.strip()))
        d.stop.setEnabled(allowed)

    def _stop_audio(self):
        player, self._player = self._player, None
        renderer, self._renderer = self._renderer, None
        path, self._audio_path = self._audio_path, None
        if player is not None:
            player.stop()
            player.deleteLater()
        if renderer is not None:
            renderer.finish_speech()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _invalidate(self):
        self._epoch += 1
        for worker in tuple(self._workers):
            worker.cancel()
        self._stop_audio()
        if self._capture is not None:
            capture, self._capture = self._capture, None
            capture.close()
        self._refresh_buttons()

    def cancel(self):
        self._invalidate()
        self._status("已停止接收本次结果并停播；尚在执行的网络请求会在后台收尾。")

    def _pet_interaction(self):
        self._invalidate()
        self._status("已切回桌宠对话；本窗口材料暂留，可稍后继续。")

    def set_paused(self, paused):
        self._invalidate()
        self._status("免打扰：工具请求与朗读均已暂停。" if paused else "已恢复，等待你主动提问，不补发旧请求。")
        self._refresh_buttons()

    def _source_changed(self):
        if self._changing_source:
            return
        if self._draft_origin.startswith("ocr"):
            self._draft_origin = "ocr-edited"
        self._draft_material = None
        self._reset_answers()

    def _replace_source(self, text, origin):
        self._changing_source = True
        self._draft_origin = origin
        self.dialog.source.setPlainText(text)
        self._changing_source = False
        self._draft_material = None
        self._reset_answers()

    def _draft(self):
        if self._draft_material is None:
            title = "图片识别文字（待核对）" if self._draft_origin.startswith("ocr") else "当前文字"
            self._draft_material = material_from_text(self.dialog.source.toPlainText(),
                material_id=self._id(), title=title, origin=self._draft_origin)
        return self._draft_material

    def _reset_answers(self):
        self._invalidate()
        self._history = []
        self._last_request = None
        self._answer = self._selection = None
        self.dialog.answer.clear()
        self.dialog.references.clear()
        self._refresh_buttons()

    def clear_session(self):
        self._reset_answers()
        self._materials = []
        self._draft_material = None
        self._draft_origin = "pasted"
        self._changing_source = True
        self.dialog.source.clear()
        self._changing_source = False
        self.dialog.question.clear()
        self.dialog.materials.clear()
        self.dialog.source_view.clear()
        self.pet._utility_busy = self.dialog.isVisible() and self._allowed()
        # closeEvent is emitted before Qt hides the dialog.
        QTimer.singleShot(0, lambda: setattr(self.pet, "_utility_busy", self.dialog.isVisible() and self._allowed()))
        self._status("本次材料与工具对话已清空。")

    def paste_clipboard(self):
        if not self._allowed():
            return
        text = QApplication.clipboard().text()
        if not text.strip():
            self._status("剪贴板没有文字。图片请使用“识别剪贴板图片”。")
            return
        if len(text) > 400_000:
            self._status("剪贴板文字过长，请先选取需要的部分。")
            return
        self._replace_source(text, "clipboard")
        self._status("已读取复制的文字。可编辑或选择任务后发送；尚未提交给模型。")

    def _can_add(self, material):
        if len(self._materials) >= MAX_MATERIALS:
            raise MaterialError("最多同时加入 12 份材料，请先清空本次材料。")
        if sum(len(item.text) for item in self._materials) + len(material.text) > MAX_TOTAL_CHARACTERS:
            raise MaterialError("本次资料总量过大，请分批阅读。")

    def add_pasted_material(self):
        if not self._allowed():
            return
        try:
            material = self._draft()
            self._can_add(material)
        except (ValueError, MaterialError) as exc:
            self._status(str(exc))
            return
        self._reset_answers()
        self._materials.append(material)
        self._draft_material = None
        self._draft_origin = "pasted"
        self._changing_source = True
        self.dialog.source.clear()
        self._changing_source = False
        self._list_materials()
        self._status("已加入材料，可继续粘贴其他内容或直接提问。")

    def _list_materials(self):
        d = self.dialog
        d.materials.clear()
        for material in self._materials:
            d.materials.addItem(f"{material.material_id} · {material.title} · {len(material.text)} 字 · {len(material.blocks)} 段")
        if self._materials:
            d.materials.setCurrentRow(len(self._materials) - 1)

    def _view_material(self, row):
        if 0 <= row < len(self._materials):
            material = self._materials[row]
            self.dialog.source_view.setPlainText("\n\n".join(
                f"[{b.block_id}]" + (f" 第 {b.page} 页" if b.page else "") + f"\n{b.text}" for b in material.blocks))

    def choose_file(self):
        if not self._allowed():
            return
        path, _filter = QFileDialog.getOpenFileName(self.dialog, "加入本次共读材料", "", "资料 (*.txt *.md *.markdown *.pdf)")
        if path:
            self.import_path(path)

    def _track(self, worker, callback):
        epoch = self._epoch
        self._workers.add(worker)
        worker.result_ready.connect(lambda value, w=worker, token=epoch: callback(value, w)
            if self._allowed() and token == self._epoch and w in self._workers else None)
        worker.error.connect(lambda message, token=epoch: self._status(message)
            if self._allowed() and token == self._epoch else None)
        worker.finished.connect(lambda w=worker: self._retire(w))
        worker.start()

    def _retire(self, worker):
        self._workers.discard(worker)
        worker.deleteLater()

    def _request_room(self):
        if sum(worker.isRunning() for worker in self._workers) >= 2:
            self._status("前面的请求仍在收尾，结束后再重试，避免重复提交。")
            return False
        return True

    def import_path(self, path):
        if not self._allowed() or not self._request_room():
            return
        self._reset_answers()
        worker = self._material_factory(path, self._id(), self.dialog.encoding.currentData(), self)
        self._status("正在读取资料…")
        def loaded(material, _worker):
            try:
                self._can_add(material)
            except MaterialError as exc:
                self._status(str(exc))
                return
            self._materials.append(material)
            self._list_materials()
            self.dialog.tabs.setCurrentIndex(1)
            self._status("材料已加入，尚未提交模型。PDF 仅提取文字层，引用可定位页码。")
        self._track(worker, loaded)

    def _make_request(self):
        question = self.dialog.question.toPlainText().strip()
        action = self.dialog.action.currentData()
        materials = list(self._materials)
        if self.dialog.source.toPlainText().strip():
            materials.append(self._draft())
        if not question and not materials:
            raise ValueError("请先输入问题，或粘贴、导入一份材料。")
        if not question:
            question = {"explain": "请解释这些材料。", "translate": "请翻译这些材料。", "summarize": "请概括这些材料的重点。", "discuss": "你怎么看这些材料？请说明依据。"}[action]
        if len(question) > 4_000:
            raise ValueError("问题最多 4000 字，正文请放在材料区域。")
        selection = select_context(materials, question) if materials else None
        return question, action, selection, list(self._history)

    def ask(self):
        if not self._allowed() or not self._request_room():
            return
        try:
            request = self._make_request()
        except (ValueError, MaterialError) as exc:
            self._status(str(exc))
            return
        self._send_request(request)

    def retry(self):
        if self._allowed() and self._last_request is not None and self._request_room():
            self._send_request(self._last_request)

    def _send_request(self, request):
        self._invalidate()
        # Stop earlier pet audio/work before this user-requested workspace job.
        self.pet.cancel_conversation()
        self.pet._utility_busy = True
        self._last_request = request
        self._answer = None
        self._selection = request[2]
        self.dialog.answer.clear()
        self.dialog.references.clear()
        question, action, selection, history = request
        note = ""
        if selection is not None:
            note = f" · 本次提交 {len(selection.blocks)} 段"
            if selection.truncated:
                note += f"，仅为摘录，另有 {selection.omitted_blocks} 个完整段落未提交"
        self._last_context_note = note
        self._status("正在生成文字，无需等待语音" + note)
        worker = self._worker_factory(self.pet.settings, question, action=action,
            selection=selection, history=history, parent=self)
        def answered(answer, _worker):
            self._answer = answer
            self._history = (history + [{"role": "user", "content": question}, {"role": "assistant", "content": answer.text}])[-8:]
            self.dialog.answer.setPlainText(answer.text)
            self.dialog.references.clear()
            for citation in answer.citations:
                block = selection.block_map[citation.block_id]
                where = f" · 第{block.page}页" if block.page else ""
                self.dialog.references.addItem(f"[{citation.block_id}] {block.title}{where} · {citation.quote[:80]}")
            self._status("文字已就绪，可复制、继续追问或按需朗读。" + self._last_context_note)
            self._refresh_buttons()
        self._track(worker, answered)
        self._refresh_buttons()

    def _view_reference(self, row):
        if self._answer is None or self._selection is None or not 0 <= row < len(self._answer.citations):
            return
        citation = self._answer.citations[row]
        block = self._selection.block_map.get(citation.block_id)
        if block:
            self.dialog.source_view.setPlainText(f"[{block.block_id}] {block.title}\n{block.text}\n\n引用：{citation.quote}")
            self.dialog.tabs.setCurrentIndex(1)

    def copy_answer(self):
        if self._allowed() and self._answer is not None:
            QApplication.clipboard().setText(self._answer.text)
            self._status("已复制当前回答。")

    def speak(self):
        if not self._allowed() or self._answer is None or not self._request_room():
            return
        answer = self._answer
        self._invalidate()
        self.pet.cancel_conversation()
        self.pet._utility_busy = True
        self._status("正在准备日语要点语音，完整文字已可阅读。")
        worker = self._voice_factory(self.pet.settings, answer, parent=self)
        def ready(path, voice_worker):
            if voice_worker.take_audio(path):
                self._play_audio(Path(path), answer.emotion)
        self._track(worker, ready)

    def _play_audio(self, path, emotion):
        self._audio_path = path
        player = self._player_factory(self)
        self._player = player
        epoch = self._epoch
        renderer = getattr(self.pet, "_viewerex", None)
        duration = self.pet._audio_duration_milliseconds(path) / 1000
        prepared = bool(renderer is not None and renderer.prepare_timeline_speech(emotion, path, duration + .5) is True)
        self._renderer = renderer if prepared else None
        def current():
            return self._allowed() and self._epoch == epoch and self._player is player
        def progress(seconds):
            if current() and prepared:
                renderer.sync_speech_position(seconds)
        def started():
            if current():
                progress(0)
                self._status("正在朗读日语要点；可随时停止。")
        def finished():
            if current():
                if prepared:
                    renderer.sync_speech_position(duration, force=True)
                self._stop_audio()
                self._status("朗读结束，文字仍保留在本次窗口。")
        def failed(_message):
            if current():
                self._stop_audio()
                self._status("语音输出失败，文字已保留。")
        player.started.connect(started)
        player.position_changed.connect(progress)
        player.finished.connect(finished)
        player.failed.connect(failed)
        if player.play(path) is not True and current():
            failed("")

    def read_clipboard_image(self):
        if not self._allowed():
            return
        image = QApplication.clipboard().image()
        if image.isNull():
            self._status("剪贴板没有图片，请先复制图片或使用屏幕框选。")
            return
        self._read_image(image)

    def capture_region(self):
        if not self._allowed():
            return
        self._invalidate()
        self.dialog.hide()
        epoch = self._epoch
        def begin():
            if not self._allowed() or epoch != self._epoch:
                return
            try:
                snapshot, geometry = desktop_snapshot()
                capture = RegionCapture(snapshot, geometry)
                self._capture = capture
                capture.captured.connect(lambda image: self._captured(image, epoch))
                capture.cancelled.connect(lambda: self._capture_cancelled(epoch))
                capture.failed.connect(lambda message: self._capture_failed(message, epoch))
                capture.show()
                capture.activateWindow()
                capture.setFocus()
            except Exception:
                self.open()
                self._status("无法开始框选，请改用复制文字或图片。")
        QTimer.singleShot(180, begin)

    def _captured(self, image, epoch):
        if self._allowed() and epoch == self._epoch:
            self._capture = None
            self.open()
            self._read_image(image)

    def _capture_cancelled(self, epoch):
        if self._allowed() and epoch == self._epoch:
            self._capture = None
            self.open()
            self._status("已取消框选。")

    def _capture_failed(self, message, epoch):
        if self._allowed() and epoch == self._epoch:
            self._capture = None
            self.open()
            self._status(message)

    def _read_image(self, image: QImage):
        if not self._allowed() or not self._request_room():
            return
        if image.width() * image.height() > 16_000_000:
            self._status("图片过大，请先框选较小区域。")
            return
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        success = image.save(buffer, "PNG")
        buffer.close()
        if not success or len(data) > 8 * 1024 * 1024:
            self._status("图片编码失败或超过 8MiB，请缩小选区。")
            return
        self._reset_answers()
        worker = self._image_factory(self.pet.settings, bytes(data), parent=self)
        self._status("正在用当前视觉模型识别选区；结果可编辑，不会自动发送聊天。")
        def recognized(text, _worker):
            self._replace_source(text, "ocr")
            self._status("识别文字已放入编辑区，请核对错误后再提问。识别结果不等于核实原文。")
        self._track(worker, recognized)

    def shutdown(self):
        self._closed = True
        self.clear_session()
        self.dialog.hide()
        self.pet._utility_busy = False
