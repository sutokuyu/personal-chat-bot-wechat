# personal-chat-bot-wechat

单账号 DeepSeek 微信聊天 Bot。通过 **wxauto4 免费版** 驱动 Windows PC 微信 4.1.x，使用
**LangChain `create_agent` + `ChatDeepSeek` + SQLite** 完成上下文、参与判断、工具调用和长期记忆。

这是 Discord 项目的独立副本，原项目 `personal-chat-bot` 保持不变。

> 本项目是桌面 UI 自动化，不是微信官方 API。仅建议在本人控制的账号和少量会话中低频使用。
> wxauto4 对客户端版本、登录状态和桌面环境敏感，微信或 wxauto4 升级后可能需要调整适配代码。

---

## 1. 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11（或 Windows Server 带桌面会话） |
| 微信 PC 客户端 | **4.1.8.107**（本项目验证版本） |
| Python | 3.9–3.12（推荐 3.12） |
| wxauto4 | 41.1.7+（免费版，PyPI 安装，无需激活码） |
| LLM | DeepSeek `deepseek-chat` |

**wxauto4（免费版）与 wxautox4（Plus 付费版）不是同一个包。** 免费版只开放
`ChatWith` / `GetAllMessage` / `SendMsg`；`AddListenChat`、`StartListening`、`GetNextNewMessage`
在官方文档里标注 ✨，属于 Plus 功能（免费版实测也会拒绝第二个监听聊天）。因此适配层用
**轮询**支持多聊天：逐个 `ChatWith(chat)` → `GetAllMessage()` → 比较消息 `id` 去重 → 交给业务层。
不安装 `wxautox4`，不需要激活码，也不使用任何注入、内存修改或协议破解。

适配层按微信 4.x 的 `mmui::*` UI 结构编写，只在 **微信 4.1.8.107 + wxauto4 41.1.7** 上验证。
启动时会检查 Windows / Python / wxauto4 / 微信客户端版本，不匹配时直接给出中文提示而不是底层
UI 自动化异常。确认要尝试其他 4.1.x 版本时，可以设置 `WECHAT_SKIP_VERSION_CHECK=1` 跳过版本比对。

免费版 wxauto4 默认会向 wxauto.org 上报设备指纹并写 `wxauto_logs/` 日志；适配层在初始化前关闭了
这两项（`WxParam.TELEMETRY_ENABLED = False`、`WxParam.ENABLE_FILE_LOGGER = False`）。

当前项目是 Python 项目，不能直接在当前 WSL/Linux 环境中驱动 Windows 微信。
请把整个目录复制到 Windows 电脑，在 Windows 的 Python 环境中安装依赖并运行。

---

## 2. 安装

在 Windows PowerShell 中：

```powershell
cd C:\path\to\personal-chat-bot-wechat

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
```

如果 PowerShell 禁止激活脚本，也可以直接使用虚拟环境里的解释器（**推荐，不需要激活**）：

```powershell
.\.venv\Scripts\python.exe -m app --list-memories
.\.venv\Scripts\python.exe -m pytest
```

> **注意**：本项目所有依赖都装在 `.venv` 里，**没有**装进系统 Python。
> 如果直接敲 `python -m app` 报 `No module named 'yaml'`（或其他第三方包），
> 说明用的是系统 Python。此时 `python -m app` 会提示当前解释器路径和正确命令，
> 按提示改用 `.\.venv\Scripts\python.exe -m app`，或先执行 `.\.venv\Scripts\Activate.ps1`。

`requirements.txt` 中的 `wxauto4` 使用 Windows 平台标记；在 Linux 上只安装核心测试依赖，不会尝试安装 wxauto4。

---

## 3. 配置 DeepSeek

编辑 `.env`：

```dotenv
DEEPSEEK_API_KEY=sk-...
```

也可以直接通过 Windows 环境变量提供。Token/Key 不会写入代码或 YAML，`.env` 已被 `.gitignore` 排除。

---

## 4. 配置监听的微信聊天

> 全部可调项（各节字段的默认值/现值/取值范围、代码内常量、环境变量、CLI 参数）汇总在
> [`docs/configuration.md`](docs/configuration.md)。想直接看当前生效值用
> `python -m app --show-config`。

编辑 `config/instances/deepseek-chan.yaml` 的 `wechat` 部分：

```yaml
wechat:
  account_id: wechat-main

  chats:
    - name: "文件传输助手"
      kind: direct
    - name: "一个群聊名称"
      kind: group
      # 可选：只给这个群改图片缓存张数
      # image_cache_size: 20

  poll_interval_seconds: 20.0
  poll_jitter_ratio: 0.5           # 每次轮询 ±50% 随机抖动
  poll_burst_size: 12              # 轮询这么多次后“离开一会儿”
  poll_pause_min_seconds: 90
  poll_pause_max_seconds: 300
  send_delay_seconds: 1.0          # 发送前的基准“读+打字”延时，0 = 关闭整套延时
  send_delay_per_char_seconds: 0.3   # 每字追加的延时（≈3.3 字/秒）
  send_delay_jitter_seconds: 1.5   # 额外随机犹豫时间
  send_delay_max_seconds: 60
  send_gap_seconds: 2.0            # 一条发完后的小停顿
  active_hours_start: "08:00"      # 作息窗口：窗口外不发消息，两边都写 null 表示全天在线
  active_hours_end: "01:00"        # start > end 表示跨午夜
  bot_name: null
  text_only: true
  capture_images: true          # 看图（见下）
  image_cache_size: 10          # 每个聊天保留最近几张图（FIFO）
  image_followup_max_images: 3  # 一次追问最多附几张原图
  image_followup_seconds: 120   # 缓存有效期
```

规则：

- `name` 必须与微信左侧聊天列表中显示的**完整名称**一致。
- `kind: direct`：私聊。每条消息都视为直接对 Bot 说，优先回复；仍受冷却和延续窗口控制。
- `kind: group`：群聊。只有内容中出现 `@Bot昵称` 时才作为直接提及；其他消息走参与判断和回复概率。
- `bot_name`：可选。省略时使用当前登录微信账号的昵称。群聊 @ 检测依赖它。\n  如果你在群里的昵称/群备注与账号昵称不同，请显式填写群里显示的名字。
- `poll_interval_seconds`：轮询间隔（秒）。示例实例用 20 秒：回复延迟落在几十秒这个人类区间，
  又不会因为太密集而显得一直盯着屏幕。配置了 N 个聊天时，每轮会依次切换这 N 个窗口。
  `poll_jitter_ratio` 会给每次间隔加随机抖动；每 `poll_burst_size` 轮再随机停
  `poll_pause_min/max_seconds`，避免窗口切换精确得像机器。
- `send_delay_seconds`：发送前的“读消息 + 打字”基准延时；实际延时 =
  `send_delay_seconds + send_delay_per_char_seconds × 字数 + 随机抖动`，上限
  `send_delay_max_seconds`。设成 `0` 会关闭整套延时（含每字耗时与抖动）。
  `send_delay_max_seconds` 不要设得太小：一旦撞到上限，再长的消息也会用同样的时间发完，
  等于“字数越多打字越快”。长回复应该靠分条，而不是靠压短延时。
- `active_hours_start` / `active_hours_end`：作息窗口，用本机时区的 24 小时制 `HH:MM`。
  窗口外仍然轮询、仍然记录上下文，但**不会发出任何消息**，就像人睡着了；
  醒来后这些消息还在上下文里，可以正常接着聊。`start > end` 表示跨午夜
  （`"08:00"` → `"01:00"` = 凌晨 1 点到早上 8 点不说话）。
  两边都写 `null` 表示全天在线；只写一边会直接报配置错误。
- `text_only: true`：只处理文本，跳过文件、语音、链接等消息。
- `capture_images: true`：处理图片消息（`text_only` 不影响图片开关）。每张图会：
  1. 在切换聊天前立刻保存下来（wxauto4 的消息控件一旦重绘就失效）；
  2. 用视觉模型生成一句中文描述，写进「[图片] …」作为消息正文，供参与判断和上下文使用；
  3. 原图同时作为多模态内容交给 Agent，所以它能回答图中细节；
  4. 存进**这个聊天的图片 FIFO 缓存**（容量 `image_cache_size`，默认 10 张，可被
     `chats[].image_cache_size` 覆盖），每条缓存都带**发送者、时间、图注**；
  5. 之后收到的消息如果自己没带图，会从这个缓存里挑图：**同一个人发的优先，其次越新越优先**，
     最多附 `image_followup_max_images` 张原图；同时把所有缓存图的「序号 + 时间 + 发送者 + 图注」
     作为 `<recent_images>` 段落放进提示词，所以模型知道每张图是谁发的、说的是不是它；
  6. 缓存按 FIFO 淘汰（满了删最旧的），超过 `image_followup_seconds` 也会被清掉；
     退出时全部删除。`image_cache_size: 0` 或 `image_followup_seconds: 0` 表示不缓存。
  7. 如果图片实在拿不到（下载和截图都失败），这条消息会变成「[图片]（图片未能获取）」
     交给模型，而不是被静默丢弃，这样它至少能说「我没拿到图，能再发一次吗」。

  举个多人场景：A 发了 3 张图、B 发了 1 张，然后 B 问「我这张是什么」——
  B 的消息会优先附上 B 自己那张原图，A 的 3 张以图注形式出现在 `<recent_images>` 里，
  模型不会搞混发送者。参与判断（judge）看到的是同一份图注，所以它不会因为"看不到图"而拒绝回答。

  免费版 wxauto4 的 `download()` 对**别人发来的**图片会失败（内部找不到保存对话框），
  适配层会自动回退为**截取该消息的屏幕区域**。因此微信窗口必须保持可见、不被其他窗口遮挡，
  否则截到的会是遮挡窗口（截到纯色区域时会被判定为失败并记录日志）。
  截图来源/失败原因都写在日志里：`wxauto4_image_saved source=download|screenshot`、
  `wxauto4_image_capture_skipped reason=...`、`wxauto4_image_unavailable`。

  **重要限制**：wxauto4 只能读到微信**当前渲染出来**的消息（大约几行）。图片很占高度，
  一张大图就可能占满可见区域，所以「连着发 3 张图」时，只有恰好被轮询看到的那几张能进缓存。
  想提高命中率：减少同时监听的聊天数、把 `poll_interval_seconds` 调小（如 `5`）、
  让微信窗口尽量大。注意调小轮询间隔会让回复来得更快、更规律，和“像真人”是相反的取舍。日志里 `wxauto4_poll ... rendered=N` 就是每轮实际读到的行数。

启动前请先登录 PC 微信 4.1.x，并把微信主窗口切到**当前桌面、放在最前面**，保持桌面会话可用。
wxauto4 基于 UI 自动化，初始化要点头像读资料、轮询要点聊天列表，锁屏、退出到托盘、
窗口被完全遮挡或停在别的虚拟桌面都会导致失败。

---

## 5. 启动

```powershell
# 在 Windows 项目根目录，不需要激活虚拟环境
.\.venv\Scripts\python.exe -m app --instance deepseek-chan
```

默认实例就是 `deepseek-chan`，也可以直接：

```powershell
.\.venv\Scripts\python.exe -m app
```

已经 `Activate.ps1` 之后，写 `python -m app` 也行（此时 `python` 指向 `.venv`）。

正常启动会看到类似日志：

```text
wxauto4_ready account=wechat-main bot=你的微信昵称 chats=文件传输助手
```

之后在配置的聊天中发消息测试。Ctrl+C 停止。

运行日志默认同时写到 `data/logs/app.log`（5 MB 轮转，保留 3 份），
所以前台窗口关掉之后也能回头看发生了什么。换路径用 `--log-file PATH`，只想看控制台用 `--no-log-file`。

排查图片问题时看这几行：

```text
image_described message=... images=1 chars=38     # 视觉描述成功
image_cached channel=... cached=1 limit=10 ttl=120s  # 进了图片 FIFO 缓存
image_followup channel=... message=... attached=1 cached=3  # 追问复用了缓存里的图
wxauto4_poll chat=... rendered=2 unread=1 new=1   # 每轮实际读到的行数/未读数/新消息数
wxauto4_image_saved source=screenshot ...         # 图片是怎么拿到的
wxauto4_image_unavailable chat=... sender=...     # 图片没拿到（会回给模型一句提示）
wxauto4_sent chat=小窝儿 🎵 🎵 🎵 chars=42        # 实际发到了哪个聊天（排查串群）
```

查看实例，以及查看**当前真正生效的全部配置**（实例 YAML + 代码默认值合并后）：

```powershell
.\.venv\Scripts\python.exe -m app --list-instances
.\.venv\Scripts\python.exe -m app --show-config
```

`--show-config` 会打印完整配置，末尾再列出「与代码默认值不同的项」，所以不用再翻
YAML、`app/config.py` 和散落在模块里的常量。

> 所有可调项（含代码内常量、环境变量、CLI 参数）的汇总表在
> [`docs/configuration.md`](docs/configuration.md)。

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
   - 否则由 Participation Gate 的 LLM 判断是否适合参与（像群成员一样判断话题是否插得上话，
     不要求被 @ 或被提问）。
   - 判断器同时给出 0~1 的**兴趣分**：低于 `min_interest` 就直接不回，即使判断器说“可以回”。
   - 判断通过后再按 `reply_probability` 决定是否真正回复。
   - 回复后 `topic_window_seconds` 内，同一聊天里的其他人继续聊同样的话题基本不再插话，
     于是一个话题大致只回一次，而不是每人的每条发言都回一遍。

> **两层过滤的默认值偏保守。** 实例配置里 `reply_probability: 0.2`、`min_interest: 0.55`，
> 也就是“不 @ 时大部分时间保持安静”。想更爱接话就把 `reply_probability` 回调到
> `0.4~0.6`、`min_interest` 降到 `0.4`，不要一次拉满。

### 为什么「A 能一直聊，B 插不上话」

这是三个机制叠加的结果，不是「只认一个用户」：

1. **延续窗口按用户算**：bot 回过 A 之后 `continuation_window_seconds` 内，A 的消息直接放行
   （`forced=True`），不经过判断器和概率。
2. **频道冷却是整个聊天共享的**：每次回复都会刷新频道时间戳。A 的延续回复只等
   `continuation_channel_cooldown_seconds`（12 秒），而 B 不是延续用户，要等
   `channel_cooldown_seconds`（45 秒）。A 聊得越密，B 的窗口越窄。
3. **冷却命中的消息只延后重判一次**（`retry_on_cooldown: true`）：到点后用
   `reprocess` 重新判断，判断器认可就回复；旧版本是直接永久丢弃，所以 B 会「永远没反应」。

想看某条为什么被跳过，用 `--log-level DEBUG`，日志会给出
`message_skipped ... reason=channel cooldown (x.xs left)` 或判断器的理由。

### 相关配置

```yaml
reply:
  channel_cooldown_seconds: 45
  user_cooldown_seconds: 25
  continuation_window_seconds: 180
  continuation_channel_cooldown_seconds: 12
  continuation_user_cooldown_seconds: 8
  retry_on_cooldown: true          # 被冷却挡下的消息延后重判一次
  max_cooldown_retry_seconds: 8    # 超过这个等待时间就不再重试
  split_replies: true              # 长回复拆成 2~3 条分开发
  split_min_chars: 160
  split_max_parts: 3
  split_probability: 0.8
  split_gap_min_seconds: 0.8
  split_gap_max_seconds: 3.0

participation:
  enabled: true
  reply_probability: 0.2           # 不是 @ 时，判断通过后也只有 20% 会回
  min_interest: 0.55               # 低于这个兴趣分就不回
  topic_window_seconds: 120        # 刚回过的话题，这 120 秒内限流
  topic_window_reply_probability: 0.15
  ignore_other_mentions: true
  max_context_messages: 15
  timeout_seconds: 30
  default_reply_on_error: false
```

- `continuation_window_seconds`：Bot 回复某个用户后，该用户继续说话的时间窗口。窗口内使用更短冷却，并跳过参与判断和概率，避免对话断掉。
- `reply_probability`：只影响“判断通过后的普通消息”，不影响直接 @。
- `min_interest`：判断器返回的 0~1 兴趣分低于它就沉默。比单纯调概率更精准，
  因为“不感兴趣”和“感兴趣但现在不说”是两件事。
- `topic_window_seconds` / `topic_window_reply_probability`：话题级限流。回复后窗口内
  其他人的消息（不带 @、也不是接着跟 Bot 说话）只会以这个低概率进入回复，
  所以一群人聊同一个话题只会得到一次回应。`topic_window_seconds: 0` 关闭。
- `split_replies` 一组：**分条位置优先由模型自己决定**——提示词要求它在自然断点处写
  `<split/>`（最多两个，不能出现在开头/结尾）。模型没写标记时，才回退到按句子边界启发式拆分：
  超过 `split_min_chars` 的回复拆成最多 `split_max_parts` 条，条间停顿 `split_gap_min/max_seconds`，
  并有 `split_probability` 的概率才拆。标记会在发送前剥掉，不会漏进消息；
  超长截断时切到标记中间也会被清理。
- 冷却命中时消息会被**跳过**，不会排队延迟发送。
- Bot 自己发送的文本会加入短期上下文并标记为 `[you]`，但不会再次触发回复。

### 让它更像真人的几个旋钮

| 想要的效果 | 调整 |
| --- | --- |
| 更爱接话 | `reply_probability: 0.4~0.6`，`min_interest` 降到 `0.4`，冷却降到 `15~20` |
| 更安静 | `reply_probability: 0.1~0.2`，`min_interest` 提到 `0.6`，冷却调到 `60~120` |
| 不要半夜回消息 | 配好 `active_hours_start` / `active_hours_end`（示例实例是 08:00–01:00；都不配则全天在线） |
| 回复不要出现“等间隔” | 别把 `max_cooldown_retry_seconds` 设得和冷却一样长，宁可让某些消息真的漏掉 |
| 同一话题不要回好几次 | 调大 `topic_window_seconds`（如 `180`），调小 `topic_window_reply_probability` |
| 回复不要像一大段说明文 | 保持 `split_replies: true`，把 `split_min_chars` 降到 `120` |
| 操作节奏太规律 | 调大 `poll_jitter_ratio`、`send_delay_jitter_seconds`，开启 `poll_burst_size` 停顿 |
| 别人互相 @ 时也想插话 | `ignore_other_mentions: false`（交由判断器决定，仍会拒绝「文件发我」这类私事） |
| 聊起来后不要断 | 保持 `continuation_window_seconds: 180`（默认配置） |
| 完全不主动 | `participation.enabled: false`（只在 @ 时回复） |
| 别人老是插不上话 | 降低 `channel_cooldown_seconds`（如 2），保持 `retry_on_cooldown: true` |
| 不想让某个人独占对话 | 调小 `continuation_window_seconds`（如 60） |

### 微信中的“Bot”识别

wxauto4 只能看到聊天成员和文本，无法可靠判断对方是不是另一个 Bot。
因此其他自动化账号会被当作普通用户处理。请自行控制会话范围和频率，避免两个 Bot 互相无限回复。

---

## 7. LangChain 与 Tools

流程：

```text
wxauto4 轮询：ChatWith(chat) + GetAllMessage()
→ 文本：WxAutoAdapter 转成 IncomingMessage
→ 图片：保存/截图 → 视觉模型生成描述 → IncomingMessage（正文带描述，附原图文件）
        进聊天级 FIFO 图片缓存（发送者/时间/图注）→ 后续消息按「同发送者优先 + 新图优先」挑图
→ 短期上下文 / 去重
→ Participation Gate（硬规则 + LLM 判断 + 概率）
→ create_agent(ChatDeepSeek)
→ LLM 决定是否调用 @tool
→ LangChain 执行工具并回填结果
→ 最终回复发送到对应微信聊天
```

- `app/wxauto_adapter.py`：wxauto4 薄封装。启动时逐个 `ChatWith` 校验聊天名并把历史消息记为已读；
  之后每轮轮询每个聊天，只把新消息交给业务层；发送走 `SendMsg(who=..., exact=True)`。
- `app/wechat_env.py`：启动时的 Windows / Python / wxauto4 / 微信版本检查。
- `app/agent/agent_factory.py`：`create_agent`、`ChatDeepSeek`、`ToolCallLimitMiddleware`。
- `app/tools/memory_tools.py`：`search_memory`、`get_user_profile`。
- `app/tools/web_tools.py`：`web_search`（DeepSeek 官方 Search API 联网搜索）。
- `app/tools/context.py`：通过 `ToolRuntime` 注入可信的账号/聊天/用户范围。
- `app/memory/extractor.py`：独立结构化输出记忆提取，ADD / UPDATE / IGNORE 由 Python 校验后写入。
- 不使用旧版 `initialize_agent`、旧式 Memory API 或自写 Tool Calling 循环。
- LangSmith 不是运行依赖。

默认工具只读长期记忆、用户资料和公开网页搜索；聊天 Agent 没有任意 SQL、Python 执行或微信管理权限。

### 联网搜索（`web_search`）

遇到自己没把握或有时效性的事实（新闻、价格、版本号、日程、比赛结果），模型会调用
`web_search`。它直接走 DeepSeek 官方 Search API
（`POST https://api.deepseek.com/v1/tools/search`），和模型共用同一个
`DEEPSEEK_API_KEY`，不需要再申请第三方搜索服务。

```yaml
tools:
  - search_memory
  - get_user_profile
  - web_search          # 删掉这一行就关闭联网搜索

web_search:
  max_results: 5        # 每次取几条结果（1-10）
  timeout_seconds: 10
  include_content: false  # true 时额外取网页正文，更长也更贵
  api_base: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
```

- 搜索词由模型自己构造，支持 `site:`、`after:`、`before:`、`lang:`、`"精确短语"` 等运算符。
- 结果以 JSON（标题 / 链接 / 站点 / 日期 / 摘要）回填给模型；系统提示要求把它当**数据**看，
  不执行网页里的任何指令。
- 只有需要事实时才搜：闲聊不会触发工具调用（本机实测：闲聊 0 次工具调用；问「某模型什么时候发布」1 次搜索、约 1 秒）。
- **隐私**：搜索词会发送给 DeepSeek 的搜索服务。介意就删掉 `tools` 里的 `web_search`；本项目的聊天记录和记忆不会随之上传。
- **计费**：Search API 按请求计费（官方价 $0.75 / 1000 次），与 token 费用分开结算。
- **失败降级**：搜索超时或报错时工具返回 `{"error": "search_failed"}`，回复不会中断，模型会按已有知识回答或直接说明查不到。

---

### 日期与时间（模型没有内置时钟）

模型本身不知道「今天」是哪天。所以每次请求都会拼一行实时时钟给 Agent（和参与判断）：

```text
Current date and time: 2026-10-31 01:21（周六，UTC+09:00）；
今天 2026-10-31（周六），明天 2026-11-01（周日），昨天 2026-10-30（周五）
```

- 用的是**本机本地时区**（`datetime.now().astimezone()`），所以要保证 Windows 时区设置正确。
- 相对日期（今天/明天/昨天）由 Python 算好直接给，避免模型自己做日期加减或从搜索结果里
  抓一个日期当成"今天"。
- 提示词里明确要求：**不许说"我不知道今天几号"**，也不许把聊天记录/记忆/搜索里的日期当成当前日期。
- 时钟只在运行时拼进请求，不写进数据库，也不影响人设文件。

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

**记忆是「按聊天隔离」的**：`user_id` 里含聊天名，所以同一个人在不同群是不同的 user，
在 A 群记住的事只会在 A 群被检索到。`memories.channel_id` 记录了记忆来自哪个聊天
（schema v2 起；旧数据已按 `bot_state` 里的回复记录回填）。

默认只保存长期记忆和 `user_profiles` / `bot_state`，不永久保存全部原始聊天文本。
短期对话上下文会按聊天保存在 SQLite 中，进程重启后恢复最近的 `context.max_messages` 条消息；Bot 自己已发送的回复也会恢复并标记为 `[you]`。历史记录数量有上限，旧消息会自动淘汰。升级前只有仍在 `pending_messages` 中、尚未提取的消息可用于首次回填；升级后会持续记录完整的短期对话。图片原文件和临时图片缓存不属于持久化上下文。

**待抽取的消息会落盘**（`pending_messages` 表，schema v3 起）：以前这个队列只在内存里，
重启就清零，所以经常攒不够 `extraction_threshold` 条、几乎不抽取。现在：

- 别人的文本消息一进来就写入 `pending_messages`，重启后继续累计；
- 每个聊天最多保留 `pending_max_per_channel` 条（默认 200），超出丢最旧的；
- 抽取成功后立即删除对应行（真正处理完就不再保留原文）；
- 不想落盘就设 `memory.persist_pending: false`，回到旧的内存队列行为。

**消息 id 不可比较**：微信 4.x 的消息 id 在本项目里是「内容指纹哈希」，不是递增序号。
所以抽取游标、消息顺序、`pending_messages` 排序一律使用**接收时间** `created_at`，
不要用 `message_id` 比大小。

### 查看已经记住的内容

```powershell
.\.venv\Scripts\python.exe -m app --list-memories
.\.venv\Scripts\python.exe -m app --list-memories --memory-limit 100    # 默认显示最近 50 条
.\.venv\Scripts\python.exe -m app --list-memories --memory-chat "老窝🌹🍎🍑💖"  # 只看某个群
```

输出包含：数据库路径、账号/人格、用户档案数量、**按聊天统计的条数**，
以及每条记忆的所属聊天 `<>`、用户 id、类别、内容、作用域、重要度、置信度、更新时间。

想确认某个群有没有记忆，就看开头那段「按聊天统计」，或者直接用 `--memory-chat` 过滤。

### 清除某个用户在某个聊天的记忆

```powershell
.\.venv\Scripts\python.exe -m app --instance deepseek-chan `
  --purge-user-memory "张三" `
  --chat-name "文件传输助手"
```

同时清除 shared 记忆：

```powershell
.\.venv\Scripts\python.exe -m app --instance deepseek-chan `
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

在 Windows 或 Linux 上都可以运行不依赖 wxauto4 的测试：

```bash
.\.venv\Scripts\python.exe -m pytest
```

当前测试结果：**154 passed, 6 skipped**。

测试使用临时 SQLite、Fake Chat Model、Fake Agent/Sender，以及假的 wxauto4 客户端；
不会访问真实微信或 DeepSeek API。

### 真实微信联调（可选）

6 个 live 测试默认跳过，只有在 Windows + 已登录的微信 4.1.8.107 上设置 `WXAUTO_LIVE=1` 才会执行
（群聊接收测试还需要 `WXAUTO_GROUP_NAME`）：

```powershell
$env:WXAUTO_LIVE = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_wechat_connection.py -s
.\.venv\Scripts\python.exe -m pytest tests/test_wechat_send.py -s
.\.venv\Scripts\python.exe -m pytest tests/test_wechat_receive.py -s   # 60 秒内人工发送 hello
$env:WXAUTO_GROUP_NAME = "你的群名"
.\.venv\Scripts\python.exe -m pytest tests/test_wechat_receive.py::test_receive_text_from_configured_group -s
.\.venv\Scripts\python.exe -m pytest tests/test_wechat_ai_flow.py -s    # 需要 DEEPSEEK_API_KEY
```

| 测试 | 验证内容 |
| --- | --- |
| `test_wechat_connection.py` | `WeChat()` 附着已登录客户端，`IsOnline()` 为真 |
| `test_wechat_send.py` | 向「文件传输助手」发送 `wxauto4 test` |
| `test_wechat_receive.py` | 轮询 `ChatWith` + `GetAllMessage` 捕获新消息，打印 `chat` / `content` / `type` / `attr` |
| `test_wechat_ai_flow.py` | 合成消息 → 参与判断 → DeepSeek → 回发到「文件传输助手」 |

### 联网搜索联调（可选）

`tests/test_web_search_live.py` 默认跳过，设置 `WEB_SEARCH_LIVE=1` 后会用真实 key 发一次
Search API 请求（计费一次）：

```powershell
$env:WEB_SEARCH_LIVE = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_web_search_live.py -s
```

---

## 10. 常见问题

### 启动时报「当前微信版本不在本项目验证范围内」

- 本项目验证的是微信 4.1.8.107，检测结果会打印在错误信息里。
- 升级到 4.1.8.107，或在确认可用后设置 `WECHAT_SKIP_VERSION_CHECK=1` 跳过比对。

### `wxauto4` 导入失败 / 无法连接客户端

- 确认在 Windows 上运行，且 Python 为 3.9–3.12。
- 确认安装的是 `wxauto4`（`pip install wxauto4`），不是 `wxautox4`，也不是旧版 `wxauto`。
- 确认微信 4.1.x 已登录且主窗口打开（不要只留在托盘）。

### 启动时报找不到聊天

- `wechat.chats[].name` 必须与微信聊天列表显示名称完全一致。
- 先手动在微信中打开该聊天，确认名称。
- 群聊名称可能带成员数量或特殊字符，建议直接复制实际显示名。

### 收不到消息

- wxauto4 依赖 UI 自动化；微信窗口不能锁屏、退出到托盘，也不能停在别的虚拟桌面/被完全遮挡，
  否则 `ChatWith` 的坐标点击会失败（启动时会直接报 `wxauto4 无法打开聊天 ...`）。
- 检查日志中的 `wxauto4_poll_failed` / `wxauto4_message_handler_failed` / `wxauto4_chat_ready`。
- 确认微信版本仍是 4.1.x，且与本项目验证版本一致。
- 确认没有另一个程序（包括另一个 Bot 进程）同时操作同一微信窗口。
- 「文件传输助手」是你和自己的聊天：手机或电脑发出的消息在 PC 端都是 `attr=self`，
  适配层会跳过 self 消息以避免自激循环，所以**不能用它测试自动回复**，请用群聊或好友聊天验证。

### 发送失败

- 检查微信是否仍登录、聊天是否仍存在。
- 检查 `send_delay_seconds`，避免操作过快。
- wxauto4 发送同样是 UI 操作；日志中的 `wxauto4_send_subwindow_failed` / `wxauto4_send_rejected` 会带上 wxauto4 返回的原因。

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

### 联网搜索报 search_failed

- 查看日志里 `tool_failed tool=web_search` 的异常：常见是网络不通、`api_base` 写错、
  `DEEPSEEK_API_KEY` 缺失或额度不足。
- 该失败不会中断回复，只是模型拿不到搜索结果。单独验证搜索接口（会真实发一次请求、计费一次）：

  ```text
  .\.venv\Scripts\python.exe -m app --check-web-search "DeepSeek V4"
  ```

- 只想关掉联网搜索：从实例 YAML 的 `tools` 列表里删除 `web_search`。

---

## 11. 已知限制

- wxauto4 是 UI 自动化，不是官方微信 API；微信升级、窗口变化、锁屏或登录失效都可能导致失败。
- 仅适配 **wxauto4 / 微信 4.1.x**（验证版本 4.1.8.107），不保证其他 4.x 小版本。
- 免费版没有 `AddListenChat` / `GetNextNewMessage`（✨Plus 专用），本项目用轮询实现多聊天；
  每轮会切换微信聊天窗口，运行期间不建议同时手动操作微信。
- `SendMsg(who=...)` 是「先切到该聊天、等输入框、再打字」，而微信窗口是全局共享状态。
  适配层因此用**一把 UI 互斥锁**串行化所有窗口操作（切聊天 / 读消息 / 截图 / 发送），
  否则发送过程中的轮询切屏会把回复发到别的聊天（曾出现过「A 群的回复出现在 B 群」）。
- 轮询只能看到当前聊天窗口里**已渲染**的消息（wxauto4 会把聊天窗口拉高以显示更多），
  一个轮询周期内超出可见范围的消息可能漏掉；启动时已存在的历史消息会被标记为已读，不会回复。
- 微信 4.x 的运行时消息 id 在切换聊天/重绘后会变化，wxauto4 的 `hash` 又混入了渲染尺寸
  （窗口一变化就全变），所以去重和轮询基线改用**内容指纹**（发送者 + 类型 + 文本）；
  代价是同一发送者连续发两条**完全相同**的文字时，第二条会被当作重复消息忽略。
- 图片识别 = 1 次视觉调用（≤1024 tokens）+ 正常的 Agent 调用；群里所有图片都会进判断器，
  但**不代表每条都会回复**。需要微信窗口可见才能截图兜底。
- wxauto4 只能读微信**已渲染**的少量消息（通常几行）；图片很占高度，所以连发多张图时
  只有被轮询时机命中的图会进缓存，其余在界面上就没渲染过，读不到。
  新消息的相对位置用「上一轮窗口」做差分：窗口完全换新且没有重叠时会当作新消息处理（最多 10 条）。
- 同一张图在 2 分钟内重复出现（重绘）只会入缓存一次；超过这个窗口再发同一张图会当成新图。
- 图片缓存按聊天的 FIFO 容量（默认 10 张）和时间窗（默认 120 秒）双重限制：
  同一聊天里图很密集时，最早的图会被挤出缓存，只能靠当时生成的图注。
- 一次追问最多附 `image_followup_max_images`（默认 3）张原图，`detail=low` 约 1024 tokens/张；
  群里频繁消息时按「会回复的条数 × 张数」计费。想省钱就调小到 1。
- 图片消息类型在免费版里没有稳定的区分标识，同一发送者连发多张图时可能被合并成一条。
- 启动时每个聊天要做 2~3 次读取来等窗口渲染稳定（每次约 2 秒），聊天越多启动越慢；
  这是为了避免把半加载的历史误判成新消息。
- 只处理文本；图片、文件、语音、视频、链接等消息会跳过。
- 无法可靠获取稳定的微信消息 ID；适配器使用 UI 消息标识/内容生成稳定哈希，重启后去重状态会重置。
- 联网搜索返回的是第三方内容，可能过时或不准确；默认只取摘要（`include_content: false`），
  不抓取正文。搜索按请求计费，且搜索词会发送给 DeepSeek 的搜索服务。
- 无法可靠区分其他 Bot 和真人，其他自动化账号会被当作普通用户。
- 群聊 @ 检测基于 `@Bot昵称` 文本匹配，昵称修改或特殊字符可能导致漏判。
- 没有微信侧的发送回执和官方限流控制，只能依靠轮询间隔、发送延迟和冷却。
- 单账号、单进程；不支持多微信账号并行。
- 没有斜杠命令、附件理解、联系人管理或自动加好友。
- wxauto4 作者明确提示该技术用于 UI 自动化学习交流，不建议直接用于生产；账号风控和使用边界需自行负责。
- live 测试需要本机已登录微信，默认跳过；尚未在真实微信群聊中验证。
