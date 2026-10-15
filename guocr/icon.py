"""程序图标：运行时绘制，无需额外资源文件。"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPen, QPixmap

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_icon(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 64, size / 64)  # 在 64×64 画布上作图

    grad = QLinearGradient(0, 0, 0, 64)
    grad.setColorAt(0, QColor(45, 155, 255))
    grad.setColorAt(1, QColor(0, 100, 230))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)

    # 四角取景框
    pen = QPen(Qt.GlobalColor.white, 5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for x, y, dx, dy in ((13, 13, 1, 1), (51, 13, -1, 1), (13, 51, 1, -1), (51, 51, -1, -1)):
        p.drawPolyline([QPointF(x, y + 10 * dy), QPointF(x, y), QPointF(x + 10 * dx, y)])

    # 框内三行“文字”
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(Qt.GlobalColor.white)
    for y, w in ((23, 24), (30, 24), (37, 15)):
        p.drawRoundedRect(QRectF(20, y, w, 4.5), 2, 2)
    p.end()
    return img


def make_icon() -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(QPixmap.fromImage(render_icon(size)))
    return icon


def save_ico(path: str) -> None:
    """生成打包用的 .ico（python -m guocr.icon out.ico）。"""
    import os
    from io import BytesIO

    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    render_icon(256).save(buf, "PNG")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    Image.open(BytesIO(bytes(buf.data()))).save(path, sizes=[(s, s) for s in SIZES])


if __name__ == "__main__":
    import sys

    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication(sys.argv)
    save_ico(sys.argv[1] if len(sys.argv) > 1 else "guocr.ico")
