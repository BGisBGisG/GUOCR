"""Win32 杂项：窗口枚举（截图时自动吸附窗口）、单实例、开机自启、前台激活。"""
import ctypes
import os
import sys
import winreg
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.GetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wintypes.DWORD)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_LAYERED = 0x20, 0x80000
LWA_ALPHA = 0x2
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
ERROR_ALREADY_EXISTS = 183


def _is_invisible(hwnd) -> bool:
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if ex & WS_EX_TRANSPARENT:  # 鼠标穿透的覆盖层（录屏/游戏叠加层等）
        return True
    if ex & WS_EX_LAYERED:
        alpha, flags = ctypes.c_ubyte(255), wintypes.DWORD(0)
        if user32.GetLayeredWindowAttributes(hwnd, None, ctypes.byref(alpha), ctypes.byref(flags)):
            if flags.value & LWA_ALPHA and alpha.value == 0:
                return True
    cloaked = wintypes.DWORD(0)
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
        return True  # 其他虚拟桌面上的窗口、挂起的 UWP 窗口
    return False


def enum_window_rects() -> list[tuple[int, int, int, int]]:
    """按 Z 序（从上到下）返回可见顶层窗口的物理像素矩形 (left, top, right, bottom)。"""
    own_pid = os.getpid()
    rects = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid or _is_invisible(hwnd):
            return True
        r = wintypes.RECT()
        # 扩展边框坐标不含 Win10 窗口外侧的透明阴影
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left >= 8 and r.bottom - r.top >= 8:
            rects.append((r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return rects


def force_foreground(hwnd: int) -> None:
    """尽量把窗口切到前台，保证能收到 Esc 等键盘事件。"""
    hwnd = wintypes.HWND(hwnd)
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_tid = kernel32.GetCurrentThreadId()
    attached = bool(fg_tid) and fg_tid != cur_tid and user32.AttachThreadInput(cur_tid, fg_tid, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


_mutex = None


def acquire_single_instance(name: str = "Local\\GUOCR_SingleInstance") -> bool:
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


# ---------------------------------------------------------------- 开机自启
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "GUOCR"


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    return f'"{exe}" "{main_py}"'


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, _RUN_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- 子进程随主程序退出
class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                              "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_job = None


def kill_with_parent(pid: int) -> bool:
    """把子进程放进一个“主程序句柄关闭即全部结束”的作业对象：主程序正常退出或崩溃时，子进程都会被系统结束。"""
    global _job
    if _job is None:
        job = kernel32.CreateJobObjectW(None, None)
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return False
        _job = job
    h = kernel32.OpenProcess(0x0101, False, pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
    if not h:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(_job, h))
    finally:
        kernel32.CloseHandle(h)


# ---------------------------------------------------------------- 挑选显卡（DirectML 用）
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", wintypes.WCHAR * 128), ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", ctypes.c_int64), ("Flags", wintypes.UINT)]


def _com_call(obj, index: int, restype, *argtypes):
    """按虚表序号调用 COM 方法。"""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def best_gpu_adapter() -> tuple[int, str, int] | None:
    """枚举 DXGI 显卡（顺序与 DirectML 的 device_id 一致），返回独立显存最大的硬件显卡：(序号, 名称, 显存 MB)。
    笔记本同时有核显和独显时会选独显；没有硬件显卡时返回 None。"""
    try:
        dxgi = ctypes.WinDLL("dxgi")
        iid = _GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
        factory = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return None
    except OSError:
        return None
    best = None
    try:
        for i in range(16):
            adapter = ctypes.c_void_p()
            # IDXGIFactory1::EnumAdapters1 是虚表第 12 项；越界时返回 DXGI_ERROR_NOT_FOUND
            if _com_call(factory, 12, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(
                    factory, i, ctypes.byref(adapter)) != 0:
                break
            desc = _DXGI_ADAPTER_DESC1()
            _com_call(adapter, 10, ctypes.c_long, ctypes.POINTER(_DXGI_ADAPTER_DESC1))(adapter, ctypes.byref(desc))
            _com_call(adapter, 2, wintypes.ULONG)(adapter)  # Release
            if desc.Flags & 0x2:  # DXGI_ADAPTER_FLAG_SOFTWARE（微软基本渲染驱动）
                continue
            mb = desc.DedicatedVideoMemory // (1 << 20)
            if best is None or mb > best[2]:
                best = (i, desc.Description, mb)
    finally:
        _com_call(factory, 2, wintypes.ULONG)(factory)
    return best
