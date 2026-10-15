"""基于 Win32 RegisterHotKey 的全局快捷键（无需管理员权限，不装键盘钩子）。"""
import ctypes
import threading
from ctypes import wintypes

from PySide6.QtCore import QThread, Signal

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_QUIT, WM_USER, WM_HOTKEY = 0x0012, 0x0400, 0x0312
PM_NOREMOVE = 0
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "meta": MOD_WIN, "super": MOD_WIN,
}

_NAMED_KEYS = {
    "print": 0x2C, "printscreen": 0x2C, "prtsc": 0x2C, "sysreq": 0x2C,
    "space": 0x20, "tab": 0x09, "pause": 0x13, "scrolllock": 0x91,
    "ins": 0x2D, "insert": 0x2D, "del": 0x2E, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pageup": 0x21, "pgdown": 0x22, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "`": 0xC0, "~": 0xC0, "-": 0xBD, "_": 0xBD, "=": 0xBB, "+": 0xBB,
    "[": 0xDB, "{": 0xDB, "]": 0xDD, "}": 0xDD, "\\": 0xDC, "|": 0xDC,
    ";": 0xBA, ":": 0xBA, "'": 0xDE, '"': 0xDE, ",": 0xBC, "<": 0xBC,
    ".": 0xBE, ">": 0xBE, "/": 0xBF, "?": 0xBF,
    # Qt 在按住 Shift 时可能上报符号而不是数字
    "!": 0x31, "@": 0x32, "#": 0x33, "$": 0x34, "%": 0x35,
    "^": 0x36, "&": 0x37, "*": 0x38, "(": 0x39, ")": 0x30,
}


def parse_hotkey(text: str) -> tuple[int, int]:
    """把 "Ctrl+Alt+O" 这类字符串解析为 (modifiers, virtual-key)。"""
    text = (text or "").strip()
    if text.endswith("++"):
        parts, key = text[:-2].split("+"), "+"
    else:
        parts = text.split("+")
        key = parts.pop().strip()
    mods = 0
    for part in parts:
        name = part.strip().lower()
        if name not in _MODIFIERS:
            raise ValueError(f"无法识别的修饰键：{part}")
        mods |= _MODIFIERS[name]

    lower = key.lower()
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif lower.startswith("f") and lower[1:].isdigit() and 1 <= int(lower[1:]) <= 24:
        vk = 0x70 + int(lower[1:]) - 1
    elif lower in _NAMED_KEYS:
        vk = _NAMED_KEYS[lower]
    else:
        raise ValueError(f"不支持的按键：{key or '(空)'}")

    standalone_ok = 0x70 <= vk <= 0x87 or vk in (0x2C, 0x13, 0x91)  # F 键 / PrtSc / Pause / ScrollLock
    if not mods and not standalone_ok:
        raise ValueError("请至少包含一个修饰键（Ctrl / Alt / Shift / Win）")
    return mods, vk


def is_hotkey_available(hotkey: str) -> bool:
    """试注册一次再立即注销，判断该组合键当前是否被其他程序占用。"""
    mods, vk = parse_hotkey(hotkey)
    if not user32.RegisterHotKey(None, 0xBFFF, mods | MOD_NOREPEAT, vk):
        return False
    user32.UnregisterHotKey(None, 0xBFFF)
    return True


class HotkeyListener(QThread):
    """在独立线程里注册一组热键并跑消息循环，按下时发出 triggered(动作名)。

    hotkeys: {动作名: "Alt+Q"}，值为空字符串表示不启用该功能的快捷键。
    """

    triggered = Signal(str)
    failed = Signal(str)

    def __init__(self, hotkeys: dict[str, str], names: dict[str, str] | None = None, parent=None):
        super().__init__(parent)
        self._names = names or {}
        self._keys = []  # [(id, action, hotkey, mods, vk)]
        self._errors = []
        for i, (action, hotkey) in enumerate(hotkeys.items(), start=1):
            if not hotkey:
                continue
            try:
                self._keys.append((i, action, hotkey, *parse_hotkey(hotkey)))
            except ValueError as e:
                self._errors.append(f"{self._names.get(action, action)}快捷键 {hotkey} 无效：{e}")
        self._tid = 0
        self._ready = threading.Event()

    def run(self) -> None:
        msg = wintypes.MSG()
        # 先 Peek 一次以确保本线程的消息队列已创建，stop() 才能投递 WM_QUIT
        user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)
        self._tid = kernel32.GetCurrentThreadId()
        actions = {}
        errors = list(self._errors)
        for hid, action, hotkey, mods, vk in self._keys:
            if user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                actions[hid] = action
                continue
            err = ctypes.get_last_error()
            name = self._names.get(action, action)
            if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                errors.append(f"{name}快捷键 {hotkey} 已被其他程序占用")
            else:
                errors.append(f"{name}快捷键 {hotkey} 注册失败（错误码 {err}）")
        self._ready.set()
        if errors:
            self.failed.emit("；".join(errors) + "。请在设置中更换。")
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in actions:
                    self.triggered.emit(actions[msg.wParam])
        finally:
            for hid in actions:
                user32.UnregisterHotKey(None, hid)

    def stop(self) -> None:
        if self.isRunning():
            self._ready.wait(2)
            if self._tid:
                user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self.wait(2000)


VK_ESCAPE = 0x1B


class EscListener(HotkeyListener):
    """临时的全局 Esc 热键：截图框/结果弹窗/贴图还在屏幕上时注册，全部关闭后注销。

    弹窗和贴图为了不抢焦点是“不激活”显示的，收不到键盘事件，所以要靠全局热键实现
    “按 Esc 全部退出”（和 QQ 截图一致）。注册期间 Esc 不会传给其他程序。
    """

    def __init__(self, parent=None):
        super().__init__({}, parent=parent)
        self._keys = [(1, "esc", "Esc", 0, VK_ESCAPE)]
