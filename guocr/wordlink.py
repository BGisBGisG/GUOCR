"""直接从 Word 读取截图区域的原文（截 Word 窗口时自动启用）。

截图识别在 Word 缩小显示多页时会因为字太小而出错（并排 6 页时字只有几个像素）。截图区域在 Word 窗口上时：
1. 用 Word 的 Window.RangeFromPoint 取得截图区域（逐页）对应的文档范围及其原文；
2. 把 OCR 识别出的词与原文逐词对齐（difflib，容忍大量识别错误），确定截图里实际包含哪一段原文、
   每一行在屏幕上的位置；
3. 输出 Word 自己的段落（文字 100% 准确），修订模式下已删除的文字会被去掉；
   每段保留对应 OCR 行的屏幕位置，供“译文贴回原位”使用。
任何一步失败（不是 Word、Word 正忙、对不上）都返回 None，调用方继续使用 OCR 结果。
"""
import ctypes
import difflib
import logging
import re
from ctypes import wintypes

from .layout import Line, Paragraph

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

_WORD = re.compile(r"[A-Za-z0-9À-ɏ]+|[一-鿿぀-ヿ]")  # 西文按词，中文、日文按字对齐
_CTRL = {"\x07": "\t", "\x0b": "\n", "\x0c": "\n", "\x0e": "\n", "\x01": "", "\x02": "", "\x13": "", "\x14": "",
         "\x15": "", "\x1e": "-", "\x1f": "", "\xa0": " "}


user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
oleacc = ctypes.WinDLL("oleacc")
oleacc.AccessibleObjectFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
OBJID_NATIVEOM = 0xFFFFFFF0


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def word_window_at(x: int, y: int):
    """屏幕物理坐标处若是 Word 的文档编辑区，返回该编辑区窗口（类名 _WwG）的句柄，否则 None。

    Word 每个文档窗口（以及拆分后的每个窗格）都有自己的 _WwG 子窗口，用它就能精确对应到
    屏幕上的那一个文档——即使开了多个 Word 程序、或几份文档并排摆着。功能区、导航窗格等不算。"""
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    while hwnd:
        cls = _class(hwnd)
        if cls == "_WwG":
            return hwnd
        if cls == "OpusApp":  # 已经到了 Word 主窗口，说明点在功能区/状态栏上
            return None
        hwnd = user32.GetParent(hwnd)
    return None


def _word_window(wwg_hwnd):
    """从 _WwG 句柄直接取得对应的 Word.Window 对象（Windows 辅助功能接口 OBJID_NATIVEOM），
    不依赖“系统里登记的那个 Word 程序”，所以不会连到别的 Word 实例或别的文档上。"""
    import pythoncom
    import win32com.client

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    iid = GUID()
    ctypes.oledll.ole32.CLSIDFromString("{00020400-0000-0000-C000-000000000046}", ctypes.byref(iid))  # IID_IDispatch
    ptr = ctypes.c_void_p()
    if oleacc.AccessibleObjectFromWindow(wwg_hwnd, OBJID_NATIVEOM, ctypes.byref(iid), ctypes.byref(ptr)) != 0 \
            or not ptr.value:
        return None
    try:
        disp = pythoncom.ObjectFromAddress(ptr.value, pythoncom.IID_IDispatch)  # 会自行 AddRef
    finally:
        # 释放 AccessibleObjectFromWindow 给的那一份引用，否则用户关闭 Word 后进程可能残留
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
            ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2])
        release(ptr)
    return win32com.client.Dispatch(disp)


def _clean(text: str) -> str:
    return "".join(_CTRL.get(c, c) for c in text)


def _u16_to_py(text: str):
    """Word 的位置按 UTF-16 计数（𝑅 这类字符占 2 个位置），Python 字符串按码点计数：返回换算函数。"""
    table = []
    for i, c in enumerate(text):
        table.append(i)
        if ord(c) > 0xFFFF:
            table.append(i)
    table.append(len(text))
    return lambda k: table[min(max(k, 0), len(table) - 1)]


def _fetch(win, regions: list[tuple[int, int, int, int]]) -> list[tuple[str, list, list]]:
    """各区域（屏幕物理坐标，阅读顺序）对应的文档文字，前后各多取一点以容忍取点误差。
    返回 [(文字, 已删除修订的区间, 公式的区间), ...]（区间为文字中的下标），相邻、重叠的范围会合并。"""
    doc = win.Document
    spans: list[list[int]] = []
    for x0, y0, x1, y1 in regions:
        inset = 4
        try:
            a = win.RangeFromPoint(x0 + inset, y0 + inset)
            b = win.RangeFromPoint(x1 - inset, y1 - inset)
        except Exception:
            continue
        if a is None or b is None:
            continue
        s, e = min(a.Start, b.Start), max(a.End, b.End)
        s, e = max(0, s - 400), min(doc.Content.End, e + 400)
        if spans and s <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], e)
        else:
            spans.append([s, e])
    out = []
    for s, e in spans:
        rng = doc.Range(s, e)
        text = rng.Text or ""
        py = _u16_to_py(text)

        def spans_of(items, keep=lambda _x: True):
            """按内容定位各对象在 text 中的区间。Word 的位置编号包含域代码等不出现在 Text 里的内容，
            越往后偏差越大，所以用“预期位置 + 已知偏差”附近查找对象自身的文字，并随时更新偏差。"""
            res, drift = [], 0
            try:
                for i in range(1, items.Count + 1):
                    it = items(i)
                    if not keep(it):
                        continue
                    r = it.Range
                    sub = r.Text or ""
                    if r.Start < s or r.End > e or not sub:
                        continue
                    expect = py(r.Start - s) + drift
                    lo = text.rfind(sub, 0, expect + len(sub))
                    hi = text.find(sub, max(expect, 0))
                    cands = [k for k in (lo, hi) if k >= 0 and abs(k - expect) <= 3000]
                    if not cands:
                        continue
                    k = min(cands, key=lambda c: abs(c - expect))
                    drift = k - py(r.Start - s)
                    res.append((k, k + len(sub)))
            except Exception:
                log.debug("读取 Word 对象位置失败", exc_info=True)
            return res

        # 修订中被删掉的文字（wdRevisionDelete）。逐条读取较慢，没有修订时不碰
        deleted = spans_of(rng.Revisions, lambda rev: rev.Type == 2) if rng.Revisions.Count else []
        out.append((text, deleted))
    return out


def _is_math_char(c: str) -> bool:
    return 0x1D400 <= ord(c) <= 0x1D7FF  # Unicode 数学字母区（Word 公式里的 𝑥 𝜀 𝐿 等）


def _math_breaks(text: str) -> list[int]:
    """Word 公式的文字里夹着把各部分隔开的 \\r（如 “𝜀=\\r\\r𝐿\\r𝑥\\r−…”），它们不是段落标记。
    判断依据：\\r 前后两个字符内有数学字母。逐个向 Word 查询公式位置太慢（几百个公式要好几秒），所以按文字判断。"""
    pieces = text.split("\r")
    out, pos = [], 0
    for k in range(len(pieces) - 1):
        pos += len(pieces[k])
        i = pos  # 第 k 个 \r 的位置
        near = text[max(0, i - 3):i] + text[i + 1:i + 4]
        # 公式里紧挨 \r 的也可能是 ′、上下标数字、运算符：相邻一侧是不含普通字母的极短片段，且再往外有数学字母
        short = [p for p in (pieces[k], pieces[k + 1]) if 0 < len(p.strip()) <= 4 and not re.search("[A-Za-z]", p)]
        wider = text[max(0, i - 8):i] + text[i + 1:i + 9]
        if any(_is_math_char(c) for c in near) or (short and any(_is_math_char(c) for c in wider)):
            out.append(i)
        pos += 1
    return out


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group().lower(), m.start(), m.end()) for m in _WORD.finditer(text)]


def refine(paras: list[Paragraph], regions: list[tuple[int, int, int, int]], center: tuple[int, int]
           ) -> list[Paragraph] | None:
    """用 Word 原文替换 OCR 段落。regions：截图中各页（或整个截图区域）在屏幕上的物理坐标矩形，按阅读顺序。

    每一页分别看它下面是哪个 Word 文档窗口（_WwG），从那个窗口取原文：框选区域跨了两份并排的文档时，
    各页取各自文档的文字；不在 Word 上的页面跳过。center 仅在没有页面信息时作为整块区域的取点。"""
    if not paras:
        return None
    import unicodedata

    import pythoncom
    pythoncom.CoInitialize()  # 本线程首次调用时初始化 COM；重复调用无副作用，这里不反初始化
    windows: dict[int, object] = {}  # _WwG 句柄 → Word.Window
    groups: list[tuple[int, list]] = []  # 阅读顺序上相邻、属于同一窗口的页面归为一组（组内相邻范围会合并，避免重复取文字）
    chunks = []
    try:
        for reg in regions:
            hwnd = word_window_at((reg[0] + reg[2]) // 2, (reg[1] + reg[3]) // 2)
            if hwnd is None:
                continue
            key = int(hwnd)
            if key not in windows:
                windows[key] = _word_window(hwnd)
                if windows[key] is not None:
                    log.info("截图区域对应 Word 文档：%s", windows[key].Document.FullName)
            if windows[key] is None:
                continue
            if groups and groups[-1][0] == key:
                groups[-1][1].append(reg)
            else:
                groups.append((key, [reg]))
        for key, regs in groups:
            chunks += _fetch(windows[key], regs)
    except Exception as e:
        log.info("读取 Word 原文失败，使用识别结果：%s", e)
        return None
    if not chunks:
        return None

    # 原文：多个范围拼接；记录已删除修订的位置
    raw, deleted = "", []
    for text, dels in chunks:
        # 需要替换的片段（删除区间随之平移）：
        # - 公式：去掉其中的 \r（不是段落标记）；数学字母（𝑅）在后面转成普通字符；
        # - 表格：单元格结尾是 \r\x07，行尾再多一组 —— 一行作为一段，单元格之间用制表符分隔。
        edits = [(i, i + 1, "") for i in _math_breaks(text)]
        for m in re.finditer(r"\r\x07\r\x07|\r\x07", text):
            edits.append((m.start(), m.end(), "\r" if len(m.group()) == 4 else "\t"))
        pieces, marks, prev = [], [], 0
        for a, b, rep in sorted(edits):
            if a < prev:
                continue
            pieces.append(text[prev:a])
            pieces.append(rep)
            marks.append((a, b, len(rep) - (b - a)))
            prev = b
        pieces.append(text[prev:])

        def remap(k, marks=marks):
            off = 0
            for a, b, d in marks:
                if k >= b:
                    off += d
                elif k > a:
                    return a + off
            return k + off

        base = len(raw)
        raw += "".join(pieces) + "\r"
        deleted += [(remap(a) + base, remap(b) + base) for a, b in dels]
    is_deleted = bytearray(len(raw))
    for a, b in deleted:
        is_deleted[a:b] = b"\x01" * (b - a)

    # OCR 的词（带所属行）与原文的词对齐
    lines = [ln for p in paras for ln in p.lines]
    ocr_words, owner = [], []
    for li, ln in enumerate(lines):
        for w, _, _ in _tokens(ln.text):
            ocr_words.append(w)
            owner.append(li)
    doc_tok = _tokens(raw)
    if len(ocr_words) < 3 or not doc_tok:
        return None
    sm = difflib.SequenceMatcher(None, ocr_words, [t[0] for t in doc_tok], autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    # 门槛依据实测：真实 Word 截图在最难的“并排 6 页”下对齐率 29～36%，其余视图 60% 以上；
    # 截图与原文不对应时（只靠 the/of 等常用词偶然对上）实测约 17%。取中间值 23%。
    if matched < max(30, 0.23 * len(ocr_words)):
        log.info("Word 原文与识别结果对不上（%d/%d），使用识别结果", matched, len(ocr_words))
        return None
    line_span: dict[int, list[int]] = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            li = owner[blk.a + k]
            _, cs, ce = doc_tok[blk.b + k]
            sp = line_span.setdefault(li, [cs, ce])
            sp[0], sp[1] = min(sp[0], cs), max(sp[1], ce)
    if not line_span:
        return None
    # 截图首尾可能只截到半段：首尾各向外扩到整行（按对齐时首尾未匹配的词数往外补）
    first_li, last_li = min(line_span), max(line_span)
    start, end = line_span[first_li][0], line_span[last_li][1]
    lead = next(i for i, li in enumerate(owner) if li == first_li)
    first_match = min(blk.a for blk in sm.get_matching_blocks() if blk.size)
    extra = max(0, first_match - lead)
    j = next(k for k, t in enumerate(doc_tok) if t[1] >= start)
    start = doc_tok[max(0, j - extra)][1] if extra else start
    tail_matched = max(blk.a + blk.size for blk in sm.get_matching_blocks() if blk.size) - 1
    extra = len(ocr_words) - 1 - tail_matched
    if extra > 0:
        j = max(k for k, t in enumerate(doc_tok) if t[2] <= end)
        end = doc_tok[min(len(doc_tok) - 1, j + extra)][2]
    # 起点退到所在行首（上一个段落标记/换行之后），终点延到行尾标点
    m = re.search(r"[^\s]*$", raw[:start])
    start -= len(m.group()) if m else 0
    m = re.match(r"[^\s\r]*", raw[end:])
    end += len(m.group()) if m else 0

    # 按 Word 的段落标记（\r）切段，去掉已删除的修订文字
    def build_text(a: int, b: int) -> str:
        """原文 raw[a:b] → 段落文字：去掉已删除的修订、控制字符，数学字母转普通字母，规整空白。"""
        text = "".join(raw[i] for i in range(a, b) if not is_deleted[i])
        text = _clean(text)
        text = "".join(unicodedata.normalize("NFKC", c) if _is_math_char(c) else c for c in text)  # 𝑅 → R
        return "\t".join(re.sub(r" +", " ", cell).strip() for cell in text.split("\t")).strip()

    pieces = []  # [(原文中的起点, 终点, 段落文字)]
    pos = start
    for piece in re.split(r"(\r)", raw[start:end]):
        if piece == "\r":
            pos += 1
            continue
        pieces.append((pos, pos + len(piece) + 1, build_text(pos, pos + len(piece))))
        pos += len(piece)

    # 每一条 OCR 行都要归到某个段落（屏幕位置用于贴译文）。对上了原文的行按其位置归段；
    # 没对上的行（字太小识别错）按前后最近的已归段行推断：前后同属一段就归那段；否则看识别时
    # 版面分析把它和哪些行分在同一段，跟随其中已归段的行；都没有就跟随前一行。
    def piece_of(li):
        cs, ce = line_span[li]
        mid = (cs + ce) / 2
        return next((k for k, (a, b, _) in enumerate(pieces) if a <= mid < b), None)

    owner_par = {li: piece_of(li) for li in line_span}
    ocr_par = [pi for pi, p in enumerate(paras) for _ in p.lines]  # 每条行在识别版面分析中的段号
    assigned: list[int | None] = []
    for li in range(len(lines)):
        k = owner_par.get(li)
        if k is None:
            prev = next((owner_par[j] for j in range(li - 1, -1, -1) if owner_par.get(j) is not None), None)
            nxt = next((owner_par[j] for j in range(li + 1, len(lines)) if owner_par.get(j) is not None), None)
            same = [owner_par[j] for j in range(len(lines))
                    if ocr_par[j] == ocr_par[li] and owner_par.get(j) is not None]
            if prev is not None and prev == nxt:
                k = prev
            elif same:
                k = max(set(same), key=same.count)
            else:
                k = prev if prev is not None else nxt
        assigned.append(k)

    vertical = sum(p.vertical for p in paras) > len(paras) / 2  # 竖排文档：贴译文时也竖排
    out: list[Paragraph] = []
    for k, (a, b, text) in enumerate(pieces):
        idx = [li for li in range(len(lines)) if assigned[li] == k]
        if not idx:  # 屏幕上一行都看不到（只是取原文时多取的范围）：不输出
            continue
        # 只截到一段的一部分时（屏幕边缘、上一排页面只露出底部等），只保留看得见的那几行对应的原文：
        # 以对上原文的行为准，首尾各留出“未对上的行数 + 1”行的余量，再对齐到词边界
        spans = [line_span[li] for li in idx if li in line_span]
        if spans:
            cpl = sorted(ce - cs for cs, ce in spans)[len(spans) // 2] * 1.3  # 每行大约多少字符
            m0 = idx.index(next(li for li in idx if li in line_span))
            m1 = len(idx) - 1 - idx[::-1].index(next(li for li in reversed(idx) if li in line_span))
            lo = min(cs for cs, _ in spans) - (m0 + 1) * cpl
            hi = max(ce for _, ce in spans) + (m1 + 1) * cpl
            a2, b2 = max(a, int(lo)), min(b - 1, int(hi))
            if a2 > a:
                a2 = a2 + len(re.match(r"\S*", raw[a2:b]).group())  # 从下一个词开始，不截半个词
            if b2 < b - 1:
                b2 = b2 + len(re.match(r"\S*", raw[b2:b]).group())
            if (a2, b2) != (a, b - 1):
                text = build_text(a2, b2)
        if re.search(r"\w", text):  # 只有符号（如图片占位的“/”）的不算一段
            out.append(Paragraph([lines[li] for li in idx], override=text, vertical=vertical))
    log.info("已用 Word 原文校正：%d 段（识别词与原文对齐 %d/%d）", len(out), matched, len(ocr_words))
    return out or None
