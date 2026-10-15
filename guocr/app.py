"""托盘程序主体：快捷键 → 截图遮罩 → 复制图片 / 后台 OCR（+翻译）→ 写入剪贴板 → 结果窗口/气泡提示。"""
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import __version__, winutil
from .config import ACTION_NAMES, ACTIONS, APP_NAME, CONFIG_DIR, LOG_PATH, Config
from .hotkey import EscListener, HotkeyListener
from .icon import make_icon
from .imaging import copy_image, render_translation
from .pin import PinWindow
from .ocr import OcrWorker, qimage_to_bgr
from .overlay import CaptureSession
from .result import ResultWindow
from .settings import SettingsDialog

log = logging.getLogger(__name__)


class App(QObject):
    ocr_requested = Signal(int, object, bool, str, object)
    warmup_requested = Signal()
    download_note = Signal(str, bool)  # 离线模型下载进度（文字, 是否结束），从下载线程发到界面线程

    def __init__(self):
        super().__init__()
        self.cfg = Config.load()
        self.icon = make_icon()
        self.hotkey: HotkeyListener | None = None
        self.session: CaptureSession | None = None
        self.mode = "ocr"           # 当前这次截图的用途：capture / ocr / translate
        self.capturing = False
        self.settings_dlg: SettingsDialog | None = None
        self.job_id = 0
        self.job_translate = False
        self.job_image: QImage | None = None  # 翻译模式下用来贴译文的原始截图
        self.job_anchor = QRect()             # 该截图在屏幕上的位置（逻辑坐标）
        self.job_source = ""
        self.pins: list[PinWindow] = []
        self.job_pin: PinWindow | None = None  # 当前这次翻译的贴图（逐段更新）
        self.job_paras: list = []
        self.job_partial: list[str] = []
        self.result = ResultWindow(self.icon)
        self.result.visibility_changed.connect(lambda _v: self._update_esc())
        self.esc: EscListener | None = None  # 有截图框/弹窗/贴图在屏幕上时才注册的全局 Esc
        self.downloading = False  # 是否正在后台下载离线翻译模型
        self.download_note.connect(self._on_download_note)

        self.ocr_thread = QThread()
        self.ocr_thread.setObjectName("ocr")
        self.worker = OcrWorker()
        self.worker.moveToThread(self.ocr_thread)
        self.ocr_requested.connect(self.worker.recognize)
        self.warmup_requested.connect(self.worker.warmup)
        self.worker.done.connect(self._on_ocr_done)
        self.worker.translated.connect(self._on_translated)
        self.worker.analyzed.connect(self._on_analyzed)
        self.worker.partial.connect(self._on_partial)
        self.worker.failed.connect(self._on_ocr_failed)
        self.ocr_thread.start()
        self.warmup_requested.emit()  # 后台预加载模型
        self._apply_glossary()
        QTimer.singleShot(0, self._preload_offline)  # 等托盘图标建好再执行（可能要用托盘提示下载进度）

        self.menu = QMenu()
        self.menu_actions = {}
        for action in ACTIONS:
            self.menu_actions[action] = self.menu.addAction(
                ACTION_NAMES[action], lambda a=action: QTimer.singleShot(150, lambda: self.start_capture(a)))
        self.menu.addSeparator()
        self.menu.addAction("翻译语境…", self.open_context)
        self.menu.addAction("设置…", self.open_settings)
        self.menu.addSeparator()
        self.menu.addAction("退出", self.quit)
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        self.tray.showMessage(f"{APP_NAME} 已在后台运行", self._hotkey_summary("，"), self.icon, 3000)
        self._start_hotkey()

    # ------------------------------------------------------------ hotkey
    def _hotkey_summary(self, sep: str) -> str:
        return sep.join(f"{hk} {ACTION_NAMES[a]}" for a, hk in self.cfg.hotkeys().items() if hk)

    def _start_hotkey(self) -> None:
        self._stop_hotkey()
        self.hotkey = HotkeyListener(self.cfg.hotkeys(), ACTION_NAMES)
        self.hotkey.triggered.connect(self.start_capture)
        self.hotkey.failed.connect(self._warn)
        self.hotkey.start()
        for action, hotkey in self.cfg.hotkeys().items():
            self.menu_actions[action].setText(f"{ACTION_NAMES[action]}\t{hotkey}" if hotkey else ACTION_NAMES[action])
        self.tray.setToolTip(f"{APP_NAME}\n{self._hotkey_summary(chr(10))}")

    def _stop_hotkey(self) -> None:
        if self.hotkey is not None:
            self.hotkey.stop()
            self.hotkey.deleteLater()
            self.hotkey = None

    def _warn(self, message: str) -> None:
        log.warning(message)
        self.tray.showMessage(APP_NAME, message, QSystemTrayIcon.MessageIcon.Warning, 5000)

    # ----------------------------------------------------------- capture
    def start_capture(self, mode: str = "ocr") -> None:
        if self.capturing:
            return
        self.capturing = True
        self.mode = mode
        delay = 0
        if self.result.isVisible():  # 先藏起上一次的结果窗，等它从屏幕上消失再截
            self.result.hide()
            delay = 80
        QTimer.singleShot(delay, self._begin_capture)

    def _begin_capture(self) -> None:
        try:
            self.session = CaptureSession(winutil.enum_window_rects())
        except Exception:
            log.exception("截屏失败")
            self.capturing = False
            return
        self.session.finished.connect(self._on_captured)
        self.session.canceled.connect(self._end_capture)
        self.session.start()
        self._update_esc()

    def _end_capture(self) -> None:
        if self.session is not None:
            self.session.deleteLater()
            self.session = None
        self.capturing = False
        self._update_esc()

    def _on_captured(self, image: QImage, anchor: QRect, origin: QPoint) -> None:
        self._end_capture()
        if self.mode == "capture":
            copy_image(image)
            self.tray.showMessage("截图已复制到剪贴板",
                                  f"{image.width()} × {image.height()}，可粘贴到聊天窗口，或在文件夹里粘贴为 PNG",
                                  self.icon, 2000)
            return
        self.job_id += 1
        self.job_translate = self.mode == "translate"
        self.job_image = image
        self.job_anchor = anchor
        if self.cfg.show_result or self.job_translate:
            self.result.show_busy(anchor, self.job_translate, image)
        engine = self.cfg.translate_engine if self.job_translate else ""
        self.ocr_requested.emit(self.job_id, qimage_to_bgr(image), self.cfg.keep_raw_lines, engine,
                                (origin.x(), origin.y()))

    # --------------------------------------------------------------- OCR
    def _on_ocr_done(self, job_id: int, text: str, elapsed: float) -> None:
        if job_id != self.job_id:
            return  # 已经有更新的截图在识别了
        self.job_source = text
        if text:
            QGuiApplication.clipboard().setText(text)  # 翻译模式下译文到了会再覆盖
        log.info("识别完成：%d 字，%.2fs", len(text), elapsed)
        if self.cfg.show_result or self.job_translate:
            self.result.show_text(text, elapsed)
        elif text:
            preview = text if len(text) <= 120 else text[:120] + "…"
            self.tray.showMessage("已复制到剪贴板", preview, self.icon, 2500)
        else:
            self.tray.showMessage(APP_NAME, "未识别到文字", self.icon, 2000)

    def _on_analyzed(self, job_id: int, paras) -> None:
        if job_id == self.job_id:
            self.job_paras = paras
            self.job_partial = [""] * len(paras)

    def _on_partial(self, job_id: int, index: int, zh: str) -> None:
        """离线大模型每翻完一段：立即把这一段贴到屏幕原位、写进弹窗（不必等整页翻完）。"""
        if job_id != self.job_id or index >= len(self.job_partial):
            return
        self.job_partial[index] = zh
        done = [(p, d) for p, d in zip(self.job_paras, self.job_partial) if d]
        text = "\n".join(d for _, d in done)
        try:
            self._pin(render_translation(self.job_image, done), text)
        except Exception:
            log.exception("生成译文图片失败")
        self.result.show_translation_progress(text, len(done), len(self.job_paras))

    def _on_translated(self, job_id: int, text: str, placed, error: str) -> None:
        if job_id != self.job_id:
            return
        image = None
        if not error:
            try:
                image = render_translation(self.job_image, placed)
                copy_image(image, text=text, name=time.strftime("翻译_%Y%m%d_%H%M%S.png"))
                self._pin(image, text)
            except Exception:
                log.exception("生成译文图片失败")
                QGuiApplication.clipboard().setText(text)
        elif self.cfg.translate_engine != "online":
            from . import translate
            if not translate.offline_ready():  # 在线连不上、又还没有离线模型：开始下载，下次就能离线翻译
                error += "（离线翻译模型尚未下载，已开始在后台下载，完成后托盘会提示）"
                self._download_offline_models()
        self.result.show_translation(text, image, error)

    def _pin(self, image: QImage, translation: str) -> None:
        """把译文图片原位盖在屏幕上截图的位置；同一次翻译重复调用时只更新已有的贴图。"""
        if self.job_pin is not None and self.job_pin.job_id == self.job_id and self.job_pin in self.pins:
            self.job_pin.set_content(image, translation)
            return
        pin = PinWindow(image, self.job_anchor, self.job_source, translation)
        pin.job_id = self.job_id
        self.job_pin = pin
        self.pins.append(pin)
        pin.destroyed.connect(lambda *_: (self.pins.remove(pin) if pin in self.pins else None, self._update_esc()))
        pin.show()
        pin.raise_()
        self._update_esc()

    def _preload_offline(self) -> None:
        """“仅离线”模式下，启动后就在后台加载离线大模型，第一次翻译不用等；
        精简版（不内置模型）还没下载过模型时，先在后台下载。"""
        if self.cfg.translate_engine == "offline":
            from . import translate
            if translate.offline_ready():
                import threading
                threading.Thread(target=translate.preload_llm, daemon=True).start()
            else:
                self._download_offline_models()

    def _download_offline_models(self) -> None:
        """后台下载离线翻译模型（约 2 GB），进度显示在托盘气泡里。"""
        if self.downloading:
            return
        self.downloading = True
        import threading

        from . import translate
        self.tray.showMessage(APP_NAME, "开始在后台下载离线翻译模型（约 2 GB，只需一次），期间仍可使用识别和在线翻译。",
                              self.icon, 4000)

        def run():
            try:
                translate.ensure_offline_models(lambda msg: self.download_note.emit(msg, False))
                self.download_note.emit("离线翻译模型已下载完成，现在可以离线翻译了。", True)
                if self.cfg.translate_engine == "offline":
                    translate.preload_llm()
            except Exception as e:
                log.exception("下载离线翻译模型失败")
                self.download_note.emit(f"离线翻译模型下载失败：{e}", True)
            finally:
                self.downloading = False

        threading.Thread(target=run, daemon=True).start()

    def _on_download_note(self, msg: str, final: bool) -> None:
        log.info(msg)
        self.tray.showMessage(APP_NAME, msg, self.icon, 5000 if final else 2000)

    def _apply_glossary(self) -> None:
        from . import translate
        translate.GLOSSARY_DOMAINS = list(self.cfg.glossary_domains)

    # ------------------------------------------------------------- Esc
    def _update_esc(self) -> None:
        """有东西在屏幕上就注册全局 Esc，否则注销，让 Esc 在其他程序里正常使用。"""
        needed = self.session is not None or self.result.isVisible() or bool(self.pins)
        if needed and self.esc is None:
            self.esc = EscListener()
            self.esc.triggered.connect(lambda _a: self.dismiss_all())
            self.esc.failed.connect(lambda m: log.warning("全局 Esc 注册失败：%s", m))
            self.esc.start()
        elif not needed and self.esc is not None:
            self.esc.stop()
            self.esc.deleteLater()
            self.esc = None

    def dismiss_all(self) -> None:
        """Esc：同时退出截图框、结果弹窗和所有贴图（和 QQ 截图一致）。"""
        if self.session is not None:
            self.session.cancel()
        self.result.hide()
        for pin in list(self.pins):
            pin.close()
        self._update_esc()

    def _on_ocr_failed(self, job_id: int, message: str) -> None:
        if job_id != self.job_id:
            return
        if self.cfg.show_result or self.job_translate:
            self.result.show_error(message)
        else:
            self._warn(f"识别失败：{message}")

    # -------------------------------------------------------------- misc
    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            QTimer.singleShot(150, lambda: self.start_capture("ocr"))

    def open_context(self) -> None:
        """翻译语境：用自然语言告诉本地大模型语境和词义，存成预设。"""
        from .contextdlg import ContextDialog
        if getattr(self, "context_dlg", None) is not None and self.context_dlg.isVisible():
            self.context_dlg.raise_()
            self.context_dlg.activateWindow()
            return
        self.context_dlg = ContextDialog()
        self.context_dlg.setWindowIcon(self.icon)
        self.context_dlg.show()
        self.context_dlg.raise_()
        self.context_dlg.activateWindow()

    def open_settings(self) -> None:
        if self.settings_dlg is not None:
            self.settings_dlg.raise_()
            self.settings_dlg.activateWindow()
            return
        self._stop_hotkey()  # 录入新快捷键、检测占用时不能有自己的热键在注册着
        self.settings_dlg = SettingsDialog(self.cfg)
        self.settings_dlg.setWindowIcon(self.icon)
        try:
            if self.settings_dlg.exec():
                self.cfg.save()
                self._apply_glossary()
                self._preload_offline()
        finally:
            self.settings_dlg = None
            self._start_hotkey()

    def quit(self) -> None:
        self._stop_hotkey()
        if self.esc is not None:
            self.esc.stop()
            self.esc = None
        self.tray.hide()
        self.ocr_thread.quit()
        self.ocr_thread.wait(15000)
        from . import translate
        translate.shutdown()  # 结束后台的离线翻译进程（即使漏掉，作业对象也会在退出时结束它）  # 等正在进行的识别/翻译结束，避免线程未退出就销毁
        QApplication.quit()


def _setup_logging() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(LOG_PATH, maxBytes=1 << 20, backupCount=1,
                                                           encoding="utf-8")]
    if sys.stderr is None:  # pythonw / 打包后的无控制台模式
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    sys.excepthook = lambda *exc: log.critical("未捕获的异常", exc_info=exc)


def _selftest() -> int:
    """不弹任何界面，识别一张内置测试图，结果写入日志。用于检查安装/打包是否完整。"""
    from PySide6.QtGui import QFont, QPainter

    from .ocr import OcrEngine

    expected = "GUOCR 自检 12345"
    img = QImage(360, 60, QImage.Format.Format_RGB32)
    img.fill(0xFFFFFFFF)
    p = QPainter(img)
    p.setFont(QFont("Microsoft YaHei UI", 16))
    p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, expected)
    p.end()
    engine = OcrEngine()
    text = engine.recognize(qimage_to_bgr(img))
    ok = "12345" in text
    log.info("自检%s（%s）：识别结果 %r", "通过" if ok else "失败", engine.device, text)
    from . import translate
    from .translate import translate_lines
    try:
        if not translate.offline_ready():  # 精简版不内置离线模型（首次使用时下载），跳过此项
            raise LookupError("skip")
        zh = translate_lines(["Check the clearance between the bearing and the shaft."], engine="offline")[0]
        log.info("离线翻译自检通过（%s）：%r", translate.__dict__["_server"].device or "小模型", zh)
        translate.shutdown()
    except LookupError:
        log.info("离线翻译模型未内置（精简版，首次使用时自动下载），跳过离线翻译自检")
    except Exception:
        log.exception("离线翻译自检失败")
        ok = False
    try:  # 截 Word 窗口时直接读取原文要用到的 COM 组件
        import pythoncom
        import win32com.client  # noqa: F401
        pythoncom.CoInitialize()
        log.info("Word 联动组件自检通过")
    except Exception:
        log.exception("Word 联动组件自检失败")
        ok = False
    return 0 if ok else 1


def main() -> int:
    _setup_logging()
    log.info("%s %s 启动", APP_NAME, __version__)
    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setQuitOnLastWindowClosed(False)
    if "--selftest" in sys.argv:
        return _selftest()
    if not winutil.acquire_single_instance():
        QMessageBox.information(None, APP_NAME, f"{APP_NAME} 已经在运行了，请在系统托盘中查看。")
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME, "系统托盘不可用，程序无法运行。")
        return 1
    _app = App()  # noqa: F841  保持引用
    return qapp.exec()
