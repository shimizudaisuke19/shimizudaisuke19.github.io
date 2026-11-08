#!/usr/bin/env python3
"""ホームページの「最近の回」を、スタンドFMの最新の放送に差し替える。

- RSS と チャンネルのページ の両方を見て、より新しい回を使う
  （RSS は更新が遅れることがあるため）
- index.html の目印の間だけを書き換える
    <!--LATEST:START--> ... <!--LATEST:END-->            題と再生の埋め込み
    <!--LATEST-LINK:START--> ... <!--LATEST-LINK:END-->  「スタンドFMで開く」のリンク
- 読めない・形がおかしいときは、何も書き換えずに終わる（ページを壊さない）
- 標準ライブラリだけで動く

使い方:  python scripts/update_latest.py [index.html の場所]
"""

import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

CHANNEL_ID = "65edab793e0b28cf816da054"
RSS_URL = f"https://stand.fm/rss/{CHANNEL_ID}"
CHANNEL_URL = f"https://stand.fm/channels/{CHANNEL_ID}"
EPISODE_URL = "https://stand.fm/episodes/{id}"
EMBED_URL = "https://stand.fm/embed/episodes/{id}"
UA = "Mozilla/5.0 (compatible; homepage-latest-episode-updater/1.0)"

ID_RE = re.compile(r"^[0-9a-f]{24}$")
START, END = "<!--LATEST:START-->", "<!--LATEST:END-->"
LSTART, LEND = "<!--LATEST-LINK:START-->", "<!--LATEST-LINK:END-->"


def _flat(msg):
    # 改行や「::」で始まる行が Actions への指示として読まれないように、1行にならす
    return " ".join(str(msg).split()).replace("::", ": :")


def log(msg):
    print(_flat(msg), flush=True)


def warn(msg):
    # GitHub Actions の画面に黄色い注意として出る
    print(f"::warning::{_flat(msg)}", flush=True)


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url} を読めませんでした: {last}")


def id_from_url(url):
    m = re.search(r"/episodes/([0-9a-f]{24})", url or "")
    return m.group(1) if m else None


def from_rss():
    """RSS のいちばん新しい回 {id, title, time(ミリ秒)} と、RSS に載っている全 ID"""
    text = fetch(RSS_URL)
    root = ET.fromstring(text.encode("utf-8"))
    best, ids = None, set()
    for item in root.iter("item"):
        ep_id = id_from_url(item.findtext("link")) or id_from_url(item.findtext("guid"))
        title = (item.findtext("title") or "").strip()
        if not ep_id or not title:
            continue
        ids.add(ep_id)
        try:
            t = int(parsedate_to_datetime(item.findtext("pubDate")).timestamp() * 1000)
        except Exception:  # noqa: BLE001
            t = 0
        if best is None or t > best["time"]:
            best = {"id": ep_id, "title": title, "time": t, "from": "RSS"}
    if best is None:
        raise RuntimeError("RSS に回が見つかりませんでした")
    return best, ids


def from_channel():
    """チャンネルのページの最初の回を、その回のページで題と公開時刻を確かめて返す"""
    page = fetch(CHANNEL_URL)
    m = re.search(r'href="/episodes/([0-9a-f]{24})"', page) or re.search(r"/episodes/([0-9a-f]{24})", page)
    if not m:
        raise RuntimeError("チャンネルのページに回が見つかりませんでした")
    ep_id = m.group(1)
    ep = fetch(EPISODE_URL.format(id=ep_id))

    title, t = None, 0
    j = ep.find(f'"id":"{ep_id}","channelId":"{CHANNEL_ID}"')
    if j >= 0:
        chunk = ep[j:j + 6000]
        k = chunk.find('"latestEpisodeIds"')
        chunk = chunk[:k] if k > 0 else chunk[:3000]   # この回の分だけを見る
        if '"isUnlisted":true' in chunk:
            raise RuntimeError("チャンネルの最初の回が限定公開でした")
        mt = re.search(r'"title":"((?:[^"\\]|\\.)*)"', chunk)
        mp = re.search(r'"publishedAt":(\d+)', chunk)
        if mt:
            title = json.loads(f'"{mt.group(1)}"').strip()
        if mp:
            t = int(mp.group(1))
    if not title:
        mo = re.search(r'property="og:title" content="([^"]+)"', ep)
        if mo:
            title = html.unescape(mo.group(1)).replace(" | stand.fm", "").rsplit(" - ", 1)[0].strip()
    if not title:
        raise RuntimeError("回のページから題を取れませんでした")
    return {"id": ep_id, "title": title, "time": t, "from": "チャンネルのページ"}


def pick_latest():
    rss, rss_ids, ch = None, set(), None
    try:
        rss, rss_ids = from_rss()
        log(f"RSS の最新: {rss['title']} ({rss['id']})")
    except Exception as e:  # noqa: BLE001
        warn(f"RSS: {e}")
    try:
        ch = from_channel()
        log(f"チャンネルのページの最新: {ch['title']} ({ch['id']})")
    except Exception as e:  # noqa: BLE001
        warn(f"チャンネルのページ: {e}")

    if rss and ch:
        if ch["time"] and rss["time"]:
            return ch if ch["time"] > rss["time"] else rss
        # 時刻が取れないとき: RSS にまだ無い回なら、チャンネルの方が新しい
        return ch if ch["id"] not in rss_ids else rss
    return rss or ch


def label(raw_title):
    """「防災意識　#594」→ 第594回「防災意識」"""
    t = re.sub(r"\s+", " ", raw_title.replace("　", " ")).strip()
    name, num = t, None
    m = re.match(r"^(.+?)\s*[#＃]\s*(\d+)$", t)       # 題　#594
    if m:
        name, num = m.group(1).strip(), m.group(2)
    else:
        m = re.match(r"^[#＃]\s*(\d+)\s+(.+)$", t)    # #594 題
        if m:
            num, name = m.group(1), m.group(2).strip()
    return name, (f"第{int(num)}回「{name}」" if num else f"「{name}」")


def build(ep, indent):
    name, shown = label(ep["title"])
    e = html.escape
    return (
        f"{START}\n"
        f'{indent}<p>最近の回　<span class="nw">{e(shown)}</span></p>\n'
        f'{indent}<iframe src="{EMBED_URL.format(id=ep["id"])}" title="スタンドFM「{e(name)}」の再生" '
        f'loading="lazy" allow="autoplay"></iframe>\n'
        f"{indent}{END}"
    ), (
        f'{LSTART}<a href="{EPISODE_URL.format(id=ep["id"])}">スタンドFMで開く</a>{LEND}'
    ), shown


def replace_between(text, start, end, new_block):
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        raise RuntimeError(f"目印 {start} / {end} が1組ずつ見つかりません")
    a = text.index(start)
    b = text.index(end) + len(end)
    return text[:a] + new_block + text[b:]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "index.html")
    path = os.path.abspath(path)

    ep = pick_latest()
    if not ep:
        warn("最新の回を取れなかったので、何も書き換えずに終わります")
        return 0
    if not ID_RE.match(ep["id"]) or not (0 < len(ep["title"]) <= 200):
        warn(f"取れた回の形がおかしいので書き換えません: {ep!r}")
        return 0

    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    text = text.replace("\r\n", "\n")

    try:
        m = re.search(r"^([ \t]*)" + re.escape(START), text, re.M)
        indent = m.group(1) if m else ""
        block, link, shown = build(ep, indent)
        new = replace_between(text, START, END, block)
        new = replace_between(new, LSTART, LEND, link)
    except Exception as e:  # noqa: BLE001
        warn(f"index.html を書き換えられませんでした（そのままにします）: {e}")
        return 0

    log(f"使う回: {shown}  {EPISODE_URL.format(id=ep['id'])}（{ep['from']}から）")
    if new == text:
        log("すでに最新です。書き換えはありません")
        return 0

    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(new.replace("\n", nl).encode("utf-8"))
    os.replace(tmp, path)
    log(f"index.html を書き換えました: {shown}")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed=true\nshown={shown}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
