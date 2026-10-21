"""OCR 引擎（RapidOCR + onnxruntime，离线）与文字版面还原。"""
import logging
import difflib
import re
import time

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

from .layout import Paragraph, analyze, japanese_forms, to_text

log = logging.getLogger(__name__)

PAD = 16  # 给截图四周补边，避免贴边的文字漏检


def qimage_to_bgr(image: QImage) -> np.ndarray:
    img = image.convertToFormat(QImage.Format.Format_BGR888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), np.uint8, count=bpl * h)
    return buf.reshape(h, bpl)[:, : w * 3].reshape(h, w, 3).copy()


def detect_pages(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    """找出截图里灰色（或其他非白）背景上的白色页面，如 Word / PDF 阅读器并排、多行显示的多页。

    返回各页矩形（不含补边）；只有一页或背景本身就是白色时返回空列表（按整块区域分析）。
    """
    import cv2
    h, w = img.shape[:2]
    scale = 2 if min(h, w) > 600 else 1  # 缩小一半做连通域，够用且快
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)[::scale, ::scale]
    white = (gray >= 246).astype(np.uint8)
    if white.mean() > 0.93:  # 几乎全白：不是“灰底上的页面”
        return []
    n, _, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
    gh, gw = gray.shape
    pages = []
    for x, y, bw, bh, area in stats[1:]:
        if bw < 0.06 * gw or bh < 0.12 * gh or bw * bh < 0.015 * gw * gh:
            continue
        if area < 0.55 * bw * bh:  # 页面里大部分应是白色（文字之间的空白连成一片）
            continue
        pages.append((int(x * scale), int(y * scale), int((x + bw) * scale), int((y + bh) * scale)))
    # 真正的页面是矩形：四个角（略往里）都是白的；漫画的对话气泡多是椭圆，角上不是白色，不能当页面
    full_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    def rectangular(r):
        x0, y0, x1, y1 = r
        ins = max(3, (x1 - x0) // 40)
        pts = [(y0 + ins, x0 + ins), (y0 + ins, x1 - 1 - ins), (y1 - 1 - ins, x0 + ins), (y1 - 1 - ins, x1 - 1 - ins)]
        return all(0 <= y < h and 0 <= x < w and full_gray[y, x] >= 240 for y, x in pts)

    pages = [r for r in pages if rectangular(r)]
    # 多页并排时各页宽度基本一致（被截掉一半的页高度可以不同）
    if pages:
        wmed = sorted(r[2] - r[0] for r in pages)[len(pages) // 2]
        pages = [r for r in pages if abs((r[2] - r[0]) - wmed) <= 0.1 * wmed]
    if len(pages) < 2 or sum((r[2] - r[0]) * (r[3] - r[1]) for r in pages) > 0.97 * w * h:
        return []
    return pages


_MARK_ONLY = re.compile(r"^[\s.·…\-—_=]*(分页符|分节符.*|分栏符|Page Break|Section Break.*|Column Break)[\s.·…\-—_=]*$",
                        re.IGNORECASE)


def clean_marks(text: str) -> str:
    """去掉 Word “显示编辑标记”带来的干扰：空格显示成的“·”（常被识别成 · 或 -）、¶ 段落标记、分页符字样。"""
    if _MARK_ONLY.match(text):
        return ""
    text = text.replace("¶", "").replace("↵", "")
    # 一个框里连字符比空格还多：多半是空格点被识别成了“-”（如 graph-construction-took-about）
    if text.count("-") >= 3 and text.count("-") > text.count(" "):
        text = re.sub(r"(?<=[A-Za-z0-9,.;:)\]])-(?=[A-Za-z0-9(\[])", " ", text)
    # 与西文相邻的“·”是空格点（中文人名里的间隔号两侧是汉字，不受影响）
    text = re.sub(r"\s*·\s*(?=[!-~])|(?<=[!-~])\s*·\s*", " ", text)
    # “word- word”：空格点被识别成“-”后又带了空格
    text = re.sub(r"(?<=[A-Za-z0-9,.;:)\]])-\s+(?=[A-Za-z0-9(\[])", " ", text)
    text = re.sub(r"\s+-$", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _pad(img: np.ndarray) -> np.ndarray:
    border = np.concatenate([img[0], img[-1], img[:, 0], img[:, -1]])
    color = np.median(border, axis=0).astype(np.uint8)
    h, w = img.shape[:2]
    out = np.empty((h + 2 * PAD, w + 2 * PAD, 3), np.uint8)
    out[:] = color
    out[PAD:PAD + h, PAD:PAD + w] = img
    return out


class OcrEngine:
    def __init__(self):
        from rapidocr import RapidOCR  # 导入较慢，放到后台线程里做

        from .winutil import best_gpu_adapter
        params = {
            "Global.log_level": "error",
            # 默认的 min/736 会把小截图放大好几倍再检测，慢 5~8 倍；屏幕文字按原尺寸检测即可
            "Det.limit_type": "max",
            "Det.limit_side_len": 2000,
            "Global.use_cls": False,  # 屏幕上几乎没有倒置文字，省掉方向分类
        }
        # 有显卡就用 DirectML（基于 DX12，NVIDIA/AMD/Intel 通用，无需另装运行库），整页识别快约 4 倍；
        # 选独立显存最大的那块，避免笔记本默认用上核显。显卡不可用时退回 CPU。
        gpu = best_gpu_adapter()
        self.device = "CPU"
        if gpu is not None:
            try:
                from rapidocr.inference_engine.onnxruntime import provider_config
                # RapidOCR 把配置里的 dml_ep_cfg 以 OmegaConf 对象传给 onnxruntime，会被拒绝；这里直接给普通字典
                provider_config.ProviderConfig.dml_ep_cfg = lambda _self, _id=gpu[0]: {"device_id": _id}
                engine = RapidOCR(params={**params, "EngineConfig.onnxruntime.use_dml": True, "Rec.rec_batch_num": 16})
                engine(np.full((48, 160, 3), 255, np.uint8))
                # 以实际生效的执行设备为准（onnxruntime 在显卡不可用时会悄悄退回 CPU）
                sessions = (engine.text_det.session.session, engine.text_rec.session.session)
                if all(s_.get_providers()[0] == "DmlExecutionProvider" for s_ in sessions):
                    self._engine = engine
                    self.device = f"显卡 {gpu[1]}"
                    return
                log.warning("显卡识别不可用（实际执行设备 %s），改用 CPU", sessions[0].get_providers())
            except Exception:
                log.exception("显卡识别初始化失败，改用 CPU")
        self._engine = RapidOCR(params=params)

    def analyze(self, img: np.ndarray, origin: tuple[int, int] | None = None) -> list[Paragraph]:
        """识别并做版面分析，返回按阅读顺序排列的段落（坐标为原图像素，已去掉补边）。

        origin：截图左上角的屏幕物理坐标。给出时若截图区域在 Word 窗口上，会改用 Word 里的原文（见 wordlink）。"""
        padded = _pad(img)
        result = self._engine(padded)
        if result.boxes is None or not result.txts:
            return []
        items = []
        split = self._split_wide_gaps(padded, result.boxes, result.txts)
        for x0, y0, x1, y1, txt in self._recognize_vertical(padded, self._split_columns(padded, split)):
            items.append((x0 - PAD, y0 - PAD, x1 - PAD, y1 - PAD, clean_marks(txt.strip())))
        pages = detect_pages(img)
        paras = analyze(items, pages)
        japanese_forms(paras)
        if origin is not None and paras:
            from . import wordlink
            from .layout import _order_pages
            ox, oy = origin
            h, w = img.shape[:2]
            regions = [(ox + x0, oy + y0, ox + x1, oy + y1) for x0, y0, x1, y1 in (_order_pages(pages) or [(0, 0, w, h)])]
            try:
                exact = wordlink.refine(paras, regions, (ox + w // 2, oy + h // 2))
            except Exception:
                log.exception("Word 原文校正失败，使用识别结果")
                exact = None
            if exact:
                return exact
        return paras

    def _split_wide_gaps(self, img: np.ndarray, boxes, txts) -> list[tuple[float, float, float, float, str]]:
        """修正文字检测把不该连在一起的内容连成一个框：
        - 行距很紧时，上下两行可能被检测成一个框（高度约为普通行的两倍）：按框内的整行空白切成多行；
        - 同一高度、只隔一两个字宽的两块内容（网页正文与侧栏、表格相邻单元格）连成一个框，识别时空白也被吞掉
          （“Pages 28” + “remain” → “28rémain”）：按框内宽于 1.3 个字高的空白列切开（正常词间距约 0.3 个字高）。
        切开的各段单独重新识别。"""
        from rapidocr.ch_ppocr_rec.typings import TextRecInput
        gray = img.mean(axis=2)
        rects = []
        for box in boxes:
            box = np.asarray(box)
            rects.append((int(box[:, 0].min()), int(box[:, 1].min()),
                          int(np.ceil(box[:, 0].max())), int(np.ceil(box[:, 1].max()))))
        med_h = float(np.median([r[3] - r[1] for r in rects])) if rects else 0
        lefts = np.array([r[0] for r in rects])

        def column_start(x: float) -> bool:
            """x 处是否像另一栏的开头：至少还有两个文字框的左端与它对齐（网页侧栏、表格列）。"""
            return int(np.sum(np.abs(lefts - x) <= 6)) >= 2

        def ink_mask(x0, y0, x1, y1):
            region = gray[max(y0, 0):y1, max(x0, 0):x1]
            if region.size == 0:
                return None
            bg = np.median(np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]]))
            return np.abs(region - bg) > 40

        def runs(flags: np.ndarray, value: bool):
            """flags 中连续等于 value 的区间 [(起, 止)]。"""
            out_, start = [], None
            for i, f in enumerate(flags):
                if f == value and start is None:
                    start = i
                elif f != value and start is not None:
                    out_.append((start, i))
                    start = None
            if start is not None:
                out_.append((start, len(flags)))
            return out_

        out, pending = [], []  # pending: 待重新识别的 (x0, y0, x1, y1, 图像)
        for (x0, y0, x1, y1), txt in zip(rects, txts):
            pieces = [(x0, y0, x1, y1)]
            # ① 异常高的框：按整行空白切成多行
            if med_h and y1 - y0 > 1.6 * med_h and x1 - x0 > 1.5 * (y1 - y0):  # 竖排的列（高而窄）不在这里切
                m = ink_mask(x0, y0, x1, y1)
                if m is not None:
                    bands = [(a, b) for a, b in runs(m.any(axis=1), True) if b - a >= 0.3 * med_h]
                    if len(bands) >= 2:
                        pieces = [(x0, y0 + a - 1, x1, y0 + b + 1) for a, b in bands]
            # ② 每一行：按宽空白列切开
            final = []
            for px0, py0, px1, py1 in pieces:
                h = py1 - py0
                m = ink_mask(px0, py0, px1, py1)
                if h < 6 or m is None or (px1 - px0) < 4 * h:
                    final.append((px0, py0, px1, py1))
                    continue
                cols = m.any(axis=0)
                ink = np.flatnonzero(cols)
                if ink.size == 0:
                    continue
                # 空白宽于 1.3 个字高就切；宽于 0.8 个字高且右边的文字正好对齐到另一栏的开头也切
                gaps = [(a, b) for a, b in runs(cols[ink[0]:ink[-1] + 1], False)
                        if b - a >= max(1.3 * h, 12)
                        or (b - a >= max(0.8 * h, 10) and column_start(px0 + ink[0] + b))]
                edges = [ink[0]] + [ink[0] + v for a, b in gaps for v in (a, b)] + [ink[-1] + 1]
                for a, b in zip(edges[::2], edges[1::2]):
                    final.append((px0 + a - 2, py0, px0 + b + 2, py1))
            if final == [(x0, y0, x1, y1)] or (len(final) == 1 and len(pieces) == 1):
                out.append((x0, y0, x1, y1, txt))  # 没有切开：沿用原识别结果
                continue
            for fx0, fy0, fx1, fy1 in final:
                pending.append((fx0, fy0, fx1, fy1, img[max(fy0, 0):fy1, max(fx0, 0):fx1]))
        if pending:
            rec = self._engine.text_rec(TextRecInput(img=[p[4] for p in pending]))
            for (sx0, sy0, sx1, sy1, _), t in zip(pending, rec.txts):
                if t.strip():
                    out.append((sx0, sy0, sx1, sy1, t))
        return out

    # ---------------------------------------------------------------- 竖排识别
    @staticmethod
    def _vertical_cells(mask: np.ndarray, w: int) -> list[tuple[int, int]]:
        """把竖排的一列按字切开：按行投影找空白，把同一个字的几段笔画（如“三”“二”）合并成约一个字高的格子，
        粘连在一起过高的按字宽均分。竖排标点（右侧的小点）单独成格；“！”下面的点并回上一格。"""
        ink = mask.any(axis=1)
        runs, s = [], None
        for i, f in enumerate(ink):
            if f and s is None:
                s = i
            elif not f and s is not None:
                runs.append([s, i])
                s = None
        if s is not None:
            runs.append([s, len(ink)])

        def xr(a, b):
            xs = np.flatnonzero(mask[a:b].any(axis=0))
            return (xs[0], xs[-1] + 1) if xs.size else (0, 0)

        merged: list[list[int]] = []
        for a, b in runs:
            if merged:
                pa, pb = merged[-1]
                x0, x1 = xr(a, b)
                px0, px1 = xr(pa, pb)
                small = b - a < 0.4 * w and x1 - x0 < 0.4 * w
                punct = small and x0 > 0.45 * w                       # 、。在格子右上：不并
                bang = small and x1 - x0 < 0.25 * w and b - a > 0.5 * (x1 - x0) \
                    and px1 - px0 < 0.3 * w and pb - pa > 0.45 * w \
                    and abs((x0 + x1) - (px0 + px1)) < 0.3 * w           # “！”的竖条 + 下面的点
                # 字最下面一笔扁平的横（“題”“且”的底横）与上面隔得稍远时也并回去
                foot = b - a < 0.2 * w and x1 - x0 > 0.4 * w and pb - pa > 0.7 * w and px1 - px0 > 0.5 * w \
                    and (b - pa) <= 1.1 * w and (a - pb) < 0.2 * w
                if bang or foot or (not punct and (b - pa) <= 1.05 * w and (a - pb) < 0.12 * w):
                    merged[-1][1] = b
                    continue
            merged.append([a, b])
        cells = []
        for a, b in merged:
            n = max(1, round((b - a) / (0.95 * w))) if b - a > 1.3 * w else 1
            cells += [(a + (b - a) * k // n, a + (b - a) * (k + 1) // n) for k in range(n)]
        return cells

    @staticmethod
    def _strip_frame(mask: np.ndarray) -> np.ndarray:
        """去掉混进列里的气泡边框、分格线：贯穿大半列高的竖线、贯穿整列宽的横线（字的笔画不会这么长）。"""
        mask = mask.copy()
        h, w = mask.shape
        if h >= 2 * w:
            long_x = mask.mean(axis=0) > 0.6
            mask[:, long_x] = False
        long_y = mask.mean(axis=1) > 0.95
        if long_y.any() and w >= 8:
            mask[long_y] = False
        return mask

    def _vertical_to_line(self, img: np.ndarray, rect) -> np.ndarray | None:
        """竖排一列 → 横排一行：逐字放进方格并按字形摆正后横向拼接，再交给（为横排训练的）识别模型。
        - 竖排标点（、。）位于字格右上方 → 移到横排标点的位置（左下）；
        - 竖排的长音“ー”是细长竖线 → 改画成横线；
        - 竖排的括号「」『』（角在右上/左下）→ 转 90° 成横排括号；
        - 小写假名（ぁっゃ…）→ 贴格子底部（横排小写假名的位置）；
        - 其余（含“一”这类扁平字）在格子里居中。
        直接把整列旋转 90° 去识别，会漏掉标点、长音和小写假名（实测准确率 92% → 本方法 97%）。"""
        x0, y0, x1, y1 = rect
        col = img[max(y0, 0):y1, max(x0, 0):x1]
        if col.size == 0:
            return None
        g = col.mean(axis=2)
        bg = float(np.median(np.concatenate([g[0], g[-1]])))
        raw = np.abs(g - bg) > 50
        mask = self._strip_frame(raw)
        xs = np.flatnonzero(mask.any(axis=0))
        if xs.size == 0:
            return None
        if (mask != raw).any():  # 去掉了边框：按真正有字的宽度裁列
            col, mask = col[:, xs[0]:xs[-1] + 1].copy(), mask[:, xs[0]:xs[-1] + 1]
            col[~mask] = int(bg)
        w = col.shape[1]
        tiles = []
        for a, b in self._vertical_cells(mask, w):
            ys, xs = np.nonzero(mask[a:b])
            if ys.size == 0:
                continue
            iy0, iy1, ix0, ix1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            glyph = col[a:b][iy0:iy1, ix0:ix1]
            gm = mask[a:b][iy0:iy1, ix0:ix1]
            ih, iw = glyph.shape[:2]
            tile = np.full((w, w, 3), int(bg), np.uint8)
            if ih < 0.45 * w and iw < 0.45 * w and ix0 > 0.35 * w:  # 竖排标点 → 左下
                m = max(1, w // 10)
                tile[w - ih - m:w - m, m:m + iw] = glyph
            elif ih > 0.5 * w and iw < 0.22 * w and gm.any(axis=1).all() and abs(ix0 + ix1 - w) < 0.4 * w:  # 竖排长音（连续竖条；“！”中间断开）→ 横线（须在列中间；贴着列边的竖条是相邻那列伸过来的」）
                th = max(2, iw)
                tile[w // 2 - th // 2:w // 2 - th // 2 + th, w // 8:w - w // 8] = glyph.min(axis=(0, 1))
            elif self._is_bracket(gm, w):  # 竖排括号 → 横排括号
                r = np.ascontiguousarray(np.rot90(glyph, 1))
                rh, rw = min(r.shape[0], w), min(r.shape[1], w)
                tile[(w - rh) // 2:(w - rh) // 2 + rh, (w - rw) // 2:(w - rw) // 2 + rw] = r[:rh, :rw]
            elif 0.25 * w < ih < 0.72 * w and 0.25 * w < iw < 0.72 * w and gm.mean() > 0.12:  # 小写假名 → 贴底
                m = max(1, w // 12)
                ox = (w - iw) // 2
                tile[w - ih - m:w - m, ox:ox + iw] = glyph
            else:
                ih, iw = min(ih, w), min(iw, w)
                oy, ox = (w - ih) // 2, (w - iw) // 2
                tile[oy:oy + ih, ox:ox + iw] = glyph[:ih, :iw]
            tiles.append(tile)
            tiles.append(np.full((w, max(2, w // 8), 3), int(bg), np.uint8))
        return np.concatenate(tiles, axis=1) if tiles else None

    @staticmethod
    def _is_bracket(gm: np.ndarray, w: int) -> bool:
        """竖排的「」：扁平的直角——一条横线加一端短短的竖钩（﹁ 钩在右、﹂ 钩在左），中间空。"""
        h, ww = gm.shape
        if ww < 0.5 * w or h > 0.5 * w or h < 3 or ww < 3 * h:
            return False
        e = max(1, round(0.4 * h))  # 线条粗细（放大过的小字边缘发虚，留点余量）
        top, bot = gm[:e].any(axis=0).mean() > 0.8, gm[-e:].any(axis=0).mean() > 0.8
        left, right = gm[:, :e].any(axis=1).mean() > 0.7, gm[:, -e:].any(axis=1).mean() > 0.7
        body = gm[e:-e, e:-e]
        return ((top and right and not left) or (bot and left and not right)) and body.mean() < 0.15

    VERTICAL_RATIO = 1.4  # 列高 / 列宽 超过它就算竖排的一列（“あ、”“誰が”这种两个字的短列也算；横排两个字约 0.5）

    def _split_columns(self, img: np.ndarray, items: list) -> list:
        """按像素找出框里的竖排字带：漫画气泡里的竖排字小、列又挨得近，文字检测常把几列连成一个框，
        再当横排去读（“フリにして著作権背景を…”）。按列投影找出一条条笔画带，若是又高又窄的竖条
        （横排文字按列投影是一个个近似方形的字，不会被误判），就把框拆成各列；拆出的列标记为待重新识别。"""
        out = []
        gray = img.mean(axis=2)
        for x0, y0, x1, y1, txt in items:
            ix0, iy0, ix1, iy1 = int(x0), int(y0), int(np.ceil(x1)), int(np.ceil(y1))
            region = gray[max(iy0, 0):iy1, max(ix0, 0):ix1]
            if region.size == 0 or not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", txt) \
                    or re.search(r"[A-Za-z]{4,}", txt):  # 只处理中日文
                out.append((x0, y0, x1, y1, txt))
                continue
            bg = np.median(np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]]))
            ink = np.abs(region - bg) > 60
            cols = ink.any(axis=0)
            # 列投影上的笔画带（允许很窄的空隙：同一列里各字左右宽窄不一）
            runs, s = [], None
            for i, f in enumerate(cols):
                if f and s is None:
                    s = i
                elif not f and s is not None:
                    runs.append([s, i])
                    s = None
            if s is not None:
                runs.append([s, len(cols)])
            merged: list[list[int]] = []
            widest = max((b - a for a, b in runs), default=0)
            for a, b in runs:
                # 空隙很窄，或这一段/上一段只是很窄的碎条（某个字的一撇一点）：并入上一段
                if merged and (a - merged[-1][1] < max(2, 0.25 * min(b - a, merged[-1][1] - merged[-1][0]))
                               or b - a < 0.35 * widest or merged[-1][1] - merged[-1][0] < 0.35 * widest) \
                        and b - merged[-1][0] <= 1.4 * widest:
                    merged[-1][1] = b
                else:
                    merged.append([a, b])
            stripes = []
            for a, b in merged:
                rows = np.flatnonzero(ink[:, a:b].any(axis=1))
                if rows.size:
                    stripes.append((a, rows[0], b, rows[-1] + 1))
            widths = [b - a for a, _, b, _ in stripes]
            if not stripes or max(widths) < 6:
                out.append((x0, y0, x1, y1, txt))
                continue
            wmed = float(np.median(widths))
            vert = [st for st in stripes if (st[3] - st[1]) >= self.VERTICAL_RATIO * (st[2] - st[0])
                    and (st[2] - st[0]) >= 0.5 * wmed]
            # 多数笔画带是竖条才拆（横排一行按列投影是一个个方形的字）
            if len(vert) == 0 or len(vert) < 0.6 * len([w for w in widths if w >= 0.5 * wmed]):
                out.append((x0, y0, x1, y1, txt))
                continue
            for a, ta, b, tb in vert:
                out.append((ix0 + a - 1, iy0 + ta - 1, ix0 + b + 1, iy0 + tb + 1, txt if len(vert) == 1 else ""))
        return out

    def _recognize_vertical(self, img: np.ndarray, items: list) -> list:
        """竖排的中日文列：分别用“整列旋转 90°”（检测时的默认做法）和“逐字摆正”两种方式识别，
        取识别模型置信度更高的结果。逐字摆正能找回标点、长音和小写假名，但字很小（十几像素）时
        切出的单字太小反而容易认错，所以两者择优；字太小的列先放大再切字。"""
        import cv2
        from rapidocr.ch_ppocr_rec.typings import TextRecInput
        idx, tiled, rotated = [], [], []
        for i, (x0, y0, x1, y1, txt) in enumerate(items):
            # 竖排的中日文列（含刚从合并框里拆出、还没有文字的列）
            if (y1 - y0) >= self.VERTICAL_RATIO * (x1 - x0) and (
                    not txt or re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", txt)):
                ix0, iy0, ix1, iy1 = int(x0), int(y0), int(np.ceil(x1)), int(np.ceil(y1))
                col = img[max(iy0, 0):iy1, max(ix0, 0):ix1]
                if col.size == 0:
                    continue
                if col.shape[1] < 28:  # 字太小：放大后再切字
                    k = 32 / col.shape[1]
                    col = cv2.resize(col, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
                line = self._vertical_to_line(col, (0, 0, col.shape[1], col.shape[0]))
                if line is None:
                    continue
                idx.append(i)
                tiled.append(line)
                rotated.append(np.ascontiguousarray(np.rot90(col)))  # 逆时针转 90°：竖排的字变成横躺
        if not idx:
            return items
        rec_t = self._engine.text_rec(TextRecInput(img=tiled))
        rec_r = self._engine.text_rec(TextRecInput(img=rotated))
        out = list(items)
        for k, i in enumerate(idx):
            t_t, s_t = rec_t.txts[k], float(rec_t.scores[k])
            t_r, s_r = rec_r.txts[k], float(rec_r.scores[k])
            text = self._pick_vertical(t_t, s_t, t_r, s_r)
            if text.strip():
                out[i] = (*items[i][:4], text)
        return out

    # 逐字摆正比整列旋转多认出来的、可以信的字：标点、长音、小写假名（整列旋转常漏掉它们）
    _V_EXTRA = set("、。，．！？!?ー・っゃゅょぁぃぅぇぉッャュョァィゥェォ「」『』…")

    @classmethod
    def _pick_vertical(cls, t_t: str, s_t: float, t_r: str, s_r: float) -> str:
        """合并“逐字摆正”(a) 和“整列旋转”(b) 两种结果：逐段对齐两者，
        - a 多出的标点、长音、小写假名保留（整列旋转常漏掉它们），多出的其他字（切字切出的碎块）丢掉；
        - b 多出的字保留（逐字切字偶尔漏字）；
        - 两者不同的地方：b 是西文（竖排里横躺的 PDF、Windows，整列旋转后正好摆正）用 b，
          否则逐字切字容易切坏，除非 a 的置信度明显更高，用 b。"""
        norm = lambda t: t.replace("!", "！").replace("?", "？")
        a, b = norm(t_t), norm(t_r)
        if "・" in a and "・" not in b:
            a = a.replace("・", "")
        trust_a = s_t > s_r + 0.02
        # 相邻的不同之处合成一段（“モーテル/モデル”里的 ー 和 テ 一起算替换，而不是插入一个长音）
        chunks = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a, autojunk=False).get_opcodes():
            if tag != "equal" and chunks and chunks[-1][0] != "equal":
                chunks[-1] = ("diff", chunks[-1][1], i2, chunks[-1][3], j2)
            else:
                chunks.append((tag, i1, i2, j1, j2))
        out = []
        for tag, i1, i2, j1, j2 in chunks:
            sa, sb = a[j1:j2], b[i1:i2]
            if tag == "equal":
                out.append(sa)
            elif not sb:                                    # 只有 a 多出
                out.append(sa if all(ch in cls._V_EXTRA for ch in sa) else "")
            elif not sa:                                    # 只有 b 多出
                out.append(sb)
            elif re.search(r"[A-Za-z0-9]", sb):
                out.append(sb)
            else:
                out.append(sa if trust_a else sb)
        return "".join(out)

    def recognize(self, img: np.ndarray, keep_line_breaks: bool = False) -> str:
        return to_text(self.analyze(img), keep_line_breaks)


class OcrWorker(QObject):
    """放在独立 QThread 中运行，避免识别时卡住界面。"""

    ready = Signal()
    done = Signal(int, str, float)   # job_id, text, 耗时(秒)
    # job_id, 整段译文, [(段落, 译文), ...], 出错信息（成功时为空）
    translated = Signal(int, str, object, str)
    analyzed = Signal(int, object)          # job_id, 段落列表（识别完、开始翻译前）
    partial = Signal(int, int, str)         # job_id, 段落序号, 该段译文（离线大模型逐段生成时）
    failed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self._engine: OcrEngine | None = None

    def _ensure_engine(self) -> OcrEngine:
        if self._engine is None:
            t = time.perf_counter()
            self._engine = OcrEngine()
            # 预热一次，首次真正识别就不会额外卡顿
            self._engine.recognize(np.full((48, 160, 3), 255, np.uint8))
            log.info("OCR 引擎加载完成（%s），用时 %.2fs", self._engine.device, time.perf_counter() - t)
        return self._engine

    @Slot()
    def warmup(self) -> None:
        try:
            self._ensure_engine()
            self.ready.emit()
        except Exception:
            log.exception("OCR 引擎加载失败")

    @Slot(int, object, bool, str, object)
    def recognize(self, job_id: int, img: np.ndarray, keep_line_breaks: bool, translate_engine: str,
                  origin=None) -> None:
        """translate_engine 为空表示只识别不翻译，否则为翻译引擎名（见 translate.ENGINES）。"""
        try:
            engine = self._ensure_engine()
            t = time.perf_counter()
            paras = engine.analyze(img, origin)
            text = to_text(paras, keep_line_breaks)
            self.done.emit(job_id, text, time.perf_counter() - t)
        except Exception as e:
            log.exception("识别失败")
            self.failed.emit(job_id, str(e))
            return
        if translate_engine and paras:
            from .translate import translate_paragraphs
            self.analyzed.emit(job_id, paras)
            try:
                dst = translate_paragraphs([p.text for p in paras], engine=translate_engine,
                                           on_progress=lambda i, zh: self.partial.emit(job_id, i, zh))
                self.translated.emit(job_id, "\n".join(dst), list(zip(paras, dst)), "")
            except Exception as e:
                log.warning("翻译失败：%s", e)
                self.translated.emit(job_id, "", [], f"翻译失败：{e}")
