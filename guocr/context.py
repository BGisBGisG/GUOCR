"""翻译语境预设：用户用自然语言告诉本地大模型“在什么语境下、某个词是什么意思、还有什么要求”。

- 每个预设保存用户说过的原话（notes）、模型整理后的语境说明（summary）和词义对照（terms）；
- 用户每说一次，模型把新内容与已保存的合并，并用中文复述它记住了什么，供用户确认；
- 翻译时把“当前预设”的语境和词义加到给本地大模型的指令里（见 translate._via_llm）。
保存在 %APPDATA%/GUOCR/presets.json。
"""
import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field

from .config import CONFIG_DIR

log = logging.getLogger(__name__)
PRESETS_PATH = os.path.join(CONFIG_DIR, "presets.json")
# “X 译为 Y / 改成 Y / 在这里指 Y / = Y”：用户直接给出词义的固定说法
TERM_RX = re.compile(r"([A-Za-z][A-Za-z0-9\-' ]{0,40}?)\s*(?:在这里|这里|此处|在此|在本文中|本文中|在文中)?"
                     r"\s*(?:统一)?(?:改成|改为|换成|译为|译成|翻译成|翻成|指的是|是指|指|=|→)"
                     r"\s*[“\"'「]?([^，。；;,\n“”\"'」（(]{1,30})")
_lock = threading.Lock()


@dataclass
class Preset:
    name: str
    notes: list[str] = field(default_factory=list)       # 用户每次输入的原话
    summary: str = ""                                    # 模型整理的语境说明
    terms: dict[str, str] = field(default_factory=dict)  # 词 → 在此语境下的译法/含义
    replies: list[str] = field(default_factory=list)     # 模型每次的复述（与 notes 一一对应）


def load() -> tuple[list[Preset], str]:
    """返回 (全部预设, 当前预设名；空字符串表示不用预设)。"""
    with _lock:
        try:
            with open(PRESETS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return [], ""
        except (OSError, ValueError):
            log.exception("读取翻译语境预设失败")
            return [], ""
    presets = []
    for p in data.get("presets", []):
        try:
            presets.append(Preset(**{k: p[k] for k in ("name", "notes", "summary", "terms", "replies") if k in p}))
        except TypeError:
            continue
    active = data.get("active", "")
    return presets, active if any(p.name == active for p in presets) else ""


def save(presets: list[Preset], active: str) -> None:
    with _lock:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = PRESETS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"active": active, "presets": [asdict(p) for p in presets]}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PRESETS_PATH)


def active_preset() -> Preset | None:
    presets, active = load()
    return next((p for p in presets if p.name == active), None)


# ------------------------------------------------------------------ 让模型理解并复述
_SYSTEM = (
    "你是截图翻译工具的“翻译语境”设置助手。用户会用自然语言说明翻译时的语境、领域、某些词在此语境下的含义"
    "或译法，以及其他要求（如哪些内容不要翻译、语气风格）。请把用户的新说明与已保存的内容合并（新说明与旧内容"
    "冲突时以新说明为准），只保留用户明确表达的信息，不要编造。输出 JSON：\n"
    "summary：用简洁的中文写出翻译时要遵守的语境和要求（合并新旧内容，不含逐词对照），不超过 300 字；\n"
    "add：这次新增或修改的词义，每项 {\"word\": 原文中的词或短语, \"meaning\": 在此语境下的中文译法或含义}，"
    "没有就给空列表；\n"
    "remove：这次用户要求删除、不再特殊处理的词（只写词本身），没有就给空列表；\n"
    "reply：用中文向用户复述你这次记住或改动了什么（一两句话，以“已记住：”开头）。"
)
_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "add": {"type": "array", "items": {"type": "object", "properties": {
            "word": {"type": "string"}, "meaning": {"type": "string"}}, "required": ["word", "meaning"]}},
        "remove": {"type": "array", "items": {"type": "string"}},
        "reply": {"type": "string"},
    },
    "required": ["summary", "add", "remove", "reply"],
}


def learn(preset: Preset, text: str) -> str:
    """把用户的新说明交给本地大模型理解、合并进预设，返回模型的复述。
    本地模型不可用时原样保存这段话（翻译时也会作为语境说明使用），并如实告知。"""
    from . import translate
    preset.notes.append(text)
    known = "；".join(f"{w} → {m}" for w, m in preset.terms.items()) or "（无）"
    user = f"已保存的语境说明：{preset.summary or '（无）'}\n已保存的词义：{known}\n\n用户的新说明：\n{text}"
    try:
        raw = translate.chat_json(_SYSTEM, user, _SCHEMA, max_tokens=900)
        data = json.loads(raw)
        preset.summary = str(data.get("summary", "")).strip() or preset.summary
        # 模型只给出这次的增删，由程序执行：没提到的旧词义保持不变（小模型改写整张表时容易漏掉条目）
        # 只执行用户原话里确实提到的词：小模型偶尔会删错对象或凭空加词
        said = text.lower()
        terms = dict(preset.terms)
        for w in data.get("remove", []):
            w = str(w).strip()
            key = next((k for k in terms if k.lower() == w.lower()), None)
            if key is not None and key.lower() in said:
                del terms[key]
        # 删除意图很明确时用规则兜底：同一小句里既有删除类说法、又提到某个已有的词
        import re
        for clause in re.split(r"[，。；;,.!?！？\n]", said):
            if re.search(r"删|去掉|去除|取消|不用|不再|按普通|恢复", clause):
                for k in list(terms):
                    if k.lower() in clause and not re.search(r"(改成|改为|换成|译为|译成|指)", clause):
                        del terms[k]
        for t in data.get("add", []):
            w, m = str(t.get("word", "")).strip(), str(t.get("meaning", "")).strip()
            if w and m and w.lower() in said:
                terms[next((k for k in terms if k.lower() == w.lower()), w)] = m
        # “X 译为 Y / 改成 Y / 指 Y / = Y”这类说法格式固定，也用规则直接识别（小模型偶尔会漏掉）
        for m in TERM_RX.finditer(text):
            w, meaning = m.group(1).strip(), m.group(2).strip()
            if w and meaning and not re.search(r"[A-Za-z]{3,}", meaning):  # 译法应是中文
                terms[next((k for k in terms if k.lower() == w.lower()), w)] = meaning
        preset.terms = terms
        reply = str(data.get("reply", "")).strip()
        if len(reply.replace("已记住", "").strip("：: \n")) < 4:  # 只回了一句“已记住：”
            reply = "已记住。"
        reply += "\n\n当前语境：" + (preset.summary or "（无）")
        if preset.terms:
            reply += "\n词义：" + "；".join(f"{w} → {m}" for w, m in preset.terms.items())
    except Exception as e:
        log.warning("语境说明交给本地模型理解失败：%s", e)
        preset.summary = (preset.summary + "\n" if preset.summary else "") + text
        reply = f"本地模型暂时不可用（{e}），已把这段话原样保存，翻译时会直接作为语境说明使用。"
    preset.replies.append(reply)
    return reply


def _relevant(summary: str, low: str) -> str:
    """去掉概述里讲具体某个外文词、而这个词本段并没有出现的分句：小模型会把“X 译为 Y”硬套到无关的文字上
    （例如把漫画人名译成“纤维蛋白纤维网络”）。"""
    keep = []
    for clause in re.split(r"(?<=[，。；;,])", summary or ""):
        words = re.findall(r"[A-Za-z][A-Za-z\- ]*[A-Za-z]|[゠-ヿ]{2,}", clause)
        if any(w.lower() not in low for w in words if len(w) >= 3 or not w.isascii()):
            continue
        keep.append(clause)
    return "".join(keep).strip("，,；; ")


def prompt_for(text: str) -> str:
    """当前预设要追加到翻译指令里的内容（只带上本段里出现的词义，避免指令过长）。"""
    p = active_preset()
    if p is None:
        return ""
    parts = []
    low = text.lower()
    summary = _relevant(p.summary, low)
    if summary:
        parts.append("本次翻译的语境与要求（用户设定，必须遵守）：" + summary)
    hits = [(w, m) for w, m in p.terms.items() if w.lower() in low]
    if hits:
        parts.append("在此语境下下列词的含义/译法：" + "；".join(f"{w} → {m}" for w, m in hits))
    return "\n".join(parts)
