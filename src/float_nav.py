#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex 会话导航 · 右侧窄长条悬浮窗（DPI 感知 · 清晰字体 · 简约风）"""
import ctypes

# DPI 声明必须在导入 tkinter / 创建任何窗口之前完成
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import os, sys, time, re, json, shutil, sqlite3, threading, queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nav_core as nc

W_BG, W_ALT, W_SIDE = "#ffffff", "#f6f8fb", "#e9edf3"
W_FG, W_MUT, W_ACC = "#1f2430", "#828b9c", "#3b6ef5"
W_ACC2, W_GOOD, W_DIV = "#eaf0ff", "#0d8f4f", "#eceff4"

F_UI   = ("Microsoft YaHei UI", 10)
F_SM   = ("Microsoft YaHei UI", 9)
F_MONO = ("Consolas", 9)
F_AN   = ("Microsoft YaHei UI", 7)
F_HD   = ("Microsoft YaHei UI", 10, "bold")

GIST_MAX = 15
PAGE_STEP = 10    # ▲/▼ 按钮每次翻多少轮
ANSWER_MAX = 20   # 回答预览取前 20 字
MARKS = ["#ffe0e0", "#fdf0c0", "#dbe9ff", "#d9f2e2", "#eadcff"]   # 浅红 浅黄 浅蓝 浅绿 浅紫
F_POP  = ("Microsoft YaHei UI", 11, "bold")
F_QHOV = ("Microsoft YaHei UI", -19, "bold")
WIN_W = 210        # 面板宽度（逻辑像素，越小越窄）
TOP_GAP = 18       # 距屏幕顶部
BOTTOM_GAP = 230   # 距屏幕底部留白 —— 避免挡住 Codex 的发送按钮

# ================= 键盘注入 / 窗口查找（点击轮次 -> 跳到 Codex 对应位置） =================
import ctypes as _ct
from ctypes import wintypes as _wt

_u32 = _ct.windll.user32
_u32.GetForegroundWindow.restype = _wt.HWND
_u32.SetForegroundWindow.argtypes = [_wt.HWND]
_u32.BringWindowToTop.argtypes = [_wt.HWND]
_u32.ShowWindow.argtypes = [_wt.HWND, _ct.c_int]
_u32.IsIconic.argtypes = [_wt.HWND]
_u32.GetAsyncKeyState.argtypes = [_ct.c_int]
_u32.GetAsyncKeyState.restype = _wt.SHORT

_INPUT_KEYBOARD, _KEYUP, _UNICODE = 1, 0x0002, 0x0004
_VK_CTRL, _VK_F, _VK_RETURN, _VK_ESC = 0x11, 0x46, 0x0D, 0x1B


class _KI(_ct.Structure):
    _fields_ = [("wVk", _wt.WORD), ("wScan", _wt.WORD), ("dwFlags", _wt.DWORD),
                ("time", _wt.DWORD), ("dwExtraInfo", _ct.POINTER(_ct.c_ulong))]


class _IU(_ct.Union):
    _fields_ = [("ki", _KI), ("pad", _ct.c_byte * 32)]   # x64 sizeof(INPUT) 必须为 40


class _INPUT(_ct.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", _wt.DWORD), ("u", _IU)]


def _tap(vk=0, scan=0, flags=0):
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.u.ki = _KI(vk, scan, flags, 0, None)
    return _u32.SendInput(1, _ct.byref(inp), _ct.sizeof(_INPUT))


def _key(vk):
    _tap(vk)
    _tap(vk, 0, _KEYUP)


def _ctrl(vk):
    n = _tap(_VK_CTRL)
    _tap(vk)
    _tap(vk, 0, _KEYUP)
    _tap(_VK_CTRL, 0, _KEYUP)
    return n


def _type(text):
    n = 0
    for ch in text:
        c = ord(ch)
        ok = _tap(0, c, _UNICODE)
        _tap(0, c, _UNICODE | _KEYUP)
        if ok:
            n += 1
    return n


def find_codex_hwnd():
    hits = []
    CB = _ct.WINFUNCTYPE(_ct.c_bool, _wt.HWND, _wt.LPARAM)

    def cb(h, _):
        if not _u32.IsWindowVisible(h):
            return True
        n = _u32.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        b = _ct.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(h, b, n + 1)
        c = _ct.create_unicode_buffer(256)
        _u32.GetClassNameW(h, c, 256)
        if c.value == "Chrome_WidgetWin_1" and b.value.strip() in ("ChatGPT", "Codex"):
            r = _wt.RECT()
            _u32.GetWindowRect(h, _ct.byref(r))
            hits.append(((r.right - r.left) * (r.bottom - r.top), h))
        return True

    _u32.EnumWindows(CB(cb), 0)
    hits.sort(key=lambda x: -x[0])
    return hits[0][1] if hits else 0



class FloatNav:
    def __init__(self, root):
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 1.0)          # 不透明 -> ClearType 生效
        except Exception:
            pass
        root.configure(bg=W_BG)

        dpi = root.winfo_fpixels("1i")
        self.S = dpi / 96.0
        try:
            root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        try:
            (HERE / "dpi-debug.log").write_text("dpi=%s screen=%sx%s scale=%s" % (dpi, sw, sh, self.S), encoding="utf-8")
        except Exception:
            pass
        self.W = self.px(WIN_W)
        self.H = sh - self.px(TOP_GAP) - self.px(BOTTOM_GAP)
        self.BAR = self.px(32)
        root.geometry("%dx%d+%d+%d" % (self.W, self.H, sw - self.W - self.px(6), self.px(TOP_GAP)))

        self.sessions, self.current_sid, self.turns = {}, None, []
        self.shown, self.expanded, self.follow = [], None, True
        self.cur_sig, self.collapsed = None, False
        self.line_of, self._busy = {}, False
        self.marks = {}
        self._shift_down = False
        self._shift_session = False
        self._color_i = -1
        self._drag_seen, self._drag_erase = set(), False
        self._msgq = queue.Queue()
        self._meta_cache, self._meta_ts, self._label_map = {}, 0, {}
        self._last_jump_ts = 0.0
        self._build()
        self.refresh(force=True)
        self.root.after(400, self.render)
        self.root.after(2000, self.poll)
        self.root.after(150, self._watch_shift)
        self.root.after(120, self._drain)

    def px(self, v):
        return int(round(v * self.S))

    # ---------------- UI ----------------
    def _build(self):
        r = self.root
        tk.Frame(r, bg=W_SIDE, width=self.px(1)).pack(side="left", fill="y")

        self.head = tk.Frame(r, bg=W_BG, height=self.BAR)
        self.head.pack(fill="x", side="top")
        self.head.pack_propagate(False)
        for w in (self.head,):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self.title = tk.Label(self.head, text="Codex", bg=W_BG, fg=W_FG, font=F_HD)
        self.title.pack(side="left", padx=(self.px(11), self.px(4)))
        self.title.bind("<Button-1>", self._drag_start)
        self.title.bind("<B1-Motion>", self._drag_move)

        for txt, cmd in (("✕", self.root.destroy), ("—", self.toggle_collapse), ("📌", self.toggle_pin)):
            b = tk.Label(self.head, text=txt, bg=W_BG, fg=W_MUT, font=("Segoe UI", 10), padx=self.px(6), cursor="hand2")
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.configure(fg=W_ACC))
            b.bind("<Leave>", lambda e, w=b: w.configure(fg=W_MUT))

        tk.Frame(r, bg=W_DIV, height=self.px(1)).pack(fill="x")

        box = tk.Frame(r, bg=W_BG)
        box.pack(fill="x", padx=self.px(7), pady=(self.px(8), self.px(6)))
        self.search = tk.Entry(box, bg=W_ALT, fg=W_MUT, insertbackground=W_ACC, font=F_UI, bd=0,
                               highlightthickness=1, highlightbackground=W_DIV, highlightcolor=W_ACC)
        self.search.pack(fill="x", ipady=self.px(4))
        self.search.insert(0, "搜索提问…")
        self._ph_on = True
        self.search.bind("<FocusIn>", self._focus_in)
        self.search.bind("<FocusOut>", self._focus_out)
        self.search.bind("<KeyRelease>", lambda e: self.render())
        self.search.bind("<Escape>", lambda e: (self._clear_search(), self.render()))

        row = tk.Frame(r, bg=W_BG)
        row.pack(fill="x", padx=self.px(7), pady=(0, self.px(7)))
        self.combo = ttk.Combobox(row, state="readonly", width=14, font=F_SM)
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", self._on_pick)
        self.follow_var = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text="跟随当前", variable=self.follow_var, bg=W_BG, fg=W_MUT, font=F_SM,
                       selectcolor=W_BG, activebackground=W_BG, activeforeground=W_ACC, bd=0,
                       highlightthickness=0, cursor="hand2",
                       command=lambda: setattr(self, "follow", self.follow_var.get())).pack(side="left", padx=(self.px(5), 0))

        tk.Frame(r, bg=W_DIV, height=self.px(1)).pack(fill="x")

        wrap = tk.Frame(r, bg=W_BG)
        wrap.pack(fill="both", expand=True)
        self.txt = tk.Text(wrap, bg=W_BG, fg=W_FG, font=F_UI, bd=0, highlightthickness=0,
                           wrap="word", padx=0, pady=0, cursor="arrow", insertwidth=0,
                           selectbackground=W_BG, selectforeground=W_FG, takefocus=0)
        self.txt.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(wrap, command=self.txt.yview, width=self.px(7), bd=0, relief="flat")
        sb.pack(side="right", fill="y")
        self.txt.config(yscrollcommand=sb.set)
        self.txt.bind("<B1-Motion>", self._on_drag)
        self.txt.bind("<ButtonRelease-1>", self._on_drag_end)
        try:
            self.f_an = tkfont.Font(root=r, family="Microsoft YaHei UI", size=7)
        except Exception:
            self.f_an = None
        self._tags()
        self.txt.config(state="disabled")

        tk.Frame(r, bg=W_DIV, height=self.px(1)).pack(fill="x", side="bottom")
        self.bar = tk.Frame(r, bg=W_BG, height=self.px(30))
        self.bar.pack(fill="x", side="bottom")
        self.bar.pack_propagate(False)
        self.status = tk.Label(self.bar, text="", bg=W_BG, fg=W_MUT, font=F_SM)
        self.status.pack(side="left", padx=self.px(7))
        self.grip = tk.Label(self.bar, text="◢", bg=W_BG, fg=W_MUT, font=("Segoe UI", 9), cursor="size_nw_se")
        self.grip.pack(side="right", padx=self.px(5))
        self.grip.bind("<Button-1>", self._rs_start)
        self.grip.bind("<B1-Motion>", self._rs_move)
        for txt, d in (("▼", PAGE_STEP), ("▲", -PAGE_STEP)):
            b = tk.Label(self.bar, text=txt, bg="#eef2ff", fg=W_ACC, font=F_SM,
                         padx=self.px(5), pady=self.px(1), cursor="hand2")
            b.pack(side="right", padx=self.px(3), pady=self.px(5))
            b.bind("<ButtonPress-1>", lambda e, dd=d: self._btn_press(dd))
            b.bind("<ButtonRelease-1>", lambda e: self._btn_release())
            b.bind("<Enter>", lambda e, w=b: w.configure(bg="#dbe6ff"))
            b.bind("<Leave>", lambda e, w=b: w.configure(bg="#eef2ff"))
        for w in (r, self.txt):
            w.bind("<Prior>", lambda e: self.page(-PAGE_STEP))
            w.bind("<Next>", lambda e: self.page(PAGE_STEP))

    def _tags(self):
        t, p = self.txt, self.px
        t.tag_config("nm", font=F_MONO, foreground=W_ACC, background=W_ALT, spacing1=p(7), lmargin1=p(9))
        t.tag_config("tm", font=F_SM, foreground=W_MUT, background=W_ALT, spacing1=p(7), lmargin2=p(9))
        t.tag_config("qq", font=F_SM, foreground="#3c4557", lmargin1=p(6), lmargin2=p(6), spacing1=p(1), spacing3=p(1))
        t.tag_config("an", font=self.f_an or F_AN, foreground="#9aa3b4", lmargin1=p(4), lmargin2=p(4), spacing1=p(1), spacing3=p(8))
        t.tag_config("you", font=("Microsoft YaHei UI", 9, "bold"), foreground=W_ACC, lmargin1=p(9), spacing1=p(10))
        t.tag_config("cx", font=("Microsoft YaHei UI", 9, "bold"), foreground=W_GOOD, lmargin1=p(9), spacing1=p(11))
        t.tag_config("bd", font=F_UI, foreground="#2c3444", lmargin1=p(11), lmargin2=p(11), spacing3=p(5))
        t.tag_config("cm", font=F_UI, foreground=W_MUT, lmargin1=p(11), lmargin2=p(11))
        t.tag_config("hint", font=F_SM, foreground=W_MUT, justify="center", spacing1=p(40))

    def _ph(self, on):
        self._ph_on = on
        if on:
            self.search.delete(0, "end")
            self.search.insert(0, "搜索提问…")
            self.search.config(fg=W_MUT)
        elif self.search.get() == "搜索提问…":
            self.search.delete(0, "end")
            self.search.config(fg=W_FG)

    def _focus_in(self, e):
        if self._ph_on:
            self._ph(False)

    def _focus_out(self, e):
        if not self.search.get().strip():
            self._ph(True)

    def _clear_search(self):
        self.search.delete(0, "end")
        self.search.config(fg=W_FG)
        self._ph_on = False

    # ---------------- 窗口 ----------------
    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))

    def _rs_start(self, e):
        self._rx, self._ry = e.x_root, e.y_root
        self._rw, self._rh = self.root.winfo_width(), self.root.winfo_height()

    def _rs_move(self, e):
        self.root.geometry("%dx%d" % (max(self.px(190), self._rw + e.x_root - self._rx),
                                      max(self.px(220), self._rh + e.y_root - self._ry)))

    def toggle_collapse(self):
        if self.collapsed:
            self.root.geometry("%dx%d" % (self._pw, self._ph))
            self.collapsed = False
        else:
            self._pw, self._ph = self.root.winfo_width(), self.root.winfo_height()
            self.root.geometry("%dx%d" % (self._pw, self.BAR))
            self.collapsed = True

    def toggle_pin(self):
        self.root.attributes("-topmost", not bool(self.root.attributes("-topmost")))

    # ---------------- 数据 ----------------
    def _db_rows(self):
        """读 Codex 的 state_5.sqlite：对话标题 + 最后查看时间(recency_at)。
        直连只读优先，失败则复制一份读；彻底失败时保留旧缓存，绝不用空数据覆盖。"""
        now = time.time()
        if self._meta_cache and now - self._meta_ts < 5:
            return self._meta_cache
        q = ("select id, coalesce(title,''), coalesce(recency_at_ms,0), coalesce(updated_at_ms,0), "
             "coalesce(thread_source,''), coalesce(archived,0) from threads")

        def harvest(con):
            out = {}
            for tid, title, rec, upd, src_kind, arch in con.execute(q):
                if arch:
                    continue
                if (title or "").strip() == "Guardian review":
                    continue
                out[tid] = {"title": title or "", "rec": rec or 0, "upd": upd or 0,
                            "user": 1 if src_kind == "user" else 0}
            return out

        src = Path(os.path.expanduser("~")) / ".codex" / "state_5.sqlite"
        rows = {}
        try:
            con = sqlite3.connect("file:%s?mode=ro" % src.as_posix(), uri=True, timeout=2)
            try:
                rows = harvest(con)
            finally:
                con.close()
        except Exception:
            rows = {}
        if not rows:
            for attempt in range(2):
                try:
                    tmp = Path(os.environ.get("TEMP", ".")) / ("codexnav_state_%d.db" % os.getpid())
                    shutil.copy2(src, tmp)
                    for suf in ("-wal", "-shm"):
                        sp = Path(str(src) + suf)
                        if sp.exists():
                            try:
                                shutil.copy2(sp, Path(str(tmp) + suf))
                            except Exception:
                                pass
                    con = sqlite3.connect(str(tmp), timeout=2)
                    try:
                        rows = harvest(con)
                    finally:
                        con.close()
                    if rows:
                        break
                except Exception:
                    rows = {}
                    time.sleep(0.15)
        if rows:
            self._meta_cache, self._meta_ts = rows, now
        return rows or self._meta_cache or {}

    def _active_thread_id(self):
        """当前正在看的对话 = recency_at 最新的【真实用户对话】"""
        rows = self._db_rows()
        best, best_r = None, -1
        for tid, m in rows.items():
            if not m.get("user"):
                continue
            if tid not in self.sessions:
                continue
            if m["rec"] > best_r:
                best_r, best = m["rec"], tid
        return best

    def _follow_target(self):
        tid = self._active_thread_id()
        if tid and tid in self.sessions:
            return tid
        return max(self.sessions.values(), key=lambda x: max(q.stat().st_mtime for q in x["files"]))["id"]

    def _sig(self, sid):
        try:
            return tuple(sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in self.sessions[sid]["files"]))
        except Exception:
            return None

    def _answer(self, t):
        last = any_txt = ""
        for a in t.get("asst", []):
            if (a.get("text") or "").strip():
                any_txt = a["text"]
                if a.get("phase") != "commentary":
                    last = a["text"]
        txt = self._plain(last or any_txt)
        if not txt:
            return ""
        return txt[:ANSWER_MAX] + ("\u2026" if len(txt) > ANSWER_MAX else "")

    def _short(self, t):
        g = (t.get("gist", "") or "").strip()
        return g[:GIST_MAX] + ("…" if len(g) > GIST_MAX else "")

    def _label(self, sid, s):
        cwd = s.get("cwd") or ""
        name = Path(cwd).name.strip()
        if not name:                      # 例如 D:\ 这种，basename 为空
            name = Path(cwd).drive or sid[:8]
        try:
            ts = time.strftime("%m-%d", time.localtime(max(q.stat().st_mtime for q in s["files"])))
        except Exception:
            ts = "?"
        return "%s · %s" % (name[:14], ts)

    def load(self, sid):
        self.current_sid = sid
        self.turns = nc.load_turns(self.sessions[sid]["files"])
        self.cur_sig = self._sig(sid)
        self.expanded = None
        self.render()

    def render(self):
        q = "" if self._ph_on else self.search.get().strip().lower()
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.shown = []
        self.line_of = {}
        for i, t in enumerate(self.turns, 1):
            if q and q not in (t.get("gist", "") + " " + (t.get("user") or "")).lower():
                continue
            self.shown.append(i - 1)
            tag, row = "it%d" % i, "row%d" % i
            self.line_of[i - 1] = int(self.txt.index("end-1c").split(".")[0])
            if i - 1 in self.marks:
                self.txt.tag_config(row, background=self.marks[i - 1])
            hd = "hd%d" % i
            self.txt.insert("end", "#%-3d" % i, ("nm", tag, row, hd))
            self.txt.insert("end", "%s\n" % nc.to_local(t.get("ts", "")), ("tm", tag, row, hd))
            self.txt.insert("end", self._short(t) + "\n", ("qq", tag, row, "qt%d" % i))
            ans = self._answer(t)
            if ans:
                self.txt.insert("end", ans + "\n", ("an", tag, row))
            self.txt.tag_bind(tag, "<Button-1>", lambda e, k=i - 1: self.on_click(k, bool(e.state & 1)))
            self.txt.tag_bind(row, "<Enter>", lambda e, k=i - 1: self._hover(k, True))
            self.txt.tag_bind(row, "<Leave>", lambda e, k=i - 1: self._hover(k, False))
        self.txt.config(state="disabled")
        self.status.config(text="%d 轮%s · %s" % (len(self.turns), (" 筛%d" % len(self.shown)) if q else "", time.strftime("%H:%M")))
        if not self.shown:
            self.txt.config(state="normal")
            self.txt.insert("end", "\n没有匹配的提问\n", ("hint",))
            self.txt.config(state="disabled")

    def _fit(self, txt, reserve=16):
        try:
            w = self.txt.winfo_width()
            if w <= 1 or not self.f_an:
                return txt
            avail = w - self.px(reserve)
            if self.f_an.measure(txt) <= avail:
                return txt
            while txt and self.f_an.measure(txt + "\u2026") > avail:
                txt = txt[:-1]
            return (txt + "\u2026") if txt else ""
        except Exception:
            return txt

    def _bg(self, name, color):
        try:
            self.txt.tag_config(name, background=color)
        except Exception:
            pass

    def _draw(self, t):
        self.txt.insert("end", "你\n", ("you",))
        self.txt.insert("end", (t.get("user") or "") + "\n", ("bd",))
        for a in t.get("asst", []):
            comm = a.get("phase") == "commentary"
            self.txt.insert("end", "\nCodex%s\n" % (" · 过程" if comm else ""), ("cx",))
            body = a.get("text") or ""
            if len(body) > 3500:
                body = body[:3500] + "\n…（截断）"
            self.txt.insert("end", body + "\n", ("cm" if comm else "bd",))

    # ---------------- 跳到 Codex 对应位置 ----------------
    def _norm(self, x):
        return re.sub(r"\s+", " ", x or "").strip()

    def _plain(self, x):
        x = re.sub(r"[*`#>|~]", "", x or "")
        x = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", x)
        return self._norm(x)

    def _search_key(self, idx):
        hay = " ".join(self._plain((a.get("user") or "") + " " + " ".join(x.get("text", "") for x in a.get("asst", [])))
                       for a in self.turns)
        t = self.turns[idx]
        cands = []
        u = self._plain(t.get("user"))
        if u:
            cands.append(u)
        last = any_txt = ""
        for x in t.get("asst", []):
            if (x.get("text") or "").strip():
                any_txt = x["text"]
                if x.get("phase") != "commentary":
                    last = x["text"]
        a = self._plain(last or any_txt)
        if a:
            cands.append(a)
        for base in cands:
            if len(base) < 4:
                continue
            for L in list(range(15, 91, 5)) + [len(base)]:
                k = base[:min(L, len(base))]
                if len(k) >= 4 and hay.count(k) == 1:
                    return k
        return None

    def _go_to(self, idx):
        if not self.turns or not (0 <= idx < len(self.turns)):
            return
        self.expanded = idx
        self.render()
        ln = self.line_of.get(idx)
        if ln is not None:
            try:
                self.txt.see("%d.0" % ln)
            except Exception:
                pass
        self._start_jump(idx)

    def page(self, delta):
        if not self.turns:
            return
        cur = self.expanded if self.expanded is not None else -1
        nxt = max(0, min(len(self.turns) - 1, cur + delta))
        if nxt == cur:
            self.status.config(text="已到边界")
            return
        self._go_to(nxt)

    # ---- ▲/▼ 按钮：短按翻页，长按跳到顶/底 ----
    def _btn_press(self, delta):
        self._btn_delta = delta
        self._btn_long_done = False
        try:
            self._btn_job = self.root.after(550, self._btn_long)
        except Exception:
            self._btn_job = None

    def _btn_long(self):
        self._btn_long_done = True
        self._btn_job = None
        n = len(self.turns)
        if not n:
            return
        if self._btn_delta < 0:
            self._go_to(0)
            self.status.config(text="⇧ 已到最上面 (#1)")
        else:
            self._go_to(n - 1)
            self.status.config(text="⇩ 已到最下面 (#%d)" % n)

    def _btn_release(self):
        job = getattr(self, "_btn_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
            self._btn_job = None
        if not getattr(self, "_btn_long_done", False):
            self.page(getattr(self, "_btn_delta", 0))

    def jump(self, idx):
        if self._busy:
            return
        self._busy = True
        try:
            self._jump(idx)
        finally:
            self._busy = False

    def _jump(self, idx):
        self._last_jump_ts = time.time()
        hwnd = find_codex_hwnd()
        if not hwnd:
            self._say("没找到 Codex 窗口")
            return
        key = self._search_key(idx)
        if not key:
            self._say("#%d 无法唯一定位" % (idx + 1))
            return

        # 1) 先激活窗口，确保深链接是由这个已存在的窗口处理（不会另开一个）
        for _ in range(3):
            if _u32.GetForegroundWindow() == hwnd:
                break
            if _u32.IsIconic(hwnd):
                _u32.ShowWindow(hwnd, 9)
            _u32.BringWindowToTop(hwnd)
            _u32.SetForegroundWindow(hwnd)
            time.sleep(0.15)

        if _u32.GetForegroundWindow() != hwnd:
            self._say("无法激活 Codex 窗口")
            return
        time.sleep(0.12)
        if not _ctrl(_VK_F):
            self._say("按键被系统拦截 (SendInput 失败)")
            return
        time.sleep(0.30)
        sent = _type(key)
        time.sleep(0.30)
        _key(_VK_RETURN)
        time.sleep(0.12)
        # 只在 Codex 仍是前台时才发 Esc：否则 Esc 会落到面板上把它关掉
        if _u32.GetForegroundWindow() == hwnd:
            _key(_VK_ESC)
        if sent < len(key):
            self._say("部分输入失败 (%d/%d)" % (sent, len(key)))
        else:
            self._say("↗ 已跳转 #%d" % (idx + 1))

    def on_click(self, idx, shift):
        try:
            self.txt.tag_remove("sel", "1.0", "end")
        except Exception:
            pass
        if shift:
            self._drag_seen = {idx}
            self._drag_erase = idx in self.marks
            if self._drag_erase:
                self._unmark(idx)
            else:
                self._mark(idx)
            self.status.config(text="已标记 %d 轮" % len(self.marks))
            return "break"
        self._shift_session = False
        self.expanded = idx
        self._start_jump(idx)
        return "break"

    def _mark(self, k):
        if k in self.marks:
            return
        if not self._shift_session:
            self._color_i = (self._color_i + 1) % len(MARKS)
            self._shift_session = True
        self.marks[k] = MARKS[self._color_i]
        self._paint(k)

    def _unmark(self, k):
        self.marks.pop(k, None)
        self._paint(k)

    def _paint(self, k):
        row = "row%d" % (k + 1)
        try:
            self.txt.tag_config(row, background=self.marks.get(k, ""))
            self.txt.tag_raise(row)
            self.txt.tag_remove("sel", "1.0", "end")
        except Exception:
            pass

    def _row_at(self, x, y):
        try:
            for n in self.txt.tag_names("@%d,%d" % (x, y)):
                if n.startswith("it"):
                    return int(n[2:]) - 1
        except Exception:
            pass
        return None

    def _on_drag(self, e):
        if not self._shift_down:
            return
        k = self._row_at(e.x, e.y)
        if k is None or k in self._drag_seen:
            return
        self._drag_seen.add(k)
        if self._drag_erase:
            self._unmark(k)
        else:
            self._mark(k)
        self.status.config(text="已标记 %d 轮" % len(self.marks))
        return "break"

    def _on_drag_end(self, e):
        if self._drag_seen:
            self.status.config(text="已标记 %d 轮" % len(self.marks))
        self._drag_seen = set()
        return "break"

    def _hover(self, k, on):
        row = "row%d" % (k + 1)
        hd = "hd%d" % (k + 1)
        qt = "qt%d" % (k + 1)
        try:
            self.txt.tag_config(hd, font=F_POP if on else "", spacing1=self.px(3) if on else "")
            self.txt.tag_config(qt, font=F_QHOV if on else "")
            self._bg(row, self._hover_color(k) if on else self._row_color(k))
            self.txt.tag_raise(row)
            self.txt.tag_raise(hd)
            self.txt.tag_raise(qt)
        except Exception:
            pass

    def _start_jump(self, idx):
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._jump_worker, args=(idx,), daemon=True).start()

    def _open_thread(self, sid):
        """用 codex:// 深链接把 Codex 切到指定对话（关键：否则在别的对话里搜不到）"""
        try:
            if sid:
                os.startfile("codex://threads/%s" % sid)
        except Exception:
            pass

    def _jump_worker(self, idx):
        try:
            self._jump(idx)
        finally:
            self._busy = False

    def _say(self, text):
        try:
            self._msgq.put(text)
        except Exception:
            pass

    def _drain(self):
        try:
            while True:
                self.status.config(text=self._msgq.get_nowait())
        except Exception:
            pass
        self.root.after(120, self._drain)

    def _row_color(self, k):
        return self.marks.get(k, "")

    def _hover_color(self, k):
        return self.marks.get(k) or "#f2f4f8"

    def _watch_shift(self):
        try:
            down = bool(_u32.GetAsyncKeyState(0x10) & 0x8000)
        except Exception:
            down = False
        if self._shift_down and not down:
            self._shift_session = False
        self._shift_down = down
        self.root.after(150, self._watch_shift)

    def _on_pick(self, e):
        sid = self._label_map.get(self.combo.get())
        if sid and sid in self.sessions:
            self.follow = False
            self.follow_var.set(False)
            self.load(sid)

    def refresh(self, force=False):
        self.sessions = nc.collect()
        if not self.sessions:
            return
        if force or not self.current_sid or self.follow or self.current_sid not in self.sessions:
            self.load(self._follow_target())
        self.update_combo()

    def update_combo(self):
        meta = self._db_rows()
        items = sorted(self.sessions.items(),
                       key=lambda kv: (meta.get(kv[0], {}).get("rec") or 0,
                                       max(p.stat().st_mtime for p in kv[1]["files"])), reverse=True)
        vals, m = [], {}
        for sid, ss in items:
            lab = self._label(sid, ss)
            if lab in m:
                lab = "%s·%s" % (lab, sid[:4])
            while lab in m:
                lab += "\u00b7"
            m[lab] = sid
            vals.append(lab)
        self._label_map = m
        self.combo["values"] = vals
        if self.current_sid in self.sessions:
            self.combo.set(self._label(self.current_sid, self.sessions[self.current_sid]))

    def poll(self):
        try:
            s = nc.collect()
            self.sessions = s
            if self.current_sid in s and self._sig(self.current_sid) != self.cur_sig:
                self.turns = nc.load_turns(s[self.current_sid]["files"])
                self.cur_sig = self._sig(self.current_sid)
                self.render()
            if self.follow:
                t = self._follow_target()
                if t and t != self.current_sid:
                    self.load(t)
                    self.status.config(text="已跟随当前对话")
            self.update_combo()
        except Exception as e:
            self.status.config(text="同步出错: %s" % str(e)[:36])
        self.root.after(2500, self.poll)


def main():
    root = tk.Tk()
    root.title("Codex 导航")
    try:
        root.option_add("*TCombobox*Listbox.background", W_BG)
        root.option_add("*TCombobox*Listbox.foreground", W_FG)
        root.option_add("*TCombobox*Listbox.selectBackground", W_ACC2)
        root.option_add("*TCombobox*Listbox.selectForeground", W_FG)
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("TCombobox", fieldbackground=W_ALT, background=W_ALT, foreground=W_FG,
                     arrowcolor=W_MUT, bordercolor=W_DIV, lightcolor=W_DIV, darkcolor=W_DIV,
                     borderwidth=1, padding=2)
    except Exception:
        pass
    app = FloatNav(root)

    def _on_esc(_e):
        # 跳转期间或刚跳完的 2 秒内忽略 Esc（那是注入给 Codex 的按键）
        if getattr(app, "_busy", False) or (time.time() - getattr(app, "_last_jump_ts", 0.0)) < 2.0:
            return "break"
        root.destroy()

    root.bind("<Escape>", _on_esc)
    root.mainloop()


if __name__ == "__main__":
    main()
