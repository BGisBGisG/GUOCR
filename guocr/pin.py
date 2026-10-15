"""贴图窗口：把译文图片原位覆盖在屏幕上截图的位置，盖住原文。

- 可拖动；双击或 Esc 关闭；
- 右键菜单：复制图片 / 复制译文 / 复制原文 / 另存为 / 关闭。
"""
import time

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QAction, QColor, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import QFileDialog, QMenu, QWidget

from .imaging import copy_image


class PinWindow(QWidget):
    def __init__(self, image: QImage, anchor: QRect, source: str, translation: str):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setToolTip("拖动可移动 · 双击或 Esc 关闭 · 右键更多操作")
        self.image = QImage(image)
        # 截图是物理像素，选区是逻辑坐标：按两者之比设 dpr，贴回去时与原文逐像素重合
        self.image.setDevicePixelRatio(image.width() / max(anchor.width(), 1))
        self.source, self.translation = source, translation
        self._drag: QPoint | None = None
        self.setGeometry(anchor)

    def set_content(self, image: QImage, translation: str) -> None:
        """逐段翻译时更新贴图内容（位置不变）。"""
        dpr = self.image.devicePixelRatio()
        self.image = QImage(image)
        self.image.setDevicePixelRatio(dpr)
        self.translation = translation
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawImage(0, 0, self.image)
        p.setPen(QPen(QColor(0, 150, 255, 200), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        for text, fn in (("复制图片", lambda: copy_image(self.image, name=time.strftime("翻译_%Y%m%d_%H%M%S.png"))),
                         ("复制译文", lambda: QGuiApplication.clipboard().setText(self.translation)),
                         ("复制原文", lambda: QGuiApplication.clipboard().setText(self.source)),
                         ("另存为…", self._save), (None, None), ("关闭", self.close)):
            if text is None:
                menu.addSeparator()
            else:
                act = QAction(text, menu)
                act.triggered.connect(fn)
                menu.addAction(act)
        menu.exec(e.globalPos())

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存译文图片", time.strftime("翻译_%Y%m%d_%H%M%S.png"),
                                              "PNG 图片 (*.png)")
        if path:
            self.image.save(path, "PNG")
