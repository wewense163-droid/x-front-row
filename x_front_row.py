#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X 中文区大号盯帖抢评助手（v1）

作用：轮询一批 X 账号的 nitter RSS，发现「新鲜窗口」内的新原创帖时，
第一时间在控制台打印告警，并追加写入 alerts.md，方便你抢前排评论。

设计原则：
- 只用 Python 3 标准库（urllib + xml.etree），零 pip 安装，任何机器可跑。
- 只读镜像站，不登录、不发帖、不碰你的 X 账号，安全无风险。
- 用 state.json 记录已见过的帖子 ID，重启不重复告警。

用法：
    python3 x_front_row.py --config config.json --once   # 只扫一轮
    python3 x_front_row.py --config config.json           # 按配置间隔循环扫
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# 每个账号在 state 里最多保留的已见帖子数，防止 state.json 无限膨胀
MAX_SEEN_PER_HANDLE = 200

# 请求镜像站时用的 User-Agent，部分镜像站会拦截默认的 Python UA
USER_AGENT = "x-front-row/0.1 (rss reader; python stdlib)"

# RSS 里 dc:creator 字段的命名空间
DC_NS = "{http://purl.org/dc/elements/1.1/}"


# ---------------------------------------------------------------------------
# 配置与状态
# ---------------------------------------------------------------------------

def load_config(path):
    """读取 JSON 配置，缺失的项用默认值补齐。"""
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("handles", [])
    cfg.setdefault("freshness_minutes", 60)      # 新鲜窗口：多少分钟内的帖才告警
    cfg.setdefault("poll_interval_minutes", 10)  # 循环模式下每轮间隔（分钟）
    cfg.setdefault("nitter_base", "https://nitter.cf")
    cfg.setdefault("state_file", "state.json")
    cfg.setdefault("alerts_file", "alerts.md")
    cfg.setdefault("request_timeout_seconds", 20)
    return cfg


def load_state(path):
    """读取去重状态文件；不存在或损坏时返回空状态。"""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict) and isinstance(state.get("seen"), dict):
                return state
        except (json.JSONDecodeError, OSError):
            print(f"[警告] 状态文件 {path} 损坏，将重新开始记录。", file=sys.stderr)
    return {"seen": {}, "last_check": None}


def save_state(path, state):
    """把状态写回磁盘（先写临时文件再改名，避免写一半损坏）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# RSS 抓取与解析
# ---------------------------------------------------------------------------

def fetch_rss(handle, cfg):
    """抓取某个账号的 nitter RSS，返回原始 XML 字节。失败时抛异常，由调用方处理。"""
    url = f"{cfg['nitter_base'].rstrip('/')}/{handle}/rss"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=cfg["request_timeout_seconds"]) as resp:
        return resp.read()


def clean_text(raw):
    """把 RSS 里的 HTML 描述清洗成纯文本：去标签、反转义、合并空白。"""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_posts(handle, xml_bytes):
    """解析 nitter RSS，返回帖子字典列表（不做新鲜/去重判断）。

    每条帖子字段：handle / author / text / link / status_id / published / guid
    """
    posts = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        desc = clean_text(item.findtext("description") or "")
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        author = (item.findtext(f"{DC_NS}creator") or f"@{handle}").strip()

        # 发布时间：RSS 用 RFC 822 格式，统一转成带时区的 datetime
        published = None
        pub_raw = item.findtext("pubDate")
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None

        # 正文优先用 description（title 可能被截断），为空时退回 title
        text = desc if desc else clean_text(title)

        # 从链接末尾提取 status_id，例如 https://nitter.cf/dotey/status/123456
        status_id = ""
        m = re.search(r"/status/(\d+)", link)
        if m:
            status_id = m.group(1)

        posts.append({
            "handle": handle,
            "author": author,
            "title": title,
            "text": text,
            "link": link,
            "status_id": status_id,
            "guid": guid,
            "published": published,
        })
    return posts


def is_retweet(post):
    """判断是否为转发帖：nitter RSS 中转发帖的标题以 "RT by" 开头。"""
    title = post.get("title", "")
    text = post.get("text", "")
    return title.startswith("RT by") or text.startswith("RT by")


def x_link(post):
    """把镜像链接换成 x.com 链接，方便直接点开原帖。"""
    if post["status_id"]:
        return f"https://x.com/{post['handle']}/status/{post['status_id']}"
    return post["link"]


# ---------------------------------------------------------------------------
# 可选钩子：回复草稿（v1 默认不启用）
# ---------------------------------------------------------------------------

def draft_replies(post):
    """【可选钩子】为一条新帖生成回复草稿，返回草稿字符串列表。

    v1 默认关闭：只有当环境变量 X_FRONT_ROW_API_KEY 存在时才视为已配置；
    未配置时直接返回空列表，扫描流程不会输出任何草稿。

    想接入时，在下面 TODO 处调用你自己的大模型 API 即可，
    post 里有 text / author / link 等字段可用。
    """
    api_key = os.environ.get("X_FRONT_ROW_API_KEY", "").strip()
    if not api_key:
        return []
    # TODO: 在这里调用模型 API，根据 post["text"] 生成 2-3 条回复草稿并返回。
    return []


# ---------------------------------------------------------------------------
# 告警输出
# ---------------------------------------------------------------------------

def format_alert(post, drafts):
    """把一条新帖格式化成一段 Markdown 告警文本。"""
    if post["published"]:
        pub_str = post["published"].astimezone().strftime("%Y-%m-%d %H:%M:%S")
    else:
        pub_str = "未知时间"
    lines = [
        f"## 🎯 新帖：{post['author']}（@{post['handle']}）",
        f"- 时间：{pub_str}",
        f"- 链接：{x_link(post)}",
        f"- 内容：{post['text']}",
    ]
    if drafts:
        lines.append("- 回复草稿：")
        for i, d in enumerate(drafts, 1):
            lines.append(f"  {i}. {d}")
    lines.append("")
    return "\n".join(lines)


def emit_alert(text, alerts_path):
    """告警同时打到控制台并追加写入 alerts.md。"""
    print(text)
    with open(alerts_path, "a", encoding="utf-8") as f:
        f.write(text + "\n")


# ---------------------------------------------------------------------------
# 主扫描逻辑
# ---------------------------------------------------------------------------

def scan_once(cfg, state):
    """扫一轮：逐个账号抓 RSS，找出新鲜窗口内的新原创帖并告警。

    返回本轮新告警的帖子数量。单个账号失败只跳过该账号，不中断整轮。
    """
    now = datetime.now(timezone.utc)
    fresh_after = now - timedelta(minutes=cfg["freshness_minutes"])
    # 允许 5 分钟的时钟偏差，避免刚发的帖因时间略超当前时刻被误杀
    fresh_before = now + timedelta(minutes=5)

    seen = state.setdefault("seen", {})
    new_count = 0

    for handle in cfg["handles"]:
        try:
            xml_bytes = fetch_rss(handle, cfg)
            posts = parse_posts(handle, xml_bytes)
        except (urllib.error.URLError, ET.ParseError, OSError) as e:
            print(f"[跳过] @{handle} 抓取/解析失败：{e}", file=sys.stderr)
            continue

        handle_seen = seen.setdefault(handle, [])
        seen_set = set(handle_seen)

        for post in posts:
            # 所有扫到的帖子都记入 seen（包括旧帖和转发），避免反复判断
            key = post["guid"] or post["status_id"] or post["link"]
            if not key:
                continue
            is_new = key not in seen_set
            if is_new:
                seen_set.add(key)
                handle_seen.append(key)

            if not is_new:
                continue
            if is_retweet(post):
                continue  # 转发不算原创，不告警
            if post["published"] is None:
                continue  # 没有时间无法判断新鲜度，保守跳过
            if not (fresh_after <= post["published"] <= fresh_before):
                continue  # 不在新鲜窗口内

            drafts = draft_replies(post)
            emit_alert(format_alert(post, drafts), cfg["alerts_file"])
            new_count += 1

        # 只保留最近 N 条，防止 state 无限增长
        if len(handle_seen) > MAX_SEEN_PER_HANDLE:
            del handle_seen[: len(handle_seen) - MAX_SEEN_PER_HANDLE]

    state["last_check"] = now.isoformat()
    save_state(cfg["state_file"], state)
    return new_count


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="X 中文区大号盯帖抢评助手：轮询 nitter RSS，发现新原创帖时告警。")
    parser.add_argument("--config", default="config.json", help="配置文件路径（JSON）")
    parser.add_argument("--once", action="store_true", help="只扫一轮就退出（默认循环运行）")
    args = parser.parse_args(argv)

    if not os.path.exists(args.config):
        print(f"找不到配置文件：{args.config}\n"
              f"请先复制一份：cp config.example.json config.json，再改成你的配置。",
              file=sys.stderr)
        return 1

    cfg = load_config(args.config)
    if not cfg["handles"]:
        print("配置里 handles 为空，没有要盯的账号。", file=sys.stderr)
        return 1

    # 状态/告警文件相对路径以配置文件所在目录为准，方便从任何目录启动
    base_dir = os.path.dirname(os.path.abspath(args.config))
    for key in ("state_file", "alerts_file"):
        if not os.path.isabs(cfg[key]):
            cfg[key] = os.path.join(base_dir, cfg[key])

    state = load_state(cfg["state_file"])
    print(f"开始盯帖：{len(cfg['handles'])} 个账号，新鲜窗口 "
          f"{cfg['freshness_minutes']} 分钟，间隔 {cfg['poll_interval_minutes']} 分钟。")

    if args.once:
        n = scan_once(cfg, state)
        print(f"本轮完成：新告警 {n} 条。")
        return 0

    try:
        while True:
            n = scan_once(cfg, state)
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] 本轮完成：新告警 {n} 条，"
                  f"{cfg['poll_interval_minutes']} 分钟后再扫。")
            time.sleep(cfg["poll_interval_minutes"] * 60)
    except KeyboardInterrupt:
        print("\n已停止盯帖。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
