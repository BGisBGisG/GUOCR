"""版面分析：分栏/分页 → 行 → 段落 → 句子。

输入是 OCR 的文字框（每个框一段同行文字），输出按阅读顺序排好的段落。

1. 分栏（递归 XY-cut）：先找贯穿整个区域的竖向空白（如 Word 并排两页之间、报纸分栏），
   左栏读完再读右栏；找不到时再按较大的横向空白切成上下几块，每块里继续找分栏。
   这样“通栏标题 + 下面两栏”也能读对。
2. 分行：同一块里纵向重叠的框归为一行，行内从左到右拼接。
3. 分段：满足任一条件就另起一段——段间距明显大于正常行距、首行缩进、上一行没写满（段尾短行）、
   列表/编号开头。
4. 分句：中英文句末标点切分，跳过 Mr. / e.g. / 3.14 等缩写和小数。
"""
import re
from dataclasses import dataclass, field
from statistics import median

Box = tuple[float, float, float, float, str]  # x0, y0, x1, y1, 文字


@dataclass
class Line:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Paragraph:
    lines: list[Line] = field(default_factory=list)
    override: str | None = None  # 已知准确原文（如从 Word 直接读取）时用它代替识别文字
    vertical: bool = False       # 竖排段落：lines 为从右往左的各列

    @property
    def text(self) -> str:
        if self.override is not None:
            return self.override
        return join_lines([ln.text for ln in self.lines])

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (min(ln.x0 for ln in self.lines), min(ln.y0 for ln in self.lines),
                max(ln.x1 for ln in self.lines), max(ln.y1 for ln in self.lines))


# ------------------------------------------------------------------ 文字工具
def is_wide(ch: str) -> bool:
    """中日韩文字及全角标点：与相邻文字之间不加空格。"""
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF
            or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFFEF)


def join_segments(segs: list[Box]) -> str:
    """同一行里从左到右的几个框拼成一行文字。"""
    text = segs[0][4]
    for prev, cur in zip(segs, segs[1:]):
        gap = cur[0] - prev[2]
        height = ((prev[3] - prev[1]) + (cur[3] - cur[1])) / 2
        tight = is_wide(prev[4][-1]) and is_wide(cur[4][0]) and gap < height
        text += ("" if tight else " ") + cur[4]
    return text


def join_lines(lines: list[str]) -> str:
    """把一个段落里折行的几行接成一段：中文直接相连，英文加空格，去掉行尾断词连字符。"""
    merged = ""
    for text in lines:
        if not merged:
            merged = text
        elif is_wide(merged[-1]) or is_wide(text[0]):
            merged += text
        elif merged.endswith("-") and text[:1].islower() and len(merged) > 1 and merged[-2].isalpha():
            merged = merged[:-1] + text
        else:
            merged += " " + text
    return merged


_ABBR = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "fig", "figs", "no",
         "vol", "pp", "inc", "ltd", "co", "corp", "dept", "approx", "eq", "ref", "al", "u.s", "u.k", "jan", "feb",
         "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}


def split_sentences(text: str) -> list[str]:
    """中英文分句。中文按 。！？； 切；英文按 .!? + 空格 + 大写/引号/数字 切，并跳过常见缩写。"""
    out, start = [], 0
    for m in re.finditer(r"[。！？；!?]+[”’」』）)]*|\.+[\"'”’)\]]*(?=\s+[\"'“‘(\[A-Z0-9])", text):
        end = m.end()
        if m.group().startswith("."):
            if re.fullmatch(r"\(?[\dA-Za-z]{1,3}(\.\d+)*\.", text[start:end].strip()):
                continue  # 句首的编号（1. / 2.3. / a.）不是句子
            word = re.search(r"([A-Za-z.]+)\.+[\"'”’)\]]*$", text[start:end])
            w = word.group(1).lower() if word else ""
            if w in _ABBR or (len(w) == 1 and w.isalpha()):  # 缩写或姓名首字母（J. Smith）
                continue
        sent = text[start:end].strip()
        if sent:
            out.append(sent)
        start = end
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


# ------------------------------------------------------------------ 分栏
def _gaps(spans: list[tuple[float, float]], min_gap: float) -> list[float]:
    """一维投影上宽度 ≥ min_gap 的空白，返回各空白的中点。"""
    spans = sorted(spans)
    cuts, reach = [], spans[0][1]
    for a, b in spans[1:]:
        if a - reach >= min_gap:
            cuts.append((reach + a) / 2)
        reach = max(reach, b)
    return cuts


def _split(items: list[Box], cuts: list[float], axis: int) -> list[list[Box]]:
    groups: list[list[Box]] = [[] for _ in range(len(cuts) + 1)]
    for it in items:
        center = (it[axis] + it[axis + 2]) / 2
        groups[sum(center > c for c in cuts)].append(it)
    return [g for g in groups if g]


def _xy_cut(items: list[Box], unit: float, depth: int = 0) -> list[list[Box]]:
    """递归切块，返回按阅读顺序排列的叶子块。unit 为典型文字高度。"""
    if len(items) <= 1 or depth > 12:
        return [items]
    # 竖向空白 → 分栏（每栏至少要有两行高的内容，避免把同一行里隔得较远的两个词拆开）
    cuts = _gaps([(it[0], it[2]) for it in items], 1.5 * unit)
    if cuts:
        groups = _split(items, cuts, 0)
        tall = [max(it[3] for it in g) - min(it[1] for it in g) >= 1.8 * unit for g in groups]
        if len(groups) > 1 and sum(tall) >= 2:
            return [leaf for g in groups for leaf in _xy_cut(g, unit, depth + 1)]
    # 较大的横向空白 → 上下分块，块内再找分栏
    cuts = _gaps([(it[1], it[3]) for it in items], 1.2 * unit)
    if cuts:
        groups = _split(items, cuts, 1)
        if len(groups) > 1:
            return [leaf for g in groups for leaf in _xy_cut(g, unit, depth + 1)]
    return [items]


# ------------------------------------------------------------------ 分行、分段
def _make_line(segs: list[Box]) -> Line:
    return Line(min(s[0] for s in segs), min(s[1] for s in segs),
                max(s[2] for s in segs), max(s[3] for s in segs), join_segments(segs))


def _group_lines(items: list[Box]) -> list[Line]:
    return [_make_line(sorted(row, key=lambda it: it[0])) for row in _group_lines_raw(items)]


def _group_lines_raw(items: list[Box]) -> list[list[Box]]:
    """按纵向重叠把文字框归成行，返回各行的文字框（未排序）。"""
    items = sorted(items, key=lambda it: (it[1] + it[3]) / 2)
    rows: list[dict] = []
    for it in items:
        x0, y0, x1, y1, _ = it
        row = rows[-1] if rows else None
        if row is not None:
            overlap = min(y1, row["bottom"]) - max(y0, row["top"])
            if overlap > 0.5 * min(y1 - y0, row["bottom"] - row["top"]):
                row["items"].append(it)
                n = len(row["items"])  # 用均值更新行的上下沿，避免倾斜文本把整行越拉越高
                row["top"] += (y0 - row["top"]) / n
                row["bottom"] += (y1 - row["bottom"]) / n
                continue
        rows.append({"top": y0, "bottom": y1, "items": [it]})
    return [row["items"] for row in rows]


# ------------------------------------------------------------------ 分区（区块）
def _split_rows(items: list[Box], unit: float) -> list[Line]:
    """先按纵向重叠把文字框归成行；同一行里两个框隔得很远（> 1.3 个字高，正常词间距约 0.3 个字高）时切开，各成一条“行片段”。
    网页、幻灯片、表格里同一高度的左右两块内容（正文与侧栏、两个单元格）因此不会被拼成一句。"""
    out: list[Line] = []
    lefts = [it[0] for it in items]
    for row in _group_lines_raw(items):
        segs = sorted(row, key=lambda it: it[0])
        h = median(s[3] - s[1] for s in segs)
        cur = [segs[0]]
        for s in segs[1:]:
            gap = s[0] - cur[-1][2]
            aligned = sum(abs(x - s[0]) <= 6 for x in lefts) >= 3  # 右边这块的左端与其他行对齐：另一栏的开头
            if gap > max(1.3 * h, 12) or (gap > max(0.8 * h, 10) and aligned):  # 与 ocr._split_wide_gaps 一致
                out.append(_make_line(cur))
                cur = [s]
            else:
                cur.append(s)
        out.append(_make_line(cur))
    return out


def _link_blocks(lines: list[Line], unit: float) -> list[list[Line]]:
    """把上下相邻、左右对齐的行连成区块（一个正文段落组、一列目录、一段图注……）。
    下一行要接在某块后面，需要：与该块最后一行的纵向间距不超过 1.8 个字高，横向有足够重叠；
    有多个候选时选左边缘最对齐的那块。"""
    blocks: list[list[Line]] = []
    for ln in sorted(lines, key=lambda l_: (l_.y0, l_.x0)):
        h = max(ln.h, 0.6 * unit)
        best, best_score = None, None
        for b in blocks:
            last = b[-1]
            gap = ln.y0 - last.y1
            if gap < -0.3 * h or gap > 1.8 * max(h, last.h):
                continue
            overlap = min(ln.x1, last.x1) - max(ln.x0, last.x0)
            if overlap < 0.5 * min(ln.x1 - ln.x0, last.x1 - last.x0):
                continue
            score = abs(ln.x0 - last.x0) + 0.2 * gap
            if best is None or score < best_score:
                best, best_score = b, score
        if best is None:
            blocks.append([ln])
        else:
            best.append(ln)
    return blocks


def _order_blocks(blocks: list[list[Line]], unit: float) -> list[list[Line]]:
    """区块的阅读顺序：先对区块外接框做 XY-cut（能切出贯通的竖向空白就先左栏后右栏，如报纸、论文的分栏），
    切不开的部分按上沿从上到下、同高时从左到右。"""
    boxes: list[Box] = []
    for i, b in enumerate(blocks):
        boxes.append((min(l_.x0 for l_ in b), min(l_.y0 for l_ in b), max(l_.x1 for l_ in b),
                      max(l_.y1 for l_ in b), str(i)))
    ordered: list[list[Line]] = []
    for leaf in _xy_cut(boxes, unit):
        for bx in sorted(leaf, key=lambda r: (round(r[1] / max(unit, 1)), r[0])):
            ordered.append(blocks[int(bx[4])])
    return ordered


_LIST_START = re.compile(r"^\s*([•·▪●○■□◆◇►▶✓✔\-–—*]\s|\d{1,3}[.、)）]\s*\S|[（(]\d{1,3}[)）]|"
                         r"[a-zA-Z][.)]\s|[一二三四五六七八九十]+[、.]|第[一二三四五六七八九十百\d]+[章节条部分])")
_END_PUNCT = tuple("。！？!?.:：;；”’\"')）")


def _ended_early(prev: Line, cur: Line, right: float, h: float) -> bool:
    """上一行是否“主动”结束：下一行开头的那个词本来放得进上一行右边的空白，却换了行。

    自然折行时，右侧空白一定小于下一行第一个词的宽度；段尾短行、标题则不然。
    """
    room = right - prev.x1
    if room < 1.5 * h:
        return False
    char_w = (prev.x1 - prev.x0) / max(len(prev.text), 1)  # 按上一行估算平均字宽
    first = cur.text[0] if is_wide(cur.text[0]) else cur.text.split()[0]
    wide = sum(2 if is_wide(c) else 1 for c in first)  # 汉字约占两个西文字符宽
    narrow_w = char_w if not any(is_wide(c) for c in prev.text) else char_w / 2
    return room > (wide + 1) * narrow_w + 0.5 * h


def _paragraphs(lines: list[Line]) -> list[Paragraph]:
    if not lines:
        return []
    h = median(ln.h for ln in lines)
    left = min(ln.x0 for ln in lines)
    right = max(ln.x1 for ln in lines)
    width = right - left
    pitches = [b.cy - a.cy for a, b in zip(lines, lines[1:])]
    pitch = median(pitches) if len(pitches) >= 2 else None

    paras = [Paragraph([lines[0]])]
    for i, (prev, cur) in enumerate(zip(lines, lines[1:])):
        step = cur.cy - prev.cy
        if pitch is not None:
            spaced = step > 1.35 * pitch and step - pitch > 0.25 * h
        else:
            spaced = cur.y0 - prev.y1 > 0.9 * h
        indented = cur.x0 - left > 1.2 * h and prev.x0 - left < 0.6 * h
        # 右边界取附近几行（前后各 3 行）的最右端：区块里若有一行特别长（如作者名单、网址），
        # 用整块的最右端会让正常折行的行看起来“没写满”而被误判为段尾
        local_right = max(ln.x1 for ln in lines[max(0, i - 3):i + 5])
        short_prev = width > 6 * h and _ended_early(prev, cur, local_right, h)
        # 注意：不用“框高变化”判断标题——OCR 给短行的框会明显偏高，容易误判；标题靠段距/短行就能分出来
        listed = bool(_LIST_START.match(cur.text))
        truncated = prev.text.rstrip().endswith(("...", "…"))  # 网页目录、列表里被截短的条目各自独立
        if spaced or indented or short_prev or listed or truncated:
            paras.append(Paragraph([cur]))
        else:
            paras[-1].lines.append(cur)
    return paras


# ------------------------------------------------------------------ 竖排（日文、繁体古籍等）
def _is_vertical(it: Box) -> bool:
    """竖排的一列：高至少是宽的 1.4 倍（漫画气泡里“あ、”这种两个字的短列也算；单个字的框不算）。"""
    w, h = it[2] - it[0], it[3] - it[1]
    return h >= 1.4 * w and len(it[4].strip()) >= 2


def _order_by_bands(paras: list[Paragraph], right_to_left: bool) -> list[Paragraph]:
    """混排页面（如漫画：竖排对白 + 横排字幕、拟声词）的阅读顺序：按纵向重叠把段落分成一层层，
    先上后下；同一层里竖排读物从右往左（横排为主的页面从左往右）。"""
    bands: list[list[Paragraph]] = []
    for p in sorted(paras, key=lambda p_: p_.bbox[1]):
        x0, y0, x1, y1 = p.bbox
        for band in bands:
            top, bottom = min(q.bbox[1] for q in band), max(q.bbox[3] for q in band)
            if min(y1, bottom) - max(y0, top) > 0.4 * min(y1 - y0, bottom - top):
                band.append(p)
                break
        else:
            bands.append([p])
    out = []
    for band in bands:
        out += sorted(band, key=lambda p_: -p_.bbox[2] if right_to_left else p_.bbox[0])
    return out


def _vertical_paragraphs(items: list[Box]) -> list[Paragraph]:
    """竖排：列从右往左、列内从上往下。相邻、上下有重叠的列连成区块；区块按“先上面一栏、栏内从右往左”排列。
    分段：列顶端比区块顶端低约一个字（首行缩进），或上一列底端提前结束（段尾），或列间空隙明显变大。"""
    cols = [Line(*it) for it in items]
    cw = median(c.x1 - c.x0 for c in cols)  # 字宽约等于列宽
    blocks: list[list[Line]] = []
    for c in sorted(cols, key=lambda l_: -l_.x1):  # 从右往左
        best = None
        for b in blocks:
            last = b[-1]
            # 按列中心算间距：拆出来的相邻列框常有几像素重叠
            gap = ((last.x0 + last.x1) - (c.x0 + c.x1)) / 2 - cw
            overlap = min(c.y1, last.y1) - max(c.y0, last.y0)
            if -0.6 * cw <= gap <= 1.6 * cw and overlap > 0.3 * min(c.y1 - c.y0, last.y1 - last.y0):
                best = b
                break
        if best is None:
            blocks.append([c])
        else:
            best.append(c)
    # 栏：上下不重叠的区块分属不同的栏，先读上面的栏；同一栏内从右往左
    blocks.sort(key=lambda b: (round(min(c.y0 for c in b) / max(cw * 4, 1)), -max(c.x1 for c in b)))
    paras: list[Paragraph] = []
    for b in blocks:
        top, bottom = min(c.y0 for c in b), max(c.y1 for c in b)
        # 列都很短（平均不到 12 个字）：多半是漫画对白气泡，整个气泡算一段，不按书页的缩进规则拆
        if median((c.y1 - c.y0) / max(c.x1 - c.x0, 1) for c in b) < 12:
            paras.append(Paragraph(list(b), vertical=True))
            continue
        gaps = [p.x0 - c.x1 for p, c in zip(b, b[1:])]
        g_med = median(gaps) if gaps else cw
        cur = Paragraph([b[0]], vertical=True)
        for prev, c in zip(b, b[1:]):
            indented = c.y0 - top > 0.6 * cw and prev.y0 - top < 0.4 * cw
            ended = bottom - prev.y1 > 1.5 * cw
            spaced = prev.x0 - c.x1 > max(1.6 * g_med, g_med + 0.5 * cw)
            if indented or ended or spaced:
                paras.append(cur)
                cur = Paragraph([c], vertical=True)
            else:
                cur.lines.append(c)
        paras.append(cur)
    return paras


def _order_pages(pages: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """页面按阅读顺序排列：先按行（上下重叠的算同一行）从上到下，行内从左到右。"""
    rows: list[list] = []
    for pg in sorted(pages, key=lambda r: r[1]):
        for row in rows:
            top, bottom = row[0][1], row[0][3]
            if min(pg[3], bottom) - max(pg[1], top) > 0.3 * min(pg[3] - pg[1], bottom - top):
                row.append(pg)
                break
        else:
            rows.append([pg])
    return [pg for row in rows for pg in sorted(row, key=lambda r: r[0])]


def _continues(prev: Paragraph, nxt: Paragraph) -> bool:
    """跨页接续：上一页最后一段没以句末标点结束，且下一页第一段不像新段落的开头。"""
    a, b = prev.text.rstrip(), nxt.text.lstrip()
    if not a or not b or a.endswith(_END_PUNCT) or _LIST_START.match(b):
        return False
    return b[0].islower() or b[0] in "，、；：）)]" or a[-1] in ",，、;；-" or is_wide(a[-1])


def analyze(items: list[Box], pages: list[tuple[int, int, int, int]] | None = None) -> list[Paragraph]:
    """OCR 文字框 → 按阅读顺序排列的段落。

    pages：截图里检测到的页面矩形（如 Word / PDF 阅读器并排、多行显示的多页）。有两页以上时
    逐页处理、按“先上后下、行内从左到右”的顺序拼接，并把跨页断开的段落接回去；
    否则按整块区域做分栏分析。
    """
    items = [it for it in items if it[4].strip()]
    if not items:
        return []
    unit = median(it[3] - it[1] for it in items)
    groups: list[list[Box]] = [items]
    if pages and len(pages) >= 2:
        ordered = _order_pages(pages)
        groups = [[] for _ in ordered]
        rest: list[Box] = []
        for it in items:
            cx, cy = (it[0] + it[2]) / 2, (it[1] + it[3]) / 2
            for k, (x0, y0, x1, y1) in enumerate(ordered):
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    groups[k].append(it)
                    break
            else:
                rest.append(it)  # 页面外的文字（批注框、标尺等）放到最后
        groups = [g for g in groups if g] + ([rest] if rest else [])
    paras: list[Paragraph] = []
    for g in groups:
        # 竖排为主（日文书籍、报纸等）：竖排的列按竖排规则排序分段，横排的标题、页码照常处理
        vert = [it for it in g if _is_vertical(it)]
        if len(vert) >= 2:
            horiz = [it for it in g if not _is_vertical(it)]
            v_paras = _vertical_paragraphs(vert)
            h_paras: list[Paragraph] = []
            if horiz:
                hu = median(it[3] - it[1] for it in horiz)
                for block in _order_blocks(_link_blocks(_split_rows(horiz, hu), hu), hu):
                    h_paras.extend(_paragraphs(sorted(block, key=lambda l_: l_.y0)))
            if len(vert) >= 0.5 * len(g) and all(len(p.lines) >= 3 for p in v_paras[:1]) and                     max((len(p.lines) for p in v_paras), default=0) >= 6:
                # 竖排书页：横排标题、页码按位置放在竖排正文前后
                v_top = min(it[1] for it in vert)
                page_paras = ([p for p in h_paras if p.bbox[3] <= v_top] + v_paras
                              + [p for p in h_paras if p.bbox[3] > v_top])
            else:
                # 漫画等竖横混排：一层层从上往下，每层从右往左
                page_paras = _order_by_bands(v_paras + h_paras, right_to_left=True)
            if paras and page_paras and len(groups) > 1 and _continues(paras[-1], page_paras[0]):
                paras[-1].lines.extend(page_paras.pop(0).lines)
            paras.extend(page_paras)
            continue
        # 分区：行片段 → 区块 → 阅读顺序；每个区块内再分段
        page_paras: list[Paragraph] = []
        for block in _order_blocks(_link_blocks(_split_rows(g, unit), unit), unit):
            page_paras.extend(_paragraphs(sorted(block, key=lambda l_: l_.y0)))
        if paras and page_paras and len(groups) > 1 and _continues(paras[-1], page_paras[0]):
            paras[-1].lines.extend(page_paras.pop(0).lines)
        paras.extend(page_paras)
    return paras


def to_text(paras: list[Paragraph], keep_line_breaks: bool = False) -> str:
    """段落模式：一段一行；保留换行模式：按阅读顺序逐行输出，段与段之间空一行。"""
    if keep_line_breaks:
        return "\n\n".join(p.override if p.override is not None else "\n".join(ln.text for ln in p.lines)
                             for p in paras)
    return "\n".join(p.text for p in paras)


# ------------------------------------------------------------------ 日文字形
# 识别模型是中日文混合训练的，日文里的汉字常被认成简体字（数値→数值、対→对）。
# 只收简体专用、日文不用的字形（叶、机、据 日文也用，不收），换回日文新字体；只在含假名的段落里替换。
_JP_FORMS = str.maketrans(
    "值对说语时发实际变关问题动读应经济气东车门见长书图电话认识设计记译转输选择显处务业产个开间场后进还过运边结构极线级纸红绿细终给统续编练现样头买卖华单历压广无员园报乐层岁带归战杂权欢汉测热爱环确离种积简类组绍维网罗职脑节药获虑览规视订讨训论证评调谈负费资质购软载较辑达违连迟适钱铁错键闭闻阅阶难预领频风飞饭马鱼鸟龙齐观觉艺项顺须页缩继绝总恶惯态怀忆举义习乡亚仅众优传价侧债儿军农决况净减击则刚创别剧劝劳势协卫厅县听启响团围圣坏块坚坛备夺奋妇宫宽寻导尔岛币师庆库废异张弹强录彻惊户执扩扫护拟损换挥敌晓暂术杀标树桥检毕污沟泽洁浓涂渐满灭灵炼烟烦牵犹疗盖盐监码础祸稳穷竞笔粮紧纪约纳纯织络绩缘缓罚肃肠肤胁胜脏舰苏范荐荣虽补让议讲许访诉词试诗诚该详误请课谁谢谓谱贝贡财责败货贩贫贯贵贸贺赏赖赞跃轨轮轻辅辈迁递逻遗邮邻释针钢银锁镜队阳阴阵陆陈险隐雾韩顶顾顿颜额饮馆驱验骑鲜齿龄",
    "値対説語時発実際変関問題動読応経済気東車門見長書図電話認識設計記訳転輸選択顕処務業産個開間場後進還過運辺結構極線級紙紅緑細終給統続編練現様頭買売華単歴圧広無員園報楽層歳帯帰戦雑権歓漢測熱愛環確離種積簡類組紹維網羅職脳節薬獲慮覧規視訂討訓論証評調談負費資質購軟載較輯達違連遅適銭鉄錯鍵閉聞閲階難預領頻風飛飯馬魚鳥竜斉観覚芸項順須頁縮継絶総悪慣態懐憶挙義習郷亜僅衆優伝価側債児軍農決況浄減撃則剛創別劇勧労勢協衛庁県聴啓響団囲聖壊塊堅壇備奪奮婦宮寛尋導爾島幣師慶庫廃異張弾強録徹驚戸執拡掃護擬損換揮敵暁暫術殺標樹橋検畢汚溝沢潔濃塗漸満滅霊錬煙煩牽猶療蓋塩監碼礎禍穏窮競筆糧緊紀約納純織絡績縁緩罰粛腸膚脅勝臓艦蘇範薦栄雖補譲議講許訪訴詞試詩誠該詳誤請課誰謝謂譜貝貢財責敗貨販貧貫貴貿賀賞頼賛躍軌輪軽輔輩遷逓邏遺郵隣釈針鋼銀鎖鏡隊陽陰陣陸陳険隠霧韓頂顧頓顔額飲館駆験騎鮮歯齢")
_KANA = re.compile(r"[぀-ゟ゠-ヿ]")
# 平假名“へべぺ”和片假名几乎一样，夹在片假名之间（ベース、データベース）时换成片假名
_KATA_HE = re.compile(r"(?<=[ァ-ヺ])[へべぺ](?=[ァ-ーー])|[へべぺ](?=ー)")


def japanese_forms(paras: list[Paragraph]) -> None:
    """含假名（日文）的段落里，把被认成简体字的汉字换回日文字形（就地修改）。"""
    for p in paras:
        if p.override is None and _KANA.search(p.text):
            for ln in p.lines:
                ln.text = _KATA_HE.sub(lambda m: chr(ord(m.group()) + 0x60), ln.text.translate(_JP_FORMS))
