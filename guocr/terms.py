"""专业词库：让离线翻译按指定译法翻译术语。

离线大模型：把段落里命中的术语作为“术语表”写进提示词（hits）。
离线小模型（兜底）：用下面的占位替换。

做法（占位替换）：翻译前把句子里命中词库的英文术语换成 {0}、{1}… 占位符——离线模型会原样照抄这种占位符；
翻译后再把占位符换成词库里的中文。这样术语按词库翻，句子的其余部分仍由模型翻译、保持通顺。
若模型弄丢了某个占位符，就放弃这句的术语替换，改用不带词库的普通翻译，保证不会输出残缺的句子。

词库来源（后加载的覆盖先加载的）：
1. 内置领域词库 guocr/glossary/*.txt（计算机、机械、生物、数学），可在设置里勾选启用；
2. 用户自定义词库 %APPDATA%\\GUOCR\\glossary.txt。
格式：每行 `英文 = 中文`，# 开头为注释；匹配不区分大小写，自动匹配复数（-s / -es / -y→-ies）。
"""
import os
import re
import sys

from .config import CONFIG_DIR

DOMAINS = {"computer": "计算机", "mechanical": "机械", "biology": "生物", "math": "数学"}
USER_GLOSSARY = os.path.join(CONFIG_DIR, "glossary.txt")
USER_TEMPLATE = """# GUOCR 自定义词库：每行一条「英文 = 中文」，# 开头为注释。
# 离线翻译时，这里的译法优先于内置词库和翻译模型。不区分大小写，自动匹配复数形式。
# 例如：
# pipeline = 流水线
# clearance = 间隙
"""


def _builtin_dir() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "guocr", "glossary")


def _parse(path: str) -> dict[str, str]:
    terms = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                en, zh = (s.strip() for s in line.split("=", 1))
                if en and zh:
                    terms[" ".join(en.lower().split())] = zh
    except FileNotFoundError:
        pass
    return terms


def ensure_user_glossary() -> str:
    """确保用户词库文件存在（不存在就写入带说明的模板），返回路径。"""
    if not os.path.exists(USER_GLOSSARY):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(USER_GLOSSARY, "w", encoding="utf-8") as f:
            f.write(USER_TEMPLATE)
    return USER_GLOSSARY


def _variants(term: str) -> str:
    """术语 → 正则：词间空白可变，末词允许复数形式。"""
    words = [re.escape(w) for w in term.split()]
    last = term.split()[-1]
    if last.endswith("y") and len(last) > 2 and last[-2] not in "aeiou":
        words[-1] = re.escape(last[:-1]) + r"(?:y|ies)"
    elif last[-1:].isalpha():
        words[-1] += r"(?:s|es)?"
    return r"\s+".join(words)


class Glossary:
    def __init__(self, terms: dict[str, str]):
        self.terms = terms
        self._pattern = None
        if terms:
            alts = sorted(terms, key=len, reverse=True)  # 长词优先：先匹配 “binary search tree” 再匹配 “binary search”
            self._pattern = re.compile(r"(?<![A-Za-z0-9\-])(" + "|".join(_variants(t) for t in alts)
                                       + r")(?![A-Za-z0-9\-])", re.IGNORECASE)
            self._lookup = [(re.compile(_variants(t) + r"\Z", re.IGNORECASE), terms[t]) for t in alts]

    def __len__(self) -> int:
        return len(self.terms)

    def _zh(self, matched: str) -> str:
        key = " ".join(matched.lower().split())
        if key in self.terms:
            return self.terms[key]
        return next(zh for pat, zh in self._lookup if pat.match(matched))

    def exact(self, sentence: str) -> str | None:
        """整句恰好就是一个术语（菜单项、标题等）时直接返回译法。"""
        s = sentence.strip().rstrip(".:：")
        if self._pattern is not None and s and self._pattern.fullmatch(s):
            return self._zh(s)
        return None

    def hits(self, text: str) -> dict[str, str]:
        """文中出现的术语 → 译法（给大模型当术语表用），按出现顺序去重。"""
        if self._pattern is None:
            return {}
        found: dict[str, str] = {}
        for m in self._pattern.finditer(text):
            found.setdefault(" ".join(m.group(1).split()), self._zh(m.group(1)))
        return found

    def protect(self, sentence: str) -> tuple[str, list[str]]:
        """把术语换成 {0} {1}…，返回 (替换后的句子, 各占位符对应的中文)。"""
        if self._pattern is None:
            return sentence, []
        zhs: list[str] = []

        def repl(m):
            zhs.append(self._zh(m.group(1)))
            return "{%d}" % (len(zhs) - 1)

        return self._pattern.sub(repl, sentence), zhs

    @staticmethod
    def restore(translated: str, zhs: list[str]) -> str | None:
        """把占位符换回中文；有占位符丢失或重复时返回 None（调用方改用普通翻译）。"""
        for i in range(len(zhs)):
            if len(re.findall(r"\{\s*%d\s*\}" % i, translated)) != 1:
                return None
        return re.sub(r"\{\s*(\d+)\s*\}", lambda m: zhs[int(m.group(1))], translated)


_cache: dict = {}


def load(domains: list[str]) -> Glossary:
    """按启用的领域加载词库（再叠加用户词库）。文件没改动时复用缓存。"""
    try:
        mtime = os.path.getmtime(USER_GLOSSARY)
    except OSError:
        mtime = 0
    key = (tuple(sorted(domains)), mtime)
    if key not in _cache:
        terms: dict[str, str] = {}
        for d in DOMAINS:
            if d in domains:
                terms.update(_parse(os.path.join(_builtin_dir(), d + ".txt")))
        terms.update(_parse(USER_GLOSSARY))
        _cache.clear()
        _cache[key] = Glossary(terms)
    return _cache[key]
