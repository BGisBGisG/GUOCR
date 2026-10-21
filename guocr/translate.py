"""在线翻译（Google 翻译的免费接口，只用标准库）。

固定译成简体中文（英文、日文等外文 → 中文）。
在线：Google 的 dict-chrome 接口（一次提交多段、逐段返回，正好用于把译文按段落贴回截图），失败退回 gtx 接口；
离线：内置 OPUS-MT 模型（CTranslate2 推理）。默认在线优先，访问不了 Google 时自动改用离线。
"""
import json
import re
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_DICT_URL = "https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl={tl}"
_GTX_URL = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={tl}&dt=t"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def target_language(text: str) -> str:  # noqa: ARG001  保留接口，便于以后加目标语言设置
    return "zh-CN"


def _post(url: str, fields, timeout: float):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _via_dict(lines: list[str], tl: str, timeout: float) -> list[str]:
    result = _post(_DICT_URL.format(tl=tl), [("q", s) for s in lines], timeout)
    # 单段时返回 [译文, 语言] 或 ["译文"]，多段时返回 [[译文, 语言], ...]
    if len(lines) == 1 and result and isinstance(result[0], str):
        result = [result]
    out = [item[0] if isinstance(item, list) else item for item in result]
    if len(out) != len(lines):
        raise ValueError("译文段数与原文不一致")
    return out


def _via_gtx(lines: list[str], tl: str, timeout: float) -> list[str]:
    result = _post(_GTX_URL.format(tl=tl), {"q": "\n".join(lines)}, timeout)
    out = "".join(seg[0] for seg in result[0] if seg and seg[0]).split("\n")
    if len(out) != len(lines):  # 行数对不上时，就整段放在第一行，其余留空
        out = ["".join(out)] + [""] * (len(lines) - 1)
    return out


_ONLINE = ("_via_dict", "_via_gtx")
_RETRY_ONLINE_AFTER = 600  # 在线接口连不上后，这么多秒内直接用离线，免得每次都卡在超时上
_online_down_until = 0.0


_progress = None  # 逐段进度回调 (序号, 译文)；只有逐段生成的离线大模型会调用


def translate_paragraphs(lines: list[str], timeout: float = 5, engine: str = "auto", on_progress=None) -> list[str]:
    """逐段翻译，返回与输入一一对应的译文。所选引擎全部失败时抛出最后一个异常。

    按段落（而不是按屏幕上的折行）提交，译文才能保留完整句意。
    on_progress(序号, 译文)：每翻完一段就回调一次，用于边翻边显示（在线接口整批返回，不会回调）。"""
    global _progress
    _progress = on_progress
    try:
        return _translate_paragraphs(lines, timeout, engine)
    finally:
        _progress = None


_NAME = re.compile(r"^(and\s+)?[A-Z][a-z]+(?:[\s-][A-Z][a-z]*\.?)?(?:\s[A-Z][a-z'\-]+)?[\s\d,*†‡#a-z]{0,6}$")


def is_name_list(text: str) -> bool:
    """作者名单（“Beikang Gu1, Kun Jiang1, … and Xianqiao Wang1”）：翻译模型常把人名音译得面目全非，保留原文。"""
    items = [s.strip() for s in re.split(r",|;|\band\b", text) if s.strip()]
    if len(items) < 3 or len(text) > 600:
        return False
    return sum(bool(_NAME.match(s)) for s in items) >= 0.7 * len(items)


def _translate_paragraphs(lines: list[str], timeout: float, engine: str) -> list[str]:
    keep = [i for i, s in enumerate(lines) if is_name_list(s)]
    if keep:  # 作者名单原样保留，其余照常翻译
        rest = [i for i in range(len(lines)) if i not in keep]
        out = list(lines)
        if rest:
            global _progress
            outer = _progress
            if outer is not None:
                _progress = lambda k, zh: outer(rest[k], zh)  # noqa: E731  进度回调的序号换回原序号
            try:
                for i, zh in zip(rest, _translate_paragraphs([lines[i] for i in rest], timeout, engine)):
                    out[i] = zh
            finally:
                _progress = outer
        if _progress is not None:
            for i in keep:
                _progress(i, lines[i])
        return out
    import time
    global _online_down_until
    tl = target_language("\n".join(lines))
    names = ENGINES.get(engine, ENGINES["auto"])[1]
    from . import context
    if engine == "auto" and context.active_preset() is not None and offline_ready():
        # 用户设了翻译语境：Google 用不上这些说明，优先用本地大模型
        names = ("_via_llm",) + tuple(n for n in names if n != "_via_llm")
    if engine == "auto" and time.monotonic() < _online_down_until:
        names = [n for n in names if n not in _ONLINE]
    error: Exception | None = None
    for name in names:
        try:
            return [s.strip() for s in globals()[name](lines, tl, timeout)]
        except Exception as e:
            log.warning("翻译接口 %s 失败：%s", name, e)
            error = e
            # 连不上（超时、DNS、被墙）而不是被限流：另一个 Google 接口大概率也连不上，直接跳过
            if name in _ONLINE and not isinstance(e, urllib.error.HTTPError) and isinstance(e, OSError):
                _online_down_until = time.monotonic() + _RETRY_ONLINE_AFTER
                names_left = [n for n in names[names.index(name) + 1:] if n not in _ONLINE]
                for name2 in names_left:
                    try:
                        return [s.strip() for s in globals()[name2](lines, tl, timeout)]
                    except Exception as e2:
                        log.warning("翻译接口 %s 失败：%s", name2, e2)
                        error = e2
                break
    raise error


def translate(text: str, timeout: float = 10, engine: str = "auto") -> str:
    return "\n".join(translate_lines(text.split("\n"), timeout, engine))


# ------------------------------------------------------------------ 离线翻译
# OPUS-MT 英→中模型（Argos Translate 打包的 CTranslate2 版本，约 80 MB），随程序分发，无需联网。
MODEL_URL = "https://argos-net.com/v1/translate-en_zh-1_9.argosmodel"


def _model_path(*parts: str) -> str:
    """离线模型文件的位置：
    - 从源码运行：项目里的 models/；
    - 完整版 exe：exe 内置的 models/；
    - 精简版 exe（不内置模型，便于发布到 GitHub）：%LOCALAPPDATA%/GUOCR/models，首次使用时下载到这里。"""
    import os
    import sys
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    bundled = os.path.join(base, "models", *parts)
    if os.path.exists(bundled) or not getattr(sys, "frozen", False):
        return bundled
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "GUOCR", "models", *parts)


def model_dir() -> str:
    return _model_path("en_zh")


_offline = None


def _tidy(s: str) -> str:
    """去掉模型在中文周围多输出的空格，把中文后面的半角标点换成全角。"""
    import re
    cjk = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
    s = s.replace("⁇", "").replace("⁇", "")  # 模型输出的未知词符号
    # 术语占位替换后，模型有时把英文冠词原样留在中文前面（如 “A哈希表”）
    s = re.sub(r"(?<![A-Za-z])(?:A|An|The|a|an|the)\s*(?=[一-鿿])", "", s)
    s = re.sub(rf"\s+(?=[{cjk}])|(?<=[{cjk}])\s+", "", s.strip())
    return re.sub(rf"(?<=[\u4e00-\u9fff])([.,;:?!])", lambda m: "。，；：？！"[".,;:?!".index(m.group(1))], s)


def _via_offline(lines: list[str], tl: str, timeout: float) -> list[str]:  # noqa: ARG001
    """兜底小模型（OPUS-MT，只会英译中）：日文段落原样返回，不交给它（会翻出乱码）。"""
    if any(is_japanese(s_) for s_ in lines):
        ja = [i for i, s_ in enumerate(lines) if is_japanese(s_)]
        rest = [i for i in range(len(lines)) if i not in ja]
        out = list(lines)
        for i, zh in zip(rest, _via_offline([lines[i] for i in rest], tl, timeout) if rest else []):
            out[i] = zh
        return out
    global _offline
    if _offline is None:
        import os

        import ctranslate2
        import sentencepiece
        d = model_dir()
        if not os.path.exists(os.path.join(d, "model", "model.bin")):
            raise FileNotFoundError(f"离线翻译模型不存在：{d}")
        _offline = (ctranslate2.Translator(os.path.join(d, "model"), device="cpu", compute_type="int8"),
                    sentencepiece.SentencePieceProcessor(model_file=os.path.join(d, "sentencepiece.model")))
    translator, sp = _offline
    # 模型按句子训练：一段里有多句时要先分句，否则后面的句子会被丢掉
    from .layout import split_sentences
    pieces, owner = [], []
    for i, line in enumerate(lines):
        for sent in split_sentences(line) or [""]:
            pieces.append(sent)
            owner.append(i)
    from . import terms
    gloss = terms.load(GLOSSARY_DOMAINS)
    fixed = {k: _glossary(p_) or gloss.exact(p_) for k, p_ in enumerate(pieces)}
    protected = [gloss.protect(p_) for p_ in pieces]  # 术语 → {0} {1}… 占位符

    def run(texts: list[str]) -> list[str]:
        toks = [sp.encode(t, out_type=str) for t in texts]
        res = translator.translate_batch(
            toks, beam_size=4, no_repeat_ngram_size=3, repetition_penalty=1.2,
            max_decoding_length=256, max_input_length=512,
        ) if texts else []
        # 截断长度防止个别短句退化成无限重复
        return [sp.decode(r.hypotheses[0][: len(t) * 3 + 8]).replace("▁", " ") for t, r in zip(toks, res)]

    raw = run([pt for pt, _ in protected])
    results: list[str | None] = [terms.Glossary.restore(r, zhs) if zhs else r
                                 for r, (_, zhs) in zip(raw, protected)]
    # 占位符被模型弄丢的句子：放弃术语替换，按原句重翻
    retry = [k for k, r in enumerate(results) if r is None]
    for k, r in zip(retry, run([pieces[k] for k in retry])):
        results[k] = r
    out = [""] * len(lines)
    for k, i in enumerate(owner):
        out[i] += fixed[k] or _tidy(results[k])
    return out

# 小模型对孤立的界面短词偶尔翻得很怪（如 OK→罢），常见的直接查表
_GLOSSARY = {
    "ok": "确定", "cancel": "取消", "yes": "是", "no": "否", "apply": "应用", "close": "关闭", "open": "打开",
    "save": "保存", "save as": "另存为", "delete": "删除", "edit": "编辑", "view": "视图", "help": "帮助",
    "file": "文件", "next": "下一步", "back": "上一步", "finish": "完成", "submit": "提交", "search": "搜索",
    "login": "登录", "log in": "登录", "sign in": "登录", "sign up": "注册", "log out": "退出登录",
    "sign out": "退出登录", "hello": "你好", "good morning": "早上好", "good night": "晚安",
    "thank you": "谢谢", "thanks": "谢谢", "welcome": "欢迎", "home": "首页", "more": "更多", "learn more": "了解更多", "read more": "阅读更多",
    "see more": "查看更多", "show more": "显示更多", "see all": "查看全部", "settings": "设置", "share": "分享",
}


def _glossary(line: str) -> str | None:
    return _GLOSSARY.get(line.strip().strip(".!:").lower())


# 离线翻译启用的专业词库领域（由 App 按设置更新），见 terms.DOMAINS
GLOSSARY_DOMAINS: list[str] = ["computer", "mechanical", "biology", "math"]


# ------------------------------------------------------------------ 离线大模型翻译
# Qwen2.5-3B-Instruct（Q4_K_M 量化，约 2 GB），用 llama.cpp 官方 Vulkan 版 llama-server 在后台推理：
# Vulkan 由显卡驱动自带，NVIDIA / AMD / Intel 显卡都能用，无需安装 CUDA 等运行库；
# 启动时自动挑选剩余显存最多、放得下模型的显卡，没有合适显卡（或显卡初始化失败）就改用 CPU。
# 推理进程绑定到主程序（作业对象），主程序退出或崩溃时会被系统一并结束。
# 加载失败或个别段落没翻出来时，退回上面的 OPUS-MT 小模型兜底。
LLM_FILE = "qwen2.5-3b-instruct-q4_k_m.gguf"
LLM_URLS = (f"https://hf-mirror.com/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/{LLM_FILE}",
            f"https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/{LLM_FILE}")
LLAMA_BUILD = "b11617"  # 固定版本，保证打包结果可复现
LLAMA_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/llama-{LLAMA_BUILD}-bin-win-vulkan-x64.zip"
LLAMA_KEEP = ("llama-server.exe", "llama-server-impl.dll", "llama-common.dll", "llama.dll", "mtmd.dll", "libomp.dll",
              "LICENSE-LLVM-OpenMP")  # 另外保留全部 ggml*.dll（含按 CPU 型号自动选择的优化版本）
NEED_VRAM_MIB = 2600  # 模型约 1.9 GB + 上下文缓存
_LLM_SYSTEM = ("你是专业翻译。把用户给出的外文（英文、日文等）准确、通顺地翻译成简体中文，专业术语使用规范译名。"
               "日文里的汉字词也要按中文习惯译出，不要照搬日文写法。"
               "保留数字、公式、代码、产品名和专有名词原样。只输出译文，不要解释，不要加引号。")


def _kana(text: str) -> int:
    """日文假名个数（平假名 3040–309F、片假名 30A0–30FF、半角片假名 FF66–FF9F）。"""
    return sum(1 for c in text if "぀" <= c <= "ヿ" or "ｦ" <= c <= "ﾟ")


def is_japanese(text: str) -> bool:
    return _kana(text) >= max(2, len(text) // 20)


def _base_dir() -> str:
    import os
    import sys
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def llm_path() -> str:
    return _model_path("llm", LLM_FILE)


def llama_dir() -> str:
    return _model_path("llama_bin")


def offline_ready() -> bool:
    """离线翻译所需的文件是否齐全（大模型 + 推理程序 + 兜底小模型）。"""
    import os
    return (os.path.exists(llm_path()) and os.path.exists(os.path.join(llama_dir(), "llama-server.exe"))
            and os.path.exists(os.path.join(model_dir(), "model", "model.bin")))


_download_lock = None


def ensure_offline_models(progress=None) -> None:
    """缺什么下载什么（精简版首次使用离线翻译时在后台调用）。progress(说明文字) 用于提示进度。"""
    import threading
    global _download_lock
    if _download_lock is None:
        _download_lock = threading.Lock()
    with _download_lock:  # 防止重复下载
        if offline_ready():
            return
        download_llama()
        download_model()
        download_llm(progress)


class _LlamaServer:
    """后台 llama-server 进程：首次使用时启动，之后常驻；线程安全。"""

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._proc = None
        self._port = 0
        self.device = ""  # 实际使用的设备，写进日志/界面

    @staticmethod
    def _pick_gpu(exe: str) -> tuple[str, str] | None:
        """列出 Vulkan 设备，返回剩余显存最多且足够放下模型的那块：(设备名如 Vulkan0, 显卡名)。"""
        import re
        import subprocess
        try:
            out = subprocess.run([exe, "--list-devices"], capture_output=True, text=True, timeout=30,
                                 creationflags=0x08000000, encoding="utf-8", errors="replace").stdout
        except Exception as e:
            log.warning("列出显卡失败：%s", e)
            return None
        best = None
        for dev, name, free in re.findall(r"^\s*(\w+\d+):\s*(.+?)\s*\(\s*\d+\s*MiB,\s*(\d+)\s*MiB free\)", out, re.M):
            if int(free) >= NEED_VRAM_MIB and (best is None or int(free) > best[2]):
                best = (dev, name, int(free))
        return best[:2] if best else None

    def _launch(self, gpu: tuple[str, str] | None) -> bool:
        import json
        import os
        import socket
        import subprocess
        import time

        from . import winutil
        from .config import CONFIG_DIR
        exe = os.path.join(llama_dir(), "llama-server.exe")
        with socket.socket() as s:  # 找一个空闲端口
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        args = [exe, "-m", llm_path(), "--host", "127.0.0.1", "--port", str(port), "-c", "4096", "--no-webui"]
        args += ["--device", gpu[0], "-ngl", "999"] if gpu else ["--device", "none", "-ngl", "0"]
        os.makedirs(CONFIG_DIR, exist_ok=True)
        logf = open(os.path.join(CONFIG_DIR, "llama-server.log"), "wb")
        proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=0x08000000, cwd=llama_dir())  # CREATE_NO_WINDOW
        logf.close()
        winutil.kill_with_parent(proc.pid)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        self._proc, self._port = proc, port
                        return True
            except Exception:
                time.sleep(0.3)
        proc.kill()
        return False

    def ensure(self) -> None:
        import os
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if not os.path.exists(llm_path()):
                raise FileNotFoundError(f"离线大模型不存在：{llm_path()}")
            gpu = self._pick_gpu(os.path.join(llama_dir(), "llama-server.exe"))
            if gpu and self._launch(gpu):
                self.device = f"显卡 {gpu[1]}"
            elif self._launch(None):
                self.device = "CPU"
            else:
                raise RuntimeError(r"离线翻译引擎启动失败，详见 %APPDATA%\GUOCR\llama-server.log")
            log.info("离线翻译引擎已启动：%s", self.device)

    def chat(self, system: str, text: str, extra: dict | None = None) -> str:
        import json
        self.ensure()
        body = json.dumps({"messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                           "temperature": 0, "max_tokens": len(text) * 2 + 64, **(extra or {})}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self._port}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None:
                self._proc.kill()
                self._proc = None


_server = _LlamaServer()


def chat_json(system: str, user: str, schema: dict, max_tokens: int = 800) -> str:
    """让本地大模型按 JSON Schema 输出（llama-server 按语法约束生成，保证格式正确），返回 JSON 字符串。"""
    import json
    _server.ensure()
    body = json.dumps({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                       "temperature": 0, "max_tokens": max_tokens,
                       "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}}
                      ).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{_server._port}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def preload_llm() -> None:
    """后台预先启动离线大模型（“仅离线”模式下程序启动时调用），首次翻译就不用等加载。"""
    try:
        _server.ensure()
    except Exception as e:
        log.warning("预加载离线大模型失败：%s", e)


def shutdown() -> None:
    _server.stop()


def _untranslated(src: str, out: str) -> bool:
    """模型偶尔原样输出原文：英文原文而译文里几乎没有汉字，或日文原文而译文里仍有大量假名，视为没翻。"""
    if is_japanese(src):
        return _kana(out) > max(2, _kana(src) // 3)
    han = sum(1 for c in out if "\u4e00" <= c <= "\u9fff")
    letters = sum(1 for c in src if c.isascii() and c.isalpha())
    return letters >= 4 and han < max(1, letters // 20)


def _llm_once(system: str, text: str) -> str:
    zh = _server.chat(system, text).strip().strip("“”\"'")
    for prefix in ("译文：", "译文:", "翻译：", "翻译:"):
        zh = zh.removeprefix(prefix).strip()
    return zh


def _leftover_english(src: str, out: str) -> bool:
    """译文中夹带了原文里的普通英文单词（不算全大写缩写、专有名词等原样保留的词）。"""
    import re
    src_words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", src)}
    return any(w in src_words for w in re.findall(r"\b[a-z]{3,}\b", out))


def _via_llm(lines: list[str], tl: str, timeout: float) -> list[str]:  # noqa: ARG001
    from . import terms
    gloss = terms.load(GLOSSARY_DOMAINS)
    out: list[str] = []
    failed: list[int] = []
    for i, text in enumerate(lines):
        if not text.strip() or not any(c.isalpha() for c in text):
            out.append(text)
            continue
        fixed = _glossary(text) or gloss.exact(text)
        if fixed:
            out.append(fixed)
            continue
        hints = gloss.hits(text)
        # 术语放进系统提示；正文只放原文，否则小模型容易被提示格式带偏
        system = _LLM_SYSTEM if not hints else (
            _LLM_SYSTEM + "\n下列术语请使用指定译名：" + "；".join(f"{en} → {zh}" for en, zh in hints.items()) + "。")
        from . import context
        extra = context.prompt_for(text)  # 用户设定的翻译语境（当前预设）
        if extra:
            system += "\n" + extra
        zh = _llm_once(system, text)
        if hints and _leftover_english(text, zh):
            # 术语一多，3B 小模型偶尔会夹带英文原词；去掉术语提示重翻一次，通顺优先
            plain = _llm_once(_LLM_SYSTEM, text)
            if not _leftover_english(text, plain):
                zh = plain
        if _untranslated(text, zh):
            failed.append(i)
        elif _progress is not None:
            _progress(i, zh)
        out.append(zh)
    retry = [i for i in failed if is_japanese(lines[i])]
    for i in retry:  # 日文：兜底小模型只会英译中，改为让大模型再翻一次（不带术语提示）
        zh = _llm_once(_LLM_SYSTEM, lines[i])
        if not _untranslated(lines[i], zh):
            out[i] = zh
    failed = [i for i in failed if i not in retry]
    if failed:  # 个别英文段落没翻出来，用小模型补上
        for i, zh in zip(failed, _via_offline([lines[i] for i in failed], tl, timeout)):
            out[i] = zh
    return out


ENGINES = {
    "auto": ("在线优先，失败时自动用离线", ("_via_dict", "_via_gtx", "_via_llm", "_via_offline")),
    "offline": ("仅离线（本地大模型，不联网）", ("_via_llm", "_via_offline")),
    "online": ("仅在线（Google）", ("_via_dict", "_via_gtx")),
}


def download_model() -> None:
    """下载并解压离线模型到 models/en_zh（打包前、或从源码首次运行时调用）。"""
    import io
    import os
    import zipfile
    d = model_dir()
    if os.path.exists(os.path.join(d, "model", "model.bin")):
        return
    print("正在下载离线翻译模型（约 70 MB）…")
    with urllib.request.urlopen(urllib.request.Request(MODEL_URL, headers={"User-Agent": _UA}), timeout=60) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in ("model/model.bin", "model/shared_vocabulary.json", "model/config.json",
                     "sentencepiece.model", "README.md", "metadata.json"):
            os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
            with open(os.path.join(d, name), "wb") as f:
                f.write(z.read(next(n for n in z.namelist() if n.endswith("/" + name))))


def download_llm(progress=None) -> None:
    """下载离线大模型（约 2 GB）；已存在则跳过。progress(说明文字) 用于提示进度。"""
    import os
    path = llm_path()
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print("正在下载离线翻译大模型（约 2 GB）…")
    error = None
    for url in LLM_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as r, open(path + ".part", "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done, last = 0, -1
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    pct = done * 100 // total if total else 0
                    if progress and pct // 10 != last:  # 每 10% 提示一次
                        last = pct // 10
                        progress(f"离线翻译模型下载中：{pct}%（{done >> 20} / {total >> 20} MB）")
            os.replace(path + ".part", path)
            return
        except Exception as e:
            print(f"从 {url} 下载失败：{e}")
            error = e
    raise RuntimeError(f"离线大模型下载失败：{error}")


def download_llama() -> None:
    """下载 llama.cpp 官方 Vulkan 版推理程序到 models/llama_bin，只保留需要的文件。"""
    import io
    import os
    import zipfile
    d = llama_dir()
    if os.path.exists(os.path.join(d, "llama-server.exe")):
        return
    print(f"正在下载离线推理程序 llama.cpp {LLAMA_BUILD}（Vulkan 版，约 32 MB）…")
    with urllib.request.urlopen(urllib.request.Request(LLAMA_URL, headers={"User-Agent": _UA}), timeout=120) as r:
        data = r.read()
    os.makedirs(d, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if base in LLAMA_KEEP or (base.startswith("ggml") and base.endswith(".dll")):
                with open(os.path.join(d, base), "wb") as f:
                    f.write(z.read(name))


if __name__ == "__main__":
    download_model()
    download_llama()
    download_llm()


translate_lines = translate_paragraphs  # 兼容旧名称
