# Google Trends × Reddit TCM 问题需求：自动抓取方法

## 1. 每次运行的顺序

`scripts/run_demand_report.py` 是唯一执行入口。它依次完成：

1. 固化本轮配置和 `taxonomy.json`；
2. 对 Google Trends 分批发起实时请求并保存原始响应；
3. 检查 Reddit 登录态，然后用当前可用的只读后端搜索并保存原始帖子；
4. 统一问题标签、去重、计算两端声量和两段4周走势；
5. 运行数据质量门槛；
6. 只有全部通过才生成报告；正式报告写入 `reports/report_YYYY-MM-DD.md`。

每次运行都把原始响应和计算审计写入独立的 `data/runs/YYYY-MM-DDTHHMMSSZ/\`，旧运行不会被覆盖。正式报告统一写入 `reports/`，名称为 `report_YYYY-MM-DD.md`；同一天的最新合格正式运行覆盖当天报告。`references/` 只保存稳定的方法、配置和词典。程序不读取历史排名参与计算。

## 2. 关键词如何产生

关键词不是在每轮根据结果临时联想，而是保存在版本化的 `references/taxonomy.json` 中。每个问题包含：

- 中文统一标签和问题大类；
- 一个主要 Google 英文搜索词；
- Reddit 中自然表达的同义词、症状描述和缩写；
- 是否进入快速连通测试。

新增关键词应来自 Google Related Queries、Autocomplete、People Also Ask、Reddit 帖子实际措辞或人工审核，但发现词本身不计入热度。修改后必须升级 taxonomy version，并让两端同时重跑，避免一边使用新词、一边沿用旧口径。

Google 主榜使用普通问题词，如 `insomnia`，回答“整体 Google 用户搜索什么问题”；不会强制加 `TCM`。Reddit 搜索则把同一个问题的自然表达与中医语境词组合，回答“中医相关讨论里在谈什么问题”。

## 3. Google Trends 技术实现

### 条件

- 地域：全球（`geo=""`）；
- 语言：英文关键词；
- 搜索类型：Web Search；
- 类别：All categories；
- 主窗口：滚动最近3个月（`today 3-m`）；
- 趋势：从该3个月时间序列中取最近4个完整周，对比再前4个完整周。

### 抓取

程序调用 Google Trends 网页所使用的实时响应：

1. `/trends/api/explore` 获取本批查询的 `TIMESERIES` widget 和临时 token；
2. `/trends/api/widgetdata/multiline` 获取逐周0–100时间序列；
3. 将每批原始 explore 与 timeline JSON、请求词和可复现 Trends URL 全部保存。

这不是 Google Search 结果数，也不是第三方 SEO 搜索量。若返回 429、空时间线或 token 缺失，整轮停止。

### 跨批校准

Google Trends 的0–100只在单次比较内部成立。每批最多4个问题词，另加共同锚点 `headache`：

```text
calibrated_interest(q) = mean(q weekly interest) / mean(anchor weekly interest)
google_share(q) = calibrated_interest(q) / sum(all calibrated interests)
```

最近4周和前4周分别做同样的锚点校准及份额归一化，再计算：

```text
google_change = recent_share / prior_share - 1
```

完整运行会检查周数和零值率。若大量长尾词被强锚点压成0，应新增桥接锚点逻辑并记录换算链，而不是估算。

## 4. Reddit 技术实现

### 条件

- 范围：Reddit-wide；
- 语言：英文；
- 主窗口：搜索后端用 `year` 做发现，再按帖子时间严格过滤为滚动最近3个日历月；对 `new` 结果翻页直至越过三个月截止时间或达到配置页数上限；
- TCM 语境：Chinese medicine、TCM、acupuncture、Chinese herbs、cupping、gua sha、moxibustion、tui na、acupressure；
- 问题词：同一 taxonomy 中的 Reddit patterns。

### 登录与后端

程序先运行 `agent-reach doctor --json`：

- OpenCLI 已连接时用 `opencli reddit search`；
- 否则仅在 `rdt status` 明确返回 `authenticated: true` 时使用 rdt-cli；
- Cookie 过期、未认证、网络失败或返回字段不全时停止，不调用匿名 JSON，不回填旧报告。

### 搜索与去重

程序不把全部中医词和问题词塞进一个大型 Boolean query，因为 Reddit 会宽松解释复杂表达式，造成大量噪声。它先对每个中医语境词分别搜索，建立本轮 TCM 语料池：

```text
"traditional Chinese medicine"
"Chinese medicine"
acupuncture
"Chinese herbs"
cupping / "gua sha" / moxibustion / "tui na" / acupressure
```

每个语境词分别按 `top` 和 `new` 搜索；年度过滤只是 Reddit 后端允许的发现窗口，最终排名只纳入本轮采集时点往前3个日历月的帖子。`new` 排序会翻页直到越过三个月截止时间或达到配置页数上限。原始响应按“语境词×排序×页码”保存。所有结果先按 Reddit post ID 去重，再在本地用同一 taxonomy 的问题词分类。所有问题词和缩写按完整词或完整短语匹配，避免 `gas` 命中 `games`、`EDS` 命中普通单词片段。问题词与明确的中医/疗法词必须在同一段近邻文本（默认相距不超过400字符），单独出现的歧义缩写 `TCM` 不足以通过资格检查。随后再检查：

1. 必须出现中医或具体疗法语境；
2. 必须匹配至少一个具体问题，并与中医语境近邻共现；
3. 必须出现个人问题、求助、症状或提问信号；
4. 排除招生、执照、诊所营销、招聘、娱乐剧情、宠物和故事搬运社区等非目标需求；
5. 一个帖子可匹配多个问题，但同一问题只贡献一次。

### Reddit 热度

本轮合格帖子重新计算 score 和评论数 P95，截尾、对数化后各占50%：

```text
thread_heat =
  0.5 × log(1 + min(score, score_P95)) / log(1 + score_P95)
+ 0.5 × log(1 + min(comments, comments_P95)) / log(1 + comments_P95)

reddit_heat_share(problem)
  = sum(thread_heat for the problem)
  / sum(thread_heat across all problem-label matches)
```

Reddit score 是净投票近似值，不是原始点赞数。两段4周各自重新计算份额后得到环比；合计不足5个匹配帖子标为低基数。

## 5. 两个平台为什么不能使用同一种抓法

Google Trends 返回的是按地点和时间归一化后的查询兴趣序列，没有“帖子”和互动数据，因此重点是同批比较、锚点校准和时间序列。

Reddit 返回的是离散帖子，必须依赖登录态搜索，随后进行文本筛选、去重、多标签分类，并用 score 与评论构造讨论热度。Google 的一个数据点代表搜索兴趣；Reddit 的一个数据点代表一条具体讨论，两者不能混用原始数值。

## 6. 输出比较

两端只比较各自问题集合内的份额、排名、环比、排名差、Spearman 相关和 Top-N 重合。不得相加成为“总热力值”。主表按 Google 排名排序，Google 列在 Reddit 前。

## 7. 质量门槛

发布前必须满足：

- Google 每个词有足够周数，整体零值率不超过配置阈值；
- Reddit 登录态有效，完整运行至少达到配置中的合格帖子数；
- 两端时间戳属于本轮，taxonomy version 相同；
- 每个排名能追溯到本轮原始响应；
- P95、分母、排名均为本轮重算；
- 决定性链接可审计。

任一关键条件失败时，只保留 manifest、原始响应和 failure.json，不生成或覆盖最新报告。

## 8. 时间窗口与更新频率

两端主排名都是滚动最近3个月；环比使用两段完整4周。相比一年窗口，它会更快反映季节和突发变化，但日内重复刷新仍没有意义。

建议默认每周运行一次并固定在同一工作日。需要监测突发事件时，可额外做每日 smoke/专项词运行，但不要用它替代每周完整榜。
