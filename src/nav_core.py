"""Codex 会话解析：把 ~/.codex/sessions 里的 rollout JSONL 解析成"按轮次"的结构。

对外只暴露三个东西：
    collect()          -> {session_id: {id, cwd, files}}
    load_turns(files)  -> [turn, ...]   每个 turn 含 user / asst / gist / lead
    to_local(ts)       -> "10-10 12:34"

设计要点：
  · 文件级缓存：rollout 文件一旦解析过、且大小/修改时间没变，就直接复用结果
  · 按需解析：先用字符串预筛 role，跳过 90% 以上的工具/思考/计费行，避免无谓的 json.loads
  · 噪音过滤：Codex 会把"自动审查回放""审查结论 JSON"也写进会话，必须滤掉
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
SESSIONS = CODEX_HOME / "sessions"
ARCHIVED = CODEX_HOME / "archived_sessions"

# Codex 注入到用户消息里的环境块，展示时要剥掉
_NOISE_BLOCKS = [
    r"<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>",
    r"<environment_context\b[^>]*>.*?</environment_context>",
    r"<codex_apps_client_time_context\b[^>]*>.*?</codex_apps_client_time_context>",
    r"<app-context\b[^>]*>.*?</app-context>",
    r"<skills_instructions\b[^>]*>.*?</skills_instructions>",
    r"<permissions[^>]*>.*?</permissions[^>]*>",
    r"<image\b[^>]*>.*?</image>",
    r"<turn_aborted\b[^>]*>.*?</turn_aborted>",
    r"<turn_aborted\b[^>]*/?>",
]
_NOISE_RE = [re.compile(p, re.S | re.I) for p in _NOISE_BLOCKS]

_FILE_CACHE = {}      # rollout 文件 -> (size, mtime, 消息列表)
_HEAD_CACHE = {}      # rollout 文件首行 -> (size, mtime, 解析结果)


# ---------------------------------------------------------------- 文本处理

def clean(text):
    """剥掉注入的环境块；若消息里有 `## My request:`，只保留它之后的内容。"""
    if not text:
        return ""
    out = text
    for rx in _NOISE_RE:
        out = rx.sub(" ", out)
    marker = re.search(r"^#{1,6}\s*My request:\s*$", out, re.M)
    if marker:
        out = out[marker.end():]
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def gist(text, limit=64):
    """列表里显示的一行摘要（取开头，去掉 markdown 记号）。"""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"^[#*\->\s`]+", "", t)
    t = re.sub(r"^\d+[\.\)、]\s*", "", t)
    if not t:
        return "(空)"
    return t[:limit] + ("…" if len(t) > limit else "")


def lead(text, limit=88):
    """助手回复的首句（用于列表第二行）。"""
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return ""
    m = re.match(r"(.{8,%d}?[。！？.!?；;])" % limit, t)
    return (m.group(1) if m else t[:limit]).strip()


def to_local(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return ""


def is_junk(text):
    """Codex 会把自动审查的输入/输出写进会话，这些不是真实对话。"""
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("The following is the Codex agent history"):
        return True
    if re.search(r"\[\d{1,4}\]\s*assistant\s*:", t[:300], re.I):
        return True
    if t[:1] in "{[" and re.search(r'"(outcome|risk_level|user_authorization|approvals_reviewer)"\s*:', t[:1800]):
        return True
    if "<turn_aborted" in t[:80]:
        return True
    return False


# ---------------------------------------------------------------- 会话扫描

def _first_json(path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except Exception:
                    continue
    except Exception:
        pass
    return None


def collect():
    """扫描所有 rollout 文件，按 session_id 归组。首行解析带缓存，轮询时基本只做 stat。"""
    files = []
    if SESSIONS.exists():
        files += list(SESSIONS.rglob("rollout-*.jsonl"))
    if ARCHIVED.exists():
        files += list(ARCHIVED.rglob("*.jsonl"))

    sessions = {}
    for path in sorted(set(files)):
        try:
            st = path.stat()
        except Exception:
            continue
        key = str(path)
        cached = _HEAD_CACHE.get(key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
            head = cached[2]
        else:
            head = _first_json(path)
            _HEAD_CACHE[key] = (st.st_size, st.st_mtime, head)
        if not head:
            continue

        payload = head.get("payload") or {}
        sid = payload.get("session_id") or payload.get("id")
        if not sid:
            m = re.match(r"rollout-\d{4}-\d{2}-\d{2}T[\d\-]+-([0-9a-fA-F\-]{36})", path.name)
            sid = m.group(1) if m else path.stem

        item = sessions.setdefault(sid, {"id": sid, "cwd": "", "created": payload.get("timestamp", ""), "files": []})
        item["files"].append(path)
        if not item["cwd"] and payload.get("cwd"):
            item["cwd"] = payload["cwd"]
    return sessions


# ---------------------------------------------------------------- 消息解析

def _parse_file(path):
    """解析一个 rollout 文件里的 user/assistant 消息（带缓存）。"""
    try:
        st = path.stat()
    except Exception:
        return []
    key = str(path)
    cached = _FILE_CACHE.get(key)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
        return cached[2]

    messages = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # 预筛：只留可能含消息的行，其余（工具调用/思考/计费）不解析
                if '"role":"user"' not in line and '"role":"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload") or {}
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = "".join(c.get("text", "") for c in (payload.get("content") or []) if isinstance(c, dict))
                if is_junk(text):
                    continue
                meta = payload.get("internal_chat_message_metadata_passthrough") or {}
                messages.append({
                    "ts": str(obj.get("timestamp", "")),
                    "role": role,
                    "text": text,
                    "tid": meta.get("turn_id") or "",
                    "phase": payload.get("phase") or "",
                    "ord": obj.get("ordinal") if isinstance(obj.get("ordinal"), int) else 0,
                })
    except Exception:
        pass

    _FILE_CACHE[key] = (st.st_size, st.st_mtime, messages)
    if len(_FILE_CACHE) > 40:              # 简单淘汰，避免长期占用内存
        for k in list(_FILE_CACHE)[:10]:
            _FILE_CACHE.pop(k, None)
    return messages


def _all_messages(files):
    messages = []
    for path in files:
        messages.extend(_parse_file(path))
    messages.sort(key=lambda m: (m["ts"], 0 if m["role"] == "user" else 1, m["ord"]))
    return messages


def _merge_similar(turns):
    """同一 turn_id 下 Codex 会把消息重发一次，去掉这种前缀重复。"""
    out = []
    for turn in turns:
        cur = (turn.get("user") or "").strip()
        if out:
            prev = (out[-1].get("user") or "").strip()
            if prev and cur and len(prev) >= 8 and (cur.startswith(prev) or prev.startswith(cur)):
                if len(cur) > len(prev):
                    out[-1] = turn
                continue
        out.append(turn)
    return out


def load_turns(files):
    """把若干分片合并成按时间顺序的轮次列表。"""
    turns, last_by_tid, current = [], {}, None

    for msg in _all_messages(files):
        if msg["role"] == "user":
            body = clean(msg["text"])
            if not body:
                continue
            tid = msg["tid"]
            prev = last_by_tid.get(tid) if tid else None
            if prev is not None and clean(prev["raw"]) == body:
                current = prev
                continue
            turn = {"ts": msg["ts"], "raw": msg["text"], "asst": [], "tid": tid}
            turns.append(turn)
            if tid:
                last_by_tid[tid] = turn
            current = turn
        else:
            turn = last_by_tid.get(msg["tid"]) if msg["tid"] else None
            if turn is None:
                turn = current
            if turn is not None:
                turn["asst"].append({"text": msg["text"], "phase": msg["phase"]})

    for turn in turns:
        turn["user"] = clean(turn["raw"])
        turn["gist"] = gist(turn["user"])
        final = ""
        for a in turn["asst"]:
            if a["text"].strip() and a.get("phase") != "commentary":
                final = a["text"]
        if not final:
            for a in turn["asst"]:
                if a["text"].strip():
                    final = a["text"]
        turn["lead"] = lead(clean(final) if final else "")

    return _merge_similar([t for t in turns if t["user"] or t["asst"]])
