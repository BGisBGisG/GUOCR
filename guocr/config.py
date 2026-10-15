"""用户配置：保存在 %APPDATA%\\GUOCR\\config.json。"""
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields

APP_NAME = "GUOCR"
# 三个功能：截图（复制图片）、识别文字、识别并翻译
ACTIONS = ("capture", "ocr", "translate")
ACTION_NAMES = {"capture": "截图", "ocr": "识别文字", "translate": "翻译"}
DEFAULT_HOTKEYS = {"capture": "Alt+Q", "ocr": "Alt+E", "translate": "Alt+R"}
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "guocr.log")

log = logging.getLogger(__name__)


@dataclass
class Config:
    hotkey_capture: str = DEFAULT_HOTKEYS["capture"]
    hotkey_ocr: str = DEFAULT_HOTKEYS["ocr"]
    hotkey_translate: str = DEFAULT_HOTKEYS["translate"]
    show_result: bool = True        # 识别后弹出结果窗口（关闭时仅托盘气泡提示）
    # False：按段落输出（自动识别分栏/分页、分段，段内折行合并）；True：保留屏幕上的原始换行
    keep_raw_lines: bool = False
    # 离线翻译启用的专业词库（见 terms.DOMAINS）
    glossary_domains: list = field(default_factory=lambda: ["computer", "mechanical", "biology", "math"])
    translate_engine: str = "auto"  # auto：在线优先、失败用离线；offline：仅离线；online：仅在线

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return cfg
        except (OSError, ValueError):
            log.exception("读取配置失败，使用默认配置")
            return cfg
        for f_ in fields(cls):
            if f_.name in data and isinstance(data[f_.name], type(getattr(cfg, f_.name))):
                setattr(cfg, f_.name, data[f_.name])
        return cfg

    def hotkeys(self) -> dict[str, str]:
        return {a: getattr(self, f"hotkey_{a}") for a in ACTIONS}

    def set_hotkey(self, action: str, hotkey: str) -> None:
        setattr(self, f"hotkey_{action}", hotkey)

    def save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
