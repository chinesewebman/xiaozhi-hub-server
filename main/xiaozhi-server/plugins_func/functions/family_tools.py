"""家庭工具：定时提醒 + 家庭账本。

设备端只做转发 —— 数据、账本、提醒状态都在 Mac Studio 上的家庭中枢（hub）里，
语音模型也在那边。hub 的接口要设备凭据（X-Client-Id + X-Token），
token 放在 ~/.family_hub_token（权限 600，不进仓库）。

另外起一个后台线程轮询 hub 的 /timers：到点的提醒用小智自己的 TTS 在设备上播报
（家庭成员在客厅听设备说话，而不是听另一台机器）。

提醒文案：
    "妈妈妈妈，5 分钟到了，您该关火啦"
"""

import json
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING, Optional

import httpx
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

HUB_URL = os.environ.get("FAMILY_HUB_URL", "http://192.168.3.123:8765").rstrip("/")
TOKEN_FILE = os.path.expanduser("~/.family_hub_token")
SEEN_FILE = os.path.expanduser("~/.family_reminders_announced.json")
CLIENT_ID = "pad-living"
# 设备自己的本地 API（客户端的 web 服务），只监听 127.0.0.1，插件在设备上直连
CLIENT_API = os.environ.get("THINKSMART_API", "http://127.0.0.1:8090")
POLL_SEC = 4.0
TIMEOUT = httpx.Timeout(8.0, connect=4.0)


# --------------------------------------------------------------------------
# 跟 hub 说话
# --------------------------------------------------------------------------
def _token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _headers() -> dict:
    return {"X-Client-Id": CLIENT_ID, "X-Token": _token()}


def _hub_post(path: str, payload: dict) -> dict:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(HUB_URL + path, headers=_headers(), json=payload)
        r.raise_for_status()
        return r.json()


def _hub_get(path: str):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(HUB_URL + path, headers=_headers())
        r.raise_for_status()
        return r.json()


def _fail(what: str, exc: Exception) -> ActionResponse:
    return ActionResponse(
        Action.REQLLM,
        f"{what}失败：{exc}。请简短告诉用户这次没成功，稍后再试。",
        None,
    )


# --------------------------------------------------------------------------
# 工具描述
# --------------------------------------------------------------------------
SET_REMINDER_DESC = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": (
            "设置一个定时提醒（倒计时）。【重要】用户要求设提醒/倒计时时，"
            "必须调用本工具真正创建提醒，绝对不能只用嘴答应、也不能用 direct_answer 回话。"
            "用户说“5分钟后提醒我关火”“半小时后叫我起来活动”"
            "“晚上八点提醒我吃药”时调用。seconds 是从现在起多少秒后提醒。"
            "谁说的就填谁（不知道就留空）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "要提醒的事，如“关火”"},
                "seconds": {"type": "integer", "description": "多少秒后提醒"},
                "who": {"type": "string", "description": "谁设的提醒（称呼），不知道就留空"},
            },
            "required": ["task", "seconds"],
        },
    },
}

LIST_REMINDERS_DESC = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": ("查询当前所有还没到点的提醒。【重要】用户问起提醒时必须调用本工具查询，"
            "不要凭印象回答、不要用 direct_answer 回话。"
            "用户问“我有什么提醒”“还有哪些定时”的时候调用。"),
        "parameters": {"type": "object", "properties": {}},
    },
}

RECORD_EVENT_DESC = {
    "type": "function",
    "function": {
        "name": "record_family_event",
        "description": (
            "把一件家庭活动记进家庭账本。【重要】用户要求记一笔时必须调用本工具，"
            "不要只答应一声。用户说“记一下我出门买菜了”“记一下爷爷在客厅看电视”"
            "这类事后补记时调用。summary 用一句话写清楚谁在做什么。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "一句话：谁在做什么"},
            },
            "required": ["summary"],
        },
    },
}

TODAY_DESC = {
    "type": "function",
    "function": {
        "name": "query_family_today",
        "description": (
            "查今天家里发生过什么（家庭账本时间线）。【重要】用户问起家里的事时必须调用本工具查，"
            "不要凭印象回答、不要用 direct_answer 回话。用户问“今天家里有什么事”"
            "“我今天都干了啥”“今天谁在家”的时候调用。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------
@register_function("set_reminder", SET_REMINDER_DESC, ToolType.SYSTEM_CTL)
async def set_reminder(
    conn: "ConnectionHandler",
    task: str = None,
    seconds: int = None,
    who: str = "",
) -> ActionResponse:
    """设一个定时提醒。"""
    if not task or seconds is None:
        return _fail("设置提醒", ValueError("缺少 task 或 seconds"))
    try:
        data = _hub_post(
            "/timers",
            {"task": str(task).strip(), "seconds": int(seconds), "who": str(who or "").strip()},
        )
    except Exception as e:  # noqa: BLE001
        return _fail("设置提醒", e)
    mins = int(seconds) // 60
    when = f"{mins} 分钟后" if mins >= 1 else f"{int(seconds)} 秒后"
    return ActionResponse(
        Action.REQLLM,
        f"已设置提醒：{data.get('who') or ''}{when}提醒“{data.get('task')}”（id={data.get('id')}）。"
        f"请用自然口语告诉用户已记好，不要念 id。",
        None,
    )


@register_function("list_reminders", LIST_REMINDERS_DESC, ToolType.SYSTEM_CTL)
async def list_reminders(conn: "ConnectionHandler") -> ActionResponse:
    """列出还没到点的提醒。"""
    try:
        data = _hub_get("/timers")
    except Exception as e:  # noqa: BLE001
        return _fail("查询提醒", e)
    timers = [t for t in (data.get("timers") or []) if t.get("state") == "pending"]
    if not timers:
        return ActionResponse(Action.REQLLM, "当前没有待触发的提醒。", None)
    import time as _t

    now = _t.time()
    items = []
    for t in timers:
        left = int(max(0, t.get("due_at", now) - now))
        mins = left // 60
        items.append(
            f"{(t.get('who') or '')}设的“{t.get('task')}”，还有{mins if mins >= 1 else left}"
            f"{'分钟' if mins >= 1 else '秒'}"
        )
    return ActionResponse(Action.REQLLM, "待触发提醒：" + "；".join(items) + "。", None)


@register_function("record_family_event", RECORD_EVENT_DESC, ToolType.SYSTEM_CTL)
async def record_family_event(
    conn: "ConnectionHandler", summary: str = None
) -> ActionResponse:
    """往家庭账本里记一笔。"""
    if not summary:
        return _fail("记录", ValueError("缺少 summary"))
    try:
        data = _hub_post("/ledger", {"summary": str(summary).strip(), "source": "语音"})
    except Exception as e:  # noqa: BLE001
        return _fail("记录", e)
    ev = data.get("event") or {}
    return ActionResponse(
        Action.REQLLM,
        f"已记入家庭账本：{ev.get('start_time', '')} {ev.get('summary', summary)}。",
        None,
    )


@register_function("query_family_today", TODAY_DESC, ToolType.SYSTEM_CTL)
async def query_family_today(conn: "ConnectionHandler") -> ActionResponse:
    """查今天的家庭时间线。"""
    try:
        events = _hub_get("/ledger.json")
    except Exception as e:  # noqa: BLE001
        return _fail("查询账本", e)
    if not events:
        return ActionResponse(Action.REQLLM, "今天的家庭账本还是空的。", None)
    lines = []
    for ev in events[-20:]:
        lines.append(f"{ev.get('start_time', '')} {ev.get('summary', '')}")
    return ActionResponse(Action.REQLLM, "今天的家庭时间线：" + "；".join(lines) + "。", None)


# --------------------------------------------------------------------------
# 到点播报：后台线程轮询 hub，到点的提醒让设备自己说
# --------------------------------------------------------------------------
_seen: set = set()
_seen_lock = threading.Lock()


def _load_seen() -> None:
    global _seen
    try:
        with open(SEEN_FILE) as f:
            _seen = set(json.load(f))
    except Exception:  # noqa: BLE001
        _seen = set()


def _save_seen() -> None:
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(sorted(_seen), f)
        os.chmod(SEEN_FILE, 0o600)
    except Exception:  # noqa: BLE001
        pass


def _deliver(text: str) -> bool:
    """把提醒送到设备。

    走客户端自己的本地 API（/api/text）：它会 ensure_connected()，
    所以设备此刻是空闲、没连着网关也没关系 —— 客户端会自己连上，
    把这段话当成一句话送进网关，助手会把它说出来。
    """
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.post(CLIENT_API + "/api/text", json={"text": text})
            return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _reminder_text(t: dict) -> str:
    """提醒话术：写成让助手转述的样子，助手会自然地说出来。"""
    who = (t.get("who") or "").strip()
    task = (t.get("task") or "").strip()
    target = who if who else "家人"
    return f"这是系统提醒，请立刻对{target}说：{task}的时间到了，现在就去吧。"


def _ticker() -> None:
    _load_seen()
    while True:
        time.sleep(POLL_SEC)
        try:
            data = _hub_get("/timers")
        except Exception:  # noqa: BLE001
            continue
        now = time.time()
        for t in data.get("timers") or []:
            tid = t.get("id")
            due = float(t.get("due_at") or 0)
            if not tid or due <= 0 or due > now + 1:
                continue
            with _seen_lock:
                if tid in _seen:
                    continue
            # 送不到就不记，下一轮再试（设备没起来 / 网关没连上都能补上）
            if not _deliver(_reminder_text(t)):
                continue
            with _seen_lock:
                _seen.add(tid)
                _save_seen()


def _start_ticker() -> None:
    th = threading.Thread(target=_ticker, name="family-reminders", daemon=True)
    th.start()


try:
    _start_ticker()
except Exception:  # noqa: BLE001
    pass
