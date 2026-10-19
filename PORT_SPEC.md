# group-insight 移植规格文档 (Yunzai JS → NoneBot2 Python)

原始仓库: `/Users/liangpuyue/Desktop/develop/group-insight-origin`（实现时可随时回读原文件核对细节）。

本文档是移植的权威规格。**所有 prompt、文案、阈值、缓存策略必须逐字/逐项保留**，除「适配差异」一节明确说明的调整外。

## 适配差异（已确认的技术决策）

- Redis → **SQLite**（数据文件经 `nonebot-plugin-localstore` 管理）。Redis 的 List/ZSet/Hash/TTL 语义用 SQL 表 + `expire_at` 字段复刻。
- Yunzai puppeteer → **nonebot-plugin-htmlrender**（模板需从 art-template 语法转为 Jinja2）。
- Yunzai 事件/段 → nonebot-adapter-onebot v11 的 `GroupMessageEvent` / `MessageSegment`。
- `moment` → `datetime`；`jieba-wasm` → `jieba`；`jsonrepair` → `json_repair`。
- AI 调用 → `openai` Python SDK 的 AsyncClient。
- 配置 → pydantic `Config`（NoneBot `.env`，扁平键名 `group_insight_*`）。
- 定时任务 → `nonebot-plugin-apscheduler`。
- 并发限流 → `asyncio.Semaphore`（原作 `runWithConcurrency` 有瑕疵，不必复刻）。
- 机器人账号过滤 `/^3889\d{6}$/` 保留；`adapter.id === 'QQBot'` 过滤 → OneBot v11 下天然不涉及，可省略。
- OpenCode Go 接入说明：OpenAI 兼容端点，baseURL `https://opencode.ai/zen/go/v1`，model `deepseek-v4.1-flash`，README 中作为推荐配置示例。

## 1. 命令总表

| 命令 | 匹配 | 权限 | 范围 |
|---|---|---|---|
| `#群聊报告 [群号] [日期]`（别名 `#群聊总结`） | 锚定全匹配；群号 `\d{5,12}`，日期 `今天\|昨天\|前天\|YYYY-MM-DD`，均可省略、顺序无关 | 所有人；指定其他群需 SUPERUSER | 群聊/私聊 |
| `#强制生成报告 [群号] [日期]` | 同上 | SUPERUSER | 群聊/私聊 |
| `#词云 [当天\|三天\|七天]`（别名 `#群聊词云`） | 无匹配天数默认当天=1天 | 所有人 | 仅群聊 |
| `#个人词云 [当天\|三天\|七天]` | 同上 | 所有人 | 仅群聊 |
| `谁艾特我` / `谁@我` 等 | 正则 `(谁\|哪个.*)(艾特\|@\|at)(我\|他\|她\|它)` | 所有人 | 仅群聊 |
| `#清除艾特数据`（# 可选） | 全匹配 | 所有人（清除自己被艾特的记录） | 群聊/私聊（非群聊提示） |
| `#清除全部艾特数据`（# 可选） | 全匹配 | SUPERUSER | 群聊/私聊 |

### 报告参数解析与错误文案（逐字保留）

- 指定其他群且非主人 → `仅主人可查看其他群的报告`
- 私聊未指定群号 → `私聊中请指定群号，例如：#群聊报告 123456789`
- 群不存在 → `Bot 不在群 {groupId} 中，无法获取报告`
- 日期非法 → `日期格式错误，请使用：YYYY-MM-DD（如 2024-11-01）`
- 日期标签：今天→`今天`；昨天/前天对应；具体日期→`YYYY年MM月DD日`

### 冷却期（仅"今天"报告）

- 冷却分钟数 `schedule.cooldownMinutes`（默认 60）；主人/定时任务忽略冷却。
- 冷却中且有缓存 → 直接渲染缓存回复（不设新冷却、不加锁）。
- 历史日期（昨天/前天/指定日）**从不检查也从不设置冷却**——首次生成后定型。
- 历史报告已有缓存时：`|当前消息数 - 缓存消息数| ≤ 50` → 用缓存；`> 50` → 无视冷却重新生成（且不重置冷却）。
- 存储冷却字段：`generatedAt`(ms)、`generatedBy`(user/scheduled/master)、`messageCount`；存储异常时视为"不在冷却"。

### 生成锁（并发保护）

- 锁 key 粒度 `{groupId}:{date}`，TTL 300s；抢锁失败文案：普通 `报告正在生成中，请稍后再试`，强制 `该日期的报告正在生成中，请稍后再试`；`finally` 释放。

### 生成流程文案（逐字）

- 开始：`正在生成今天的群聊报告{hint}（{n}条消息），请稍候...`（hint=` [群名]` 仅远程查询时）；历史为 `正在生成{dateLabel}的群聊报告{hint}（{n}条消息），请稍候...`
- 无消息：今天 → `今天还没有消息，无法生成报告`；历史无缓存 → `{dateLabel}没有消息记录（未收集到消息）` 或 `（消息已过期且无缓存报告）`
- 分析失败 → `分析失败，请查看日志`；渲染失败 → `报告已生成并保存，但渲染失败`；外层异常 → `查询报告失败: {err}`
- 强制：`正在强制生成{dateLabel}的群聊报告{hint}，请稍候...`；失败 → `生成报告失败: {err}`

### 词云文案（逐字）

- 群词云开始 `正在生成{当天|三天|七天}的词云，请稍候...`；无消息 `没有找到最近{days}天的消息记录`；生成返回空 `词云生成失败，请查看日志`；异常 `词云生成失败: {err}`；服务未就绪 `词云功能未就绪`
- 个人词云开始 `正在生成 {userName} {当天|三天|七天}的个人词云，请稍候...`；无消息 `您在最近{days}天内没有消息记录`；**消息 < 5 条 → `您在最近{days}天内的消息太少（仅{n}条），无法生成词云`**；异常 `个人词云生成失败: {err}`
- 天数标签：`当天`(1) / `三天`(3) / `七天`(7)，无匹配默认 1 天

### 清除艾特数据文案（逐字）

- 非群聊 → `此功能仅支持群聊使用`；收集未启用 → `消息收集功能未启用`
- `#清除艾特数据`：删除的是**当前用户被艾特**的记录；`deleted>0` → `已成功清除你的艾特记录`，否则 `你目前没有艾特记录`
- `#清除全部艾特数据`（主人）→ `已成功清除 {count} 条艾特记录`

## 2. 消息收集（collector）

### 过滤与处理顺序

1. 仅处理群消息。
2. 机器人账号过滤：`user_id` 匹配 `^3889\d{6}$` → 丢弃。
3. 顺序：解析消息段 → （rkey 更新可省略，见 §6.4）→ 有内容则入库 → 检查 pending at 补全 → 检查阈值触发部分分析 → 若消息含 at 则记录 at。

### 消息段解析（extractMessage）

| 段类型 | 处理 |
|---|---|
| `text` | 累加文本；统计 emoji（正则见下） |
| `at` | `atUsers.append(qq)` |
| `image` | summary 匹配 `动画表情\|表情\|sticker\|emoji`(i) → 归入 `faces.mface`（取 url）；否则入 `images`（受 `collectImages` 开关） |
| `reply` | `hasReply = true` |
| `face` | 取 id 入 `faces.face`；`faces.total++`（受 `collectFaces` 开关） |
| `json` | 解析 JSON 卡片（受 `collectLinks`）：先反转义 HTML 实体（`&#44; &#91; &#93; &amp; &lt; &gt; &quot;`），按 app 分类：`com.tencent.tuwen.lua` 或 view==='news' → `{type:'link', source: meta.tag||'unknown', title: meta.title||json.prompt||'', url: meta.jumpUrl}`；`com.tencent.miniapp_01` → `{type:'miniapp', source: meta.detail_1.title, title: meta.detail_1.desc||json.prompt, url: qqdocurl||url}`；`com.tencent.structmsg`+view==='music' → `{type:'music', source: meta.music.tag, title, url: jumpUrl||musicUrl}`；其他 → `{type:'json_other', source: app||'unknown', title: json.prompt||'', url: null}` |
| `video` | `{url, file, name}`（受 `collectVideos` 开关） |

- Emoji 正则：`[\U0001F600-\U0001F64F]|[\U0001F300-\U0001F5FF]|[\U0001F680-\U0001F6FF]|[\U0001F700-\U0001F77F]|[\U0001F780-\U0001F7FF]|[\U0001F800-\U0001F8FF]|[\U0001F900-\U0001F9FF]|[\U0001FA00-\U0001FA6F]|[\U0001FA70-\U0001FAFF]|[-☀-➿]|[⬀-⯿]`（即原作各 Unicode 区间的 Python 等价）
- 文本清洗：`re.sub(r'\[(.*?)\]', '', text).trim()`（会误删用户方括号内容，保留该行为）；超长截断 `text[:maxMessageLength] + '...'`（默认 500）
- nickname：`nicknameMode=='card'` ? `sender.card or sender.nickname` : `sender.nickname`

### 入库字段

```json
{"user_id": int, "nickname": str, "message": "文本或占位符'[表情]'", "time": 秒级时间戳,
 "timestamp": 毫秒, "hour": 0-23, "length": len(message), "hasReply": bool,
 "images": [...], "faces": {"face": [...], "mface": [...], "emoji": [计数...], "total": n},
 "links": [...], "videos": [...]}
```
保存条件：`text or faces.total>0 or images or links or videos`。

### 存储结构与保留（SQLite 复刻 Redis 语义）

- 消息按 `{groupId}:{YYYY-MM-DD}` 分桶存储；查询 `getMessages(groupId, days, targetDate)` 以 targetDate（默认今天）为基准向前 days 天拼接，**时间正序**；每条带 `expire_at`（首次写入该日桶时设 `retentionDays*86400`，默认 7 天；后续写入不刷新）。
- 报告表：key `{groupId}:{date}`，字段 `analysisData`(`{stats, topics, goldenQuotes, userTitles}`)、`messageCount`、`tokenUsage`、`lastUpdateTime`、`savedAt`、`date`；永久保存。
- 批次缓存表：key `{groupId}:{date}:{batchIndex}`，JSON `{batchIndex, startIndex, endIndex, messageCount, topics, goldenQuotes, tokenUsage, analyzedAt, success, retried?}` 或失败 `{batchIndex, success:false, error, analyzedAt, retried?}`；TTL 86400s。
- 冷却表：key `{groupId}:{date}`，`generatedAt/generatedBy/messageCount`，TTL 86400s。
- 生成锁：可用进程内 `asyncio.Lock` dict + 超时清理（原 Redis SET NX EX 300；单机 NoneBot 用内存锁即可）。
- at 记录：`{groupId}_{userId}_{time}` 为 key（userId=**被艾特者**），字段 `user_id, nickname, message, images, faces, time, messageId, contextMessages, endTime, nextMessages`；`expire_at = atTime + atRetentionHours*3600`（默认 24h），≤0 时不保存但 pending 仍写（保留原作行为）。
- at pending：key `{groupId}_{发起者userId}`，member=recordId，`expireTime = e.time + nextMessageTimeout`（默认 300s）。

### 阈值触发部分分析

- 白名单 `schedule.whitelist` 非空且群不在其中 → 不触发。
- 当日消息数 `% maxMessages(默认1000) === 0` 时触发批次 `batchIndex = count/maxMessages - 1`；若该批次缓存不存在 → 后台异步 `triggerPartialAnalysis`。
- 部分分析：范围 `[max(0, start-50), end)`（上下文重叠固定 50）；轻量 stats（以 nickname 去重的 users 列表）；**并行**跑话题+金句；写批次缓存（成功 `success:true`，失败 `success:false` 无 retried），TTL 86400s。

## 3. AI 服务与分析器

### AIService

- AsyncOpenAI(api_key, base_url, timeout)；无 apiKey → 服务不可用（分析器全部停用），warn 日志。
- `chat(prompt, temperature=0.7, timeout=llm_timeout(默认100s))`：用 `asyncio.wait_for` 实现秒级超时，超时抛 `AI 请求超时 ({timeout}秒)`。
- 请求：`chat.completions.create(model, temperature, messages=[{"role":"user","content":prompt}])` —— 单 user 消息、无 system、**不传 max_tokens**。
- 返回 `{content, usage:{prompt_tokens, completion_tokens, total_tokens}}`；content 为空时：`finish_reason=='length'` → 抛 `AI 响应被截断 (finish_reason=length)，已消耗 N tokens（其中推理 M），请提高服务商侧的输出长度上限`；否则抛 `AI 返回内容为空 (finish_reason=...)`。

### BaseAnalyzer

- `callAI(prompt, temperature)`：共尝试 `llm_retries+1 = 3` 次；失败后等 `llm_backoff * attempt` 秒（2s、4s）；全失败**返回 None 不抛异常**。
- `parseJSON(content)` 四级回退：① 直解；② 提取 ```` ```json ````/```` ``` ```` 代码块，否则 `\[...\]` 数组，否则 `\{...\}` 对象 → 直解 → 失败则 json_repair 后解；③ 对原文 json_repair 后解；④ 返回 None。
- `formatMessages(messages, include_time, filter)` → `(formatted_text, user_map)`：
  - 每行 `[HH:MM] [user_id]: 消息`（include_time=False 省略时间）；**始终用 user_id 不用昵称**；user_map 记录 user_id→最新昵称。
  - 追加链接占位 ` [小程序:来源]`/`[链接:来源]`（source 缺省 '分享'）、` [图片xN]`、` [视频xN]`；**不写表情**。

### 三个分析器的 prompt（必须逐字保留，见原仓库文件核对）

- `services/analyzers/TopicAnalyzer.js`：话题提取，temperature 0.7，includeTime=true。后处理：过滤缺 `topic`/`detail` 项；contributors 取前 5，映射 `{user_id, nickname}`；`detail` 中 `[数字ID]` 替换为 HTML 胶囊：
  `<span class="user-capsule"><img class="user-capsule-avatar" src="https://q1.qlogo.cn/g?b=qq&nk={userId}&s=100" alt="{nickname}" onerror="this.style.display='none'"><span class="user-capsule-name">{nickname}</span></span>`
- `services/analyzers/GoldenQuoteAnalyzer.js`：金句，temperature 0.8，includeTime=false；前置过滤（trim 后：长度 <5 或 >100、以 `#`/`/`/`.` 开头、纯数字空白、纯 URL、纯标点 → 丢弃）；后处理保留 `quote/sender/reason`，截 `maxQuotes(5)`。
- `services/analyzers/UserTitleAnalyzer.js`：称号+MBTI，temperature 0.9；输入为 `stats.users`（过滤 messageCount ≥ minMessages(5)，取前 20）；行为标签：nightRatio>0.3 夜猫子 / emojiRatio>0.5 表情包达人 / messageCount>users[0]*0.5 话痨 / avgLength>50 长文爱好者 / replyRatio>0.3 互动积极 / shareRatio>0.1 分享达人；后处理 mbti 大写，截 `maxTitles(9)`。

> **实现时必须打开原仓库这三个文件逐字复制 prompt 文本**，不要用本文件的转述。

### Token 统计

- 汇总 batch（含缓存批次） + topic + quote + title 的 usage（total_tokens>0 才累加）；渲染映射 `{prompt, completion, total}`。

## 4. 统计（StatisticsService）

单次遍历输出：

```
basic: {totalMessages, totalUsers, totalChars, totalEmojis, totalReplies, replyRatio,
        avgCharsPerMsg(1位小数), dateRange:{start, end}}  # zh-CN 本地日期
users: [{user_id, nickname, messageCount, charCount, emojiCount, replyCount, nightCount,
         linkCount, videoCount, hourlyDistribution[24], avgLength(1位), emojiRatio/replyRatio/
         nightRatio/shareRatio(2位), mostActiveHour}]
hourly: {hourlyCount[24], hourlyActivity[24], peakHour, peakCount, peakPeriod="HH:00-HH:00"}
emoji: {face, mface, emoji, total}
links: {total, bySource}
videos: int
topUsers: users 按 messageCount 降序 + rank
```

- 夜间：hour ∈ [night_start_hour, night_end_hour)，默认 [0,6)。
- 活跃度分级：count==0→none；ratio=count/peakCount ≥0.7→high、≥0.4→medium、否则 low。
- peakHour 取首个最大值下标。

### ActivityVisualizer.prepareChartData

```
{hours:[{hour, count, heightPercent=count/peak*100, activityLevel, activityRatio, isPeak}],
 peakHour, peakCount, peakNextHour=(peakHour+1)%24}
```

## 5. 报告生成编排（performAnalysis）

1. `stats = analyze(messages)`。
2. `len(messages) < min_messages_threshold(20)` → 返回 `{stats, topics:[], goldenQuotes:[], userTitles:[], skipped:true, reason:'消息数不足 (需要至少 N 条)'}`，不调 AI。
3. 增量/批次模式（`groupId && date && len > maxMessages && days==1`）：
   - `completedBatches = len // maxMessages`，`remaining = len % maxMessages`。
   - forceRegenerate → 所有完整批次视为 missing；否则读批次缓存：`success` → 收入并累计 token；`success:false` 无 `retried` → 重试；有 `retried` → 跳过；解析失败 → 重试；不存在 → missing。
   - 顺序补全 `[...missing, ...failed]` 排序后逐批：范围 `[max(0,start-50), end)`，并行话题+金句，成功写 `retried:true` 缓存、失败写 `success:false, retried:true`。
   - 有任意成功批次 → 增量模式：按 batchIndex 排序 mergeTopics/mergeGoldenQuotes；`remaining>0` 时增量消息 = 上一批末尾 50 条 + 剩余消息，再并行跑话题+金句后合并。
   - 任何异常 → 回退全量。
4. 全量：`len > maxMessages` → **只取最后 maxMessages 条**；并行话题+金句（各自 catch 降级 `[]`）；受 `analysis.topic.enabled`/`analysis.goldenQuote.enabled` 开关。
5. 用户称号始终全量实时算，受 `analysis.userTitle.enabled` 开关。
6. 返回 `{stats, topics, goldenQuotes, userTitles, skipped:false, useIncrementalAnalysis, tokenUsage}`。

### 合并算法

- mergeTopics：键 `topic.topic`；同名 → `existing.detail += "\n\n[后续]: " + detail`，contributors 按 `user_id or nickname` 去重追加；不限数量。
- mergeGoldenQuotes：去重键 `{sender.user_id or nickname or 'unknown'}_{quote}`；不限数量。

## 6. 渲染

### 模板

- 原作 art-template 模板在 `resources/summary-templates/{default,scrapbook}/` 与 `resources/wordcloud/`。**移植为 Jinja2 模板**（htmlrender 用 Jinja2），HTML/CSS 内容基本原样保留，仅转换语法（`{{each}}`→`{% for %}` 等），`{{@ detail}}` → `{{ detail | safe }}`。
- 报告模板字段（templateData）：`model, groupName, totalMessages, totalUsers, totalChars, totalEmojis, avgLength, dateRange, peakPeriod, totalLinks, linksBySource, totalVideos, enableActivityChart, activityChart, topics, goldenQuotes, userTitles, createTime, tokenUsage{prompt,completion,total}`。
- 词云模板字段：`groupName, timeRange('当天'/'近三天'/'近七天'), messageCount, createTime, wordListJson, width(1200), height(800), backgroundColor('#ffffff'), userName, isPersonal`。沿用原作 `wordcloud2.js`。
- 截图：htmlrender 整页截图（full page）；报告模板宽度 800(default)/1000(scrapbook)，词云 1200×800 canvas。
- 渲染失败 → 返回 None，调用方走文本兜底（历史报告）或提示文案。

### 文本兜底 sendTextSummary

```
📊 {dateLabel}群聊报告

消息总数: {totalMessages or messageCount}
参与人数: {totalUsers or 0}
日期: {queryDate}

💬 热门话题:
1. {topic}
...

🏆 群友称号:
• {user} - {title} ({mbti})

💎 群圣经:
1. "{quote}" —— {sender}
```

## 6.4 rkey 处理

原作刷新 QQ 图片 rkey 的机制（ImageRkeyManager）：NoneBot 侧简化——记录时直接存图片 url，展示时尝试原 url，失败则显示 `[图片xN]` 占位。可不实现 rkey 管理器。

## 7. @消息追踪

### 记录（handleAt）

- atall → 展开为全群成员（`get_group_member_list`）逐个建记录。
- 回复 ID：若事件有 reply 段，取被回复消息 id（OneBot: reply 段自带 id，无需 getChatHistory）。
- `isPureAt = not message.text.strip()`（清洗后）。
- 上下文消息仅纯@时收集：`contextMessageCount>0 and isPureAt` → 取该@之前、同一发起人的最近 N 条（时间倒序返回）。
- 每个被@用户写一条记录。
- 纯@且 `contextMessageCount>0` → 写 pending（`expireTime = time + nextMessageTimeout(300)`）。

### 补全（checkAndUpdatePendingAts）

- 仅在当前消息有内容时触发（纯 reply 段不算）。
- 取该群该发起人的未过期 pending（并清理过期）；`nextMessages` **恒只补 1 条**（复刻原作 break 行为）；格式 `{message, time, images, faces, links, videos}`；更新成功后从 pending 移除。

### 查询展示（whoAtMe）

- 目标用户：at 了 bot → bot 自己；at 了别人 → 被 at 者；否则自己。
- 无记录 → `目前还没有人艾特过`；未启用 → `消息收集功能未启用`。
- 用**合并转发**发送，每条记录按序：前文节点（`'💬 [前文]: '` 前缀，time 升序，跳过纯 `[表情]` 占位）→ 主消息节点（可带 reply 引用；文本空则 `[仅@]`）→ 后文节点（`'💬 [后文]: '` 前缀）。节点内容含 face/图片（失败 → `[图片xN]`）/动画表情（`[动画表情xN]`）/链接（`formatLinkText`）/视频（失败 → `[视频: file]`）；内容仅剩标注则跳过该节点。
- `formatLinkText`：图标 link→🔗 miniapp→📱 music→🎵 json_other→📄；`{icon}[{source or '未知来源'}] {title or '未知内容'}`，有 url 追加 `\n🔗 {url}`。
- 发送失败 → `发送消息失败，请查看日志`。

## 8. 词云生成

1. jieba 分词（HMM），加载 `userdict.txt`（数据目录下，用户自建，缺失仅 debug 日志）与 `stopwords.txt`（每行一词，`#` 注释）。
2. `cleanText`：去 `@提及`、去 `[...]`、去行首 `#`/`/`、合并空白。
3. 四层过滤：长度 < minLength(2)；停用词；**必须含中文** `[\u4e00-\u9fa5]`；词性过滤 loose `{u,y}` / standard(默认) `{u,y,d,c,r}` / strict `{u,y,d,c,r,q,t,f,m}`。
4. 权重：frequency 模式 → 过滤 `count >= minFrequency(2)`，词频降序取前 maxWords(100)，对数缩放归一到 1–10（`freqRange==0` 时权重恒 5）；tfidf 模式 → 以用户为文档，`idf=log((N+1)/(df+1))+1`，归一 0–1 后渲染时 `1+w*9`，忽略 minFrequency。
5. 模板数据见 §6；空结果 → 返回 None（`没有足够的词汇生成词云` 警告日志）。
6. 词云 HTML 自适应参数：词数 <20 → gridSize 12, baseFontSize width/25, minSize max(24,width/30)；<50 → 10, width/30, max(20,width/35)；<100 → 4, width/45, max(16,width/60)；≥100 → 2, width/65, max(12,width/80)。字体 `Noto Sans SC, Microsoft YaHei, PingFang SC, SimHei, sans-serif`，rotateRatio 0.2。

## 9. 定时任务

- 每日生成：固定 cron `59 23 * * *`（23:59），`schedule.enabled` 且白名单非空才执行；targetDate=当天；并发 `schedule.concurrency(3)`（asyncio.Semaphore）；单群消息 < `schedule.minMessages(99)` → 跳过；抢锁失败 → 跳过；成功后设冷却('scheduled')；`send.mode=='immediate'` 立即发送报告到群。
- 每日发送：`send.mode=='scheduled'` 时按 `{sendMinute} {sendHour} * * *`（默认 08:00）触发；targetDate=昨天；**串行**（并发=1）。
- 发送到群：从存储取报告 → 渲染 → 发图片；失败原因记录（report_not_found/group_not_found/render_failed）。

## 10. 配置项全表（默认值必须一致）

NoneBot `.env` 扁平键（`group_insight_` 前缀），括号内为默认：

- 保留：`retention_days`(7), `at_retention_hours`(24), `context_message_count`(1), `next_message_timeout`(300)
- AI：`ai_api_key`(''), `ai_model`('gpt-4.1'), `ai_base_url`(''), `ai_timeout`(60000ms), `ai_max_messages`(1000), `ai_llm_timeout`(100s), `ai_llm_retries`(2), `ai_llm_backoff`(2)
- 分析：`topic_enabled`(true), `golden_quote_enabled`(true), `max_golden_quotes`(5), `min_quote_length`(5), `max_quote_length`(100), `user_title_enabled`(true), `max_user_titles`(9), `min_messages_for_title`(5), `activity_enabled`(true), `min_messages_threshold`(20)
- 统计：`night_start_hour`(0), `night_end_hour`(6)
- 词云：`wordcloud_max_words`(100), `wordcloud_min_length`(2), `wordcloud_min_frequency`(2), `wordcloud_filter_strength`('standard'), `wordcloud_extract_method`('frequency'), `wordcloud_width`(1200), `wordcloud_height`(800), `wordcloud_background_color`('#ffffff')
- 收集：`collection_enabled`(true), `collect_images`(true), `collect_faces`(true), `collect_links`(true), `collect_videos`(true), `max_message_length`(500), `nickname_mode`('nickname')
- 定时：`schedule_enabled`(false), `schedule_whitelist`([]), `schedule_min_messages`(99), `schedule_concurrency`(3), `cooldown_minutes`(60), `send_mode`('disabled'), `send_hour`(8), `send_minute`(0)
- 报告渲染：`summary_template`('default'), `render_img_type`('png'), `render_quality`(100)

## 11. 易遗漏清单（移植 checklist）

1. 冷却只作用于"今天"报告；历史报告靠 messageDiff≤50 定型。
2. 历史重新生成不重置冷却。
3. 生成锁 300s、finally 释放、两种失败文案。
4. 批次缓存与阈值触发共用；已触发批次不重复消耗 token。
5. 失败批次只重试一次（retried 标记）；forceRegenerate 忽略所有缓存。
6. 上下文重叠固定 50 条。
7. remaining==0 时不调 AI，token 仍计入缓存批次。
8. <20 条消息跳过 AI 但基础统计仍生成入库。
9. 超量截断取**最后** maxMessages 条。
10. 消息桶 TTL 只在首次写入设置。
11. at 记录过期静默不保存但 pending 仍写。
12. nextMessages 恒 1 条。
13. pending 补全仅在有内容的消息上触发。
14. 纯@判定用清洗后文本。
15. atall 展开全群成员（大群写入量注意）。
16. 机器人过滤 `^3889\d{6}$`。
17. 称号分析依赖 `stats.users` 非 topUsers，talkative 基准 users[0]。
18. detail 的 HTML 胶囊必须 `| safe` 渲染。
19. parseJSON 四级回退完整复刻。
20. callAI 失败返回 None 不抛。
21. 不传 max_tokens；finish_reason=length 单独报错。
22. 个人词云最少 5 条消息。
23. userdict/stopwords 缺失时静默降级。
24. strict 过滤剔除数词/量词/时间词。
25. TF-IDF 忽略 minFrequency。
26. 发送时间改动需重启生效（apscheduler 任务在启动时注册）。
27. e.time 是秒；上下文查询 beforeTime 严格 `<`。
28. prompt 中一律用 user_id，不用昵称。
