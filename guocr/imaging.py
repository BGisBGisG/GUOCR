"""图片相关：把译文贴回截图（图片翻译），以及把图片放进剪贴板。

剪贴板里同时放三种格式：
- 普通位图 / PNG：粘贴到 QQ、微信、Word、画图等程序时得到图片；
- 虚拟文件（FileGroupDescriptorW + FileContents）：在资源管理器的文件夹里 Ctrl+V 会生成一个 .png 文件，
  而且不会让别的程序把它当成“附件文件”来粘贴；
- 可选的纯文本：粘贴到文本框里时得到文字。
"""
import re
import struct
import time

import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QPainter

from .ocr import qimage_to_bgr

_WIN_MIME = 'application/x-qt-windows-mime;value="{}"'
FD_FILESIZE, FD_PROGRESSUI = 0x40, 0x4000
FONT_FAMILY = "Microsoft YaHei UI"


# ------------------------------------------------------------------ 剪贴板
def png_bytes(image: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


def _file_group_descriptor(name: str, size: int) -> bytes:
    """FILEGROUPDESCRIPTORW，只含一个文件。FILEDESCRIPTORW 共 592 字节。"""
    fname = name.encode("utf-16-le")[: 259 * 2].ljust(260 * 2, b"\0")
    desc = struct.pack("<I16s8s8sI24sII", FD_FILESIZE | FD_PROGRESSUI, b"\0" * 16, b"\0" * 8, b"\0" * 8,
                       0, b"\0" * 24, size >> 32, size & 0xFFFFFFFF) + fname
    return struct.pack("<I", 1) + desc


def copy_image(image: QImage, text: str | None = None, name: str | None = None) -> None:
    data = png_bytes(image)
    name = name or time.strftime("截图_%Y%m%d_%H%M%S.png")
    md = QMimeData()
    md.setImageData(image)
    md.setData("image/png", QByteArray(data))
    md.setData(_WIN_MIME.format("FileGroupDescriptorW"), QByteArray(_file_group_descriptor(name, len(data))))
    md.setData(_WIN_MIME.format("FileContents"), QByteArray(data))
    if text:
        md.setText(text)
    QGuiApplication.clipboard().setMimeData(md)


# ------------------------------------------------------------------ 图片翻译
def _line_style(img: np.ndarray, x0: int, y0: int, x1: int, y1: int):
    """估计一行文字的背景色（框边缘像素的中位数）、文字色（离背景最远的那批像素），
    以及文字笔画实际占据的范围（检测框通常比文字大一圈）。返回 (背景色, 文字色, (ix0, iy0, ix1, iy1))。"""
    h, w = img.shape[:2]
    ex0, ey0, ex1, ey1 = max(x0 - 2, 0), max(y0 - 2, 0), min(x1 + 2, w), min(y1 + 2, h)
    patch = img[ey0:ey1, ex0:ex1].reshape(-1, 3) if ey1 > ey0 and ex1 > ex0 else img.reshape(-1, 3)
    region = img[ey0:ey1, ex0:ex1]
    border = np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]]) if region.size else patch
    bg = np.median(border, axis=0)
    dist = np.abs(patch.astype(np.int16) - bg.astype(np.int16)).sum(axis=1)
    far = patch[dist >= np.percentile(dist, 95)] if len(patch) else patch
    fg = np.median(far, axis=0) if len(far) else np.zeros(3)
    to_color = lambda bgr: QColor(int(bgr[2]), int(bgr[1]), int(bgr[0]))  # noqa: E731
    bg_c, fg_c = to_color(bg), to_color(fg)
    ink = (region.astype(np.int16) - bg.astype(np.int16)).__abs__().sum(axis=2) > 60 if region.size else None
    ink_box = (x0, y0, x1, y1)
    if ink is not None and ink.any():
        ys, xs = np.nonzero(ink)
        ink_box = (ex0 + int(xs.min()), ey0 + int(ys.min()), ex0 + int(xs.max()) + 1, ey0 + int(ys.max()) + 1)
    # 对比度太低（比如原文本来就很淡）时，按背景亮度选黑或白
    if abs(bg_c.lightness() - fg_c.lightness()) < 80:
        fg_c = QColor(0, 0, 0) if bg_c.lightness() > 128 else QColor(255, 255, 255)
    return bg_c, fg_c, ink_box


_NO_LINE_START = set("，。、；：！？）》」』”’,.;:!?)]}%")  # 不能出现在行首的标点（避头尾）


def _wrap(text: str, fm: QFontMetricsF, width: float, first_indent: float = 0.0) -> list[str]:
    """按像素宽度折行：汉字可在任意字间断开，英文单词不拆开，标点不放到行首。"""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-_.]*|\s+|.", text)
    lines, cur, avail = [], "", width - first_indent
    for tok in tokens:
        trial = cur + tok
        if cur and fm.horizontalAdvance(trial.rstrip()) > avail and not tok.isspace() and tok not in _NO_LINE_START:
            lines.append(cur.rstrip())
            cur, avail = tok.lstrip(), width
        else:
            cur = trial
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def render_translation(image: QImage, pairs) -> QImage:
    """把译文按段落贴回原文位置：先用背景色抹掉原文各行，再在原段落区域内重新排版译文。

    pairs: [(layout.Paragraph, 译文), ...]，坐标为图片像素。
    字号从原文字高开始，放不下就逐步缩小；缩到原来的 55% 仍放不下时向下延伸。
    单行段落（按钮、标签等）不折行，太长时缩小字号或向右延伸。
    """
    out = image.convertToFormat(QImage.Format.Format_RGB32)
    out.setDevicePixelRatio(1.0)
    src = qimage_to_bgr(out)
    W, H = out.width(), out.height()
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    for para, dst in pairs:
        if not dst:
            continue
        styles = []
        for ln in para.lines:
            x0, y0 = max(int(ln.x0), 0), max(int(ln.y0), 0)
            x1, y1 = min(int(round(ln.x1)), W), min(int(round(ln.y1)), H)
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue
            bg, fg, ink = _line_style(src, x0, y0, x1, y1)
            p.fillRect(QRectF(x0 - 1, y0 - 1, x1 - x0 + 2, y1 - y0 + 2), bg)  # 抹掉原文
            styles.append((bg, fg, ink, x1))
        if not styles:
            continue
        bg, fg = styles[0][0], styles[0][1]
        inks = [s[2] for s in styles]
        ink_h = max(sorted(i[3] - i[1] for i in inks)[len(inks) // 2], 6)
        left = min(i[0] for i in inks[1:]) if len(inks) > 1 else inks[0][0]
        indent = max(inks[0][0] - left, 0)
        right = max(s[3] for s in styles)
        top = inks[0][1] - ink_h * 0.2
        bottom = inks[-1][3] + ink_h * 0.2
        pitch = (inks[-1][1] - inks[0][1]) / (len(inks) - 1) if len(inks) > 1 else ink_h * 1.5

        font = QFont(FONT_FAMILY)
        start = max(ink_h * 1.05, 8.0)
        size = start
        while True:
            font.setPixelSize(int(size))
            fm = QFontMetricsF(font)
            step = max(pitch * size / start, size * 1.2)
            if len(inks) == 1:
                rows = [dst]
                fits = fm.horizontalAdvance(dst) <= right - left + ink_h
            else:
                rows = _wrap(dst, fm, right - left, indent)
                fits = len(rows) * step <= bottom - top + step * 0.3
            if fits or size <= start * 0.55 or size <= 8:
                break
            size -= 0.5
        text_h = len(rows) * step
        text_w = max(fm.horizontalAdvance(r) for r in rows) + (indent if len(rows) == 1 else 0)
        # 实在放不下：单行向右、多行向下扩一块背景
        p.fillRect(QRectF(left - 1, top, max(right - left, text_w) + 2, max(bottom - top, text_h)), bg)
        p.setFont(font)
        p.setPen(fg)
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine
        y = top if len(inks) > 1 else (inks[0][1] + inks[0][3]) / 2 - step / 2
        for i, row in enumerate(rows):
            x = left + (indent if i == 0 else 0)
            p.drawText(QRectF(x, y + i * step, W - x, step), flags, row)
    p.end()
    return out
