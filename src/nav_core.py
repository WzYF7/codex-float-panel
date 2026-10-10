#!/usr/bin/env python3
# Codex 会话导航：核心解析模块
import json, os, re
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
CODEX = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
SESSIONS = CODEX / "sessions"
ARCHIVED = CODEX / "archived_sessions"

BLOCKS = [
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
BLOCK_RE = [re.compile(p, re.S | re.I) for p in BLOCKS]

def clean(text):
    if not text:
        return ""
    t = text
    for r in BLOCK_RE:
        t = r.sub(" ", t)
    m = re.search(r"^#{1,6}\s*My request:\s*$", t, re.M)
    if m:
        t = t[m.end():]
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def gist(text, limit=64):
    t = re.sub(r"\s+", " ", (text or "")).strip()
    t = re.sub(r"^[#*\->\s`]+", "", t)
    t = re.sub(r"^\d+[\.\)、]\s*", "", t)
    if not t:
        return "(空)"
    return t[:limit] + ("…" if len(t) > limit else "")

def lead(text, limit=88):
    t = re.sub(r"\s+", " ", (text or "")).strip()
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

def first_json(path):
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

_HEAD_CACHE = {}


def collect():
    """扫描所有会话文件，按 session_id 归组。首行解析结果带缓存，轮询时只做 stat。"""
    files = []
    if SESSIONS.exists():
        files += list(SESSIONS.rglob("rollout-*.jsonl"))
    if ARCHIVED.exists():
        files += list(ARCHIVED.rglob("*.jsonl"))
    out = {}
    for p in sorted(set(files)):
        try:
            st = p.stat()
        except Exception:
            continue
        key = str(p)
        hit = _HEAD_CACHE.get(key)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
            head = hit[2]
        else:
            head = first_json(p)
            _HEAD_CACHE[key] = (st.st_size, st.st_mtime, head)
        if not head:
            continue
        pl = head.get("payload") or {}
        sid = pl.get("session_id") or pl.get("id")
        if not sid:
            m = re.match(r"rollout-\d{4}-\d{2}-\d{2}T[\d\-]+-([0-9a-fA-F\-]{36})", p.name)
            sid = m.group(1) if m else p.stem
        it = out.setdefault(sid, {"id": sid, "cwd": "", "created": pl.get("timestamp", ""), "files": []})
        it["files"].append(p)
        if not it["cwd"] and pl.get("cwd"):
            it["cwd"] = pl["cwd"]
    return out


_FILE_CACHE = {}
_FILE_CACHE_MAX = 40


def _file_msgs(p):
    """解析单个 rollout 文件；文件没变就复用上次结果（缓存）。"""
    try:
        st = p.stat()
    except Exception:
        return []
    key = str(p)
    hit = _FILE_CACHE.get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
        return hit[2]
    msgs = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # 快速预筛：工具调用/思考/计费这些行占 90% 以上，直接跳过不解析
                if '"role":"user"' not in line and '"role":"assistant"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "response_item":
                    continue
                pl = o.get("payload") or {}
                if pl.get("type") != "message":
                    continue
                role = pl.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = "".join(c.get("text", "") for c in (pl.get("content") or []) if isinstance(c, dict))
                if is_junk(text):
                    continue
                meta = pl.get("internal_chat_message_metadata_passthrough") or {}
                msgs.append({
                    "ts": str(o.get("timestamp", "")),
                    "role": role,
                    "text": text,
                    "tid": meta.get("turn_id") or "",
                    "phase": pl.get("phase") or "",
                    "ord": o.get("ordinal") if isinstance(o.get("ordinal"), int) else 0,
                })
    except Exception:
        pass
    _FILE_CACHE[key] = (st.st_size, st.st_mtime, msgs)
    if len(_FILE_CACHE) > _FILE_CACHE_MAX:
        for k in list(_FILE_CACHE)[:10]:
            _FILE_CACHE.pop(k, None)
    return msgs


def _msgs(files):
    out = []
    for p in files:
        out.extend(_file_msgs(p))
    out.sort(key=lambda m: (m["ts"], 0 if m["role"] == "user" else 1, m["ord"]))
    return out


def load_turns(files):
    turns, last_by_tid, cur = [], {}, None
    for m in _msgs(files):
        if m["role"] == "user":
            body = clean(m["text"])
            if not body:
                continue
            key = m["tid"] or ""
            prev = last_by_tid.get(key) if key else None
            if prev is not None and clean(prev["raw"]) == body:
                cur = prev
                continue
            t = {"ts": m["ts"], "raw": m["text"], "asst": [], "tid": key}
            turns.append(t)
            if key:
                last_by_tid[key] = t
            cur = t
        else:
            t = last_by_tid.get(m["tid"]) if m["tid"] else None
            if t is None:
                t = cur
            if t is not None:
                t["asst"].append({"text": m["text"], "phase": m["phase"]})
    for t in turns:
        t["user"] = clean(t["raw"])
        t["gist"] = gist(t["user"])
        final = ""
        for a in t["asst"]:
            if a["text"].strip() and a.get("phase") != "commentary":
                final = a["text"]
        if not final:
            for a in t["asst"]:
                if a["text"].strip():
                    final = a["text"]
        t["lead"] = lead(clean(final) if final else "")
    turns = [t for t in turns if t["user"] or t["asst"]]
    out = []
    for t in turns:
        b = (t.get("user") or "").strip()
        if out:
            a = (out[-1].get("user") or "").strip()
            if a and b and len(a) >= 8 and (b.startswith(a) or a.startswith(b)) and not b.startswith("The following"):
                if len(b) > len(a):
                    out[-1] = t
                continue
        out.append(t)
    return out
