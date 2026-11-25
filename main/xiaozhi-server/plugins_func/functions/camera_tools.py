"""
相机工具：拍一张照，用视觉模型看图回答。

设备端的 S5KC505A 摄像头走 libcamera v0.8 软 ISP（CSI/CSI-2 -> CSID -> VFE ->
去马赛克 + CCM/LSC），能出真实的 XBGR8888 彩色帧，1280x960@30fps。

取帧路径（按优先级）：
  1. 落点文件 /run/thinksmart/camera/frame.jpg（外部程序可以往这里写现成的帧）；
  2. libcamera + lcam：连抓 20 帧让 AGC/AWB 收敛，取最后一帧，
     再用 ffmpeg 转成竖屏 JPEG（传感器横装 90 度，逆时针转 270 度才是正竖屏）；
  3. 都没有就明确告诉模型"相机暂时拍不出照片"，而不是假装看过。

拍到的图走**设备自己的** /mcp/vision/explain（hub 的视觉入口），
视觉模型是 Mac 上的本地 oMLX Qwen3.8（本地推理，不出家门）。
"""

import os
import subprocess
import sys
import tempfile
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

# 取帧的落点：外部程序可以往这里写现成的帧
FRAME_DIR = os.environ.get("CAMERA_FRAME_DIR", "/run/thinksmart/camera")
FRAME_PATH = os.path.join(FRAME_DIR, "frame.jpg")

# libcamera 软 ISP 的运行环境（设备上装在 /opt/libcamera）
LIBCAMERA_PREFIX = os.environ.get("LIBCAMERA_PREFIX", "/opt/libcamera")
SOFTISP_MODE = os.environ.get("LIBCAMERA_SOFTISP_MODE", "gpu")
LCAM = os.environ.get("LCAM_BIN", "/usr/local/bin/lcam")

# 传感器横装 90 度 -> 抓到的帧是 1280x960 的横图，逆时针转 270 度才是正竖屏
CAPTURE_WIDTH = int(os.environ.get("CAMERA_WIDTH", "1280"))
CAPTURE_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", "960"))
# AGC/AWB 要 20 帧左右才收敛，第 1 帧看起来像全黑
CAPTURE_FRAMES = int(os.environ.get("CAMERA_FRAMES", "20"))
CAPTURE_INDEX = int(os.environ.get("CAMERA_FRAME_INDEX", str(CAPTURE_FRAMES - 1)))

# 视觉模型是本地 27B，看图要几秒，给足超时
TIMEOUT = httpx.Timeout(60.0, connect=5.0)
CAPTURE_TIMEOUT = 40


def _fail(what: str, exc: Exception) -> ActionResponse:
    return ActionResponse(
        Action.REQLLM,
        f"{what}失败：{exc}。请简短告诉用户这次没成功，稍后再试。",
        None,
    )


def _capture_from_libcamera() -> Optional[bytes]:
    """用 libcamera 软 ISP 抓一帧彩色图，返回 JPEG 字节。

    连抓 CAPTURE_FRAMES 帧让 AGC/AWB 收敛，取第 CAPTURE_INDEX 帧，
    再用 ffmpeg 转成竖屏 JPEG。
    """
    env = dict(os.environ)
    env["LIBCAMERA_PREFIX"] = LIBCAMERA_PREFIX
    env["LIBCAMERA_SOFTISP_MODE"] = SOFTISP_MODE

    with tempfile.TemporaryDirectory(prefix="thinksmart-cam-") as tmp:
        pattern = os.path.join(tmp, "frame-#.bin")
        cmd = [
            LCAM,
            "cam",
            "-c",
            "1",
            f"--capture={CAPTURE_FRAMES}",
            "-s",
            (
                f"role=viewfinder,width={CAPTURE_WIDTH},height={CAPTURE_HEIGHT},"
                "pixelformat=XBGR8888"
            ),
            f"--file={pattern}",
        ]
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                timeout=CAPTURE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # 超时也可能有帧落盘，继续往下走
            proc = None

        raw = None
        expected = os.path.join(tmp, f"frame-cam0-stream0-{CAPTURE_INDEX:06d}.bin")
        if os.path.exists(expected):
            raw = expected
        else:
            # lcam 偶尔会少产一两帧，取编号最大的可用帧（>= 目标帧优先）
            produced = []
            for name in os.listdir(tmp):
                if not name.endswith(".bin"):
                    continue
                stem = name.rsplit(".", 1)[0]
                digits = stem.rsplit("-", 1)[-1]
                if digits.isdigit():
                    produced.append((int(digits), os.path.join(tmp, name)))
            produced.sort()
            at_or_after = [p for p in produced if p[0] >= CAPTURE_INDEX]
            usable = at_or_after or produced
            if not usable:
                stderr = (proc.stderr.decode("utf-8", "ignore") if proc else "")[-400:]
                raise RuntimeError(f"libcamera 没有产出任何帧。{stderr}")
            raw = usable[0][1] if at_or_after else usable[-1][1]
            sys.stderr.write(
                "[camera_tools] 目标帧 %d 不在，退回第 %d 帧\n"
                % (CAPTURE_INDEX, int(os.path.basename(raw).rsplit("-", 1)[-1].split(".")[0]))
            )

        jpg = os.path.join(tmp, "frame.jpg")
        # XBGR8888 是 4 字节/像素（BGR + padding），用 bgr0 读；transpose=2 = 逆时针 90 度
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "rawvideo",
                "-pixel_format",
                "bgr0",
                "-video_size",
                f"{CAPTURE_WIDTH}x{CAPTURE_HEIGHT}",
                "-i",
                raw,
                "-vf",
                "transpose=2",
                "-q:v",
                "3",
                jpg,
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        with open(jpg, "rb") as f:
            return f.read()


def _load_frame() -> Optional[bytes]:
    """拿一帧图：先看落点文件，再走 libcamera。"""
    try:
        if os.path.exists(FRAME_PATH):
            with open(FRAME_PATH, "rb") as f:
                data = f.read()
            if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
    except OSError:
        pass
    return _capture_from_libcamera()


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

    return ActionResponse(
        Action.REQLLM,
        f"你通过摄像头拍了一张照片（{len(image)} 字节），视觉模型的回答是：{answer}\n"
        "请用自然口语把结论告诉用户，不要复述技术细节。",
        None,
    )
