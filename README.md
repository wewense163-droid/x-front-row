# X 中文区大号盯帖抢评助手（x-front-row）

盯住一批 X（推特）中文区大号的新帖，第一时间把「新鲜出炉」的原创帖推到你面前，方便你抢前排评论。

## 它做什么

- 按你给的账号名单，轮询每个账号的 nitter RSS（`https://nitter.cf/<handle>/rss`）。
- 只认**新鲜窗口内**（默认 60 分钟）发布的**新原创帖**，转发（RT）不算。
- 发现新帖立刻在控制台打印，并追加写入 `alerts.md`，包含作者、时间、x.com 原帖链接和全文。
- 用 `state.json` 记录已见过的帖子，重启、重复运行都不会重复告警。

## 为什么做这个

X 官方页面在很多网络环境下打不开、也不适合程序化轮询；nitter 镜像提供公开 RSS，稳定、免费、无需登录。这个工具只读公开信息，不登录任何账号、不发帖、不点赞，零封号风险。

实现上是**单文件 + Python 3 标准库**（urllib + xml.etree），不装任何 pip 包，下载下来就能跑。

## 快速开始（3 步）

```bash
# 1. 复制一份配置
cp config.example.json config.json

# 2. 编辑 config.json，把 handles 改成你要盯的账号（不带 @）

# 3. 先单跑一轮试试
python3 x_front_row.py --config config.json --once

# 确认没问题后，循环运行（默认每 10 分钟一轮，Ctrl+C 停止）
python3 x_front_row.py --config config.json
```

新帖告警会同时出现在控制台和 `alerts.md` 里。

## 配置说明（config.json）

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `handles` | （示例名单） | 要盯的 X 账号列表，不带 `@` |
| `freshness_minutes` | `60` | 新鲜窗口：发布超过这个分钟数的帖不再告警 |
| `poll_interval_minutes` | `10` | 循环模式下每轮间隔分钟数（`--once` 时忽略） |
| `nitter_base` | `https://nitter.cf` | nitter 镜像地址，镜像不可用时可换其他实例 |
| `state_file` | `state.json` | 去重状态文件路径（自动生成，已 gitignore） |
| `alerts_file` | `alerts.md` | 告警追加写入的文件路径 |
| `request_timeout_seconds` | `20` | 单个 RSS 请求超时秒数 |

## 工作方式小结

- 每轮逐个账号抓 RSS，单个账号失败只跳过它，不影响其他账号。
- 所有扫到的帖子（包括旧帖、转发）都会记入 `state.json` 的已见名单（每账号最多保留最近 200 条），只有「没见过 + 原创 + 在新鲜窗口内」的帖才告警。
- 转发判定：nitter RSS 中转发帖标题以 `RT by` 开头，直接跳过。

## 路线图

- **回复草稿钩子（已预留，未启用）**：脚本里有 `draft_replies(post)` 函数，默认返回空列表。只有当环境变量 `X_FRONT_ROW_API_KEY` 被设置时才视为「已配置」，届时可在函数内的 TODO 处接入任意大模型 API，让告警里自动附上几条回复草稿。v1 不做这件事，是因为草稿质量和风格需要人工把关，先保证「发现得快」这件事可靠。
- 告警通道扩展：企业微信 / Telegram / 邮件推送。
- 关键词过滤：只告警命中指定话题（如财经、AI）的帖。
- 多镜像自动切换：主镜像挂了自动换备用实例。

## 许可

MIT，见 [LICENSE](LICENSE)。
