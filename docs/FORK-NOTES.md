# xiaomianao fork — 基于小智网关的二次开发

本仓库是 [justhalfbit/xiaozhi-hub-server](https://github.com/justhalfbit/xiaozhi-hub-server)
的 fork，在它之上做家庭中枢（xiaomianao）的二次开发。

- 上游：`justhalfbit/xiaozhi-hub-server`（MIT）
- 分支策略：`main` 与上游保持一致，方便 `gh repo sync`；我们的改动都在 `xiaomianao` 分支
- 部署目标：Lenovo ThinkSmart View（postmarketOS / aarch64）作为设备端，
  Mac Studio 作为中枢机（本地大模型 + 家庭账本/提醒服务）
- 源头与部署：nas-d 上的克隆 `~/xiaozhi-hub-server` 是**唯一源头**，
  改完用 `~/bin/sync-xiaozhi.sh`（`--restart` 顺带重启）同步到设备

## 这个 fork 解决的核心问题

上游小智网关把「设备端」和「网关」都做全了，唤醒、ASR/TTS、摄像头、声纹、MCP 都有。
我们要做的是**不重造设备端**，把自研能力做成网关能调用的工具，接进它的函数调用体系。

问题在于：上游的工具（函数）有两套清单，模型只看其中一套，
结果就是**模型看不见我们的工具 → 只会口头答应**（"好的，一分钟后提醒你"）而不真的调用。

## 三处必须同时登记

自定义工具要出现在模型的工具列表里，需要同时满足：

| 位置 | 内容 | 作用 |
|---|---|---|
| `main/xiaozhi-server/config.yaml` → `Intent.function_call.functions` | 函数**名**列表 | 模型实际能调用的工具（写模块名会变成非法工具） |
| 同文件 `plugins:` 段 | 每个函数的条目 | 插件配置与开关 |
| `main/xiaozhi-server/core/admin/catalog.py` → `PLUGINS` | 插件清单 | 后台「意图识别」页能勾选；不登记的话后台保存一次就把工具丢了 |

## 本 fork 的改动

### 1. `core/connection.py`：全局配置与后台私有配置的插件合并

上游在这里用后台私有配置**整体替换**全局配置的 `plugins` 段：

```python
plugin_from_server = private_config.get("plugins", {})
...
self.config["plugins"] = plugin_from_server
```

后台私有配置只包含它自己存过的那部分，于是全局配置里手写的插件（我们的
`family_tools`）在模型眼里根本不存在。改成先取全局、再叠加私有：

```python
plugin_from_server = dict(self.config.get("plugins") or {})
for _plugin, _value in (private_config.get("plugins") or {}).items():
    plugin_from_server[_plugin] = _value
for plugin, config_str in list(plugin_from_server.items()):
    if isinstance(config_str, str):
        plugin_from_server[plugin] = json.loads(config_str)
```

值解析同时兼容字符串（后台私有配置是 JSON 字符串）与映射（全局配置是 YAML 映射）。

### 2. `core/admin/catalog.py`：登记家庭工具插件

后台「意图识别」页的插件清单里新增 4 条：

- `set_reminder` 定时提醒
- `list_reminders` 查提醒
- `record_family_event` 记一笔
- `query_family_today` 查家庭账本

### 3. `plugins_func/functions/family_tools.py`：家庭工具插件

用上游的 `@register_function(name, DESC, ToolType.SYSTEM_CTL)` 机制注册，
设备端只做 HTTP 转发，数据与逻辑都在中枢机上的家庭 hub 里：

| 工具 | 作用 | 中枢接口 |
|---|---|---|
| `set_reminder` | 设倒计时提醒 | `POST /timers` |
| `list_reminders` | 查还没到点的提醒 | `GET /timers` |
| `record_family_event` | 家庭账本记一笔 | `POST /ledger` |
| `query_family_today` | 今天的家庭时间线 | `GET /ledger.json` |

插件自带一个后台线程轮询 `GET /timers`，到点时把提醒送到设备播报
（走客户端本地 API `POST 127.0.0.1:8090/api/text`，客户端会自己连网关，
所以设备空闲、没连着网关时也能收到；送不到就下一轮重试）。

配置：hub 地址 `FAMILY_HUB_URL`（默认 `http://192.168.3.123:8765`），
设备凭据放在设备上的 `~/.family_hub_token`（权限 600）。

### 4. `config.yaml`：LLM / VLLM 指向本机 oMLX

设备上的对话与视觉都走 Mac Studio 的 oMLX（本地推理，不再出户）：

| 条目 | 值 |
|---|---|
| `LLM.ChatGLMLLM` | `type: openai`，`url: http://192.168.3.123:8000/v1`，模型 `Qwen3.8-27B-Uncensored-MLX-4bit` |
| `VLLM.ChatGLMVLLM` | 同上（Qwen3.8 也能看图） |
| `extra_body` | `chat_template_kwargs.enable_thinking: false`（关思考，首字 0.1~0.2 秒） |

**`api_key` 在仓库里一律是占位符**，真值只留在设备上（见下面的密钥规则）。

### 5. `core/providers/llm/openai/openai.py`：记录每次 LLM 请求

在共用��� `client.chat.completions.create` 之前加一行：

```python
logger.bind(tag=TAG).info(f"[LLM-REQ-2] model={request_params.get('model')} "
                         f"base_url={self.base_url} "
                         f"extra_body={request_params.get('extra_body')} "
                         f"has_functions={'tools' in request_params}")
```

用途见下面的坑 #5：**每台设备记住的模型会覆盖本地配置**，只看 `config.yaml`
永远看不出设备实际在用哪个模型。

## 6. `plugins_func/functions/camera_tools.py`：设备拍照 + 本地视觉模型看图

设备（Lenovo ThinkSmart View）上的 S5KC505A 摄像头走 libcamera v0.8 软 ISP，
能出真实的 XBGR8888 彩色帧。`take_photo` 工具：抓 20 帧让 AGC/AWB 收敛、取最后一帧，
`ffmpeg -vf transpose=2` 转成 960×1280 竖屏 JPEG，再交给本地视觉模型看图回答。

**看图必须在进程内直接调模型，不能绕 `http://127.0.0.1:16688/mcp/vision/explain`。**
hub 是单线程 asyncio，工具在 event loop 里同步等自己的 HTTP 端口会**死锁**
（用户侧表现是「图片传过来就卡住了」，日志是「工具调用超时或异常」）。

```python
messages = [{
    "role": "user",
    "content": [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ],
}]
resp = vllm.client.chat.completions.create(model=vllm.model_name, messages=messages)
```

注意 provider 的 `response(session_id, dialogue)` 是给**对话流**用的，
`dialogue` 必须是 message 列表 —— 拿它做单图问答会拿到 generator 或直接报错。

配套的两处配置：

- `config.yaml` 里 `tool_call_timeout` 要 **90**（默认 30 不够：取帧 3s + 看图 ~6s + 生成）
- `Intent.function_call.functions` 里必须列出 `take_photo`，否则插件不加载、模型看不到

**另外**：设备上原本还有一个 client 侧的 `self.camera.take_photo`（设备 MCP 上报），
功能重复但**永远失败** —— client 是系统级服务，`PrivateDevices=yes` 导致进程的 `/dev`
是空的，拿不到 `/dev/media0`。同名工具同时暴露会让模型挑到坏的那个，已从 client
的 MCP TOOLS 里移除（幂等脚本 `~/todo/dnd/remove_client_take_photo.py`，已进 client 补丁包）。

**还有**：模型不调工具时，别只查配置 —— 对话历史里如果有过「摄像头罢工」这类
失败回复，模型会照着历史学、继续拒绝。清理 `chat.db` 的 `messages` 表即可恢复。

### v4：不再阻塞 event loop（2026-11-25 补）

`take_photo` 虽然是 `async def`，但内部两个调用都是**同步阻塞**的：

- `_load_frame()` —— `subprocess.run` 跑 lcam + ffmpeg，约 3.3 秒
- `_vision()` —— 同步 HTTP 调视觉模型，约 6 秒

直接卡在 event loop 里，**拍照那 9 秒内语音打断、其它连接的请求、管理后台全部延迟**。

改法（`b4947b1a`）：

```python
async with _CAMERA_LOCK:                       # 摄像头只有一台，串行化
    image = await asyncio.to_thread(_load_frame)
answer = await asyncio.to_thread(_vision, question, image, conn.config)
```

实测：拍照期间 hub 对 HTTP 请求的响应从「卡住 9 秒」降到 **7~20 毫秒**。

顺带三个修正：

- `asyncio.Lock` —— 两次拍照撞在一起会因设备被占用而失败
- `_vision` 加 `timeout`（默认 60 秒）—— 避免模型卡住时工具一直挂着
- 落点文件 `/run/thinksmart/camera/frame.jpg` 加新鲜度检查（默认 20 秒）——
  过期就忽略，否则会把几分钟前的旧图当成刚拍的

> 用 codegraph 探索调用链时发现的：
> `take_photo → _load_frame → _capture_from_libcamera`、`_vision → create_instance`，
> 以及 `register_function → module_func_map → auto_import_modules("plugins_func.functions")`
> 的注册路径（hub fork 已在本地 `codegraph init` 过，可直接查）。

## 踩过的坑（留给后来人）

1. **对话历史会教坏模型。** `data/chat.db` 的 `messages` 表里如果存着
   "好的，一分钟��提醒你" 这种口头答应的范例，模型会**模仿历史**继续口头答应，
   即使工具清单里明明有 `set_reminder`。清空该表后立刻正常。
   → 排查这类"模型不调工具"的问题，先看真实请求里的 `messages` 条数。
2. **客户端是按需连接的。** 唤醒才建 WebSocket，空闲 3 分钟服务端主动断开，
   所以不能用服务端 TTS 队列做后台播报（拿不到活跃连接）。
3. **诊断手法**：把 `core/providers/llm/openai/openai.py` 里 `response_with_functions`
   的 `request_params` 落盘，能看到真实发出的 messages/tools，再用最小请求复现对比。
   （现在这条已经固化成代码里的 `[LLM-REQ-2]` 日志 ✔）
4. `mac`/`linux` 上抓包前先确认绑定地址：上游 provider 默认只监听回环，
   局域网要显式改 `server.host`。
5. **改本地 `config.yaml` 不等于改到设备在用的模型。** 每台设备在
   `data/agent_sessions.json` 里记着它选中的**条目名**，连接时覆盖本地配置：

   ```json
   {"last_model": {"<设备 MAC>": {"model": "ChatGLMLLM"}}}
   ```

   要换设备用的模型，得写这个文件（或用后台的"切换模型"接口），
   改 `config.yaml` 的 `model_name` 是没用的。→ **日志里看实际发出的 model 字段** ✔
6. **`extra_body` 是 OpenAI SDK 的概念，curl 不会摊平它。** 用 SDK 时
   `extra_body={"chat_template_kwargs": {...}}` 会被合并到请求体顶层；
   直接用 curl 测时必须把 `chat_template_kwargs` 写在顶层，否则参数根本没送到。
7. **密钥不能进同步清单。** 设备上的 `config.yaml` 存着 Mac oMLX 的明文
   `api_key`，仓库里只有占位符 —— 所以 `~/bin/sync-xiaozhi.sh` **不**同步
   `config.yaml`（同步会用占位符覆盖真值，设备的 LLM 直接连不上）。
   改设备配置要手动改，同时把占位符版本更新回仓库。
8. **scp 的 `~` 是在本地展开的。** `scp host:~/path` 里的 `~` 用**本地**用户
   解析（nas-d 是 `webman`，设备是 `ubuntu`）→ 路径不存在。
   跨用户必须写绝对路径 `/home/ubuntu/...`。
9. **设备上的小智是镜像装的**，git 只是工作副本；部署靠
   `~/bin/sync-xiaozhi.sh` 文件同步，不是 git pull。

## 相关仓库

- 家庭中枢（本 fork 的下游）：`chinesewebman/xiaomianao`
- 设备端客户端：`/opt/thinksmart/client`（上游 thinksmart-view 镜像自带）
