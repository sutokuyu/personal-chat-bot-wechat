# 配置总览

这个项目的可调项分布在三个地方。这份文档把三层合成一张可查的表：

| 层 | 位置 | 说明 |
| --- | --- | --- |
| ① 默认值与校验 | `app/config.py` | 所有字段的默认值、类型、取值范围。改动代码才会变 |
| ② 实例现值 | `config/instances/<实例>.yaml` | 你实际在调的参数；没写的字段回落为 ① |
| ③ 代码内常量 | 各模块 | 写死在逻辑里、YAML 改不到，但会影响行为 |

看**当前真正生效**的全部配置（①+②合并后的结果），比翻三个地方更直接：

```powershell
.\.venv\Scripts\python.exe -m app --show-config
.\.venv\Scripts\python.exe -m app --show-config --no-log-file > cfg.yaml
```

输出分两段：完整生效配置，以及**与代码默认值不同的设置**（一眼看出你改过哪些）。
`--show-config` 只读配置，不连微信、不连数据库、不调用模型，不需要 API Key。

> 表中「默认」= `app/config.py` 的代码默认值，「现值」= 当前 `deepseek-chan.yaml` 的值。

---

## 1. 顶层

| 字段 | 默认 | 现值 | 说明 |
| --- | --- | --- | --- |
| `instance_id` | 必填 | `deepseek-chan` | 必须与 YAML 文件名一致 |
| `persona_id` | 必填 | `deepseek-chan` | 对应 `config/personas/<id>.md` |
| `database_path` | `./data/bot.db` | `./data/bot.db` | 相对路径以项目根为基准 |
| `tools` | `[search_memory, get_user_profile]` | 增加 `web_search` | 只允许 `app/tools/registry.py` 里的名字 |

---

## 2. `wechat`：客户端与发送节奏

| 字段 | 默认 | 现值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `account_id` | `wechat-main` | `wechat-main` | ≤64 字符 | 本地数据的命名空间 |
| `chats` | 必填 | 3 个群 | ≥1 | 每项 `name`（完整聊天名）、`kind`（`direct`/`group`）、可选 `image_cache_size` |
| `poll_interval_seconds` | `1.0` | `20.0` | 0.2–30 | 每轮轮询的间隔 |
| `poll_jitter_ratio` | `0.35` | `0.5` | 0–0.9 | 间隔的 ± 随机比例 |
| `poll_burst_size` | `30` | `12` | 1–1000 | 连续活动多少轮后“离开一会儿” |
| `poll_pause_min_seconds` | `10` | `90` | 0–1800 | 离开时长下限 |
| `poll_pause_max_seconds` | `45` | `300` | 0–3600 | 离开时长上限 |
| `send_delay_seconds` | `1.0` | `1.0` | 0–60 | 发送前的基准“读+打字”延时；0 关闭整套延时 |
| `send_delay_per_char_seconds` | `0.05` | `0.3` | 0–1 | 每字追加时间（0.3 ≈ 3.3 字/秒） |
| `send_delay_jitter_seconds` | `1.5` | `1.5` | 0–30 | 额外随机犹豫 |
| `send_delay_max_seconds` | `25` | `60` | 0–120 | 延时上限；太小会让长消息“打字过快” |
| `send_gap_seconds` | `0.6` | `2.0` | 0–30 | 一条发完后的停顿（实际取 0.5–1.0 倍） |
| `active_hours_start` | `null` | `"08:00"` | `HH:MM` | 作息窗口起点 |
| `active_hours_end` | `null` | `"01:00"` | `HH:MM` | 作息窗口终点；`start > end` 跨午夜 |
| `bot_name` | `null` | `null` | ≤100 | 群聊 @ 检测用；省略则取登录昵称 |
| `text_only` | `true` | `true` | 布尔 | 跳过非文本消息 |
| `capture_images` | `false` | `true` | 布尔 | 是否处理图片消息 |
| `image_cache_size` | `10` | `10` | 0–50 | 每个聊天的图片 FIFO 容量，可被 `chats[].image_cache_size` 覆盖 |
| `image_followup_max_images` | `3` | `3` | 1–6 | 一次追问最多附几张原图 |
| `image_followup_seconds` | `120` | `120` | 0–900 | 图片缓存有效期 |

作息窗口两边必须同时设置或同时为 `null`，否则启动报配置错误。窗口外照常读取并写入历史，但不发送任何消息。

---

## 3. `model`：DeepSeek

| 字段 | 默认 | 现值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `provider` | `deepseek` | `deepseek` | 固定 | 目前只支持 DeepSeek |
| `model_name` | `deepseek-chat` | `deepseek-chat` | — | `deepseek-reasoner` 不支持工具调用，会被拒绝 |
| `temperature` | `0.7` | `0.9` | 0–2 | 越高越发散 |
| `timeout_seconds` | `60` | `60` | 0–600 | 单次模型调用超时 |
| `max_retries` | `2` | `2` | 0–10 | 网络失败重试 |
| `max_tokens` | `null` | `null` | 1–32768 | 为空则用服务端默认 |
| `api_key_env` | `DEEPSEEK_API_KEY` | `DEEPSEEK_API_KEY` | 环境变量名 | 只存变量名，不存 Key |
| `api_base_env` | `null` | `null` | 环境变量名 | 需要自定义 API 网关时填写 |

---

## 4. `web_search`：联网搜索

| 字段 | 默认 | 现值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `max_results` | `5` | `5` | 1–10 | 每次返回条数 |
| `timeout_seconds` | `10` | `10` | 0–60 | 搜索超时 |
| `include_content` | `false` | `false` | 布尔 | 是否抓取正文（更贵更慢） |
| `api_base` | `https://api.deepseek.com` | 同左 | URL | 搜索接口地址 |
| `api_key_env` | `DEEPSEEK_API_KEY` | `DEEPSEEK_API_KEY` | 环境变量名 | 与模型共用 Key |

---

## 5. `memory`：长期记忆

| 字段 | 默认 | 现值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `enabled` | `true` | `true` | 布尔 | 关闭后不再抽取，记忆工具也会失效 |
| `allow_shared_memory` | `false` | `false` | 布尔 | 允许人格间共享记忆 |
| `extraction_threshold` | `5` | `5` | 2–100 | 攒够多少条未抽取消息触发一次 |
| `extraction_batch_size` | `20` | `18` | 2–100 | 单批上限，必须 ≥ 阈值 |
| `extraction_max_attempts` | `3` | `3` | 1–10 | 失败重试次数 |
| `extraction_timeout_seconds` | `90` | `90` | 0–600 | 抽取超时 |
| `persist_pending` | `true` | `true` | 布尔 | 待抽取消息落库，重启不清零 |
| `pending_max_per_channel` | `200` | `200` | 10–2000 | 每个聊天待抽取队列上限 |
| `max_memories_per_search` | `5` | `5` | 1–20 | 检索返回条数 |
| `max_memory_chars` | `500` | `500` | 20–2000 | 单条记忆长度上限 |
| `min_importance` | `0.4` | `0.35` | 0–1 | 低于此重要度的候选丢弃 |
| `min_confidence` | `0.5` | `0.45` | 0–1 | 低于此置信度的候选丢弃 |
| `max_candidates_per_batch` | `10` | `10` | 1–50 | 一批最多写入的候选数 |

---

## 6. `agent`：工具调用

| 字段 | 默认 | 现值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `max_tool_rounds` | `3` | `3` | 1–20 | 工具调用轮数上限，超限中止本次回复 |
| `timeout_seconds` | `120` | `90` | 0–600 | 整个 Agent 的超时 |

---

## 7. `reply`：回复节奏与拆分

| 字段 | 默认 | 现值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `mention_priority` | `true` | `true` | 布尔 | 被 @ 时优先处理 |
| `channel_cooldown_seconds` | `20` | `45` | 0–3600 | 整个聊天共享的冷却 |
| `user_cooldown_seconds` | `10` | `25` | 0–3600 | 单用户冷却 |
| `continuation_window_seconds` | `0` | `180` | 0–3600 | 刚回过的人继续说话的时间窗口；0 关闭 |
| `continuation_channel_cooldown_seconds` | `2` | `12` | 0–300 | 窗口内的频道冷却 |
| `continuation_user_cooldown_seconds` | `1` | `8` | 0–300 | 窗口内的用户冷却 |
| `retry_on_cooldown` | `true` | `true` | 布尔 | 被冷却挡下的消息延后重判一次 |
| `max_cooldown_retry_seconds` | `15` | `8` | 0–300 | 超过这个等待就不再重试 |
| `max_reply_length` | `1800` | `450` | 50–2000 | 单次回复字符上限（超出截断） |
| `claim_message_across_instances` | `true` | `false` | 布尔 | 多进程抢答保护；单账号关掉 |
| `message_claim_ttl_seconds` | `120` | `120` | 0–3600 | 抢答标记有效期 |
| `split_replies` | `true` | `true` | 布尔 | 长回复拆成多条气泡 |
| `split_min_chars` | `160` | `160` | 20–2000 | 达到这个长度才拆 |
| `split_max_parts` | `3` | `3` | 2–6 | 最多几条 |
| `split_probability` | `0.8` | `0.8` | 0–1 | 满足条件时真正拆分的概率 |
| `split_gap_min_seconds` | `0.8` | `0.8` | 0–30 | 气泡间停顿下限（叠加在每条自己的打字延时之上） |
| `split_gap_max_seconds` | `3.0` | `3.0` | 0–60 | 气泡间停顿上限 |
| `empty_reply_fallback` | `（不知道该说什么……先听你们聊。）` | 同左 | ≤200 | 模型返回空文本时发这句 |

---

## 8. `context`：短期上下文

| 字段 | 默认 | 现值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `max_messages` | `30` | `30` | 5–200 | 内存历史条数，**同时**是 SQLite 里 `conversation_history` 每聊天的保留条数（重启后按它恢复） |
| `merge_window_seconds` | `3` | `3` | 0–60 | 同一人连续发言合并成一行转录的窗口 |
| `include_other_bots` | `false` | `false` | 布尔 | 是否把其他机器人的消息算进上下文 |

---

## 9. `participation`：参与判断

| 字段 | 默认 | 现值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `enabled` | `true` | `true` | 布尔 | 关闭后只在 @ 时回复 |
| `reply_probability` | `1.0` | `0.2` | 0–1 | 判断通过后仍只有这个概率回复 |
| `min_interest` | `0.0` | `0.55` | 0–1 | 兴趣分低于此值直接沉默 |
| `topic_window_seconds` | `0` | `120` | 0–3600 | 话题级限流窗口；0 关闭 |
| `topic_window_reply_probability` | `0.15` | `0.15` | 0–1 | 窗口内其他人插话的通过率 |
| `ignore_other_mentions` | `true` | `true` | 布尔 | 消息 @ 了别人就跳过 |
| `reply_to_other_bots` | `false` | `false` | 布尔 | 是否回应其他机器人 |
| `bot_reply_probability` | `0.1` | `0.1` | 0–1 | 回应其他机器人的概率 |
| `allow_consecutive_bot_messages` | `false` | `false` | 布尔 | 允许连续跟机器人对话 |
| `max_context_messages` | `15` | `15` | 3–50 | 判断器看到的上下文条数 |
| `timeout_seconds` | `30` | `30` | 0–120 | 判断器超时 |
| `default_reply_on_error` | `false` | `false` | 布尔 | 判断失败时是否默认回复 |

---

## 10. 代码内常量（YAML 改不到）

这些值写死在逻辑里，需要改代码才能调整。列在这里是为了“没有隐藏配置”。

| 值 | 位置 | 作用 |
| --- | --- | --- |
| 2000 | `app/context_manager.py:63` | 每个聊天未抽取消息的内存缓冲上限 |
| 4000 | `app/context_manager.py:71` | 已处理消息 ID 的去重集合上限 |
| 4000 | `app/runtime.py:259` | 冷却重试记录上限 |
| 500 | `app/context_manager.py:247` | 抽取失败后回队列的截断上限 |
| 20 | `app/memory/extractor.py:460` | 抽取时排除已有记忆的参考条数 |
| 5 | `app/tools/memory_tools.py:64` | `search_memory` 工具的默认返回条数 |
| 30.0 秒 | `app/runtime.py:787` | 视觉模型识别图片的超时 |
| 4 次 / 0.4 秒 | `app/wxauto_adapter.py:260,266` | 启动时等待聊天窗口渲染稳定 |
| 3600 秒 | `app/wxauto_adapter.py:315` | 启动时清理残留图片的年龄阈值 |
| 8 / 30 | `app/humanize.py:25,27` | 气泡最短字符数、可独立成条的短开场句长度 |
| 5000 ms / 3 次 / 0.05 秒 | `app/storage/database.py:42-44` | SQLite busy timeout、写重试次数、退避基数 |
| 5 MB × 3 | `app/logging_setup.py:55` | 日志轮转大小与保留份数 |
| 4.1.8.107 / 41.1.7 | `app/wechat_env.py:18-21` | 经验证的微信与 wxauto4 版本 |

---

## 11. 环境变量

| 变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 模型与联网搜索共用的 Key（名字由 `model.api_key_env` / `web_search.api_key_env` 决定） |
| `<model.api_base_env>` | 自定义 API 网关地址（默认不启用） |
| `WECHAT_SKIP_VERSION_CHECK=1` | 跳过微信客户端版本比对 |
| `WXAUTO_LIVE=1` | 只影响测试：启用需要真实微信的用例 |
| `WXAUTO_LISTEN_TIMEOUT` | 只影响测试：实时用例的等待秒数（默认 60） |

---

## 12. 命令行参数

| 参数 | 作用 |
| --- | --- |
| `--instance <id>` | 实例 ID，默认 `deepseek-chan` |
| `--config-dir <path>` | 配置目录，默认 `<项目>/config` |
| `--project-root <path>` | 解析相对数据库路径的根目录 |
| `--show-config` | 打印合并后的生效配置并退出 |
| `--list-instances` | 列出可用实例 |
| `--list-memories` | 打印长期记忆 |
| `--memory-limit <n>` / `--memory-chat <name>` | `--list-memories` 的条数与聊天过滤 |
| `--purge-user-memory <name>` / `--chat-name` / `--include-shared` | 删除某个用户的记忆 |
| `--check-web-search <query>` | 用实例配置跑一次联网搜索 |
| `--log-level` / `--log-file` / `--no-log-file` | 日志控制 |

---

## 13. 想调“像真人”该看哪儿

- 发送节奏与作息：`wechat.poll_*`、`wechat.send_delay_*`、`wechat.active_hours_*`
- 回复频率与接话：`reply.*_cooldown_*`、`participation.reply_probability`、`participation.min_interest`
- 消息形态：`reply.max_reply_length`、`reply.split_*`

README 的「让它更像真人的几个旋钮」一节有对应的组合建议。
