# personal-chat-bot-wechat

单账号 DeepSeek 微信聊天 Bot。通过 **wxauto 3.9.x** 驱动 Windows PC 微信，使用
**LangChain `create_agent` + `ChatDeepSeek` + SQLite** 完成上下文、参与判断、工具调用和长期记忆。

这是 Discord 项目的独立副本，原项目 `personal-chat-bot` 保持不变。

> 本项目是桌面 UI 自动化，不是微信官方 API。仅建议在本人控制的账号和少量会话中低频使用。
> wxauto 对上位机版本、登录状态和桌面环境敏感，微信或 wxauto 升级后可能需要调整适配代码。

---

## 1. 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11（或 Windows Server 带桌面会话） |
| 微信 PC 客户端 | **3.9.x** |
| Python | 3.11+（推荐 3.11 或 3.12） |
| wxauto | 3.9.11+，仅支持 Windows |
| LLM | DeepSeek `deepseek-chat` |

**wxauto 3.9.x 与 wxautox4 的 API 不同。** 本项目使用 wxauto 3.9.x 的
`AddListenChat` / `GetListenMessage` / `SendMsg` 轮询接口，不包含 wxautox4 / 微信 4.x 适配。

当前项目是 Python 项目，不能直接在当前 WSL/Linux 环境中驱动 Windows 微信。
请把整个目录复制到 Windows 电脑，在 Windows 的 Python 环境中安装依赖并运行。

---

## 2. 安装

在 Windows PowerShell 中：

```powershell
cd C:\path\to\personal-chat-bot-wechat

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
```

如果 PowerShell 禁止激活脚本，也可以直接使用：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

`requirements.txt` 中的 `wxauto` 使用 Windows 平台标记；在 Linux 上安装核心测试依赖时不会尝试安装 wxauto。

---

## 3. 配置 DeepSeek

编辑 `.env`：

```dotenv
DEEPSEEK_API_KEY=sk-...
```

也可以直接通过 Windows 环境变量提供。Token/Key 不会写入代码或 YAML，`.env` 已被 `.gitignore` 排除。

---

## 4. 配置监听的微信聊天

编辑 `config/instances/deepseek-chan.yaml` 的 `wechat` 部分：

```yaml
wechat:
  account_id: wechat-main

  chats:
    - name: "文件传输助手"
      kind: direct
    - name: "一个群聊名称"
      kind: group

  poll_interval_seconds: 1.0
  send_delay_seconds: 1.0
  bot_name: null
  text_only: true
```

规则：

- `name` 必须与微信左侧聊天列表中显示的**完整名称**一致。
- `kind: direct`：私聊。每条消息都视为直接对 Bot 说，优先回复；仍受冷却和延续窗口控制。
- `kind: group`：群聊。只有内容中出现 `@Bot昵称` 时才作为直接提及；其他消息走参与判断和回复概率。
- `bot_name`：可选。省略时使用当前登录微信账号的昵称。群聊 @ 检测依赖它。\n  如果你在群里的昵称/群备注与账号昵称不同，请显式填写群里显示的名字。
- `poll_interval_seconds`：轮询间隔，建议 1 秒。
- `send_delay_seconds`：每次发送后的等待时间，避免操作过快。
- `text_only: true`：第一版只处理文本，跳过图片、文件、语音、链接等消息。

启动前请先登录 PC 微信，保持微信窗口和桌面会话可用。wxauto 基于 UI 自动化，锁屏、微信退出、窗口结构变化都可能导致监听失败。

---

## 5. 启动

```powershell
# 在 Windows 项目根目录
.\.venv\Scripts\Activate.ps1
python -m app --instance deepseek-chan
```

默认实例就是 `deepseek-chan`，也可以直接：

```powershell
python -m app
```

正常启动会看到类似日志：

```text
wxauto_ready account=wechat-main bot=你的微信昵称 chats=文件传输助手
```

之后在配置的聊天中发消息测试。Ctrl+C 停止。

查看实例：

```powershell
python -m app --list-instances
```

---

## 6. 参与判断与回复行为

### 私聊（direct）

- 每条文本消息都被视为 `is_mention=True`，优先进入回复流程。
- 仍受频道冷却、用户冷却和“对话延续”窗口控制。
- 私聊里不会触发“别人 @ 了其他用户”的过滤。

### 群聊（group）

1. 内容中出现 `@Bot昵称`：视为直接提及，优先回复。
2. 没有 @ Bot：
   - 如果消息 @ 了别人：默认跳过（`ignore_other_mentions: true`）。
   - 否则由 Participation Gate 的 LLM 判断是否适合参与。
   - 判断通过后再按 `reply_probability` 概率决定是否真正回复。

### 相关配置

```yaml
reply:
  channel_cooldown_seconds: 5
  user_cooldown_seconds: 3
  continuation_window_seconds: 180
  continuation_channel_cooldown_seconds: 2
  continuation_user_cooldown_seconds: 1

participation:
  enabled: true
  reply_probability: 0.35
  ignore_other_mentions: true
  max_context_messages: 15
  timeout_seconds: 30
  default_reply_on_error: false
```

- `continuation_window_seconds`：Bot 回复某个用户后，该用户继续说话的时间窗口。窗口内使用更短冷却，并跳过参与判断和概率，避免对话断掉。
- 普通闲聊的 `reply_probability` 只影响“判断通过后的普通消息”，不影响直接 @。
- 冷却命中时消息会被**跳过**，不会排队延迟发送。
- Bot 自己发送的文本会加入短期上下文并标记为 `[you]`，但不会再次触发回复。

### 微信中的“Bot”识别

wxauto 只能看到聊天成员和文本，无法可靠判断对方是不是另一个 Bot。
因此其他自动化账号会被当作普通用户处理。请自行控制会话范围和频率，避免两个 Bot 互相无限回复。

---

## 7. LangChain 与 Tools

流程：

```text
wxauto 轮询新消息
→ WxAutoAdapter 转成 IncomingMessage
→ 短期上下文 / 去重
→ Participation Gate（硬规则 + LLM 判断 + 概率）
→ create_agent(ChatDeepSeek)
→ LLM 决定是否调用 @tool
→ LangChain 执行工具并回填结果
→ 最终回复发送到对应微信聊天
```

- `app/agent/agent_factory.py`：`create_agent`、`ChatDeepSeek`、`ToolCallLimitMiddleware`。
- `app/tools/memory_tools.py`：`search_memory`、`get_user_profile`。
- `app/tools/context.py`：通过 `ToolRuntime` 注入可信的账号/聊天/用户范围。
- `app/memory/extractor.py`：独立结构化输出记忆提取，ADD / UPDATE / IGNORE 由 Python 校验后写入。
- 不使用旧版 `initialize_agent`、旧式 Memory API 或自写 Tool Calling 循环。
- LangSmith 不是运行依赖。

默认工具只读长期记忆和用户资料；聊天 Agent 没有任意 SQL、Python 执行或微信管理权限。

---

## 8. 数据与记忆

默认数据库：`./data/bot.db`。

微信没有 Discord 的 guild/channel 概念，数据库字段映射为：

| 字段 | 微信含义 |
| --- | --- |
| `guild_id` | 单账号 `account_id` 的稳定哈希 |
| `channel_id` | 聊天名称的稳定哈希 |
| `user_id` | 聊天内发送者名称的稳定哈希 |

因此同一账号、同一聊天、同一发送者的长期记忆和冷却状态是稳定的。
修改微信昵称、群名或备注可能导致对应 ID 变化。

默认只保存长期记忆和 `user_profiles` / `bot_state`，不永久保存全部原始聊天文本。
短期上下文只存在内存中，进程重启会清空。

### 清除某个用户在某个聊天的记忆

```powershell
python -m app --instance deepseek-chan `
  --purge-user-memory "张三" `
  --chat-name "文件传输助手"
```

同时清除 shared 记忆：

```powershell
python -m app --instance deepseek-chan `
  --purge-user-memory "张三" `
  --chat-name "文件传输助手" `
  --include-shared
```

单账号默认不需要 shared memory，`allow_shared_memory` 保持 `false`。

### 备份

```powershell
# 停止 Bot 后复制
Copy-Item data\bot.db data\bot.db.backup
```

也可以使用 sqlite3 的 `.backup` 命令。不要在多个写入进程间共享同一个 SQLite 文件。

---

## 9. 测试

在 Windows 或 Linux 上都可以运行不依赖 wxauto 的测试：

```bash
python -m pytest
```

当前测试结果：**55 passed**。

测试使用临时 SQLite、Fake Chat Model、Fake Agent/Sender，以及假的 wxauto 客户端；
不会访问真实微信或 DeepSeek API。真实 wxauto 集成必须在 Windows + 微信 3.9.x 上手动验证。

---

## 10. 常见问题

### `wxauto` 导入失败

- 确认在 Windows 上运行。
- 确认安装的是 `wxauto` 3.9.x，而不是 wxautox4。
- 确认 PC 微信 3.9.x 已登录。
- 检查 Python 架构与 wxauto/pywin32 是否匹配。

### 启动时报找不到聊天

- `wechat.chats[].name` 必须与微信聊天列表显示名称完全一致。
- 先手动在微信中打开该聊天，确认名称。
- 群聊名称可能带成员数量或特殊字符，建议直接复制实际显示名。

### 收不到消息

- wxauto 依赖 UI 自动化；微信窗口不能处于异常/最小化/锁屏状态。
- 检查日志中的 `wxauto_poll_failed` / `wxauto_get_messages_failed`。
- 确认微信版本仍是 3.9.x。
- 确认没有另一个程序同时操作微信窗口。

### 发送失败

- 检查微信是否仍登录、聊天是否仍存在。
- 检查 `send_delay_seconds`，避免操作过快。
- wxauto 发送是 UI 操作，失败不会像官方 API 一样返回明确错误码，需要看日志。

### 总是自动回复或回复太频繁

- 群聊调低 `participation.reply_probability`。
- 增大 `reply.channel_cooldown_seconds` / `user_cooldown_seconds`。
- 私聊中的每条消息都是直接提及，主要靠冷却和延续窗口控制；需要更克制可关闭 `participation.enabled` 或增加冷却。

### DeepSeek 401 / 429

- 检查 `DEEPSEEK_API_KEY`。
- 429 时降低聊天频率；项目不会无限重试。
- 日志不会输出 API Key。

### 数据库锁定

- 确认只有一个 Bot 进程写入同一个 `data/bot.db`。
- 不要把 SQLite 放在网络共享盘上。
- 确认没有其他程序长期持有写事务。

---

## 11. 已知限制

- wxauto 是 UI 自动化，不是官方微信 API；微信升级、窗口变化、锁屏或登录失效都可能导致失败。
- 仅适配 **wxauto 3.9.x / 微信 3.9.x**，不支持 wxautox4 / 微信 4.x。
- 只处理文本；图片、文件、语音、视频、链接等消息会跳过。
- 无法可靠获取稳定的微信消息 ID；适配器使用 UI 消息标识/内容生成稳定哈希，重启后去重状态会重置。
- 无法可靠区分其他 Bot 和真人，其他自动化账号会被当作普通用户。
- 群聊 @ 检测基于 `@Bot昵称` 文本匹配，昵称修改或特殊字符可能导致漏判。
- 没有微信侧的发送回执和官方限流控制，只能依靠轮询间隔、发送延迟和冷却。
- 单账号、单进程；不支持多微信账号并行。
- 没有斜杠命令、附件理解、联系人管理或自动加好友。
- wxauto 作者明确提示该技术用于 UI 自动化学习交流，不建议直接用于生产；账号风控和使用边界需自行负责。
- 没有内置真实微信/DeepSeek 集成测试，真实连接需要手动验证。
