"""Codex 会话导航 · 右侧窄长条悬浮窗。

职责分工：
    nav_core.py   解析 ~/.codex 的会话记录（纯数据）
    win_input.py  窗口查找 + 键盘注入（纯系统调用）
    float_nav.py  本文件：界面与交互

界面结构（从上到下）：
    标题栏   Codex + 置顶/收起/关闭
    搜索框   过滤轮次
    选择栏   项目下拉 + 跟随当前
    轮次列表 每轮三行：#编号+时间 / 提问前15字 / 回答前20字
    状态栏   轮数 · 时间 + ▲/▼ 翻页按钮
"""

# ⚠️ DPI 感知必须在导入 tkinter、创建任何窗口之前完成，
#    否则 150% 缩放下整个窗口会被系统拉伸，字迹发虚。
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import os
import queue
import re
import sqlite3
import shutil
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk

import nav_core as nc
import win_input as wi

# ----------------------------------------------------------------- 主题
BG, ALT, LINE = "#ffffff", "#f6f8fb", "#eceef2"
FG, MUTED, ACCENT = "#1f2430", "#8b93a3", "#3b6ef5"
GOOD = "#0d8f4f"
HOVER_BG = "#f2f4f8"
MARK_COLORS = ["#ffe0e0", "#fdf0c0", "#dbe9ff", "#d9f2e2", "#eadcff"]   # 浅红黄蓝绿紫

F_TITLE = ("Microsoft YaHei UI", 10, "bold")
F_BODY = ("Microsoft YaHei UI", 10)
F_SMALL = ("Microsoft YaHei UI", 9)
F_TINY = ("Microsoft YaHei UI", 7)
F_MONO = ("Consolas", 9)
F_HOVER_TITLE = ("Microsoft YaHei UI", 11, "bold")
F_HOVER_BODY = ("Microsoft YaHei UI", -19, "bold")       # 像素字号，保证 15 字仍在一行

# ----------------------------------------------------------------- 行为参数
WIN_W, TOP_GAP, BOTTOM_GAP = 210, 18, 230     # 面板宽度 / 距顶 / 距底留白（避开发送按钮）
QUESTION_CHARS, ANSWER_CHARS = 15, 20
PAGE_STEP = 10                                # ▲/▼ 每次翻多少轮
LONG_PRESS_MS = 550                           # 长按判定阈值
POLL_MS = 2500                                # 轮询间隔
META_CACHE_S = 5                              # 会话元信息缓存时长


class FloatNav:
    # ============================================================ 生命周期
    def __init__(self, root):
        self.root = root
        self.sessions = {}
        self.current_sid = None
        self.turns = []
        self.visible = []                     # 过滤后剩余的轮次下标
        self.selected = None                  # 当前高亮/跳转到的轮次下标
        self.marks = {}                       # 轮次下标 -> 颜色
        self.follow = True

        self._busy = False
        self._last_jump = 0.0
        self._status_queue = queue.Queue()
        self._meta_cache, self._meta_time = {}, 0.0
        self._signature = None
        self._render_key = None
        self._hay_cache = None
        self._combo_values, self._combo_sid, self._label_map = None, None, {}
        self._shift_down = self._shift_session = False
        self._mark_color = -1
        self._drag_seen, self._drag_erase = set(), False
        self._press_delta, self._press_job, self._press_long = 0, None, False

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 1.0)         # 不透明 -> ClearType 生效
        root.configure(bg=BG)

        self.dpi = root.winfo_fpixels("1i")
        self.scale = self.dpi / 96.0
        try:
            root.tk.call("tk", "scaling", self.dpi / 72.0)
        except Exception:
            pass

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.width = self.px(WIN_W)
        self.height = sh - self.px(TOP_GAP) - self.px(BOTTOM_GAP)
        root.geometry("%dx%d+%d+%d" % (self.width, self.height,
                                       sw - self.width - self.px(6), self.px(TOP_GAP)))

        self._build()
        self.refresh(force=True)
        root.after(400, lambda: self.render(force=True))    # 首帧拿到真实宽度后重排
        root.after(POLL_MS, self.poll)
        root.after(150, self._watch_shift)
        root.after(120, self._drain_status)

    def px(self, value):
        """逻辑像素 -> 物理像素。"""
        return int(round(value * self.scale))

    # ============================================================ 界面构建
    def _build(self):
        self._build_titlebar()
        self._build_search()
        self._build_picker()
        self._build_list()
        self._build_statusbar()

    def _build_titlebar(self):
        self.titlebar = tk.Frame(self.root, bg=BG, height=self.px(32))
        self.titlebar.pack(fill="x", side="top")
        self.titlebar.pack_propagate(False)
        for widget in (self.titlebar,):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        title = tk.Label(self.titlebar, text="Codex", bg=BG, fg=FG, font=F_TITLE)
        title.pack(side="left", padx=(self.px(11), self.px(4)))
        title.bind("<Button-1>", self._drag_start)
        title.bind("<B1-Motion>", self._drag_move)

        self.pin_button = None
        for text, action in (("✕", self.root.destroy),
                             ("—", self.toggle_collapse),
                             ("📌", self.toggle_pin)):
            btn = tk.Label(self.titlebar, text=text, bg=BG, fg=MUTED,
                           font=("Segoe UI", 10), padx=self.px(6), cursor="hand2")
            btn.pack(side="right")
            btn.bind("<Button-1>", lambda _e, a=action: a())
            # 悬停只改背景色，避免盖掉钉子的"已置顶/未置顶"颜色状态
            btn.bind("<Enter>", lambda _e, w=btn: w.configure(bg="#eef2ff"))
            btn.bind("<Leave>", lambda _e, w=btn: w.configure(bg=BG))
            if text == "📌":
                self.pin_button = btn
        self._sync_pin_button()

        tk.Frame(self.root, bg=LINE, height=self.px(1)).pack(fill="x")

    def _build_search(self):
        holder = tk.Frame(self.root, bg=BG)
        holder.pack(fill="x", padx=self.px(7), pady=(self.px(8), self.px(6)))
        self.search = tk.Entry(holder, bg=ALT, fg=MUTED, insertbackground=ACCENT,
                               font=F_BODY, bd=0, highlightthickness=1,
                               highlightbackground=LINE, highlightcolor=ACCENT)
        self.search.pack(fill="x", ipady=self.px(4))
        self._placeholder = True
        self._show_placeholder()
        self.search.bind("<FocusIn>", self._on_search_focus)
        self.search.bind("<FocusOut>", self._on_search_blur)
        self.search.bind("<KeyRelease>", lambda _e: self.render())
        self.search.bind("<Escape>", lambda _e: self._clear_search())

    def _build_picker(self):
        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=self.px(7), pady=(0, self.px(7)))
        self.combo = ttk.Combobox(row, state="readonly", width=14, font=F_SMALL)
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", self._on_pick)
        self.follow_var = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text="跟随当前", variable=self.follow_var, bg=BG, fg=MUTED,
                       font=F_SMALL, selectcolor=BG, activebackground=BG,
                       activeforeground=ACCENT, bd=0, highlightthickness=0, cursor="hand2",
                       command=self._on_follow_toggle).pack(side="left", padx=(self.px(5), 0))
        tk.Frame(self.root, bg=LINE, height=self.px(1)).pack(fill="x")

    def _build_list(self):
        holder = tk.Frame(self.root, bg=BG)
        holder.pack(fill="both", expand=True)
        self.text = tk.Text(holder, bg=BG, fg=FG, font=F_BODY, bd=0, highlightthickness=0,
                            wrap="word", padx=0, pady=0, cursor="arrow", insertwidth=0,
                            selectbackground=BG, selectforeground=FG, takefocus=0)
        self.text.pack(side="left", fill="both", expand=True)
        scroll = tk.Scrollbar(holder, command=self.text.yview, width=self.px(7), bd=0, relief="flat")
        scroll.pack(side="right", fill="y")
        self.text.config(yscrollcommand=scroll.set)
        self.text.bind("<B1-Motion>", self._on_pointer_drag)
        self.text.bind("<ButtonRelease-1>", self._on_pointer_release)
        self._configure_tags()
        self.text.config(state="disabled")

    def _build_statusbar(self):
        tk.Frame(self.root, bg=LINE, height=self.px(1)).pack(fill="x", side="bottom")
        self.statusbar = tk.Frame(self.root, bg=BG, height=self.px(30))
        self.statusbar.pack(fill="x", side="bottom")
        self.statusbar.pack_propagate(False)

        self.status = tk.Label(self.statusbar, text="", bg=BG, fg=MUTED, font=F_SMALL)
        self.status.pack(side="left", padx=self.px(7))

        for text, delta in (("▼", PAGE_STEP), ("▲", -PAGE_STEP)):
            btn = tk.Label(self.statusbar, text=text, bg="#eef2ff", fg=ACCENT, font=F_SMALL,
                           padx=self.px(5), pady=self.px(1), cursor="hand2")
            btn.pack(side="right", padx=self.px(3), pady=self.px(5))
            btn.bind("<ButtonPress-1>", lambda _e, d=delta: self._press_page_button(d))
            btn.bind("<ButtonRelease-1>", lambda _e: self._release_page_button())
            btn.bind("<Enter>", lambda _e, w=btn: w.configure(bg="#dbe6ff"))
            btn.bind("<Leave>", lambda _e, w=btn: w.configure(bg="#eef2ff"))

        grip = tk.Label(self.statusbar, text="◢", bg=BG, fg=MUTED,
                        font=("Segoe UI", 9), cursor="size_nw_se")
        grip.pack(side="right", padx=self.px(5))
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)

        for widget in (self.root, self.text):
            widget.bind("<Prior>", lambda _e: self._page(-PAGE_STEP))
            widget.bind("<Next>", lambda _e: self._page(PAGE_STEP))

    def _configure_tags(self):
        p = self.px
        t = self.text
        t.tag_config("num", font=F_MONO, foreground=ACCENT, background=ALT,
                     spacing1=p(7), lmargin1=p(9))
        t.tag_config("time", font=F_SMALL, foreground=MUTED, background=ALT,
                     spacing1=p(7), lmargin2=p(9))
        t.tag_config("question", font=F_SMALL, foreground="#3c4557",
                     lmargin1=p(6), lmargin2=p(6), spacing1=p(1), spacing3=p(1))
        t.tag_config("answer", font=F_TINY, foreground="#9aa3b4",
                     lmargin1=p(4), lmargin2=p(4), spacing1=p(1), spacing3=p(8))
        t.tag_config("empty", font=F_SMALL, foreground=MUTED, justify="center", spacing1=p(40))

    # ============================================================ 窗口操作
    def _drag_start(self, event):
        self._drag_dx = event.x_root - self.root.winfo_x()
        self._drag_dy = event.y_root - self.root.winfo_y()

    def _drag_move(self, event):
        self.root.geometry("+%d+%d" % (event.x_root - self._drag_dx, event.y_root - self._drag_dy))

    def _resize_start(self, event):
        self._resize_xy = (event.x_root, event.y_root)
        self._resize_wh = (self.root.winfo_width(), self.root.winfo_height())

    def _resize_move(self, event):
        width = max(self.px(190), self._resize_wh[0] + event.x_root - self._resize_xy[0])
        height = max(self.px(220), self._resize_wh[1] + event.y_root - self._resize_xy[1])
        self.root.geometry("%dx%d" % (width, height))

    def toggle_collapse(self):
        if getattr(self, "_collapsed", False):
            self.root.geometry("%dx%d" % self._expanded_size)
            self._collapsed = False
        else:
            self._expanded_size = (self.root.winfo_width(), self.root.winfo_height())
            self.root.geometry("%dx%d" % (self._expanded_size[0], self.px(32)))
            self._collapsed = True

    def toggle_pin(self):
        """置顶开关：开启时始终浮在其他窗口之上。开=蓝色实心，关=灰色。"""
        pinned = not bool(self.root.attributes("-topmost"))
        self.root.attributes("-topmost", pinned)
        self._sync_pin_button()
        self.status.config(text="已置顶（始终在最前）" if pinned else "已取消置顶（可被其他窗口盖住）")

    def _sync_pin_button(self):
        if getattr(self, "pin_button", None) is None:
            return
        pinned = bool(self.root.attributes("-topmost"))
        self.pin_button.configure(fg=ACCENT if pinned else "#c3c9d4")

    # ============================================================ 搜索框
    def _show_placeholder(self):
        self.search.delete(0, "end")
        self.search.insert(0, "搜索提问…")
        self.search.config(fg=MUTED)
        self._placeholder = True

    def _on_search_focus(self, _event):
        if self._placeholder:
            self.search.delete(0, "end")
            self.search.config(fg=FG)
            self._placeholder = False

    def _on_search_blur(self, _event):
        if not self.search.get().strip():
            self._show_placeholder()

    def _clear_search(self):
        self.search.delete(0, "end")
        self.search.config(fg=FG)
        self._placeholder = False
        self.render()

    def _search_query(self):
        return "" if self._placeholder else self.search.get().strip().lower()

    # ============================================================ 会话元信息
    def _meta_rows(self):
        """读 Codex 的 state_5.sqlite：每条对话的标题与「最后查看时间」recency_at。

        优先直连只读；失败则复制一份再读；彻底失败时保留上次结果，
        绝不用空数据覆盖（否则会退化成按文件写入时间判断，跟错对话）。
        """
        now = time.time()
        if self._meta_cache and now - self._meta_time < META_CACHE_S:
            return self._meta_cache

        sql = ("select id, coalesce(title,''), coalesce(recency_at_ms,0), coalesce(updated_at_ms,0), "
               "coalesce(thread_source,''), coalesce(archived,0) from threads")

        def harvest(con):
            rows = {}
            for tid, title, recency, updated, source, archived in con.execute(sql):
                if archived or (title or "").strip() == "Guardian review":
                    continue                      # 自动审查线程会污染排序，跳过
                rows[tid] = {"title": title or "", "recency": recency or 0,
                             "updated": updated or 0, "is_user": source == "user"}
            return rows

        db = nc.CODEX_HOME / "state_5.sqlite"
        rows = {}
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=2)
            try:
                rows = harvest(con)
            finally:
                con.close()
        except Exception:
            rows = {}
        if not rows:
            for _ in range(2):
                try:
                    tmp = Path(os.environ.get("TEMP", ".")) / ("codexnav_%d.db" % os.getpid())
                    shutil.copy2(db, tmp)
                    for suffix in ("-wal", "-shm"):
                        extra = Path(str(db) + suffix)
                        if extra.exists():
                            try:
                                shutil.copy2(extra, Path(str(tmp) + suffix))
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
            self._meta_cache, self._meta_time = rows, now
        return rows or self._meta_cache or {}

    def _current_thread(self):
        """当前正在看的对话 = recency_at 最新的真实用户对话。"""
        best, best_time = None, -1
        for tid, info in self._meta_rows().items():
            if not info["is_user"] or tid not in self.sessions:
                continue
            if info["recency"] > best_time:
                best, best_time = tid, info["recency"]
        return best

    def _follow_target(self):
        tid = self._current_thread()
        if tid:
            return tid
        return max(self.sessions.values(),
                   key=lambda s: max(p.stat().st_mtime for p in s["files"]))["id"]

    def _label(self, sid, session):
        cwd = session.get("cwd") or ""
        name = Path(cwd).name.strip() or Path(cwd).drive or sid[:8]
        try:
            stamp = time.strftime("%m-%d", time.localtime(max(p.stat().st_mtime for p in session["files"])))
        except Exception:
            stamp = "?"
        return "%s · %s" % (name[:14], stamp)

    # ============================================================ 数据加载
    def _session_signature(self, sid):
        try:
            return tuple(sorted((str(p), p.stat().st_mtime, p.stat().st_size) for p in self.sessions[sid]["files"]))
        except Exception:
            return None

    def load_session(self, sid):
        self.current_sid = sid
        self.turns = nc.load_turns(self.sessions[sid]["files"])
        self._signature = self._session_signature(sid)
        self._hay_cache = None
        self._render_key = None
        self.selected = None
        self.render(force=True)

    def refresh(self, force=False):
        self.sessions = nc.collect()
        if not self.sessions:
            return
        if force or not self.current_sid or self.follow or self.current_sid not in self.sessions:
            self.load_session(self._follow_target())
        self.update_combo()

    def poll(self):
        try:
            self.sessions = nc.collect()
            if self.current_sid in self.sessions and self._session_signature(self.current_sid) != self._signature:
                self._hay_cache = None
                self.turns = nc.load_turns(self.sessions[self.current_sid]["files"])
                self._signature = self._session_signature(self.current_sid)
                self.render()
            if self.follow:
                target = self._follow_target()
                if target and target != self.current_sid:
                    self.load_session(target)
                    self.status.config(text="已跟随当前对话")
            self.update_combo()
        except Exception as exc:
            self.status.config(text="同步出错：%s" % str(exc)[:32])
        self.root.after(POLL_MS, self.poll)

    def update_combo(self):
        meta = self._meta_rows()
        ordered = sorted(self.sessions.items(),
                         key=lambda kv: (meta.get(kv[0], {}).get("recency") or 0,
                                         max(p.stat().st_mtime for p in kv[1]["files"])),
                         reverse=True)
        values, mapping = [], {}
        for sid, session in ordered:
            label = self._label(sid, session)
            if label in mapping:
                label = "%s·%s" % (label, sid[:4])
            while label in mapping:
                label += "·"
            mapping[label] = sid
            values.append(label)

        if values == self._combo_values and self.current_sid == self._combo_sid:
            return                                  # 内容没变就别重建下拉（几百个会话时很贵）
        self._combo_values, self._combo_sid, self._label_map = values, self.current_sid, mapping
        self.combo["values"] = values
        if self.current_sid in self.sessions:
            self.combo.set(self._label(self.current_sid, self.sessions[self.current_sid]))

    # ============================================================ 渲染
    def _preview_question(self, turn):
        text = turn.get("gist", "") or ""
        return text[:QUESTION_CHARS] + ("…" if len(text) > QUESTION_CHARS else "")

    def _answer_text(self, turn):
        """该轮助手最终回复的纯文本（只算一次，缓存在 turn 里）。"""
        cached = turn.get("_answer_text")
        if cached is None:
            final = any_text = ""
            for item in turn.get("asst", []):
                if (item.get("text") or "").strip():
                    any_text = item["text"]
                    if item.get("phase") != "commentary":
                        final = item["text"]
            cached = self._plain(final or any_text)
            turn["_answer_text"] = cached
        return cached

    def _preview_answer(self, turn):
        text = self._answer_text(turn)
        return text[:ANSWER_CHARS] + ("…" if len(text) > ANSWER_CHARS else "")

    def render(self, force=False):
        query = self._search_query()
        key = (len(self.turns),
               tuple(self._preview_question(t) for t in self.turns),
               tuple(self._preview_answer(t) for t in self.turns),
               tuple(sorted(self.marks.items())))
        if not force and key == self._render_key:
            self._update_status()
            return
        self._render_key = key

        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.visible = []
        line_of = {}
        for index, turn in enumerate(self.turns):
            blob = (turn.get("gist", "") + " " + (turn.get("user") or "")).lower()
            if query and query not in blob:
                continue
            self.visible.append(index)
            row = index + 1
            # 四个标签：turn=点击 / bg=底色 / head=标题放大 / q=提问放大
            turn_tag, bg_tag = "turn%d" % row, "bg%d" % row
            head_tag, q_tag = "head%d" % row, "q%d" % row
            line_of[index] = int(self.text.index("end-1c").split(".")[0])
            if index in self.marks:
                self.text.tag_config(bg_tag, background=self.marks[index])
            self.text.insert("end", "#%-3d" % row, ("num", turn_tag, bg_tag, head_tag))
            self.text.insert("end", "%s\n" % nc.to_local(turn.get("ts", "")), ("time", turn_tag, bg_tag, head_tag))
            self.text.insert("end", self._preview_question(turn) + "\n", ("question", turn_tag, bg_tag, q_tag))
            answer = self._preview_answer(turn)
            if answer:
                self.text.insert("end", answer + "\n", ("answer", turn_tag, bg_tag))
            self.text.tag_bind(turn_tag, "<Button-1>",
                               lambda _e, k=index: self._on_row_click(k, self._shift_down))
            self.text.tag_bind(bg_tag, "<Enter>", lambda _e, k=index: self._hover(k, True))
            self.text.tag_bind(bg_tag, "<Leave>", lambda _e, k=index: self._hover(k, False))
        self._line_of = line_of
        self.text.config(state="disabled")

        if not self.visible:
            self.text.config(state="normal")
            self.text.insert("end", "\n没有匹配的提问\n", ("empty",))
            self.text.config(state="disabled")
        self._update_status()

    def _update_status(self):
        query = self._search_query()
        suffix = " 筛%d" % len(self.visible) if query else ""
        self.status.config(text="%d 轮%s · %s" % (len(self.turns), suffix, time.strftime("%H:%M")))

    def _hover(self, index, entering):
        bg_tag, head_tag, q_tag = "bg%d" % (index + 1), "head%d" % (index + 1), "q%d" % (index + 1)
        try:
            if entering:
                self.text.tag_config(head_tag, font=F_HOVER_TITLE, spacing1=self.px(3))
                self.text.tag_config(q_tag, font=F_HOVER_BODY)
                self.text.tag_config(bg_tag, background=self.marks.get(index) or HOVER_BG)
            else:
                self.text.tag_config(head_tag, font="", spacing1="")
                self.text.tag_config(q_tag, font="")
                self.text.tag_config(bg_tag, background=self.marks.get(index, ""))
            for tag in (bg_tag, head_tag, q_tag):
                self.text.tag_raise(tag)
        except Exception:
            pass

    def _repaint_row(self, index):
        try:
            self.text.tag_config("bg%d" % (index + 1), background=self.marks.get(index, ""))
            self.text.tag_raise("bg%d" % (index + 1))
            self.text.tag_remove("sel", "1.0", "end")
        except Exception:
            pass

    # ============================================================ 交互
    def _on_row_click(self, index, shift):
        self.text.tag_remove("sel", "1.0", "end")
        if shift:
            self._drag_seen = {index}
            self._drag_erase = index in self.marks
            self._erase(index) if self._drag_erase else self._mark(index)
            self.status.config(text="已标记 %d 轮" % len(self.marks))
        else:
            self._shift_session = False
            self.selected = index
            self._start_jump(index)
        return "break"                       # 阻止 Text 的默认选区行为（否则 Shift 点击会留蓝底）

    def _mark(self, index):
        if index in self.marks:
            return
        if not self._shift_session:
            self._mark_color = (self._mark_color + 1) % len(MARK_COLORS)
            self._shift_session = True
        self.marks[index] = MARK_COLORS[self._mark_color]
        self._repaint_row(index)

    def _erase(self, index):
        self.marks.pop(index, None)
        self._repaint_row(index)

    def _on_pointer_drag(self, event):
        """按住 Shift 拖动 = 连续刷同一种颜色；从已标记的行起拖 = 连续擦除。"""
        if not self._shift_down:
            return
        index = self._row_at(event.x, event.y)
        if index is None or index in self._drag_seen:
            return
        self._drag_seen.add(index)
        self._erase(index) if self._drag_erase else self._mark(index)
        self.status.config(text="已标记 %d 轮" % len(self.marks))
        return "break"

    def _on_pointer_release(self, _event):
        if self._drag_seen:
            self.status.config(text="已标记 %d 轮" % len(self.marks))
        self._drag_seen = set()
        return "break"

    def _row_at(self, x, y):
        try:
            for name in self.text.tag_names("@%d,%d" % (x, y)):
                if name.startswith("turn"):
                    return int(name[4:]) - 1
        except Exception:
            pass
        return None

    def _watch_shift(self):
        down = wi.shift_pressed()
        if self._shift_down and not down:
            self._shift_session = False          # 松开 Shift -> 本次标记会话结束，下次换色
        self._shift_down = down
        self.root.after(150, self._watch_shift)

    def _on_follow_toggle(self):
        self.follow = self.follow_var.get()

    def _on_pick(self, _event):
        sid = self._label_map.get(self.combo.get())
        if sid and sid in self.sessions:
            self.follow = False                  # 手动选过就固定，不再自动跟随
            self.follow_var.set(False)
            self.load_session(sid)

    # ============================================================ 翻页
    def _page(self, delta):
        if not self.turns:
            return
        current = self.selected if self.selected is not None else -1
        target = max(0, min(len(self.turns) - 1, current + delta))
        if target == current:
            self.status.config(text="已到边界")
            return
        self._go_to(target)

    def _go_to(self, index):
        if not self.turns or not 0 <= index < len(self.turns):
            return
        self.selected = index
        self.render()
        line = getattr(self, "_line_of", {}).get(index)
        if line is not None:
            try:
                self.text.see("%d.0" % line)
            except Exception:
                pass
        self._start_jump(index)

    def _press_page_button(self, delta):
        self._press_delta, self._press_long = delta, False
        self._press_job = self.root.after(LONG_PRESS_MS, self._long_press)

    def _long_press(self):
        self._press_long, self._press_job = True, None
        if not self.turns:
            return
        if self._press_delta < 0:
            self._go_to(0)
            self.status.config(text="⇧ 已到最上面 (#1)")
        else:
            self._go_to(len(self.turns) - 1)
            self.status.config(text="⇩ 已到最下面 (#%d)" % len(self.turns))

    def _release_page_button(self):
        if self._press_job:
            try:
                self.root.after_cancel(self._press_job)
            except Exception:
                pass
            self._press_job = None
        if not self._press_long:
            self._page(self._press_delta)

    # ============================================================ 跳转 Codex
    def _plain(self, text):
        text = re.sub(r"[*`#>|~]", "", text or "")
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
        return re.sub(r"\s+", " ", text).strip()

    def _haystack(self):
        """判重用的文本：每轮只取前 160 字，避免把几 MB 正文拼成一个大字符串。"""
        if self._hay_cache is None:
            parts = []
            for turn in self.turns:
                parts.append(self._plain(turn.get("user"))[:160] + " " + self._answer_text(turn)[:160])
            self._hay_cache = " ".join(parts)
        return self._hay_cache

    def _search_key(self, index):
        """选一个「在整段对话里唯一」的片段给 Codex 的 Ctrl+F 用。

        优先级：提问原文 -> 该轮回复。长度从 15 字起、每次加 5 字，
        直到该片段在全文里只出现一次为止。
        """
        haystack = self._haystack()
        turn = self.turns[index]
        candidates = []
        question = self._plain(turn.get("user"))
        if question:
            candidates.append(question)
        answer = self._answer_text(turn)
        if answer:
            candidates.append(answer)
        for base in candidates:
            if len(base) < 4:
                continue
            for length in list(range(15, 91, 5)) + [len(base)]:
                key = base[:min(length, len(base))]
                if len(key) >= 4 and haystack.count(key) == 1:
                    return key
        return None

    def _start_jump(self, index):
        if self._busy:
            return                                # 上一条还在发，避免按键串行打乱
        self._busy = True
        threading.Thread(target=self._jump_worker, args=(index,), daemon=True).start()

    def _jump_worker(self, index):
        try:
            self._jump(index)
        finally:
            self._busy = False

    def _jump(self, index):
        """让 Codex 自己滚到第 index 轮：靠它自带的 Ctrl+F 查找定位。"""
        self._last_jump = time.time()
        hwnd = wi.find_codex_window()
        if not hwnd:
            self._status("没找到 Codex 窗口")
            return
        key = self._search_key(index)
        if not key:
            self._status("#%d 无法唯一定位" % (index + 1))
            return
        if not wi.activate(hwnd):
            self._status("无法激活 Codex 窗口")
            return
        time.sleep(0.12)
        if not wi.ctrl(wi.VK_F):
            self._status("按键被系统拦截")
            return
        time.sleep(0.28)
        sent = wi.type_text(key)
        time.sleep(0.28)
        wi.tap(wi.VK_RETURN)
        time.sleep(0.12)
        if wi.foreground() == hwnd:                # 只有 Codex 仍在前台才发 Esc，否则会关掉本面板
            wi.tap(wi.VK_ESC)
        if sent < len(key):
            self._status("部分输入失败 (%d/%d)" % (sent, len(key)))
        else:
            self._status("↗ 已跳转 #%d" % (index + 1))

    def _status(self, text):
        """线程安全地更新状态栏（Tk 控件只能在主线程碰）。"""
        self._status_queue.put(text)

    def _drain_status(self):
        try:
            while True:
                self.status.config(text=self._status_queue.get_nowait())
        except Exception:
            pass
        self.root.after(120, self._drain_status)


def main():
    root = tk.Tk()
    root.title("Codex 导航")
    try:
        root.option_add("*TCombobox*Listbox.background", BG)
        root.option_add("*TCombobox*Listbox.foreground", FG)
        root.option_add("*TCombobox*Listbox.selectBackground", "#eaf0ff")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=ALT, background=ALT, foreground=FG,
                        arrowcolor=MUTED, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                        borderwidth=1, padding=2)
    except Exception:
        pass

    app = FloatNav(root)

    def on_escape(_event):
        # 跳转期间、以及刚跳完 2 秒内的 Esc 是注入给 Codex 的，不能关掉本窗口
        if app._busy or time.time() - app._last_jump < 2.0:
            return "break"
        root.destroy()

    root.bind("<Escape>", on_escape)
    root.mainloop()


if __name__ == "__main__":
    main()

