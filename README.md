# nonebot-plugin-group-insight-ai

AI 驱动的群聊洞察插件（NoneBot2）：**群聊报告**、**词云生成**、**@ 消息追踪**。

移植自 Yunzai 插件 [KBVsent/group-insight](https://github.com/KBVsent/group-insight)（MIT），
保留原作的全部命令文案、prompt、阈值与缓存策略；技术栈替换为
nonebot-adapter-onebot(v11) + SQLite + nonebot-plugin-htmlrender + openai SDK。
逐项移植规格见 [PORT_SPEC.md](./PORT_SPEC.md)。

## 功能

### 1. 群聊报告

- **基础统计**：消息数、参与人数、字符数、表情数、平均长度、链接 / 视频统计
- **活跃度分析**：24 小时活跃度图表与峰值时段
- **AI 话题提取**：不限数量，含参与者胶囊标签
- **AI 金句识别**：默认最多 5 条
- **AI 用户称号**：行为模式标签 + MBTI，默认最多 9 人
- **Token 统计**：报告页脚展示 AI 用量
- **增量批次分析**：消息数超过 `ai_max_messages` 时自动分批 + 缓存，重复查询不重复消耗 token
- **今天 / 历史报告**：今天报告 60 分钟冷却（冷却期内返回缓存）；历史报告首次生成后定型
- **定时生成与发送**：每日 23:59 为白名单群生成，可配置立即发送或次日定时发送

### 2. 词云

- `#词云`：群聊词云，支持当天 / 三天 / 七天
- `#个人词云`：个人词云（少于 5 条消息时提示无法生成）
- jieba 分词 + 词性过滤，支持自定义词典与过滤词
- 词频 / TF-IDF 两种提取方式

### 3. @ 消息追踪

- 记录每条 @ 消息（含前文 / 后文上下文），`谁艾特我` 以合并转发展示
- 支持 `#清除艾特数据` / `#清除全部艾特数据`

## 安装

```bash
pip install nonebot-plugin-group-insight-ai
```

在 NoneBot 项目中使用：

```python
# bot.py
nonebot.load_plugin("nonebot_plugin_group_insight")
```

或在 `pyproject.toml` / `.env` 中声明（`[tool.nonebot] plugins = [...]` / `PLUGINS=[...]`）。

报告与词云依赖 **nonebot-plugin-htmlrender**（Playwright 截图），首次使用前必须安装浏览器内核：

```bash
playwright install chromium
```

> 未安装 Chromium 时插件仍可加载、消息收集正常，但渲染会失败并回退到文本摘要。

## 配置

在 NoneBot 项目的 `.env` 中配置，键名前缀 `group_insight_`（大小写不敏感），**全部配置项均有默认值**，
不配置任何内容也能加载（AI 功能自动降级为基础统计）。

### AI 配置（推荐 OpenCode Go）

```env
group_insight_ai_api_key=你的OpenCode-Go-API-Key
group_insight_ai_base_url=https://opencode.ai/zen/go/v1
group_insight_ai_model=deepseek-v4.1-flash
```

兼容任何 OpenAI 格式服务商（OpenAI / DeepSeek / 通义千问 / 智谱 / 硅基流动 / OpenCode Go 等），
只需填写对应的 `api_key`、`base_url` 与 `model`。留空 `ai_api_key` 时话题 / 金句 / 称号分析与定时报告停用，
报告只输出基础统计。

### 完整配置表

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `group_insight_retention_days` | `7` | 群消息保留天数（过期后无法重新生成历史报告） |
| `group_insight_at_retention_hours` | `24` | @ 记录保留小时数 |
| `group_insight_context_message_count` | `1` | 纯 @ 消息收集的前文条数（>0 生效） |
| `group_insight_next_message_timeout` | `300` | 纯 @ 消息等待「后文」的秒数 |
| `group_insight_ai_api_key` | `""` | AI 服务 API Key，留空则停用 AI 分析 |
| `group_insight_ai_model` | `gpt-4.1` | 模型名 |
| `group_insight_ai_base_url` | `""` | OpenAI 兼容端点，留空使用 SDK 默认 |
| `group_insight_ai_timeout` | `60000` | 单次请求超时（毫秒，SDK 侧） |
| `group_insight_ai_max_messages` | `1000` | 单次分析的最大消息数，超出后分批 / 截断取最后 N 条 |
| `group_insight_ai_llm_timeout` | `100` | 单次 AI 调用超时（秒） |
| `group_insight_ai_llm_retries` | `2` | AI 调用失败重试次数（共 3 次尝试） |
| `group_insight_ai_llm_backoff` | `2` | 重试退避基数（秒，第 n 次等待 n × backoff） |
| `group_insight_topic_enabled` | `true` | 启用话题提取 |
| `group_insight_golden_quote_enabled` | `true` | 启用金句提取 |
| `group_insight_max_golden_quotes` | `5` | 金句最大条数 |
| `group_insight_min_quote_length` | `5` | 参与金句评选的最短字符数 |
| `group_insight_max_quote_length` | `100` | 参与金句评选的最长字符数 |
| `group_insight_user_title_enabled` | `true` | 启用用户称号 |
| `group_insight_max_user_titles` | `9` | 称号最大人数 |
| `group_insight_min_messages_for_title` | `5` | 参与称号评选的最少发言条数 |
| `group_insight_activity_enabled` | `true` | 报告展示 24 小时活跃度图表 |
| `group_insight_min_messages_threshold` | `20` | 少于该条数时跳过 AI 分析（仅基础统计） |
| `group_insight_night_start_hour` | `0` | 夜间时段起始小时（含） |
| `group_insight_night_end_hour` | `6` | 夜间时段结束小时（不含） |
| `group_insight_wordcloud_max_words` | `100` | 词云最大词数 |
| `group_insight_wordcloud_min_length` | `2` | 词语最小长度 |
| `group_insight_wordcloud_min_frequency` | `2` | 词频模式下词语最小出现次数（TF-IDF 模式忽略） |
| `group_insight_wordcloud_filter_strength` | `standard` | 词性过滤强度：`loose` / `standard` / `strict` |
| `group_insight_wordcloud_extract_method` | `frequency` | 提取方式：`frequency` / `tfidf` |
| `group_insight_wordcloud_width` | `1200` | 词云画布宽度（像素） |
| `group_insight_wordcloud_height` | `800` | 词云画布高度（像素） |
| `group_insight_wordcloud_background_color` | `#ffffff` | 词云背景色 |
| `group_insight_collection_enabled` | `true` | 启用消息收集（关闭后不记录消息 / @） |
| `group_insight_collect_images` | `true` | 收集图片 |
| `group_insight_collect_faces` | `true` | 收集表情 / 动画表情 / emoji |
| `group_insight_collect_links` | `true` | 收集链接 / 小程序 / 音乐分享卡片 |
| `group_insight_collect_videos` | `true` | 收集视频 |
| `group_insight_max_message_length` | `500` | 单条消息文本入库最大长度（超出截断） |
| `group_insight_nickname_mode` | `nickname` | 昵称来源：`nickname`（QQ 昵称）/ `card`（优先群名片） |
| `group_insight_schedule_enabled` | `false` | 启用定时报告 |
| `group_insight_schedule_whitelist` | `[]` | 定时报告群白名单，如 `[123456789, 987654321]`（同时决定批次预分析范围） |
| `group_insight_schedule_min_messages` | `99` | 定时报告的单群最少消息数，不足则跳过 |
| `group_insight_schedule_concurrency` | `3` | 定时生成并发数 |
| `group_insight_cooldown_minutes` | `60` | 今天报告的冷却分钟数（主人与定时任务忽略） |
| `group_insight_send_mode` | `disabled` | 发送模式：`disabled`（仅保存）/ `immediate`（生成后立即发送）/ `scheduled`（次日定时发送） |
| `group_insight_send_hour` | `8` | 定时发送小时（`scheduled` 模式） |
| `group_insight_send_minute` | `0` | 定时发送分钟（`scheduled` 模式） |
| `group_insight_summary_template` | `default` | 报告模板：`default` / `scrapbook` |
| `group_insight_render_img_type` | `png` | 截图格式：`png` / `jpeg` |
| `group_insight_render_quality` | `100` | 截图质量（jpeg） |

> 定时任务的触发时间在插件加载时注册，**修改发送时间需重启 NoneBot 生效**。

数据文件（SQLite 数据库、词云词典）位于 nonebot-plugin-localstore 管理的插件数据目录
（默认 `./data/nonebot_plugin_group_insight/`）。

## 指令

| 指令 | 权限 | 范围 | 说明 |
|---|---|---|---|
| `#群聊报告 [群号] [日期]`（别名 `#群聊总结`） | 所有人（指定其他群需主人） | 群聊 / 私聊 | 查询或生成报告，日期支持 `今天` / `昨天` / `前天` / `YYYY-MM-DD`，默认今天；群号与日期顺序无关、均可省略 |
| `#强制生成报告 [群号] [日期]` | 主人 | 群聊 / 私聊 | 忽略批次缓存强制重新生成 |
| `#词云 [当天\|三天\|七天]`（别名 `#群聊词云`） | 所有人 | 仅群聊 | 群聊词云，默认当天 |
| `#个人词云 [当天\|三天\|七天]` | 所有人 | 仅群聊 | 个人词云，少于 5 条消息时提示无法生成 |
| `谁艾特我` / `谁@我` / `谁at我`（如 `哪个小可爱艾特她`） | 所有人 | 仅群聊 | 合并转发展示被 @ 的记录（含前文 / 后文） |
| `#清除艾特数据`（`#` 可省略） | 所有人 | 群聊 / 私聊 | 清除**自己被 @**的记录 |
| `#清除全部艾特数据`（`#` 可省略） | 主人 | 群聊 / 私聊 | 清除全部 @ 记录 |

报告行为细节：

- **今天报告**：冷却期（默认 60 分钟）内直接返回缓存；冷却期外重新生成并刷新冷却。
- **历史报告**：与缓存消息数差异 ≤ 50 条时使用缓存；> 50 条时重新生成，且不设置冷却。
- **并发保护**：同一群同一日期同时只允许一个生成任务，重复触发提示「报告正在生成中，请稍后再试」。
- **渲染失败**：历史报告回退为文本摘要，今天报告提示「报告已生成并保存，但渲染失败」。

## 自定义词典

词云分词支持两个可选文件，放在插件数据目录下，**修改后重启生效**：

### `userdict.txt`（自定义词典）

让分词器正确识别群内专有名词（游戏角色、黑话等），避免被切碎。

```text
# 格式：词语 词频(可选) 词性(可选)
# 词频越高，该词被分出的概率越大
千恋万花 10 nr
八重神子 10 nr
鸣潮
碧蓝档案
```

### `stopwords.txt`（过滤词）

这些词不会出现在词云中，用于过滤无意义词汇或机器人指令。

```text
# 每行一个词，支持 # 开头的注释
的
了
是
# 可添加群内特定词汇
```

两个文件缺失时静默降级（仅记录 debug 日志），不影响词云生成。

## 常见问题

<details>
<summary>AI 分析失败 / 报告里没有话题和金句</summary>

1. 检查 `group_insight_ai_api_key` 是否填写正确
2. 检查 `group_insight_ai_base_url` 是否与服务商匹配（OpenAI 格式端点，需带 `/v1`）
3. 查看后台日志中的 AI 报错（重试 3 次后仍失败会返回空结果，不抛异常）
4. 确认服务商账户额度充足
5. 消息数少于 `group_insight_min_messages_threshold`（默认 20）时会跳过 AI 分析，只出基础统计

</details>

<details>
<summary>提示「AI 响应被截断」（finish_reason=length）</summary>

说明服务商侧的输出长度上限不够用，常见于推理模型——思考过程会占用同一份输出预算。

1. 若服务商控制台可调整单次输出上限，将其调高
2. 换用推理长度更短的模型
3. 调小 `group_insight_ai_max_messages`，减少单次分析的消息量

</details>

<details>
<summary>消息收集不工作 / 报告显示「今天还没有消息」</summary>

1. 确认 `group_insight_collection_enabled=true`
2. 确认机器人在群内且没有被其他插件以更高优先级拦截消息
3. 消息按天分桶且只保留 `group_insight_retention_days` 天，检查是否已过期
4. 检查插件数据目录是否有 `group_insight.db` 写入权限

</details>

<details>
<summary>词云没有词 / 词太少</summary>

1. 消息数太少：个人词云要求至少 5 条，群词云需要达到 `group_insight_wordcloud_min_frequency`
2. 词语默认必须含中文，纯英文 / 数字不会进入词云
3. 词性过滤强度过高（`strict`）会过滤掉数词、量词、时间词，可改为 `standard` 或 `loose`
4. 过滤词过多时检查 `stopwords.txt`

</details>

<details>
<summary>渲染失败 / 图片发送不出来</summary>

1. 执行 `playwright install chromium` 安装浏览器内核
2. 确认 htmlrender 依赖（playwright）安装完整
3. 渲染失败时历史报告会回退为文本摘要，今天报告会提示渲染失败，可先按文本查看

</details>

<details>
<summary>批次生成失败</summary>

消息量大时（超过 `group_insight_ai_max_messages`）插件会分批分析并缓存：

1. 再次请求报告会自动补全缺失 / 失败的批次
2. 已重试过的失败批次（`retried` 标记）不会重复消耗 token
3. 使用 `#强制生成报告` 可忽略所有缓存重新分析

</details>

<details>
<summary>定时任务没有触发</summary>

1. 需要同时满足 `group_insight_schedule_enabled=true` 且 `group_insight_schedule_whitelist` 非空
2. 单群消息数需达到 `group_insight_schedule_min_messages`（默认 99）
3. 生成时间为每天 23:59，定时发送仅在 `group_insight_send_mode=scheduled` 时注册
4. 发送时间 / 模式改动需要重启 NoneBot

</details>

## 许可证

[MIT](./LICENSE)，与原作 [KBVsent/group-insight](https://github.com/KBVsent/group-insight) 一致。
