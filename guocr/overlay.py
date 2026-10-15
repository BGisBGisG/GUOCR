"""全屏截图遮罩：拖拽框选，或像 QQ 截图一样悬停自动识别窗口、单击选中。

每块屏幕一个遮罩窗口，这样不同缩放比例的多显示器也能按物理像素精确裁剪。
"""
from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from . import winutil

ACCENT = QColor(0, 150, 255)
MASK = QColor(0, 0, 0, 120)
PANEL = QColor(0, 0, 0, 190)
MAG_W, MAG_H, MAG_ZOOM = 128, 96, 4
INFO_H = 40
DRAG_THRESHOLD = 4
HINT = "拖动鼠标框选区域 · 单击识别窗口 · Enter 确认 · Esc / 右键 退出"


class CaptureSession(QObject):
    finished = Signal(QImage, QRect, QPoint)  # 截取的图像（物理像素）、选区的全局逻辑坐标、选区左上角的屏幕物理坐标
    canceled = Signal()

    def __init__(self, window_rects=(), parent=None):
        super().__init__(parent)
        self._done = False
        # 先把所有屏幕截完再显示遮罩，避免遮罩自己被截进去
        shots = [(s, s.grabWindow(0)) for s in QGuiApplication.screens()]
        self.overlays = [CaptureOverlay(self, s, pix, window_rects) for s, pix in shots]

    def start(self) -> None:
        cursor_screen = QGuiApplication.screenAt(QCursor.pos())
        active = self.overlays[0]
        for ov in self.overlays:
            ov.show()
            if ov.screen_ref is cursor_screen:
                active = ov
        active.raise_()
        active.activateWindow()
        winutil.force_foreground(int(active.winId()))
        active.setFocus()

    def finish(self, image: QImage, global_rect: QRect, origin: QPoint) -> None:
        if not self._done:
            self._done = True
            self._close_all()
            self.finished.emit(image, global_rect, origin)

    def cancel(self) -> None:
        if not self._done:
            self._done = True
            self._close_all()
            self.canceled.emit()

    def _close_all(self) -> None:
        for ov in self.overlays:
            ov.close()
        self.overlays = []


class CaptureOverlay(QWidget):
    def __init__(self, session: CaptureSession, screen, pix: QPixmap, window_rects):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.session = session
        self.screen_ref = screen
        self.pix = pix
        self.image = pix.toImage()
        self.dpr = pix.devicePixelRatio()
        self.dim = QPixmap(pix)
        p = QPainter(self.dim)
        p.fillRect(self.dim.rect(), MASK)
        p.end()
        self.font_ = QFont("Microsoft YaHei UI", 9)

        geo = screen.geometry()
        self.setScreen(screen)
        self.setGeometry(geo)
        full = QRect(0, 0, geo.width(), geo.height())
        # Qt6 中屏幕左上角的逻辑坐标与物理坐标相同，屏内按 dpr 线性缩放
        ox, oy, d = geo.x(), geo.y(), self.dpr
        self.win_rects = []
        for l, t, r, b in window_rects:
            x0, y0 = round((l - ox) / d), round((t - oy) / d)
            x1, y1 = round((r - ox) / d), round((b - oy) / d)
            rect = QRect(x0, y0, x1 - x0, y1 - y0) & full
            if not rect.isEmpty():
                self.win_rects.append(rect)

        self.press_pos: QPoint | None = None
        self.sel: QRect | None = None
        pos = QCursor.pos() - geo.topLeft()
        self.cursor_pos: QPoint | None = pos if full.contains(pos) else None
        # 光标不在本屏时整屏保持变暗，不高亮任何窗口
        self.hover: QRect | None = self._window_at(pos) if self.cursor_pos is not None else None

    # ------------------------------------------------------------ helpers
    def _window_at(self, pos: QPoint) -> QRect:
        return next((r for r in self.win_rects if r.contains(pos)), self.rect())

    def _device_rect(self, rect: QRect) -> QRect:
        d = self.dpr
        x0, y0 = round(rect.left() * d), round(rect.top() * d)
        x1, y1 = round((rect.right() + 1) * d), round((rect.bottom() + 1) * d)
        return QRect(x0, y0, x1 - x0, y1 - y0) & self.image.rect()

    def _current_rect(self) -> QRect | None:
        return self.sel if self.sel is not None else self.hover

    def _finish(self, rect: QRect | None) -> None:
        if rect is None or rect.width() < 2 or rect.height() < 2:
            return
        dr = self._device_rect(rect)
        image = self.image.copy(dr)
        # Qt6 中屏幕左上角的逻辑坐标与物理坐标相同，屏内按像素偏移即为物理坐标（读取 Word 原文时要用）
        origin = self.screen_ref.geometry().topLeft() + dr.topLeft()
        self.session.finish(image, rect.translated(self.geometry().topLeft()), origin)

    # ------------------------------------------------------------- events
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.press_pos = e.position().toPoint()
            self.sel = None
        elif e.button() == Qt.MouseButton.RightButton:
            self.session.cancel()

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        self.cursor_pos = pos
        if self.press_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            if self.sel is not None or (pos - self.press_pos).manhattanLength() >= DRAG_THRESHOLD:
                self.sel = QRect(self.press_pos, pos).normalized() & self.rect()
        else:
            self.hover = self._window_at(pos)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.press_pos is None:
            return
        rect = self._current_rect()
        self.press_pos, self.sel = None, None
        self.hover = self._window_at(e.position().toPoint())
        self._finish(rect)
        self.update()

    def keyPressEvent(self, e):
        key = e.key()
        if key == Qt.Key.Key_Escape:
            self.session.cancel()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.press_pos is None:
            self._finish(self.hover)
        else:
            step = {Qt.Key.Key_Left: QPoint(-1, 0), Qt.Key.Key_Right: QPoint(1, 0),
                    Qt.Key.Key_Up: QPoint(0, -1), Qt.Key.Key_Down: QPoint(0, 1)}.get(key)
            if step is not None:  # 方向键逐像素微调光标
                QCursor.setPos(QCursor.pos() + step)

    def leaveEvent(self, e):
        if self.press_pos is None:
            self.cursor_pos, self.hover = None, None
            self.update()

    # ------------------------------------------------------------ painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.setFont(self.font_)
        p.drawPixmap(0, 0, self.dim)

        rect = self._current_rect()
        if rect is not None:
            p.drawPixmap(QRectF(rect), self.pix, QRectF(self._device_rect(rect)))
            pen = QPen(ACCENT, 2)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(rect).adjusted(1, 1, -1, -1))
            self._draw_size_label(p, rect)

        if self.sel is None and self.cursor_pos is not None:
            self._draw_hint(p)
        if self.cursor_pos is not None and self.rect().contains(self.cursor_pos):
            self._draw_magnifier(p)

    def _draw_panel(self, p: QPainter, rect: QRectF, text: str, align=Qt.AlignmentFlag.AlignCenter):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(PANEL)
        p.drawRoundedRect(rect, 3, 3)
        p.setPen(Qt.GlobalColor.white)
        p.drawText(rect, align, text)

    def _draw_size_label(self, p: QPainter, rect: QRect):
        dr = self._device_rect(rect)
        text = f"{dr.width()} × {dr.height()}"
        fm = p.fontMetrics()
        w, h = fm.horizontalAdvance(text) + 12, fm.height() + 6
        x, y = rect.left(), rect.top() - h - 4
        if y < 0:
            x, y = rect.left() + 6, rect.top() + 6
        self._draw_panel(p, QRectF(x, y, w, h), text)

    def _draw_hint(self, p: QPainter):
        fm = p.fontMetrics()
        w, h = fm.horizontalAdvance(HINT) + 24, fm.height() + 12
        self._draw_panel(p, QRectF((self.width() - w) / 2, 12, w, h), HINT)

    def _draw_magnifier(self, p: QPainter):
        pos, d = self.cursor_pos, self.dpr
        x, y = pos.x() + 20, pos.y() + 24
        if x + MAG_W > self.width():
            x = pos.x() - 20 - MAG_W
        if y + MAG_H + INFO_H > self.height():
            y = pos.y() - 24 - MAG_H - INFO_H
        target = QRectF(x, y, MAG_W, MAG_H)

        sw, sh = MAG_W / MAG_ZOOM * d, MAG_H / MAG_ZOOM * d
        cx, cy = (pos.x() + 0.5) * d, (pos.y() + 0.5) * d
        p.fillRect(target, Qt.GlobalColor.black)
        p.drawPixmap(target, self.pix, QRectF(cx - sw / 2, cy - sh / 2, sw, sh))
        p.setPen(QPen(QColor(0, 150, 255, 150), 1))
        c_ = target.center()
        p.drawLine(QPointF(c_.x(), target.top()), QPointF(c_.x(), target.bottom()))
        p.drawLine(QPointF(target.left(), c_.y()), QPointF(target.right(), c_.y()))
        p.setPen(QPen(Qt.GlobalColor.white, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(target)

        px = min(max(int(pos.x() * d), 0), self.image.width() - 1)
        py = min(max(int(pos.y() * d), 0), self.image.height() - 1)
        c = self.image.pixelColor(px, py)
        info = f"({px}, {py})\nRGB({c.red()}, {c.green()}, {c.blue()})"
        self._draw_panel(p, QRectF(x, y + MAG_H, MAG_W, INFO_H), info)
