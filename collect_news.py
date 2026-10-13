#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日晨报自动化采集脚本
======================
每天定时运行，收集【前一天】的新闻：
  1. 人工智能发展（国内外）
  2. 世界金融变化（全球各金融中心）
  3. 地缘政治动态
  4. 国内外大型科技/AI公司动态

内置【三遍核对】机制：
  第一遍：多来源交叉验证 —— 同一事件须被 >=2 个独立来源报道，
          或来自权威来源白名单，否则丢弃；
  第二遍：关键词精确匹配 —— 标题/摘要必须真实命中所属栏目关键词，
          清除搜索引擎误配项；
  第三遍：日期与内容一致性复核 —— 发布时间必须落在"前一天"窗口内、
          URL 合法、去重、剔除异常条目；全部核对统计写入运行日志。

采集后端：
  --backend rss  （默认，仅用 Python 标准库，适合部署在自己电脑/服务器）
  --backend zai  （使用 z-ai CLI 的 web_search，适合受限网络环境）

推送通道（config.json 中 choose）：
  bark / serverchan / pushplus / telegram / email / none

用法：
  python3 collect_news.py                 # 正常运行：采集+核对+推送
  python3 collect_news.py --dry-run      # 只采集核对生成日报，不推送
  python3 collect_news.py --backend zai  # 指定搜索后端
  python3 collect_news.py --date 2026-10-08  # 指定收集某一天
仅依赖 Python 3.9+ 标准库。
"""

import argparse
import concurrent.futures
import configparser
import hashlib
import html as html_mod
from html.parser import HTMLParser
import json
import logging
import re
import smtplib
import subprocess
import sys
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone as dt_timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("newsbot")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        fh = logging.FileHandler(log_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "timezone": "Asia/Shanghai",
    "push_hour": 6,
    "push_minute": 30,
    "backend": "rss",
    "max_items_per_feed": 40,
    "min_sources_per_event": 2,
    "authoritative_domains": [
        "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com",
        "bbc.com", "nytimes.com", "apnews.com", "economist.com",
        "xinhuanet.com", "news.cn", "people.com.cn", "thepaper.cn",
        "caixin.com", "yicai.com", "21jingji.com", "jiemian.com",
        "zaobao.com", "dw.com", "voachinese.com", "rfi.fr",
        "nature.com", "science.org", "technologyreview.com",
    ],
    "push": {
        "channel": "bark",
        "bark_url": "https://api.day.app/你的BarkKey",
        "serverchan_sendkey": "SCTxxxx",
        "pushplus_token": "xxxxxxxx",
        "telegram_bot_token": "123456:ABC",
        "telegram_chat_id": "123456789",
        "email": {
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "username": "you@example.com",
            "password": "授权码",
            "to_addr": "you@example.com",
        },
    },
    "categories": {
        "ai": {
            "title": "AI·人工智能发展",
            "keywords": ["人工智能", "AI", "大模型", "机器学习", "深度学习", "生成式", "AGI",
                         "artificial intelligence", "LLM", "machine learning", "deep learning",
                         "generative", "chatbot", "transformer", "神经网络", "算法", "算力", "芯片"],
            "google_news_queries": [
                {"q": "人工智能 OR 大模型 OR 生成式AI", "lang": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
                {"q": "artificial intelligence OR \"AI model\" OR LLM", "lang": "en-US", "gl": "US", "ceid": "US:en"},
            ],
            "feeds": [
                "https://www.jiqizhixin.com/rss",
                "https://www.technologyreview.com/feed/",
                "https://venturebeat.com/category/ai/feed/",
            ],
        },
        "finance": {
            "title": "金融·世界经济与金融中心",
            "keywords": ["金融", "股市", "美联储", "央行", "汇率", "利率", "债券", "IPO", "资本",
                         "经济", "通胀", "贸易", "华尔街", "交易所", "证券", "货币", "降息", "加息",
                         "stock market", "Federal Reserve", "central bank", "finance", "inflation",
                         "interest rate", "bond", "currency", "Wall Street", "economy", "recession"],
            "google_news_queries": [
                {"q": "全球股市 OR 美联储 OR 央行 OR 金融市场", "lang": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
                {"q": "纽约股市 OR 华尔街 OR 伦敦金融 OR 东京股市 OR 香港股市 OR 新加坡金融 OR 法兰克福", "lang": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
                {"q": "stock market OR Federal Reserve OR global finance OR central bank", "lang": "en-US", "gl": "US", "ceid": "US:en"},
                {"q": "Wall Street OR London financial OR Tokyo market OR Hong Kong exchange OR Singapore finance", "lang": "en-US", "gl": "US", "ceid": "US:en"},
            ],
            "feeds": [
                "https://feeds.bbci.co.uk/news/business/rss.xml",
                "https://www.cnbc.com/id/10001147/device/rss/rss.html",
                "https://finance.yahoo.com/news/rssindex",
                "https://www.yicai.com/rss/feed.xml",
            ],
        },
        "geo": {
            "title": "地缘政治·国际局势",
            "keywords": ["地缘政治", "制裁", "外交", "局势", "冲突", "谈判", "峰会", "条约", "军事",
                         "选举", "关税", "出口管制", "国际", "联合国", "双边", "协议",
                         "geopolitics", "sanctions", "diplomatic", "conflict", "summit",
                         "tariff", "export control", "treaty", "election", "military", "war"],
            "google_news_queries": [
                {"q": "地缘政治 OR 国际局势 OR 制裁 OR 外交", "lang": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
                {"q": "geopolitics OR sanctions OR diplomacy OR international tensions", "lang": "en-US", "gl": "US", "ceid": "US:en"},
            ],
            "feeds": [
                "https://feeds.bbci.co.uk/news/world/rss.xml",
                "https://www.zaobao.com/rss/rss-china",
            ],
        },
        "tech": {
            "title": "科技·全球科技公司动态",
            "keywords": ["OpenAI", "Anthropic", "谷歌", "Google", "微软", "Microsoft", "Meta",
                         "英伟达", "Nvidia", "苹果", "Apple", "特斯拉", "Tesla", "亚马逊", "Amazon",
                         "华为", "百度", "阿里", "腾讯", "字节跳动", "小米", "DeepSeek", "比亚迪",
                         "中芯", "京东", "美团", "宁德时代", "台积电", "三星", "台积",
                         "发布", "营收", "财报", "市值", "收购", "融资", " lawsuit"],
            "google_news_queries": [
                {"q": "OpenAI OR Anthropic OR 谷歌 OR 微软 OR 英伟达 OR Meta OR 苹果 OR 特斯拉", "lang": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
                {"q": "华为 OR 百度 OR 阿里巴巴 OR 腾讯 OR 字节跳动 OR 小米 OR DeepSeek", "lang": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
                {"q": "OpenAI OR Anthropic OR Nvidia OR Google OR Microsoft OR Meta OR Apple OR Tesla", "lang": "en-US", "gl": "US", "ceid": "US:en"},
                {"q": "Huawei OR Alibaba OR Tencent OR Baidu OR ByteDance OR Xiaomi OR DeepSeek OR TSMC", "lang": "en-US", "gl": "US", "ceid": "US:en"},
            ],
            "feeds": [
                "https://36kr.com/feed",
                "https://www.theverge.com/rss/index.xml",
                "https://techcrunch.com/feed/",
            ],
        },
    },
}


def load_config(path: Path) -> dict:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 文件缺失 / 编码错误 / JSON 损坏：回退内置默认配置，避免定时任务崩溃
        print(f"[警告] 读取配置失败（{exc}），改用内置默认配置", file=sys.stderr)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(cfg, dict):
        print("[警告] 配置文件内容不是对象，改用内置默认配置", file=sys.stderr)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # 浅合并默认值，保证旧配置文件兼容
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


# ---------------------------------------------------------------------------
# 时间窗口：目标日（默认=昨天，本地时区）
# ---------------------------------------------------------------------------
def target_window(cfg: dict, date_str: str | None):
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    if date_str:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    else:
        day = (now - timedelta(days=1)).date()
    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start, end, tz, day


# ---------------------------------------------------------------------------
# RSS / Atom 抓取与解析（仅标准库）
# ---------------------------------------------------------------------------
def http_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; DailyNewsBot/1.0)",
        "Accept": "application/rss+xml, application/xml, application/atom+xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"(缺少字词：|必须包含：).*$", " ", text)  # 清除搜索引擎提示残留
    return re.sub(r"\s+", " ", text).strip()


# 导航页/专题页/聚合页特征（非具体新闻，剔除）
LANDING_PATTERNS = re.compile(
    r"(latest news and updates|news & events|press releases"
    r"|topics/|/topic/|/tag/|/tags/|关键词/|/keyword)",
    re.I,
)


def parse_feed(xml_bytes: bytes, limit: int) -> list[dict]:
    """解析 RSS2.0 / Atom，返回统一结构条目列表。"""
    items = []
    root = ET.fromstring(xml_bytes)
    ns_atom = "{http://www.w3.org/2005/Atom}"
    if root.tag == "feed" or root.find(f"{ns_atom}entry") is not None:  # Atom
        entries = root.findall(f"{ns_atom}entry")
        for e in entries[:limit]:
            title = e.findtext(f"{ns_atom}title") or ""
            link = ""
            le = e.find(f"{ns_atom}link")
            if le is not None:
                link = le.get("href") or le.text or ""
            summary = e.findtext(f"{ns_atom}summary") or e.findtext(f"{ns_atom}content") or ""
            pub = e.findtext(f"{ns_atom}updated") or e.findtext(f"{ns_atom}published") or ""
            items.append({"title": strip_html(title), "link": (link or "").strip(),
                          "summary": strip_html(summary)[:600], "date_raw": pub})
    else:  # RSS
        for it in root.iter("item"):
            if len(items) >= limit:
                break
            title = it.findtext("title") or ""
            link = it.findtext("link") or ""
            if not link:
                guid = it.findtext("guid") or ""
                link = guid if guid.startswith("http") else ""
            desc = it.findtext("description") or it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
            pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
            items.append({"title": strip_html(title), "link": (link or "").strip(),
                          "summary": strip_html(desc)[:600], "date_raw": pub})
    return items


def parse_date(date_raw: str):
    if not date_raw:
        return None
    try:
        dt = parsedate_to_datetime(date_raw)  # RFC822
        if dt is None:
            raise ValueError
    except (TypeError, ValueError):
        try:  # ISO8601
            dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# 采集后端
# ---------------------------------------------------------------------------
def fetch_via_rss(cfg: dict, logger) -> list[dict]:
    raw = []
    limit = cfg.get("max_items_per_feed", 40)
    for cat_id, cat in cfg["categories"].items():
        urls = list(cat.get("feeds", []))
        for q in cat.get("google_news_queries", []):
            qs = urllib.parse.quote(q["q"])
            urls.append(f"https://news.google.com/rss/search?q={qs}&hl={q['lang']}&gl={q['gl']}&ceid={q['ceid']}")
        for url in urls:
            try:
                data = http_get(url)
                for it in parse_feed(data, limit):
                    it["category"] = cat_id
                    raw.append(it)
                logger.info(f"[rss] OK  {cat_id}: {url[:90]}")
            except Exception as exc:
                logger.warning(f"[rss] 失败(跳过) {cat_id}: {url[:90]} -> {exc}")
    return raw


def fetch_via_zai(cfg: dict, logger, target_day) -> list[dict]:
    """使用 z-ai CLI 的 web_search（受限网络环境/本工作区的备用后端）。"""
    raw = []
    day_s = target_day.strftime("%Y-%m-%d")
    for cat_id, cat in cfg["categories"].items():
        for q in cat.get("google_news_queries", []):
            query = f'{q["q"]} {day_s} 新闻'
            try:
                out = BASE_DIR / "tmp_zai_search.json"
                args = json.dumps({"query": query, "num": 15, "recency_days": 3}, ensure_ascii=False)
                subprocess.run(
                    ["z-ai", "function", "-n", "web_search", "-a", args, "-o", str(out)],
                    check=True, capture_output=True, timeout=90,
                )
                results = json.loads(out.read_text(encoding="utf-8"))
                for r in results if isinstance(results, list) else []:
                    raw.append({
                        "title": strip_html(r.get("name", "")),
                        "link": r.get("url", ""),
                        "summary": strip_html(r.get("snippet", ""))[:400],
                        "date_raw": r.get("date", "") or "",
                        "category": cat_id,
                    })
                logger.info(f"[zai] OK  {cat_id}: {query[:80]} ({len(results)} 条)")
            except Exception as exc:
                logger.warning(f"[zai] 失败(跳过) {cat_id}: {query[:80]} -> {exc}")
    return raw


# ===========================================================================
# 三遍核对机制
# ===========================================================================
def _norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"[-_–—|/\\·,，。.！!？?：:;；\"'“”‘’()\[\]{}<>【】]", " ", t)
    # 去掉常见媒体后缀
    t = re.sub(r"-\s*[^-]{2,20}$", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(t: str) -> set:
    toks = set(re.findall(r"[a-z0-9]+", t))          # 英文词
    toks |= {t[i:i + 2] for i in range(0, max(0, len(t) - 1))}  # 中文 bigram
    return toks


def _similar(a: str, b: str) -> float:
    ta, tb = _tokens(_norm_title(a)), _tokens(_norm_title(b))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb))


def _host_is_authoritative(host: str, auth: tuple) -> bool:
    """仅精确域名或其子域才算权威（避免 sputniknews.cn 误配 news.cn）。"""
    return any(host == a or host.endswith("." + a) for a in auth)


def pass1_crosscheck(items: list[dict], cfg: dict, logger) -> tuple[list[dict], dict]:
    """第一遍：多来源交叉验证。"""
    min_sources = cfg.get("min_sources_per_event", 2)
    auth = tuple(cfg.get("authoritative_domains", []))
    # 按 URL 指纹先去重
    seen, uniq = set(), []
    for it in items:
        fp = hashlib.md5((it["link"] or it["title"]).encode()).hexdigest()
        if fp not in seen:
            seen.add(fp)
            uniq.append(it)

    # 标题相似聚类
    clusters: list[list[dict]] = []
    for it in uniq:
        placed = False
        for cl in clusters:
            if _similar(cl[0]["title"], it["title"]) >= 0.55:
                cl.append(it)
                placed = True
                break
        if not placed:
            clusters.append([it])

    kept, dropped = [], 0
    for cl in clusters:
        hosts = {(urllib.parse.urlparse(it["link"]).hostname or "").lower().removeprefix("www.") for it in cl}
        if len(hosts) >= min_sources or (len(cl) == 1 and any(_host_is_authoritative(h, auth) for h in hosts)):
            rep = max(cl, key=lambda x: len(x["title"]) + len(x["summary"]))  # 取信息量最大者为代表
            rep["source_count"] = len(hosts)
            rep["sources"] = sorted(h for h in hosts if h)[:4]
            kept.append(rep)
        else:
            dropped += 1
    logger.info(f"[核对·第一遍/来源交叉] 聚类 {len(clusters)} 组，保留 {len(kept)}，"
                f"剔除单一未确认来源 {dropped} 条")
    return kept, {"cluster": len(clusters), "kept": len(kept), "dropped": dropped}


def pass2_keyword(items: list[dict], cfg: dict, logger) -> tuple[list[dict], dict]:
    """第二遍：栏目关键词精确匹配（标题或摘要须真实命中）。"""
    kept, dropped = [], 0
    for it in items:
        kws = (cfg["categories"].get(it["category"]) or {}).get("keywords", [])
        text = f"{it['title']} {it['summary']}".lower()
        if any(k.lower() in text for k in kws):
            kept.append(it)
        else:
            dropped += 1
    logger.info(f"[核对·第二遍/关键词匹配] 保留 {len(kept)}，剔除主题误配 {dropped} 条")
    return kept, {"kept": len(kept), "dropped": dropped}


def pass3_date_content(items: list[dict], cfg: dict, logger,
                       start: datetime, end: datetime, tz, strict: bool = True) -> tuple[list[dict], dict]:
    """第三遍：日期窗口 + 内容完整性一致性复核。
    strict=True：仅保留明确发布在目标日窗口内的条目（默认，保证准确）。
    strict=False：日期无法解析的条目标注"日期未标注"保留，目标日之外的标注"近日"保留。"""
    kept, dropped_date, dropped_bad, undated = [], 0, 0, 0
    for it in items:
        if not it.get("title") or len(it["title"]) < 8 or not it["link"].startswith("http"):
            dropped_bad += 1
            continue
        if LANDING_PATTERNS.search(it["title"]) or LANDING_PATTERNS.search(it["link"]):
            dropped_bad += 1  # 导航/专题页，非具体新闻
            continue
        dt = parse_date(it.get("date_raw", ""))
        if dt:
            dt_local = dt.astimezone(tz)
            it["date_local"] = dt_local.strftime("%m-%d %H:%M")
            if start <= dt_local <= end:
                kept.append(it)
            elif strict:
                dropped_date += 1
            else:
                it["date_local"] += "（近日）"
                kept.append(it)
        else:
            if strict:
                dropped_date += 1
            else:
                it["date_local"] = "日期未标注"
                undated += 1
                kept.append(it)
    logger.info(f"[核对·第三遍/日期与内容] 保留 {len(kept)}（其中无日期 {undated}），"
                f"非目标日剔除 {dropped_date} 条，格式异常 {dropped_bad} 条")
    return kept, {"kept": len(kept), "dropped_date": dropped_date, "dropped_bad": dropped_bad}


# ---------------------------------------------------------------------------
# 日报生成
# ---------------------------------------------------------------------------
def build_report(verified: dict, day, cfg: dict, stats: dict) -> tuple[str, str]:
    """返回 (完整 markdown 日报, 推送用简报文本)。"""
    tzname = cfg["timezone"]
    head = (f"# 每日晨报 · {day:%Y年%m月%d日}\n\n"
            f"> 覆盖：人工智能 / 世界金融与各金融中心 / 地缘政治 / 国内外科技公司\n"
            f"> 三遍核对：多来源交叉验证 → 关键词精确匹配 → 日期内容复核\n"
            f"> 原始抓取 {stats['raw']} 条 → 核对通过 {stats['total_kept']} 条\n\n")
    parts_full = [head]

    # 简报（推送文本）头部
    brief = [f"📰 每日晨报 {day:%m-%d}（已三遍核对：{stats['raw']}→{stats['total_kept']}条）"]

    for cat_id, cat in cfg["categories"].items():
        items = verified.get(cat_id, [])
        items.sort(key=lambda x: x.get("date_local", ""), reverse=True)
        parts_full.append(f"\n## {cat['title']}（{len(items)} 条）\n")
        brief.append(f"\n【{cat['title']}】")
        if not items:
            parts_full.append("_今日该栏目经核对后暂无足够可信条目。_\n")
            brief.append("（经核对无可信条目）")
            continue
        for i, it in enumerate(items, 1):
            src = "/".join(it.get("sources", [])[:2]) or "来源"
            zh_title = (it.get("zh_title") or "").strip()
            zh_summary = (it.get("zh_summary") or "").strip()
            line_full = (f"{i}. **{it['title']}**\n"
                         + (f"   - 译文: {zh_title}\n" if zh_title else "")
                         + f"   - 来源: {src}（{it.get('source_count', 1)} 个独立来源）· {it.get('date_local', '')}\n"
                         f"   - {it['summary'][:180]}\n"
                         + (f"   - 译文: {zh_summary[:240]}\n" if zh_summary else "")
                         + f"   - 链接: {it['link']}\n")
            parts_full.append(line_full)
            brief.append(f"{i}. {it['title'][:60]}（{src}）\n   {it['link']}")
    return "\n".join(parts_full), "\n".join(brief)


# ---------------------------------------------------------------------------
# 网页日报（一目了然的文字页面，每篇附来源网站与原文链接）
# ---------------------------------------------------------------------------
SOURCE_NAMES = {
    "xinhuanet.com": "新华社",
    "news.cn": "新华社",
    "sputniknews.cn": "俄罗斯卫星通讯社",
    "sina.com.cn": "新浪",
    "ithome.com": "IT之家",
    "sspai.com": "少数派",
    "ifanr.com": "爱范儿",
    "qbitai.com": "量子位",
    "cnbc.com": "CNBC",
    "wallstreetcn.com": "华尔街见闻",
    "technologyreview.com": "MIT科技评论",
    "theverge.com": "The Verge",
    "techcrunch.com": "TechCrunch",
    "techradar.com": "TechRadar",
    "tech.eu": "Tech.eu",
    "zdnet.com": "ZDNet",
    "sifted.eu": "Sifted",
    "itnews.com.au": "iTnews",
    "thepaper.cn": "澎湃新闻",
    "caixin.com": "财新",
    "jiemian.com": "界面",
    "zaobao.com": "联合早报",
    "ftchinese.com": "FT中文网",
}


def _friendly_source(host: str) -> str:
    host = (host or "").lower().removeprefix("www.")
    for k, v in SOURCE_NAMES.items():
        if host == k or host.endswith("." + k):
            return v
    return host or "来源"


def _esc(text: str) -> str:
    return ((text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


# ---------------------------------------------------------------------------
# 整篇正文抓取（纯标准库，失败自动退回 RSS 摘要）
# ---------------------------------------------------------------------------
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _decode_body(data: bytes, content_type: str = "") -> str:
    """按 Content-Type / meta 猜测字符集解码网页字节。"""
    enc = None
    m = re.search(r"charset=[\"']?([\w-]+)", content_type, re.I)
    if m:
        enc = m.group(1)
    if not enc:
        m = re.search(rb"charset=[\"']?([\w-]+)", data[:4096], re.I)
        if m:
            enc = m.group(1).decode("ascii", "ignore")
    enc = (enc or "utf-8").lower().replace("gb2312", "gbk").replace("gb_2312", "gbk")
    for e in (enc, "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(e)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "ignore")


class _ArticleParser(HTMLParser):
    """抽正文：跳过脚本/导航/页脚，收集段落文本。"""
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside",
            "form", "iframe", "svg", "template", "button", "select", "textarea"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._buf = []
        self._paras = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "br", "div", "article", "section", "li", "blockquote",
                     "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("p", "div", "article", "section", "li", "blockquote",
                     "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self._flush()

    def handle_data(self, data):
        if self._skip == 0:
            self._buf.append(data)

    def _flush(self):
        text = "".join(self._buf)
        self._buf = []
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 2:
            self._paras.append(text)

    def paras(self):
        self._flush()
        return self._paras


def _extract_article_text(html_text: str, max_chars: int = 2500) -> str:
    """从网页 HTML 里抽取正文：找最长的一段连续长段落（正文主体）。"""
    parser = _ArticleParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    paras = parser.paras()
    if not paras:
        return ""
    best, best_len, run, run_len = [], 0, [], 0
    for p in paras:
        if len(p) >= 12:  # 长段落视为正文，短串多为导航/菜单
            run.append(p)
            run_len += len(p)
            if run_len > best_len:
                best, best_len = run[:], run_len
        else:
            run, run_len = [], 0
    if not best:
        best = paras
    # 去掉开头残留的按钮/导航短文本（如“Send email / Copy link”）
    while len(best) > 1 and len(best[0]) < 40:
        best = best[1:]
    out, total = [], 0
    for p in best:
        if total >= max_chars:
            break
        out.append(p)
        total += len(p) + 1
    text = "\n".join(out).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "……"
    return text


def fetch_full_text(url: str, timeout: int = 8, max_chars: int = 2500) -> str:
    """抓取单篇文章正文；任何异常都返回空串（由调用方退回摘要）。"""
    if not url:
        return ""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(2_000_000)
            ct = resp.headers.get("Content-Type", "")
    except Exception:
        return ""
    if not data:
        return ""
    return _extract_article_text(_decode_body(data, ct), max_chars)


# ---------------------------------------------------------------------------
# 英文→简体中文预翻译（优先谷歌，失败回退 MyMemory，再失败返回空串）
# ---------------------------------------------------------------------------
def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", text or ""))


def _translate_google(text: str, timeout: int = 8) -> str:
    url = ("https://translate.googleapis.com/translate_a/single"
           "?client=gtx&sl=en&tl=zh-CN&dt=t&q=" + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(seg[0] for seg in (data[0] or []) if seg and seg[0]).strip()


def _translate_mymemory(text: str, timeout: int = 8) -> str:
    url = ("https://api.mymemory.translated.net/get?langpair=en%7Czh-CN&q="
           + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    bad = ("QUERY LENGTH", "MAX ALLOWED", "MYMEMORY WARNING", "INVALID",
           "PLEASE SELECT", "NO QUERY")
    if not out or any(b in out.upper() for b in bad):
        return ""
    return out


def translate_zh(text: str) -> str:
    """英文文本翻成简体中文；已含中文/为空/接口全失败时返回空串。"""
    text = (text or "").strip()
    if not text or _has_cjk(text):
        return ""
    for fn in (_translate_google, _translate_mymemory):
        try:
            out = fn(text)
            if out:
                return out
        except Exception:
            continue
    return ""


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日晨报</title>
<style>
  :root{
    --bg:#0b0b12; --card:#16161f; --line:#262636; --text:#e9e6e0;
    --muted:#9a99a6; --gold:#f5c518; --violet:#8b5cf6;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
    max-width:780px;margin:0 auto;padding:20px 16px 64px;color:var(--text);line-height:1.7;
    background:var(--bg);
    background-image:radial-gradient(1200px 620px at 50% -12%, #1d1d36 0%, transparent 60%),
                     radial-gradient(900px 520px at 100% 112%, #1a1430 0%, transparent 55%)}
  .progress{display:none}
  .js .progress{display:block;position:fixed;top:0;left:0;height:3px;width:0;
    background:linear-gradient(90deg,var(--violet),var(--gold));z-index:99;
    box-shadow:0 0 12px rgba(245,197,24,.55)}
  .head{position:relative;padding:30px 22px;margin-bottom:12px;border:1px solid var(--line);
    border-radius:16px;overflow:hidden;
    background:linear-gradient(135deg,rgba(139,92,246,.16),rgba(245,197,24,.08))}
  .head::after{content:"";position:absolute;inset:0;pointer-events:none;
    background:repeating-linear-gradient(0deg,rgba(255,255,255,.015) 0 2px,transparent 2px 4px)}
  .head h1{margin:0;font-size:26px;font-weight:800;letter-spacing:1px;
    background:linear-gradient(90deg,#fff,var(--gold));-webkit-background-clip:text;
    background-clip:text;color:transparent}
  .head .meta{margin-top:10px;font-size:13px;color:var(--muted)}
  .cat{margin-top:30px}
  .cat h2{font-size:17px;margin:0 0 10px;padding-left:12px;border-left:3px solid var(--gold);
    letter-spacing:1px;font-weight:700}
  .item{position:relative;background:var(--card);border:1px solid var(--line);border-radius:14px;
    padding:15px 16px;margin:12px 0;box-shadow:0 6px 24px rgba(0,0,0,.35)}
  .item:hover{border-color:rgba(245,197,24,.45);transform:translateY(-2px);transition:transform .25s,border-color .25s}
  .item .num{display:inline-block;min-width:24px;font-weight:800;color:var(--gold)}
  .item .title{font-size:15.5px;font-weight:700;color:#fff;text-decoration:none}
  .item .title:hover{color:var(--gold)}
  .item .src{font-size:12px;color:var(--muted);margin:6px 0 8px}
  .item .src a{color:var(--violet);text-decoration:none}
  .item p{font-size:14px;color:#cfccc6;margin:0;white-space:pre-wrap}
  .zh-btn{display:inline-block;margin-top:10px;padding:6px 14px;border:1px solid var(--gold);
    border-radius:999px;color:var(--gold);font-size:13px;cursor:pointer;user-select:none;
    background:rgba(245,197,24,.06)}
  .zh-btn:hover{background:rgba(245,197,24,.16)}
  .zh-toggle{display:none}
  .zh-btn .on{display:none}
  .zh-toggle:checked ~ .zh-btn .off{display:none}
  .zh-toggle:checked ~ .zh-btn .on{display:inline}
  .zh-box{display:none;margin-top:10px;padding:12px 14px;border-left:3px solid var(--violet);
    background:rgba(139,92,246,.08);border-radius:0 10px 10px 0}
  .zh-toggle:checked ~ .zh-box{display:block;animation:fadein .4s ease}
  .zh-title{font-size:14px;color:#c4b5fd;font-weight:600;margin-bottom:4px}
  .zh{color:#b8b4c9}
  @keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
  .empty{color:var(--muted);font-size:13px}
  .foot{margin-top:34px;text-align:center;font-size:12px;color:#6a6a78}
  .js .reveal{opacity:0;transform:translateY(24px);filter:blur(6px);
    transition:opacity .7s ease,transform .7s ease,filter .7s ease}
  .js .reveal.in{opacity:1;transform:none;filter:none}
</style>
</head>
<body>
<div class="progress" id="progress"></div>
__BODY__
<div class="foot">本页由每日晨报机器人自动生成 · 覆盖亚欧美澳</div>
<script>
(function(){
  if(!("IntersectionObserver" in window)){return;}
  document.documentElement.classList.add("js");
  var els=document.querySelectorAll(".reveal");
  var io=new IntersectionObserver(function(entries){
    entries.forEach(function(e){
      if(e.isIntersecting){e.target.classList.add("in");io.unobserve(e.target);}
    });
  },{threshold:.08,rootMargin:"0px 0px -8% 0px"});
  els.forEach(function(el){io.observe(el);});
  var bar=document.getElementById("progress");
  function paint(){
    var h=document.documentElement;
    var max=h.scrollHeight-window.innerHeight;
    bar.style.width=(max>0?(h.scrollTop/max)*100:0)+"%";
  }
  window.addEventListener("scroll",paint,{passive:true});
  paint();
})();
</script>
</body>
</html>"""


def build_html(verified: dict, day, cfg: dict, stats: dict) -> str:
    """生成可直接阅读的文字网页：每篇附来源网站名 + 原文链接，英文条目附简体中文译文（默认隐藏，点按钮才显示）。"""
    head = (f'<div class="head"><h1>📰 每日晨报 · {day:%Y年%m月%d日}</h1>'
            f'<div class="meta">原始抓取 {stats["raw"]} 条 → 三遍核对通过 {stats["total_kept"]} 条'
            f' · 覆盖 AI / 金融 / 地缘政治 / 科技</div></div>')
    parts = [head]
    uid = 0
    for cat_id, cat in cfg["categories"].items():
        items = sorted(verified.get(cat_id, []), key=lambda x: x.get("date_local", ""), reverse=True)
        parts.append(f'<div class="cat"><h2>{_esc(cat["title"])}（{len(items)} 条）</h2>')
        if not items:
            parts.append('<p class="empty">今日该栏目经核对后暂无足够可信条目。</p>')
        for i, it in enumerate(items, 1):
            uid += 1
            link = _esc(it["link"])
            host = (urllib.parse.urlparse(it["link"]).hostname or "").lower().removeprefix("www.")
            fname = _friendly_source(host)
            date = _esc(it.get("date_local", ""))
            summary = ((it.get("full_text") or "").strip()
                       or (it["summary"] or "").strip()
                       or "（该来源未提供摘要，点击标题或“阅读原文”查看全文。）")
            zh_title = (it.get("zh_title") or "").strip()
            zh_summary = (it.get("zh_summary") or "").strip()
            item = (
                f'<div class="item reveal" style="transition-delay:{min((i - 1) * 60, 400)}ms">'
                f'<span class="num">{i}</span>'
                f'<a class="title" href="{link}">{_esc(it["title"])}</a>'
                f'<div class="src">来源：{_esc(fname)}（{_esc(host)}） · {date} · '
                f'<a href="{link}">阅读原文 →</a></div>'
                f'<p>{_esc(summary)}</p>'
            )
            if zh_title or zh_summary:
                zid = f"zh-{uid}"
                item += (
                    f'<input class="zh-toggle" type="checkbox" id="{zid}">'
                    f'<label class="zh-btn" for="{zid}">'
                    f'<span class="off">🌐 显示中文翻译</span><span class="on">🌐 收起翻译</span></label>'
                    f'<div class="zh-box">'
                    + (f'<div class="zh-title">🌐 {_esc(zh_title)}</div>' if zh_title else "")
                    + (f'<p class="zh">🌐 {_esc(zh_summary)}</p>' if zh_summary else "")
                    + "</div>"
                )
            item += "</div>"
            parts.append(item)
        parts.append("</div>")
    return _HTML_TEMPLATE.replace("__BODY__", "\n".join(parts))


# ---------------------------------------------------------------------------
# 推送通道
# ---------------------------------------------------------------------------
def http_post(url: str, data: dict | bytes, headers: dict | None = None) -> bytes:
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def push_brief(brief: str, cfg: dict, logger, html: str | None = None):
    p = cfg["push"]
    ch = p.get("channel", "none")
    if ch == "none":
        logger.info("[推送] channel=none，跳过推送")
        return
    try:
        if ch == "bark":  # iOS Bark
            base = p["bark_url"].rstrip("/")
            url = f"{base}/{urllib.parse.quote(brief[:3500])}"
            req = urllib.request.Request(url)
            urllib.request.urlopen(req, timeout=30)
        elif ch == "serverchan":  # Server酱·微信
            http_post(f"https://sctapi.ftqq.com/{p['serverchan_sendkey']}.send",
                      data=urllib.parse.urlencode({"title": "每日晨报", "desp": brief}).encode())
        elif ch == "pushplus":  # PushPlus·微信
            http_post("https://www.pushplus.plus/send",
                      data={"token": p["pushplus_token"], "title": "每日晨报",
                            "content": f"<pre style='white-space:pre-wrap'>{brief}</pre>",
                            "template": "html"})
        elif ch == "telegram":  # Telegram
            for i in range(0, len(brief), 3800):
                http_post(f"https://api.telegram.org/bot{p['telegram_bot_token']}/sendMessage",
                          data={"chat_id": p["telegram_chat_id"],
                                "text": brief[i:i + 3800], "disable_web_page_preview": True})
        elif ch == "email":  # 邮件
            e = p["email"]
            # 同时带上纯文本与 HTML 网页版：手机/电脑邮箱都能直接渲染成一目了然的页面
            msg = MIMEMultipart("alternative")
            msg["Subject"] = Header("每日晨报（AI/金融/地缘政治/科技）", "utf-8")
            msg["From"] = e["username"]
            msg["To"] = e["to_addr"]
            msg.attach(MIMEText(brief, "plain", "utf-8"))
            if html:
                msg.attach(MIMEText(html, "html", "utf-8"))
            with smtplib.SMTP_SSL(e["smtp_host"], e["smtp_port"], timeout=30) as s:
                s.login(e["username"], e["password"])
                s.sendmail(e["username"], [e["to_addr"]], msg.as_string())
        else:
            logger.warning(f"[推送] 未知通道 {ch}，跳过")
            return
        logger.info(f"[推送] 已通过 {ch} 发送晨报")
    except Exception as exc:
        logger.error(f"[推送] 失败: {exc} —— 日报文件已保存在 reports/ 目录，可手动查看")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="每日晨报采集机器人")
    ap.add_argument("--backend", choices=["rss", "zai"], help="采集后端（默认读 config.json）")
    ap.add_argument("--dry-run", action="store_true", help="只生成日报，不推送")
    ap.add_argument("--date", help="指定收集哪一天（YYYY-MM-DD，默认=昨天）")
    ap.add_argument("--lenient-date", action="store_true",
                    help="宽松日期模式：无日期或近日条目标注后保留（默认严格限定前一天）")
    args = ap.parse_args()

    cfg = load_config(CONFIG_PATH)
    if args.backend:
        cfg["backend"] = args.backend
    start, end, tz, day = target_window(cfg, args.date)

    logger = setup_logging(BASE_DIR / "logs")
    logger.info(f"=== 每日晨报启动 | 目标日 {day} | 后端 {cfg['backend']} | 时区 {cfg['timezone']} ===")

    # --- 采集 ---
    if cfg["backend"] == "zai":
        raw = fetch_via_zai(cfg, logger, day)
    else:
        raw = fetch_via_rss(cfg, logger)
    logger.info(f"原始抓取共 {len(raw)} 条")
    if not raw:
        logger.error("未抓取到任何内容，请检查网络或新闻源配置")
        sys.exit(1)

    # --- 三遍核对 ---
    p1, s1 = pass1_crosscheck(raw, cfg, logger)
    p2, s2 = pass2_keyword(p1, cfg, logger)
    p3, s3 = pass3_date_content(p2, cfg, logger, start, end, tz, strict=not args.lenient_date)

    verified: dict = {cid: [] for cid in cfg["categories"]}
    for it in p3:
        verified[it["category"]].append(it)

    stats = {"raw": len(raw), "total_kept": len(p3), **{f"p1_{k}": v for k, v in s1.items()},
             **{f"p2_{k}": v for k, v in s2.items()}, **{f"p3_{k}": v for k, v in s3.items()}}

    # --- 整篇正文抓取（并发，失败自动退回摘要）---
    to_fetch = [it for lst in verified.values() for it in lst if it.get("link")]
    if to_fetch:
        logger.info(f"开始抓取 {len(to_fetch)} 篇正文（并发 8，失败退回摘要）...")
        got = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(fetch_full_text, it["link"]): it for it in to_fetch}
            for fut in concurrent.futures.as_completed(futs):
                it = futs[fut]
                try:
                    txt = fut.result()
                except Exception:
                    txt = ""
                if txt and len(txt) > len(it.get("summary", "")):
                    it["full_text"] = txt
                    got += 1
        logger.info(f"正文抓取完成：成功 {got}/{len(to_fetch)} 篇（其余退回摘要）")

    # --- 英文条目预翻译为简体中文（原文下方附译文；接口失败自动跳过）---
    to_translate = [it for lst in verified.values() for it in lst if not _has_cjk(it["title"])]
    if to_translate:
        logger.info(f"开始翻译 {len(to_translate)} 个英文条目为简体中文 ...")

        def _do_translate(it):
            it["zh_title"] = translate_zh(it["title"])
            disp = ((it.get("full_text") or "").strip() or (it["summary"] or "").strip())
            it["zh_summary"] = translate_zh(disp[:480]) if disp else ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(_do_translate, to_translate))
        got_zh = sum(1 for it in to_translate if it.get("zh_title"))
        logger.info(f"翻译完成：{got_zh}/{len(to_translate)} 个条目获得中文译文")

    # --- 日报 ---
    reports_dir = BASE_DIR / "reports"
    reports_dir.mkdir(exist_ok=True)
    full, brief = build_report(verified, day, cfg, stats)
    report_path = reports_dir / f"daily_{day:%Y-%m-%d}.md"
    report_path.write_text(full, encoding="utf-8")
    logger.info(f"完整日报已保存: {report_path}")

    # 网页版：文字直接显示在页面中，每篇附来源网站与原文链接
    html_page = build_html(verified, day, cfg, stats)
    html_path = reports_dir / f"daily_{day:%Y-%m-%d}.html"
    html_path.write_text(html_page, encoding="utf-8")
    logger.info(f"网页版日报已保存: {html_path}")

    # --- 推送 ---
    if args.dry_run:
        logger.info("[推送] dry-run 模式，跳过推送")
    else:
        push_brief(brief, cfg, logger, html=html_page)
    logger.info("=== 运行结束 ===")


if __name__ == "__main__":
    main()
