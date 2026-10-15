"""OCR 引擎（RapidOCR + onnxruntime，离线）与文字版面还原。"""
import logging
import re
import time

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

from .layout import Paragraph, analyze, to_text

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
        for x0, y0, x1, y1, txt in self._split_wide_gaps(padded, result.boxes, result.txts):
            items.append((x0 - PAD, y0 - PAD, x1 - PAD, y1 - PAD, clean_marks(txt.strip())))
        pages = detect_pages(img)
        paras = analyze(items, pages)
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
        """文字检测会把同一高度、只隔一两个字宽的两块内容（网页正文与侧栏、表格相邻单元格）连成一个框，
        识别时空白也被吞掉（“Pages 28” + “remain” → “28rémain”）。这里按像素找出框内宽于 1.3 个字高的
        空白列（正常词间距约 0.3 个字高），在那里切开，各段单独重新识别。"""
        from rapidocr.ch_ppocr_rec.typings import TextRecInput
        out, pending = [], []  # pending: 待重新识别的 (x0, y0, x1, y1, 图像)
        gray = img.mean(axis=2)
        for box, txt in zip(boxes, txts):
            box = np.asarray(box)
            x0, y0 = int(box[:, 0].min()), int(box[:, 1].min())
            x1, y1 = int(np.ceil(box[:, 0].max())), int(np.ceil(box[:, 1].max()))
            h = y1 - y0
            region = gray[max(y0, 0):y1, max(x0, 0):x1]
            if h < 6 or region.size == 0 or (x1 - x0) < 4 * h:
                out.append((x0, y0, x1, y1, txt))
                continue
            bg = np.median(np.concatenate([region[0], region[-1]]))
            cols = (np.abs(region - bg) > 40).any(axis=0)  # 这一列有没有笔画
            ink = np.flatnonzero(cols)
            if ink.size == 0:
                out.append((x0, y0, x1, y1, txt))
                continue
            cuts, run_start = [], None
            for c in range(ink[0], ink[-1] + 1):
                if not cols[c]:
                    run_start = c if run_start is None else run_start
                elif run_start is not None:
                    if c - run_start >= max(1.3 * h, 12):
                        cuts.append((run_start, c))
                    run_start = None
            if not cuts:
                out.append((x0, y0, x1, y1, txt))
                continue
            edges = [ink[0]] + [v for a, b in cuts for v in (a, b)] + [ink[-1] + 1]
            for a, b in zip(edges[::2], edges[1::2]):
                sx0, sx1 = x0 + a - 2, x0 + b + 2
                pending.append((sx0, y0, sx1, y1, img[max(y0, 0):y1, max(sx0, 0):sx1]))
        if pending:
            rec = self._engine.text_rec(TextRecInput(img=[p[4] for p in pending]))
            for (sx0, sy0, sx1, sy1, _), t in zip(pending, rec.txts):
                if t.strip():
                    out.append((sx0, sy0, sx1, sy1, t))
        return out

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
