# 每日晨报机器人（Daily News Bot）部署指南

每天早上 **6:30** 自动收集**前一天**的新闻，经**三遍核对**后推送到你的手机：

- 🤖 AI·人工智能发展（国内外）
- 💰 金融·世界经济与全球金融中心（纽约/华尔街/伦敦/东京/香港/新加坡/法兰克福）
- 🌏 地缘政治·国际局势
- 🏢 科技·全球科技公司动态（OpenAI/谷歌/微软/英伟达/Meta/苹果/特斯拉/华为/百度/阿里/腾讯/字节/小米/DeepSeek 等）

**三遍核对机制（内置，全自动）：**
1. 第一遍·来源交叉验证：同一事件须被 ≥2 个独立来源报道，或来自权威媒体白名单（新华社、路透、彭博、财新、BBC 等），否则丢弃；
2. 第二遍·关键词精确匹配：标题/摘要必须真实命中栏目关键词，清除搜索引擎误配项与导航页/专题页；
3. 第三遍·日期与内容复核：发布时间必须落在"前一天"窗口内（严格模式），URL 合法、去重、剔除异常条目。

每条核对统计都写入 `logs/` 运行日志；每份日报保存在 `reports/` 目录。

---

## 一、文件说明

| 文件 | 作用 |
|---|---|
| `collect_news.py` | 主程序（仅依赖 Python 3.9+ 标准库，无需 pip 安装任何包） |
| `config.json` | 配置文件：推送通道、新闻源、关键词（可自行增删） |
| `deploy/daily-news.yml` | GitHub Actions 云端定时方案（无需自己的服务器） |
| `示例日报.md` | 实际运行生成的日报样例 |

## 二、第一步：配置手机推送通道（选一种即可）

编辑 `config.json` 的 `"push"` 段：

| 通道 | 适合 | 设置方法 |
|---|---|---|
| **Bark**（推荐，iPhone） | 免费秒推、系统级通知 | App Store 装 Bark → 复制 App 内的推送 URL，填入 `"bark_url": "https://api.day.app/你的Key"`，`"channel": "bark"` |
| **Server酱**（微信） | 免费版每天限 5 条 | sct.ftqq.com 微信扫码登录 → 复制 SendKey 填入 `"serverchan_sendkey"`，`"channel": "serverchan"` |
| **PushPlus**（微信） | 简单 | pushplus.plus 登录 → 复制 token 填入 `"pushplus_token"`，`"channel": "pushplus"` |
| **Telegram** | 需科学上网 | @BotFather 建 bot 拿 token，给 bot 发一条消息后填入两个参数，`"channel": "telegram"` |
| **邮件** | 任意手机 | 填 SMTP 信息（QQ 邮箱用授权码），`"channel": "email"` |

测试推送是否打通：
```bash
python3 collect_news.py --dry-run    # 先不推送，检查采集与日报
python3 collect_news.py              # 正式运行并推送一次
```

## 三、第二步：设置每天 6:30 定时运行

### 方案 A：自己的电脑 / 服务器（Linux、macOS、NAS）

```bash
crontab -e
# 添加一行（把路径换成你的实际路径）：
30 6 * * * cd /path/to/daily-news-bot && /usr/bin/python3 collect_news.py >> logs/cron.log 2>&1
```
> 服务器若是 UTC 时区，请写 `30 22 * * *`（UTC 22:30 = 北京 6:30）。

### 方案 B：Windows 电脑

任务计划程序 → 创建基本任务 → 每天触发 `6:30` → 操作填：
```
程序：python
参数：C:\path\to\daily-news-bot\collect_news.py
起始于：C:\path\to\daily-news-bot
```

### 方案 C：GitHub Actions 云端定时（零服务器，推荐没有常开电脑的人）

1. 新建一个 **私有** GitHub 仓库，把本目录所有文件上传（结构保持 `collect_news.py`、`config.json` 在仓库根目录，`daily-news.yml` 放到 `.github/workflows/`）；
2. 推送通道的密钥不要明文提交：在仓库 Settings → Secrets 添加（如 `BARK_URL`），并按 yml 注释调整；
3. 该工作流每天 UTC 22:30（北京时间 6:30）自动运行并推送；
4. 也可在 Actions 页面手动 Run workflow 测试。

> ⚠️ GitHub Actions 的定时触发可能有几分钟到半小时延迟，介意 punctuality 请优先用方案 A。

## 四、自定义收集范围

编辑 `config.json`：
- **加新闻源**：在对应栏目 `feeds` 数组里加 RSS 地址；
- **加搜索词**：在 `google_news_queries` 里加查询词（自动经 Google News RSS 拉取）；
- **调严格度**：`min_sources_per_event`（默认 2 个来源交叉确认）、`authoritative_domains`（权威来源白名单）；
- **时区**：`timezone` 默认 `Asia/Shanghai`。

## 五、常见问题

- **某天没收到推送？** 查 `logs/` 最新日志：可能是网络问题或当日无通过核对的新闻（严格模式下，宁可少发也不发不准的新闻）。
- **想要条数多一点？** 把 `min_sources_per_event` 改为 1，或在运行命令加 `--lenient-date`（允许"近日"新闻并明确标注）。
- **能收集哪几天的新闻？** 默认收"昨天"（以 config 的时区为准）；可用 `--date 2026-10-08` 补收任意一天。
- **隐私**：脚本不采集任何个人数据，推送只含新闻标题、来源与链接。
