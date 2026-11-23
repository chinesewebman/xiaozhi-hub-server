# xiaomianao fork — 基于小智网关的二次开发

本仓库是 [justhalfbit/xiaozhi-hub-server](https://github.com/justhalfbit/xiaozhi-hub-server)
的 fork，在它之上做家庭中枢（xiaomianao）的二次开发。

- 上游：`justhalfbit/xiaozhi-hub-server`（MIT）
- 分支策略：`main` 与上游保持一致，方便 `gh repo sync`；我们的改动都在 `xiaomianao` 分支
- 部署目标：Lenovo ThinkSmart View（postmarketOS / aarch64）作为设备端，
  Mac Studio 作为中枢机（本地 32B 大模型 + 家庭账本/提醒服务）

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

## 踩过的坑（留给后来人）

1. **对话历史会教坏模型。** `data/chat.db` 的 `messages` 表里如果存着
   "好的，一分钟��提醒你" 这种口头答应的范例，模型会**模仿历史**继续口头答应，
   即使工具清单里明明有 `set_reminder`。清空该表后立刻正常。
   → 排查这类"模型不调工具"的问题，先看真实请求里的 `messages` 条数。
2. **客户端是按需连接的。** 唤醒才建 WebSocket，空闲 3 分钟服务端主动断开，
   所以不能用服务端 TTS 队列做后台播报（拿不到活跃连接）。
3. **诊断手法**：把 `core/providers/llm/openai/openai.py` 里 `response_with_functions`
   的 `request_params` 落盘，能看到真实发出的 messages/tools，再用最小请求复现对比。
4. `mac`/`linux` 上抓包前先确认绑定地址：上游 provider 默认只监听回环，
   局域网要显式改 `server.host`。

## 相关仓库

- 家庭中枢（本 fork 的下游）：`chinesewebman/xiaomianao`
- 设备端客户端：`/opt/thinksmart/client`（上游 thinksmart-view 镜像自带）
