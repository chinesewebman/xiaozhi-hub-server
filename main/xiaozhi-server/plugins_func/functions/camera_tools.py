"""相机工具：拍一张照，用视觉模型看图回答。

设备端的 S5KC505A 摄像头已经能驱动（chip_id=0x5034），但完整的
CSI/ISP 媒体管线还没打通，所以**取帧这一层做成可插拔**：

  1. 如果 /run/thinksmart/camera/frame.jpg 存在，就用它当这一帧；
  2. 否则尝试用 v4l2-ctl 从 /dev/video* 抓一帧 MJPEG；
  3. 都没有就明确告诉模型"相机暂时拍不出照片"，而不是假装看过。

拍到的图走**设备自己的** /mcp/vision/explain（hub 的视觉入口），
视觉模型是 Mac 上的本地 oMLX Qwen3.8（本地推理，不出家门）。
"""

import os
import subprocess
import time
from typing import TYPE_CHECKING, Optional

import httpx
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

# hub 的视觉入口（同一个进程，127.0.0.1）
VISION_URL = os.environ.get("VISION_URL", "http://127.0.0.1:16688/mcp/vision/explain")
# 设备自己的 token（client 用的是 test-token）
CLIENT_ID = os.environ.get("VISION_CLIENT_ID", "web_test_client")
DEVICE_ID = os.environ.get("VISION_DEVICE_ID", "test_device")
TOKEN = os.environ.get("VISION_TOKEN", "test-token")

# 取帧的落点：真相机通了以后，写到这里就行
FRAME_DIR = os.environ.get("CAMERA_FRAME_DIR", "/run/thinksmart/camera")
FRAME_PATH = os.path.join(FRAME_DIR, "frame.jpg")

# 视觉模型是本地 27B，看图要几秒，给足超时
TIMEOUT = httpx.Timeout(60.0, connect=5.0)
CAPTURE_TIMEOUT = 12


def _fail(what: str, exc: Exception) -> ActionResponse:
    return ActionResponse(
        Action.REQLLM,
        f"{what}失败：{exc}。请简短告诉用户这次没成功，稍后再试。",
        None,
    )


def _probe_video_devices() -> list:
    """列出能抓 MJPEG 的 video 设备。"""
    try:
        out = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    devs = []
    cur = None
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")):
            cur = line.strip()
        elif line.strip().startswith("/dev/video"):
            devs.append((cur or "", line.strip()))
    return devs


def _capture_from_v4l2() -> Optional[bytes]:
    """从 v4l2 抓一帧 JPEG。通了之后这就是真正的相机路径。"""
    for name, dev in _probe_video_devices():
        if "camss" not in name.lower() and "s5kc" not in name.lower():
            # 优先挑摄像头相关的设备，但设备名不认识时也试第一个
            pass
        try:
            r = subprocess.run(
                [
                    "v4l2-ctl",
                    "-d",
                    dev,
                    "--set-fmt-video=width=640,height=480,pixelformat=MJPEG",
                    "--stream-mmap=3",
                    "--stream-count=1",
                    "--stream-to=/dev/stdout",
                ],
                capture_output=True,
                timeout=CAPTURE_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            continue
        if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8":
            return r.stdout
    return None


def _load_frame() -> Optional[bytes]:
    """拿一帧图：先看落点文件，再试 v4l2。"""
    try:
        if os.path.exists(FRAME_PATH):
            with open(FRAME_PATH, "rb") as f:
                data = f.read()
            if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
    except OSError:
        pass
    return _capture_from_v4l2()


def _vision(question: str, image: bytes) -> str:
    """把图交给 hub 的视觉入口，返回模型的描述。"""
    files = {"image": ("frame.jpg", image, "image/jpeg")}
    data = {"question": question}
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Client-Id": CLIENT_ID,
        "Device-Id": DEVICE_ID,
    }
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(VISION_URL, headers=headers, data=data, files=files)
        r.raise_for_status()
        obj = r.json()
    if not obj.get("success"):
        raise RuntimeError(obj.get("message") or "视觉接口返回失败")
    return str(obj.get("response") or "").strip()


TAKE_PHOTO_DESC = {
    "type": "function",
    "function": {
        "name": "take_photo",
        "description": (
            "用摄像头拍一张照片并看图回答。【重要】用户问“你看看这是什么”"
            "“现在房间里有什么”“帮我看一下窗外”“拍张照”这类需要眼睛的问题时，"
            "必须调用本工具真正拍照，不要凭想象回答、不要用 direct_answer 回话。"
            "question 写清楚要看什么（比如“左边是什么”“桌上有什么”）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问这张图什么，如“房间里有什么”“窗外是什么”",
                },
            },
            "required": ["question"],
        },
    },
}


@register_function("take_photo", TAKE_PHOTO_DESC, ToolType.SYSTEM_CTL)
async def take_photo(
    conn: "ConnectionHandler",
    question: str = None,
) -> ActionResponse:
    """拍一张照，用视觉模型看图回答。"""
    question = (question or "这张图里有什么？").strip()
    try:
        image = _load_frame()
    except Exception as e:  # noqa: BLE001
        return _fail("拍照", e)

    if not image:
        return ActionResponse(
            Action.REQLLM,
            "相机现在拍不出照片（图像传感器已识别，但取帧链路还没通）。"
            "请直接告诉用户暂时看不到画面，不要描述任何你猜测的内容。",
            None,
        )

    try:
        answer = _vision(question, image)
    except Exception as e:  # noqa: BLE001
        return _fail("看图", e)

    if not answer:
        return ActionResponse(
            Action.REQLLM,
            "拍到了一张照片，但视觉模型没有返回描述。请简短告诉用户没看清。",
            None,
        )

    return ActionResponse(
        Action.REQLLM,
        f"拍照并看过图了。视觉模型的回答：{answer}",
        None,
    )
