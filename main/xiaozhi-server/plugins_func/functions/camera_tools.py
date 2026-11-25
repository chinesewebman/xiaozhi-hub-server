"""
相机工具：拍一张照，用视觉模型看图回答。

设备端的 S5KC505A 摄像头走 libcamera v0.8 软 ISP（CSI/CSI-2 -> CSID -> VFE ->
去马赛克 + CCM/LSC），能出真实的 XBGR8888 彩色帧，1280x960@30fps。

取帧路径（按优先级）：
  1. 落点文件 /run/thinksmart/camera/frame.jpg（外部程序可以往这里写现成的帧，
     但只认**新鲜**的，见 FRAME_MAX_AGE）；
  2. libcamera + lcam：连抓 20 帧让 AGC/AWB 收敛，取最后一帧，
     再用 ffmpeg 转成竖屏 JPEG（传感器横装 90 度，逆时针转 270 度才是正竖屏）；
  3. 都没有就明确告诉模型"相机暂时拍不出照片"，而不是假装看过。

看图：直接在进程内调用配置里的视觉模型（VLLM），不走 HTTP。
**不能绕 http://127.0.0.1:16688/mcp/vision/explain** —— hub 是 asyncio 单线程，
工具在 event loop 里同步等自己的 HTTP 端口会死锁（表现为"图片传过来就卡住了"）。

并发：摄像头只有一个，两次拍照撞在一起会失败（设备被占用），
所以用一把 asyncio.Lock 串行化。
"""

import asyncio
import base64
import os
import shutil
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, Optional

from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

# 取帧的落点：外部程序（感知守护进程）可以往这里写现成的帧
FRAME_DIR = os.environ.get("CAMERA_FRAME_DIR", "/run/thinksmart/camera")
FRAME_PATH = os.path.join(FRAME_DIR, "frame.jpg")

# 感知守护进程（thinksmart-perception.service）常驻持有摄像头，
# 把帧流写在 STREAM_DIR 里，每秒只留几帧。它在的时候我们**不能**自己跑 lcam
# （摄像头只有一个，会 "Device or resource busy"），只能从它那儿取帧。
STREAM_DIR = os.environ.get("CAMERA_STREAM_DIR", "/tmp/thinksmart-perception")
# 感知守护进程的出图尺寸（必须和它的 PERCEPTION_FRAME_* 一致，
# 不然 raw 的字节数对不上，ffmpeg 会解出错位）
STREAM_WIDTH = int(os.environ.get("CAMERA_STREAM_WIDTH", "640"))
STREAM_HEIGHT = int(os.environ.get("CAMERA_STREAM_HEIGHT", "480"))

# 落点文件超过这个秒数就算过期 —— 不然会把一张几分钟前的旧图当成刚拍的
FRAME_MAX_AGE = float(os.environ.get("CAMERA_FRAME_MAX_AGE", "20"))

# libcamera 软 ISP 的运行环境（设备上装在 /opt/libcamera）
LIBCAMERA_PREFIX = os.environ.get("LIBCAMERA_PREFIX", "/opt/libcamera")
SOFTISP_MODE = os.environ.get("LIBCAMERA_SOFTISP_MODE", "gpu")
LCAM = os.environ.get("LCAM_BIN", "/usr/local/bin/lcam")

# 传感器横装 90 度 -> 抓到的帧是 1280x960 的横图，逆时针转 270 度才是正竖屏
CAPTURE_WIDTH = int(os.environ.get("CAMERA_WIDTH", "1280"))
CAPTURE_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", "960"))
# AGC/AWB 要 20 帧左右才收敛，第 1 帧看起来像全黑
CAPTURE_FRAMES = int(os.environ.get("CAMERA_FRAMES", "20"))

# 视觉模型是本地 27B，看图要 20 秒左右
VISION_TIMEOUT = float(os.environ.get("CAMERA_VISION_TIMEOUT", "60"))

# 摄像头只有一台，串行化避免两次拍照撞在一起
_CAMERA_LOCK = asyncio.Lock()


def _fail(what: str, exc: Exception) -> ActionResponse:
    return ActionResponse(
        Action.REQLLM,
        f"{what}失败：{exc}。请简短告诉用户这次没成功，稍后再试。",
        None,
    )


def _capture_from_libcamera() -> Optional[bytes]:
    """用 libcamera 软 ISP 抓一帧彩色图，返回 JPEG 字节。

    连抓 CAPTURE_FRAMES 帧让 AGC/AWB 收敛，取最后一帧，
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
            subprocess.run(cmd, env=env, capture_output=True, timeout=60)
        except Exception:
            pass

        produced = []
        for name in os.listdir(tmp):
            if not name.endswith(".bin"):
                continue
            digits = name.rsplit("-", 1)[-1].split(".")[0]
            if digits.isdigit():
                produced.append((int(digits), os.path.join(tmp, name)))
        if not produced:
            return None
        produced.sort()
        raw = produced[-1][1]

        jpg = os.path.join(tmp, "frame.jpg")
        # XBGR8888 是 4 字节/像素（BGR + padding），用 bgr0 读；
        # transpose=2 = 逆时针 90 度
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
            timeout=30,
        )
        with open(jpg, "rb") as f:
            return f.read()


def _latest_stream_frame():
    """从感知守护进程的流目录里取最新一帧的路径（没有则 None）。"""
    try:
        names = os.listdir(STREAM_DIR)
    except OSError:
        return None
    best = None
    for n in names:
        if not n.endswith(".bin"):
            continue
        stem = n.rsplit("-", 1)[-1].split(".")[0]
        if stem.isdigit():
            seq = int(stem)
            if best is None or seq > best[0]:
                best = (seq, os.path.join(STREAM_DIR, n))
    return best[1] if best else None


def _raw_to_jpeg(src, dst, width, height):
    """raw(XBGR8888) -> 竖屏 JPEG。返回 True/False。"""
    tmp = dst + ".tmp.jpg"   # ffmpeg 靠扩展名认格式
    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "rawvideo", "-pixel_format", "bgr0",
                "-video_size", "%dx%d" % (width, height),
                "-i", src,
                "-vf", "transpose=2",
                "-q:v", "3",
                tmp,
            ],
            check=True, capture_output=True, timeout=30,
        )
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, dst)
        return True
    return False


def _capture_from_stream() -> Optional[bytes]:
    """从感知守护进程的流里取一帧（守护进程占着摄像头时的唯一办法）。"""
    src = _latest_stream_frame()
    if not src:
        return None
    with tempfile.TemporaryDirectory(prefix="thinksmart-cam-") as tmp:
        # 先复制出来 —— 守护进程会随时删旧帧，直接读可能读一半就没了
        raw = os.path.join(tmp, "frame.bin")
        try:
            shutil.copyfile(src, raw)
        except OSError:
            return None
        jpg = os.path.join(tmp, "frame.jpg")
        if not _raw_to_jpeg(raw, jpg, STREAM_WIDTH, STREAM_HEIGHT):
            return None
        with open(jpg, "rb") as f:
            return f.read()


def _load_frame() -> Optional[bytes]:
    """拿一帧图。

    优先级：
      1. 落点文件（感知守护进程在检测到变化时写的）—— 只要新鲜就用；
      2. 感知守护进程的流目录里取最新一帧（守护进程在跑时的常态路径）；
      3. 自己跑 lcam（守护进程没跑时的兜底）。
    """
    try:
        if os.path.exists(FRAME_PATH):
            age = time.time() - os.path.getmtime(FRAME_PATH)
            if age <= FRAME_MAX_AGE:
                with open(FRAME_PATH, "rb") as f:
                    data = f.read()
                if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
                    return data
    except OSError:
        pass

    # 守护进程在跑？直接从它的流里取
    if _latest_stream_frame():
        try:
            data = _capture_from_stream()
            if data:
                return data
        except Exception:
            pass

    return _capture_from_libcamera()


def _vision(question: str, image: bytes, config: dict) -> str:
    """在进程内直接调用配置里的视觉模型，返回描述。

    走 OpenAI 兼容的多模态格式（text + image_url 的 data URI），
    和 core/api/vision_handler.py 用的是同一个模型。

    **不能绕 http://127.0.0.1:16688/mcp/vision/explain** —— hub 是 asyncio 单线程，
    工具在 event loop 里同步等自己的 HTTP 端口会死锁（表现为"图片传过来就卡住了"）。

    也不能直接用 provider 的 response(session_id, dialogue) —— 那个签名是给
    对话流用的，不是给单图问答的，dialogue 必须是 message 列表。
    """
    from core.utils.llm import create_instance

    select_vllm = (config.get("selected_module") or {}).get("VLLM")
    if not select_vllm:
        raise RuntimeError("还没设置默认的视觉分析模块（selected_module.VLLM）")
    entry = (config.get("VLLM") or {}).get(select_vllm) or {}
    vllm_type = entry.get("type") or select_vllm
    vllm = create_instance(vllm_type, entry)

    image_b64 = base64.b64encode(image).decode("utf-8")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                },
            ],
        }
    ]

    kwargs = {
        "model": vllm.model_name,
        "messages": messages,
        "stream": False,
        "timeout": VISION_TIMEOUT,
    }
    if getattr(vllm, "max_tokens", None):
        kwargs["max_tokens"] = vllm.max_tokens
    if getattr(vllm, "temperature", None) is not None:
        kwargs["temperature"] = vllm.temperature
    # 透传配置里的 extra_body —— 里面有 chat_template_kwargs.enable_thinking=False，
    # 不透传的话视觉模型会先写一长串 reasoning：实测 7.27 秒而且回答被截断
    # （content 只剩 8 字）；关掉 thinking 是 4.65 秒、内容完整。
    # 配置里没写就自己关 —— 单图问答不需要思考，本地 oMLX 的域名也不在
    # provider 的自动名单里。
    extra = entry.get("extra_body")
    if not (isinstance(extra, dict) and extra):
        extra = {"chat_template_kwargs": {"enable_thinking": False}}
    kwargs["extra_body"] = extra

    resp = vllm.client.chat.completions.create(**kwargs)
    return str(resp.choices[0].message.content or "").strip()


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
        # 取帧和看图都是阻塞操作（subprocess / 同步 HTTP），
        # 放到线程里跑，别把 hub 的 event loop 卡住 —— 卡住的话
        # 语音打断、其它连接的请求都会延迟。
        async with _CAMERA_LOCK:
            image = await asyncio.to_thread(_load_frame)
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
        answer = await asyncio.to_thread(_vision, question, image, conn.config)
    except Exception as e:  # noqa: BLE001
        return _fail("看图", e)

    return ActionResponse(
        Action.REQLLM,
        f"你通过摄像头拍了一张照片（{len(image)} 字节），视觉模型的回答是：{answer}\n"
        "请用自然口语把结论告诉用户，不要复述技术细节。",
        None,
    )
