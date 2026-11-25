"""
感知工具：读「感知守护进程」的状态。

设备上的 thinksmart-perception.service 常驻持有摄像头，每 3 秒做一次变化检测；
一旦画面有明显变化就截一张图送去识别，把结果写在 state.json 里。

这个工具让助手不用真的拍照就能回答"现在房间里是什么情况""刚才是谁进来了"
这类问题 —— 守护进程已经看过了。

**不要和 take_photo 混用**：
  - look_around —— 读已有的观察结果，瞬时返回，不额外拍照
  - take_photo —— 真的拍一张现在 的照片并看图（要 6~20 秒）
"""

import json
import os
import time
from typing import TYPE_CHECKING, Optional

from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

_RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or ("/run/user/%d" % os.getuid())
STATE_JSON = os.environ.get(
    "PERCEPTION_STATE",
    os.path.join(_RUNTIME, "thinksmart", "vision", "state.json"),
)


def _read_state() -> Optional[dict]:
    try:
        with open(STATE_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _fmt_age(ts) -> str:
    try:
        d = int(time.time() - float(ts))
    except (TypeError, ValueError):
        return "未知"
    if d < 0:
        return "刚刚"
    if d < 60:
        return "%d 秒前" % d
    if d < 3600:
        return "%d 分钟前" % (d // 60)
    return "%d 小时前" % (d // 3600)


LOOK_AROUND_DESC = {
    "type": "function",
    "function": {
        "name": "look_around",
        "description": (
            "查看设备摄像头一直在观察到的当前情况（瞬时返回，不用等拍照）。"
            "适合问「现在房间里是什么情况」「刚才是谁进来了」「灯刚才是谁开的」"
            "「客厅现在有人吗」这类问题。"
            "如果用户想看**此刻**的画面细节（比如「看一下左边是什么」），"
            "改用 take_photo 真正拍一张。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


@register_function("look_around", LOOK_AROUND_DESC, ToolType.SYSTEM_CTL)
async def look_around(
    conn: "ConnectionHandler",
) -> ActionResponse:
    """读感知守护进程的最新观察结果。"""
    state = _read_state()
    if not state:
        return ActionResponse(
            Action.REQLLM,
            "摄像头守护进程还没提供观察结果（要么它没在跑，要么刚启动还没看到东西）。"
            "如果用户想看现在的画面，建议调用 take_photo 拍一张。",
            None,
        )

    parts = []
    if state.get("description"):
        parts.append("最近一次画面变化时的观察：%s" % state["description"])
    else:
        parts.append(
            "摄像头一直在看着，但最近没有检测到明显变化（画面基本没动）。"
        )

    faces = state.get("faces") or []
    if faces:
        names = []
        for fc in faces:
            if not isinstance(fc, dict):
                continue
            who = fc.get("name") or fc.get("identity") or "陌生人"
            score = fc.get("score")
            names.append(
                "%s%s" % (who, ("（相似度 %.2f）" % score) if isinstance(score, (int, float)) else "")
            )
        if names:
            parts.append("画面里的人：%s" % "、".join(names))
    elif state.get("faces") is not None and state.get("faces_checked"):
        parts.append("画面里没有人脸。")

    chg = state.get("changes")
    if chg:
        parts.append("今天一共检测到 %d 次画面变化。" % chg)

    last = state.get("last_change_at")
    if last:
        parts.append("最后一次变化是%s。" % _fmt_age(last))

    return ActionResponse(
        Action.REQLLM,
        "摄像头守护进程的观察结果：\n- %s\n\n"
        "请用自然口语转述给用户，不要复述技术细节（比如不要念字节数、帧率）。"
        % "\n- ".join(parts),
        None,
    )
