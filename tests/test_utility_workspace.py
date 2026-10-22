"""Offscreen workspace tests: fake jobs/audio/clipboard and synthetic pixels."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QMimeData, QObject, QPoint, QPointF, QRect, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDragEnterEvent, QDropEvent, QKeyEvent, QMouseEvent, QPixmap
from PyQt5.QtWidgets import QApplication

from aipet.core.config import AppSettings
from aipet.core.reading_materials import material_from_text
from aipet.core.utility_assistant import UtilityAnswer, UtilityCitation
from aipet.ui.utility_assistant import UtilityController, UtilityDialog
from aipet.ui.utility_capture import RegionCapture


class FakePet(QObject):
    pause_changed = pyqtSignal(bool)
    test_mode_changed = pyqtSignal(bool)
    interaction_started = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.settings = AppSettings()
        self.settings.character.profile_id = "nene"
        self.settings.tts.enabled = True
        self._closing = self._dnd = self._utility_busy = False
        self._viewerex = None
        self.cancel_conversation = Mock()
        self._audio_duration_milliseconds = Mock(return_value=2000)

    def is_dnd_enabled(self):
        return self._dnd

    def dnd(self, value):
        self._dnd = self.settings.idle.do_not_disturb = value
        self.pause_changed.emit(value)


class FakeWorker(QObject):
    result_ready = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, *args, parent=None, **kwargs):
        super().__init__(parent)
        self.args, self.kwargs = args, kwargs
        self.running = self.cancelled = self.transferred = self.disposed = False
        self.audio = None
        self.start = Mock(side_effect=self._start)
        self.cancel = Mock(side_effect=self._cancel)
        self.isRunning = Mock(side_effect=lambda: self.running)
        self.take_audio = Mock(side_effect=self._take_audio)

    def _start(self):
        self.running = True

    def _cancel(self):
        self.cancelled = True
        if self.audio is not None and not self.transferred:
            self.audio.unlink(missing_ok=True)
            self.audio = None

    def _take_audio(self, path):
        if self.cancelled or self.audio != path:
            return False
        self.transferred = True
        self.audio = None
        return True

    def complete(self, result):
        self.result_ready.emit(result)
        self.running = False
        self.finished.emit()

    def deleteLater(self):
        self.disposed = True
        super().deleteLater()


class FakePlayer(QObject):
    started = pyqtSignal()
    position_changed = pyqtSignal(float)
    finished = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.play = Mock(side_effect=self._play)
        self.stop = Mock()
        self.disposed = False

    def _play(self, path):
        self.started.emit()
        return True

    def deleteLater(self):
        self.disposed = True
        super().deleteLater()


def answer(text="这段材料说明先比较两个解释。", *, citations=(), speech="二つの説明を比べてみましょう。"):
    return UtilityAnswer(text=text, speech_ja=speech, emotion="平静", citations=citations)


class UtilityWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.pet = FakePet()
        self.dialog = UtilityDialog()
        self.workers, self.voices, self.images, self.material_jobs, self.players = [], [], [], [], []
        self.clipboard = Mock()
        self.clipboard.text.side_effect = AssertionError("Clipboard reading was not explicitly requested")
        self.clipboard.image.side_effect = AssertionError("Real clipboard images forbidden")
        self.enterContext(patch("aipet.ui.utility_assistant.QApplication.clipboard", return_value=self.clipboard))
        self.enterContext(patch("requests.Session.request", side_effect=AssertionError("Real HTTP forbidden")))
        self.enterContext(patch("aipet.core.utility_assistant.TTSClient", side_effect=AssertionError("Real TTS forbidden")))
        self.enterContext(patch("aipet.ui.utility_assistant.desktop_snapshot", side_effect=AssertionError("Real screenshot forbidden")))
        self.controller = UtilityController(self.pet, dialog=self.dialog,
            worker_factory=self.factory(self.workers), voice_factory=self.factory(self.voices),
            image_factory=self.factory(self.images), material_factory=self.factory(self.material_jobs),
            player_factory=self.player_factory)
        self.addCleanup(self.cleanup)

    def factory(self, target):
        def create(*args, **kwargs):
            worker = FakeWorker(*args, **kwargs)
            target.append(worker)
            return worker
        return create

    def player_factory(self, parent):
        player = FakePlayer(parent)
        self.players.append(player)
        return player

    def cleanup(self):
        self.controller.shutdown()
        self.app.processEvents()
        for widget in (self.dialog, self.controller, self.pet):
            if not sip.isdeleted(widget):
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.DeferredDelete)

    def ask(self, question="这段话是什么意思？", text="甲先离开，乙随后关灯。"):
        if text is not None:
            self.dialog.source.setPlainText(text)
        self.dialog.question.setPlainText(question)
        self.controller.ask()
        return self.workers[-1]

    def answered(self, value=None):
        worker = self.ask()
        worker.complete(value or answer())
        return worker

    def start_voice(self):
        self.answered()
        self.controller.speak()
        worker = self.voices[-1]
        path = self.root / "owned-reading.wav"
        path.write_bytes(b"synthetic audio, fake player only")
        worker.audio = path
        worker.result_ready.emit(path)
        return worker, self.players[-1], path

    def test_text_answer_is_displayed_without_creating_tts_or_audio_player(self):
        worker = self.answered()
        self.assertEqual(self.dialog.answer.toPlainText(), answer().text)
        self.assertEqual(self.voices, [])
        self.assertEqual(self.players, [])
        self.assertTrue(self.dialog.speak.isEnabled())
        worker.start.assert_called_once()
        self.assertEqual(len(self.controller._history), 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_voice_is_explicit_and_take_audio_transfers_cleanup_ownership(self):
        worker, player, path = self.start_voice()
        worker.take_audio.assert_called_once_with(path)
        self.assertTrue(worker.transferred)
        self.assertTrue(path.exists())
        player.play.assert_called_once_with(path)
        player.finished.emit()
        player.stop.assert_called_once()
        self.assertTrue(player.disposed)
        self.assertFalse(path.exists())
        self.assertIsNone(self.controller._player)
        self.assertIsNone(self.controller._audio_path)
        self.assertEqual(self.dialog.answer.toPlainText(), answer().text)

    def test_close_clears_ram_materials_questions_answers_history_and_audio(self):
        self.dialog.source.setPlainText("只在这个窗口出现的材料。")
        self.controller.add_pasted_material()
        self.dialog.question.setPlainText("解释它")
        self.controller.ask()
        self.workers[-1].complete(answer())
        self.controller.open()
        self.dialog.close()
        self.app.processEvents()
        self.assertEqual(self.controller._materials, [])
        self.assertEqual(self.controller._history, [])
        self.assertIsNone(self.controller._last_request)
        self.assertIsNone(self.controller._answer)
        self.assertIsNone(self.controller._selection)
        for editor in (self.dialog.source, self.dialog.question, self.dialog.answer, self.dialog.source_view):
            self.assertEqual(editor.toPlainText(), "")
        self.assertEqual(self.dialog.materials.count(), 0)
        self.assertFalse(self.pet._utility_busy)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_new_request_invalidates_old_answer_and_uses_latest_question(self):
        old = self.ask()
        self.dialog.question.setPlainText("现在解释第二个问题")
        self.controller.ask()
        new = self.workers[-1]
        self.assertIsNot(old, new)
        old.result_ready.emit(answer("旧回答不能回来"))
        self.assertEqual(self.dialog.answer.toPlainText(), "")
        new.complete(answer("新问题的回答"))
        self.assertEqual(self.dialog.answer.toPlainText(), "新问题的回答")
        self.assertEqual(new.args[1], "现在解释第二个问题")

    def test_source_change_invalidates_running_result_and_retry_snapshot(self):
        old = self.ask()
        self.dialog.source.setPlainText("已经换成新的正文。")
        old.result_ready.emit(answer("不能用于新正文的旧回答"))
        self.assertIsNone(self.controller._last_request)
        self.assertEqual(self.controller._history, [])
        self.assertEqual(self.dialog.answer.toPlainText(), "")
        self.assertFalse(self.dialog.retry.isEnabled())
        old.cancel.assert_called()

    def test_stop_ignores_queued_answer_and_late_error(self):
        worker = self.ask()
        self.controller.cancel()
        stopped = self.dialog.status.text()
        worker.result_ready.emit(answer("过期结果"))
        worker.error.emit("过期错误")
        self.assertEqual(self.dialog.answer.toPlainText(), "")
        self.assertEqual(self.dialog.status.text(), stopped)
        self.assertEqual(self.controller._history, [])

    def test_dnd_cancels_worker_and_rejects_new_work_and_clipboard_reads(self):
        worker = self.ask()
        self.pet.dnd(True)
        worker.result_ready.emit(answer("免打扰期间不能显示"))
        self.controller.open(clipboard=True)
        self.controller.ask()
        self.controller.speak()
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(self.voices, [])
        self.clipboard.text.assert_not_called()
        self.assertFalse(self.dialog.send.isEnabled())
        self.pet.dnd(False)
        self.assertTrue(self.dialog.send.isEnabled())
        self.assertEqual(len(self.workers), 1, "Resume waits for a new explicit request")

    def test_stale_voice_path_is_deleted_by_cancel_without_take_or_play(self):
        self.answered()
        self.controller.speak()
        voice = self.voices[-1]
        path = self.root / "unclaimed.wav"
        path.write_bytes(b"worker-owned synthetic result")
        voice.audio = path
        self.controller.cancel()
        voice.result_ready.emit(path)
        self.assertFalse(path.exists())
        voice.take_audio.assert_not_called()
        self.assertEqual(self.players, [])

    def test_active_voice_stop_and_old_progress_do_not_touch_renderer(self):
        renderer = self.pet._viewerex = Mock()
        renderer.prepare_timeline_speech.return_value = True
        _voice, player, path = self.start_voice()
        renderer.sync_speech_position.assert_called_with(0)
        player.position_changed.emit(.5)
        renderer.sync_speech_position.assert_called_with(.5)
        self.controller.cancel()
        player.stop.assert_called_once()
        self.assertFalse(path.exists())
        renderer.reset_mock()
        player.position_changed.emit(2.)
        player.finished.emit()
        player.failed.emit("late error")
        renderer.sync_speech_position.assert_not_called()
        self.assertIsNone(self.controller._audio_path)

    def test_source_change_and_dnd_each_cleanup_active_audio(self):
        for reason in ("source", "dnd"):
            with self.subTest(reason=reason):
                if self.pet.is_dnd_enabled():
                    self.pet.dnd(False)
                _voice, player, path = self.start_voice()
                if reason == "source":
                    self.dialog.source.setPlainText("更换正文")
                else:
                    self.pet.dnd(True)
                player.stop.assert_called_once()
                self.assertTrue(player.disposed)
                self.assertFalse(path.exists())

    def test_renderer_sync_eof_and_audio_failure_cleanup(self):
        renderer = self.pet._viewerex = Mock()
        renderer.prepare_timeline_speech.return_value = True
        _voice, player, path = self.start_voice()
        player.finished.emit()
        renderer.sync_speech_position.assert_called_with(2., force=True)
        renderer.finish_speech.assert_called_once()
        self.assertFalse(path.exists())
        self.assertEqual(self.dialog.answer.toPlainText(), answer().text)

    def test_new_question_cancels_old_audio_without_restarting_it(self):
        _voice, player, path = self.start_voice()
        self.dialog.question.setPlainText("继续解释第二个问题")
        self.controller.ask()
        player.stop.assert_called_once()
        self.assertFalse(path.exists())
        player.position_changed.emit(2.)
        player.finished.emit()
        self.assertIsNone(self.controller._player)
        self.assertEqual(len(self.players), 1)
        self.workers[-1].complete(answer("新的文字回答"))
        self.assertEqual(self.dialog.answer.toPlainText(), "新的文字回答")

    def test_async_audio_failure_retains_text_and_releases_owned_path(self):
        _voice, player, path = self.start_voice()
        player.failed.emit("Synthetic device failure")
        player.stop.assert_called_once()
        self.assertTrue(player.disposed)
        self.assertFalse(path.exists())
        self.assertIsNone(self.controller._audio_path)
        self.assertEqual(self.dialog.answer.toPlainText(), answer().text)

    def test_retry_uses_original_question_selection_and_history_without_duplicates(self):
        first = self.answered()
        original = self.controller._last_request
        self.controller.retry()
        retried = self.workers[-1]
        self.assertIsNot(retried, first)
        self.assertEqual(retried.args[1], original[0])
        self.assertIs(retried.kwargs["selection"], original[2])
        self.assertEqual(retried.kwargs["history"], [])
        retried.complete(answer("重试后的回答"))
        self.assertEqual(len(self.controller._history), 2)
        self.assertEqual(self.controller._history[-1]["content"], "重试后的回答")
        self.dialog.question.setPlainText("继续说明因果")
        self.controller.ask()
        following = self.workers[-1]
        self.assertEqual(len(following.kwargs["history"]), 2)
        following.complete(answer("进一步回答"))
        self.controller.retry()
        self.workers[-1].complete(answer("再次回答同一追问"))
        self.assertEqual(len(self.controller._history), 4)

    def test_actual_material_and_excerpt_ids_link_to_visible_source(self):
        self.dialog.source.setPlainText("第一份材料的原话。")
        self.controller.add_pasted_material()
        self.dialog.source.setPlainText("第二份材料的原话。")
        self.controller.add_pasted_material()
        self.dialog.question.setPlainText("比较两份材料")
        self.controller.ask()
        selection = self.workers[-1].kwargs["selection"]
        self.assertEqual({block.material_id for block in selection.blocks}, {"M1", "M2"})
        block = selection.blocks[-1]
        self.workers[-1].complete(answer("见引用 [" + block.block_id + "]", citations=(UtilityCitation(block_id=block.block_id, quote=block.text),)))
        self.assertIn(block.block_id, self.dialog.references.item(0).text())
        self.dialog.references.setCurrentRow(0)
        self.assertIn(block.text, self.dialog.source_view.toPlainText())
        self.assertIn(block.block_id, self.dialog.source_view.toPlainText())
        self.assertEqual(self.dialog.materials.count(), 2)

    def test_copy_answer_preserves_literal_plain_text_not_rendered_markup(self):
        text = "**原文**\n<script>alert('文字');</script>\n[链接](https://example.invalid/)"
        self.answered(answer(text))
        self.controller.copy_answer()
        self.assertEqual(self.dialog.answer.toPlainText(), text)
        self.clipboard.setText.assert_called_once_with(text)
        self.clipboard.text.assert_not_called()

    def test_open_reads_explicitly_copied_text_but_never_sends_automatically(self):
        self.clipboard.text.side_effect = None
        self.clipboard.text.return_value = "用户主动复制的原文"
        self.controller.open(clipboard=True)
        self.assertEqual(self.dialog.source.toPlainText(), "用户主动复制的原文")
        self.assertEqual(self.workers, [])
        self.assertEqual(self.voices, [])
        self.assertEqual(self.images, [])

    def test_import_is_fake_background_job_with_real_material_id_and_page(self):
        self.controller.import_path("synthetic.pdf")
        job = self.material_jobs[-1]
        self.assertEqual(job.args[:3], ("synthetic.pdf", "M1", None))
        material = material_from_text("PDF 文字层中的普通段落。", material_id=job.args[1], title="合成 PDF")
        material = replace(material, blocks=(replace(material.blocks[0], page=2),))
        job.complete(material)
        self.assertEqual(self.dialog.materials.count(), 1)
        self.assertIn("第 2 页", self.dialog.source_view.toPlainText())
        self.assertEqual(self.workers, [], "Import alone never submits model requests")

    def test_image_reading_uses_only_synthetic_pixels_and_waits_for_explicit_question(self):
        pixmap = QPixmap(40, 30)
        pixmap.fill(Qt.white)
        self.controller._read_image(pixmap.toImage())
        self.assertEqual(len(self.images), 1)
        self.assertTrue(self.images[-1].args[1].startswith(b"\x89PNG"))
        self.images[-1].complete("识别出来但需要用户核对的文字。")
        self.assertIn("识别出来", self.dialog.source.toPlainText())
        self.assertEqual(self.workers, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_image_result_keeps_ocr_provenance_in_actual_request_selection(self):
        pixmap = QPixmap(20, 20)
        pixmap.fill(Qt.white)
        self.controller._read_image(pixmap.toImage())
        self.images[-1].complete("图片中识别的原话，需要核对。")
        selection = self.controller._make_request()[2]
        self.assertTrue(selection.blocks)
        self.assertEqual({block.origin for block in selection.blocks}, {"ocr"})
        self.assertTrue(all("图片识别" in block.title for block in selection.blocks))
        self.assertIn("图片中识别的原话", selection.blocks[0].text)
        self.assertEqual(self.workers, [], "OCR source preparation never sends a question")

    def test_edited_image_source_retains_provenance_after_joining_materials(self):
        pixmap = QPixmap(20, 20)
        pixmap.fill(Qt.white)
        self.controller._read_image(pixmap.toImage())
        self.images[-1].complete("图片识别有一处错字。")
        self.dialog.source.setPlainText("用户修正后的图片识别文字。")
        edited = self.controller._make_request()[2]
        self.assertEqual({block.origin for block in edited.blocks}, {"ocr-edited"})
        material_id = edited.blocks[0].material_id
        self.controller.add_pasted_material()
        self.assertEqual(self.controller._materials[0].origin, "ocr-edited")
        self.assertEqual(self.controller._materials[0].material_id, material_id)
        self.assertIn("图片识别", self.controller._materials[0].title)
        retained = self.controller._make_request()[2]
        self.assertEqual({block.origin for block in retained.blocks}, {"ocr-edited"})
        self.assertEqual(retained.blocks[0].material_id, material_id)

    def test_explicit_clipboard_text_replaces_and_clears_image_provenance(self):
        pixmap = QPixmap(20, 20)
        pixmap.fill(Qt.white)
        self.controller._read_image(pixmap.toImage())
        self.images[-1].complete("先前的图片识别内容。")
        old_selection = self.controller._make_request()[2]
        self.clipboard.text.side_effect = None
        self.clipboard.text.return_value = "用户随后主动复制的文字。"
        self.controller.paste_clipboard()
        current = self.controller._make_request()[2]
        self.assertEqual({block.origin for block in current.blocks}, {"clipboard"})
        self.assertTrue(all("图片识别" not in block.title for block in current.blocks))
        self.assertNotEqual(current.blocks[0].material_id, old_selection.blocks[0].material_id)
        self.assertIn("主动复制", current.blocks[0].text)
        self.assertEqual(self.workers, [])

    def test_text_editors_decline_drops_and_parent_imports_one_local_file(self):
        self.assertFalse(self.dialog.source.acceptDrops())
        self.assertFalse(self.dialog.question.acceptDrops())
        self.assertTrue(self.dialog.acceptDrops())
        path = self.root / "synthetic-local.md"
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        self.dialog.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        drop = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        self.dialog.dropEvent(drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(len(self.material_jobs), 1)
        self.assertEqual(Path(self.material_jobs[0].args[0]), path)
        self.assertFalse(path.exists(), "Fake import must not access or create the dropped file")

    def test_capture_pending_start_is_discarded_when_dnd_begins(self):
        pending = []
        with patch("aipet.ui.utility_assistant.QTimer.singleShot", side_effect=lambda _ms, callback: pending.append(callback)):
            self.controller.capture_region()
            self.pet.dnd(True)
            self.assertEqual(len(pending), 1)
            with patch("aipet.ui.utility_assistant.desktop_snapshot") as snapshot:
                pending[0]()
                snapshot.assert_not_called()
        self.assertIsNone(self.controller._capture)
        self.assertEqual(self.images, [])

    def test_capture_uses_synthetic_snapshot_and_only_selected_pixels_reach_image_job(self):
        pending = []
        pixmap = QPixmap(60, 50)
        pixmap.fill(Qt.white)
        with patch("aipet.ui.utility_assistant.QTimer.singleShot", side_effect=lambda _ms, callback: pending.append(callback)), \
                patch("aipet.ui.utility_assistant.desktop_snapshot", return_value=(pixmap, QRect(200, 100, 60, 50))):
            self.controller.capture_region()
            pending[0]()
        capture = self.controller._capture
        self.assertIsNotNone(capture)
        for kind, x, y in ((QEvent.MouseButtonPress, 10, 10), (QEvent.MouseButtonRelease, 29, 24)):
            event = QMouseEvent(kind, QPointF(x, y), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
            if kind == QEvent.MouseButtonPress:
                capture.mousePressEvent(event)
            else:
                capture.mouseReleaseEvent(event)
        self.assertIsNone(self.controller._capture)
        self.assertTrue(capture._snapshot.isNull())
        self.assertEqual(len(self.images), 1)
        self.assertEqual(self.workers, [])
        self.assertTrue(self.images[-1].args[1].startswith(b"\x89PNG"))

    def test_formal_interaction_or_test_mode_switch_invalidates_workspace(self):
        worker = self.ask()
        self.pet.interaction_started.emit()
        worker.result_ready.emit(answer("过期结果"))
        self.assertEqual(self.dialog.answer.toPlainText(), "")
        self.assertTrue(self.dialog.source.toPlainText())
        self.pet.test_mode_changed.emit(True)
        self.assertEqual(self.dialog.source.toPlainText(), "")
        self.assertEqual(self.controller._history, [])


class RegionCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        pixmap = QPixmap(120, 80)
        pixmap.fill(QColor("#ff0000"))
        self.capture = RegionCapture(pixmap, QRect(400, 300, 120, 80))
        self.images, self.cancelled, self.errors = [], [], []
        self.capture.captured.connect(self.images.append)
        self.capture.cancelled.connect(lambda: self.cancelled.append(True))
        self.capture.failed.connect(self.errors.append)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if not sip.isdeleted(self.capture):
            self.capture.close()
            QCoreApplication.sendPostedEvents(self.capture, QEvent.DeferredDelete)

    def mouse(self, kind, x, y):
        event = QMouseEvent(kind, QPointF(x, y), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        {QEvent.MouseButtonPress: self.capture.mousePressEvent, QEvent.MouseMove: self.capture.mouseMoveEvent,
         QEvent.MouseButtonRelease: self.capture.mouseReleaseEvent}[kind](event)

    def test_selection_copies_only_requested_original_pixels_and_releases_snapshot(self):
        self.mouse(QEvent.MouseButtonPress, 10, 10)
        self.mouse(QEvent.MouseMove, 29, 24)
        self.mouse(QEvent.MouseButtonRelease, 29, 24)
        self.assertEqual(len(self.images), 1)
        self.assertEqual((self.images[0].width(), self.images[0].height()), (20, 15))
        self.assertEqual(self.images[0].pixelColor(0, 0), QColor("#ff0000"))
        self.assertTrue(self.capture._snapshot.isNull())
        self.assertEqual(self.cancelled, [])

    def test_escape_cancels_once_and_releases_in_memory_snapshot(self):
        self.capture.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.capture.close()
        self.assertEqual(self.cancelled, [True])
        self.assertEqual(self.images, [])
        self.assertTrue(self.capture._snapshot.isNull())

    def test_small_selection_remains_open_for_retry_and_edges_are_clamped(self):
        self.mouse(QEvent.MouseButtonPress, 2, 2)
        self.mouse(QEvent.MouseButtonRelease, 3, 3)
        self.assertEqual(self.images, [])
        self.assertFalse(self.capture._completed)
        self.mouse(QEvent.MouseButtonPress, -10, -10)
        self.mouse(QEvent.MouseButtonRelease, 15, 14)
        self.assertEqual((self.images[0].width(), self.images[0].height()), (16, 15))

    def test_oversize_selection_fails_without_creating_an_image(self):
        with patch("aipet.ui.utility_capture.MAX_REGION_PIXELS", 100):
            self.mouse(QEvent.MouseButtonPress, 10, 10)
            self.mouse(QEvent.MouseButtonRelease, 29, 29)
        self.assertEqual(self.images, [])
        self.assertEqual(self.cancelled, [])
        self.assertEqual(len(self.errors), 1)
        self.assertTrue(self.capture._snapshot.isNull())


if __name__ == "__main__":
    unittest.main()
