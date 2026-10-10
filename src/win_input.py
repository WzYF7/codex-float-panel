"""Windows 底层封装：窗口查找 + 键盘注入。

只做两件事，不含任何业务逻辑：
  1. 找到 Codex 桌面版主窗口
  2. 向它发送 Ctrl+F / 文本 / Enter / Esc

⚠️ 注意：SendInput 的 INPUT 结构体在 x64 上必须是 40 字节。
   少写填充（写成 24 字节）会让 SendInput 直接返回 0（错误码 87），
   按键静默失效——这个坑踩过一次，见 _INPUTUNION 的注释。
"""

import ctypes
import time
from ctypes import wintypes

_u32 = ctypes.windll.user32
_u32.GetForegroundWindow.restype = wintypes.HWND
_u32.SetForegroundWindow.argtypes = [wintypes.HWND]
_u32.BringWindowToTop.argtypes = [wintypes.HWND]
_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.IsIconic.argtypes = [wintypes.HWND]
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_u32.GetAsyncKeyState.restype = wintypes.SHORT

SW_RESTORE = 9
VK_CTRL, VK_SHIFT, VK_F, VK_RETURN, VK_ESC = 0x11, 0x10, 0x46, 0x0D, 0x1B
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

CODEX_WINDOW_CLASS = "Chrome_WidgetWin_1"
CODEX_WINDOW_TITLES = ("ChatGPT", "Codex")


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    # union 必须是 32 字节，INPUT 整体才是 40（x64）。写成 24 会让 SendInput 失败。
    _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(vk=0, scan=0, flags=0):
    ev = _INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, None)))
    return _u32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(_INPUT))


def tap(vk):
    ok = _send(vk)
    _send(vk, 0, KEYEVENTF_KEYUP)
    return ok


def ctrl(vk):
    """Ctrl+<vk>；返回否成功发出（0 表示被系统拒绝）。"""
    ok = _send(VK_CTRL)
    _send(vk)
    _send(vk, 0, KEYEVENTF_KEYUP)
    _send(VK_CTRL, 0, KEYEVENTF_KEYUP)
    return ok


def type_text(text):
    """按 Unicode 逐字输入（不碰剪贴板）；返回成功发出的字符数。"""
    sent = 0
    for ch in text:
        code = ord(ch)
        if _send(0, code, KEYEVENTF_UNICODE):
            sent += 1
        _send(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
    return sent


def shift_pressed():
    return bool(_u32.GetAsyncKeyState(VK_SHIFT) & 0x8000)


def foreground():
    return _u32.GetForegroundWindow()


def find_codex_window():
    """返回 Codex 主窗口句柄；找不到返回 0。"""
    hits = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not _u32.IsWindowVisible(hwnd):
            return True
        n = _u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        title = ctypes.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(hwnd, title, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        _u32.GetClassNameW(hwnd, cls, 256)
        if cls.value == CODEX_WINDOW_CLASS and title.value.strip() in CODEX_WINDOW_TITLES:
            r = wintypes.RECT()
            _u32.GetWindowRect(hwnd, ctypes.byref(r))
            hits.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
        return True

    _u32.EnumWindows(CB(cb), 0)
    hits.sort(key=lambda x: -x[0])
    return hits[0][1] if hits else 0


def activate(hwnd, tries=3):
    """把窗口切到前台。返回是否真的成功了——失败时调用方必须放弃发按键，否则会打进别的窗口。"""
    for _ in range(tries):
        if _u32.GetForegroundWindow() == hwnd:
            return True
        if _u32.IsIconic(hwnd):
            _u32.ShowWindow(hwnd, SW_RESTORE)
        _u32.BringWindowToTop(hwnd)
        _u32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
    return _u32.GetForegroundWindow() == hwnd
