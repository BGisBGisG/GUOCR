"""识别结果小窗：出现在选区旁边，不抢焦点，可编辑后再次复制。

翻译模式下最上面是贴好译文的截图（可复制图片/另存为），下面两栏分别是原文和译文，各自带「复制」按钮。
"""
import time

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .imaging import copy_image

TITLE_BAR_H = 32  # 窗口首次显示前拿不到边框尺寸，用于估算


class _Pane(QWidget):
    """标题 + 文本框 + 复制按钮。"""

    def __init__(self, title: str, on_copied):
        super().__init__()
        self.title = QLabel(title)
        self.title.setStyleSheet("font-weight: bold;")
        self.copy_btn = QPushButton("复制")
        self.copy_btn.clicked.connect(lambda: on_copied(self.copy()))
        self.edit = QPlainTextEdit()
        self.edit.setFont(QFont("Microsoft YaHei UI", 10))
        head = QHBoxLayout()
        head.addWidget(self.title, 1)
        head.addWidget(self.copy_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.edit, 1)

    def set_text(self, text: str, placeholder: str = "") -> None:
        self.edit.setPlaceholderText(placeholder)
        self.edit.setPlainText(text)
        self.copy_btn.setEnabled(bool(text))

    def copy(self) -> str:
        text = self.edit.toPlainText()
        if text:
            QGuiApplication.clipboard().setText(text)
        return text


class _ImagePane(QWidget):
    """译文图片预览：按窗口宽度缩放显示，可复制或另存为 PNG。"""

    MAX_W, MAX_H = 640, 360

    def __init__(self, on_status):
        super().__init__()
        self.image: QImage | None = None
        self._on_status = on_status
        title = QLabel("译文图片")
        title.setStyleSheet("font-weight: bold;")
        self.copy_btn = QPushButton("复制图片")
        self.copy_btn.clicked.connect(self._copy)
        self.save_btn = QPushButton("另存为…")
        self.save_btn.clicked.connect(self._save)
        self.view = QLabel()
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.view.setStyleSheet("background: #e8e8e8; color: #888;")
        head = QHBoxLayout()
        head.addWidget(title, 1)
        head.addWidget(self.copy_btn)
        head.addWidget(self.save_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.view, 1)

    def preview_size(self, image: QImage) -> QSize:
        return image.size().scaled(QSize(self.MAX_W, self.MAX_H), Qt.AspectRatioMode.KeepAspectRatio)             if image.width() > self.MAX_W or image.height() > self.MAX_H else image.size()

    def set_image(self, image: QImage | None, placeholder: str = "") -> None:
        self.image = image
        self.copy_btn.setEnabled(image is not None)
        self.save_btn.setEnabled(image is not None)
        self.view.setText("" if image is not None else placeholder)
        self._refresh()

    def _refresh(self) -> None:
        if self.image is None:
            self.view.setPixmap(QPixmap())
            return
        pix = QPixmap.fromImage(self.image)
        if pix.width() > self.view.width() or pix.height() > self.view.height():
            pix = pix.scaled(self.view.size(), Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        self.view.setPixmap(pix)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh()

    def _copy(self) -> None:
        if self.image is not None:
            copy_image(self.image, name=time.strftime("翻译_%Y%m%d_%H%M%S.png"))
            self._on_status("译文图片已复制，可直接粘贴到聊天窗口或文件夹")

    def _save(self) -> None:
        if self.image is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存译文图片", time.strftime("翻译_%Y%m%d_%H%M%S.png"),
                                              "PNG 图片 (*.png)")
        if path:
            self._on_status("已保存" if self.image.save(path, "PNG") else "保存失败")


class ResultWindow(QWidget):
    visibility_changed = Signal(bool)
    def __init__(self, icon=None):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("识别结果")
        if icon is not None:
            self.setWindowIcon(icon)
        # 不抢焦点：用户可以直接在原来的程序里 Ctrl+V
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self.picture = _ImagePane(lambda msg: self.status.setText(msg))
        self.source = _Pane("原文", self._on_copied)
        self.target = _Pane("译文", self._on_copied)
        self.status = QLabel()
        self.status.setStyleSheet("color: #666;")
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.hide)

        bar = QHBoxLayout()
        bar.addWidget(self.status, 1)
        bar.addWidget(close_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.picture, 3)
        layout.addWidget(self.source, 1)
        layout.addWidget(self.target, 1)
        layout.addLayout(bar)
        self._translate = False
        self._elapsed = 0.0

    def show_busy(self, anchor: QRect, translate: bool, image: QImage | None = None) -> None:
        self._translate = translate
        self.setWindowTitle("翻译结果" if translate else "识别结果")
        self.source.title.setVisible(translate)  # 只有一栏时不需要“原文”标题
        self.target.setVisible(translate)
        self.picture.setVisible(False)  # 译文图片直接盖在屏幕原位，弹窗里只放文字
        self.picture.set_image(None, "识别、翻译中…")
        self.source.set_text("", "识别中…")
        self.target.set_text("", "等待识别完成…")
        self.status.setText("正在识别…")
        self.resize(480, 360 if translate else 240)
        self._place(anchor)
        self.show()
        self.raise_()

    def show_text(self, text: str, elapsed: float) -> None:
        self._elapsed = elapsed
        self.source.set_text(text, "未识别到文字")
        if not text:
            self.status.setText("未识别到文字")
            self.target.set_text("", "")
            self.picture.set_image(None, "未识别到文字")
        elif self._translate:
            self.target.set_text("", "翻译中…")
            self.status.setText(f"识别完成 · {len(text)} 字 · {elapsed:.2f} 秒 · 正在翻译…")
        else:
            self.status.setText(f"已复制到剪贴板 · {len(text)} 字 · {elapsed:.2f} 秒")
        self._ensure_visible()

    def show_translation(self, text: str, image: QImage | None, error: str) -> None:
        if error:
            self.target.set_text("", error)
            self.picture.set_image(None, error)
            self.status.setText("翻译失败，原文已复制到剪贴板")
        else:
            self.target.set_text(text)
            self.picture.set_image(image, "")
            from . import context
            p = context.active_preset()
            self.status.setText("译文已覆盖在原位（右键可复制/另存，双击关闭）；剪贴板：图片+译文"
                                + (f"；语境：{p.name}" if p else ""))
        self._ensure_visible()

    def show_translation_progress(self, text: str, done: int, total: int) -> None:
        self.target.set_text(text)
        self.status.setText(f"正在翻译… {done}/{total} 段（已翻好的段落已贴到原位）")
        self._ensure_visible()

    def show_error(self, message: str) -> None:
        self.source.set_text("")
        self.target.set_text("")
        self.picture.set_image(None, "")
        self.status.setText(f"识别失败：{message}")
        self._ensure_visible()

    def _on_copied(self, text: str) -> None:
        if text:
            self.status.setText(f"已复制到剪贴板 · {len(text)} 字")

    def _ensure_visible(self) -> None:
        if not self.isVisible():
            self.show()

    def _place(self, anchor: QRect) -> None:
        """优先放在选区下方，放不下就放上方，再不行就贴屏幕底部。"""
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        w, h = self.width(), self.height() + TITLE_BAR_H
        y = anchor.bottom() + 8
        if y + h > avail.bottom():
            y = anchor.top() - h - 8
            if y < avail.top():
                y = avail.bottom() - h
        x = min(max(anchor.left(), avail.left()), avail.right() - w)
        self.move(x, max(y, avail.top()))

    def showEvent(self, e):
        super().showEvent(e)
        self.visibility_changed.emit(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        self.visibility_changed.emit(False)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)
