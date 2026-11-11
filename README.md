# GitHub 雷达 · 每日好项目日报

每天自动抓取 GitHub 上值得关注的项目，生成一份网页日报 + Markdown，并弹出系统通知。
纯 Python 标准库，**无需安装任何依赖**，也不需要 GitHub 账号（可选 token）。

## 日报包含四个板块

| 板块 | 含义 | 数据来源 |
|---|---|---|
| 🔥 今日飙升 | 当天新增星数最多的项目 | github.com/trending（全站 + 你关注的语言） |
| 🌱 新星项目 | 近 30 天新建、按“日均增星”排序 | GitHub 搜索 API |
| 🚀 老牌项目重大更新 | 3000★ 以上项目在 36 小时内发布的**大版本/功能版本** | 各项目的 releases.atom |
| 📈 星标加速 | 不在 Trending 上但星数涨得快的项目 | 本地每日快照对比（第二天起有数据） |

- **NEW** 标记 = 该项目第一次出现在这个板块，方便每天只看新面孔。
- 重大更新会自动过滤补丁版（x.y.Z）、预发布（alpha/beta/rc）和旧分支维护版；
  距上一版不到 7 天的标为“例行”并排在后面。

## 使用

```bash
python3 ghradar.py --open        # 立即生成并打开日报（约 3 分钟）
./install_schedule.sh            # 安装每天 09:00 自动运行
./install_schedule.sh 08:30      # 自定义时间
./install_schedule.sh uninstall  # 卸载
```

日报位置：`reports/latest.html`（最新）、`reports/YYYY-MM-DD.html`、`reports/index.html`（历史）。
运行日志：`data/run.log`。

## ☁️ 电脑关机也能跑：部署到 GitHub Actions（免费）

GitHub 每天在云端帮你运行一次，日报发布成网页，并推送到微信 / 手机 / 邮箱。

1. 在 github.com 新建一个仓库（例如 `gh-radar`）。想用网页版日报就选 **Public**（免费账号的私有仓库不能开网页，但推送照常）。
2. 在本文件夹里执行（把地址换成你的仓库）：
   ```bash
   git init && git add . && git commit -m "init" && git branch -M main
   git remote add origin https://github.com/你的用户名/gh-radar.git
   git push -u origin main
   ```
3. 仓库 **Settings → Pages → Source** 选 **GitHub Actions**。
4. 仓库 **Settings → Secrets and variables → Actions → New repository secret**，按需添加推送方式（任选，可多选）：

   | 推送到 | 需要添加的 Secret |
   |---|---|
   | 微信 | `SERVERCHAN_KEY`（在 sct.ftqq.com 微信扫码登录获取 SendKey） |
   | iPhone 通知 | `BARK_URL`（Bark App 里复制，形如 `https://api.day.app/xxxx`） |
   | Telegram | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` |
   | 邮箱（完整日报） | `SMTP_HOST`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO`，可选 `SMTP_PORT`（默认 465）。QQ 邮箱：`smtp.qq.com`，密码填“授权码” |

5. 打开 **Actions → GitHub 雷达日报 → Run workflow** 手动跑一次试试。之后每天北京时间 9 点左右自动运行。

网页版日报：`https://你的用户名.github.io/gh-radar/latest.html`（历史：`/index.html`）。
每次运行会把日报和星标快照提交回仓库，所以“星标加速”在云端也能逐日累积。

## 配置（config.json）

| 键 | 说明 |
|---|---|
| `trending_languages` | 抓哪些语言的 Trending，`""` 表示全站 |
| `new_star_days` / `new_star_min_stars` | 新星的“新”和最低星数门槛 |
| `release_min_stars` / `release_window_hours` | 重大更新的星数门槛、时间窗口 |
| `release_include_patch` | 设为 `true` 则补丁版也列出 |
| `watchlist` | 你特别关注的项目，如 `["ollama/ollama", "astral-sh/uv"]`，有新版必检查 |
| `exclude_keywords` | 名称/简介包含这些词就过滤，如 `["awesome", "interview"]` |
| `notify` / `open_report` | 运行后是否弹通知 / 自动打开网页 |
| `github_token` | 可选。也可用环境变量 `GITHUB_TOKEN`。不填也能正常运行（云端运行时自动提供） |
