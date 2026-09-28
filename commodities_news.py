#!/usr/bin/env python3
"""
Commodities News Aggregator
----------------------------
Pulls headlines + short summaries from multiple commodities/markets RSS
feeds, tags each with source and "X ago" time, dedupes near-identical
headlines, and writes docs/index.html — designed to be hosted on
GitHub Pages (see setup notes at the bottom of this file).

LOCAL TEST:
    pip install feedparser
    python commodities_news.py
    -> open docs/index.html in a browser
"""

import feedparser
import html
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

# ---------------------------------------------------------------------
# 1. FEEDS — add/remove freely.
#    - Economic Times: grab the exact "Commodities" RSS URL from
#      economictimes.indiatimes.com/rssfeedsdefault.cms and paste below.
#    - TradingView has no public RSS feed — not pullable this way.
#    - Moneycontrol: check moneycontrol.com/rss for their current list.
# ---------------------------------------------------------------------
def gnews(query):
    """Google News RSS for a search query (India edition). Lets us pull
    headlines from sites that don't publish a usable RSS feed."""
    return ("https://news.google.com/rss/search?q=" + quote_plus(query)
            + "&hl=en-IN&gl=IN&ceid=IN:en")

FEEDS = {
    # --- Direct RSS feeds (these include short summaries) ---
    "Barchart - Metals":           "https://www.barchart.com/news/rss/commodities/metals",
    "Barchart - Energies":         "https://www.barchart.com/news/rss/commodities/energies",
    "Barchart - Grains":           "https://www.barchart.com/news/rss/commodities/grains",
    "Barchart - Softs":            "https://www.barchart.com/news/rss/commodities/softs",
    "SCMP - Commodities":          "https://www.scmp.com/rss/265840/feed",
    "Kitco News":                  "https://www.kitco.com/rss/KitcoNews.xml",
    "Investing.com - Commodities": "https://www.investing.com/rss/commodities.rss",
    "Business Recorder - Commodities": "https://www.brecorder.com/feeds/news/1761",
    "Trading Economics":           "https://tradingeconomics.com/rss/news.aspx",

    # --- Indian sources via Google News (headline + publisher + time;
    #     no summary text). when:2d = last 2 days only. ---
    "Economic Times":      gnews("(commodities OR gold OR silver OR crude OR copper) site:economictimes.indiatimes.com when:2d"),
    "Moneycontrol":        gnews("(commodities OR gold OR silver OR crude OR MCX) site:moneycontrol.com when:2d"),
    "Business Standard":   gnews("(commodities OR gold OR silver OR crude) site:business-standard.com when:2d"),
    "Mint":                gnews("(commodities OR gold OR silver OR crude) site:livemint.com when:2d"),
    "Hindu BusinessLine":  gnews("(commodities OR gold OR silver OR crude OR spices) site:thehindubusinessline.com when:2d"),
    "MCX news":            gnews("MCX Multi Commodity Exchange when:2d"),
    "NCDEX news":          gnews("NCDEX agri commodities when:2d"),
    "Trading Economics (backup)": gnews("commodities site:tradingeconomics.com when:2d"),
}

KEYWORDS = [
    "gold", "silver", "crude", "oil", "copper", "commodity", "commodities",
    "mcx", "ncdex", "opec", "natural gas", "aluminium", "zinc", "nickel",
    "wheat", "sugar", "cotton", "bullion", "metal", "energy",
]

SUMMARY_MAX_CHARS = 200

# ---------------------------------------------------------------------
# 2. HELPERS
# ---------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")

def clean_summary(raw_summary, title):
    """Strip HTML tags, collapse whitespace, trim, and drop it if it's
    just a repeat of the title (some feeds do that)."""
    text = TAG_RE.sub("", raw_summary or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.lower() == title.lower().strip():
        return ""
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def time_ago(published_struct):
    if not published_struct:
        return "unknown time"
    dt = datetime(*published_struct[:6], tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 0:
        return "just now"
    minutes, hours, days = seconds / 60, seconds / 3600, seconds / 86400
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)} min ago"
    if hours < 24:
        return f"{int(hours)} hr ago"
    return f"{int(days)} days ago"


def matches_keywords(title, summary):
    if not KEYWORDS:
        return True
    text = f"{title} {summary}".lower()
    return any(k.lower() in text for k in KEYWORDS)


def fetch_all():
    items = []
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[!] Could not fetch {source}: {e}")
            continue
        if not feed.entries:
            print(f"[!] {source} returned no entries — feed URL may have moved.")
            continue
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            raw_summary = entry.get("summary", "")
            link = entry.get("link", "")
            label = source
            if "news.google.com" in url:
                # Google titles look like "Headline - Publisher"; show the
                # real publisher and drop the redundant summary.
                if " - " in title:
                    title, publisher = title.rsplit(" - ", 1)
                    label = publisher.strip()
                raw_summary = ""
            published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if not matches_keywords(title, raw_summary):
                continue
            items.append({
                "source": label,
                "title": title,
                "summary": clean_summary(raw_summary, title),
                "link": link,
                "ago": time_ago(published_struct),
                "sort_key": published_struct or (1970, 1, 1, 0, 0, 0, 0, 0, 0),
            })

    seen, deduped = set(), []
    for it in items:
        key = it["title"].lower().strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    deduped.sort(key=lambda x: x["sort_key"], reverse=True)
    return deduped


def write_html(items, path="docs/index.html"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cards = "\n".join(
        f'''<div class="card">
  <div class="row"><span class="src">{html.escape(it["source"])}</span><span class="ago">{html.escape(it["ago"])}</span></div>
  <a class="title" href="{html.escape(it["link"])}" target="_blank">{html.escape(it["title"])}</a>
  {f'<p class="summary">{html.escape(it["summary"])}</p>' if it["summary"] else ""}
</div>'''
        for it in items
    )
    page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Commodities News</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, Arial, sans-serif; margin: 0; background: #f4f5f7; color: #1a1a1a; }}
header {{ background: #14213d; color: #fff; padding: 20px 24px; }}
header h1 {{ margin: 0; font-size: 20px; }}
header .meta {{ color: #b8c1d9; font-size: 12px; margin-top: 4px; }}
.grid {{ max-width: 860px; margin: 20px auto; padding: 0 16px; display: flex; flex-direction: column; gap: 10px; }}
.card {{ background: #fff; border-radius: 8px; padding: 14px 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
.row {{ display: flex; justify-content: space-between; font-size: 11px; margin-bottom: 6px; }}
.src {{ color: #14213d; font-weight: 600; text-transform: uppercase; letter-spacing: .03em; }}
.ago {{ color: #999; }}
.title {{ display: block; font-size: 15px; font-weight: 600; color: #14213d; text-decoration: none; line-height: 1.4; }}
.title:hover {{ text-decoration: underline; }}
.summary {{ font-size: 13px; color: #555; margin: 6px 0 0; line-height: 1.5; }}
</style></head>
<body>
<header>
  <h1>Commodities News</h1>
  <div class="meta">Updated {datetime.now().strftime('%d %b %Y, %I:%M %p')} IST &middot; {len(items)} stories</div>
</header>
<div class="grid">
{cards}
</div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {len(items)} headlines to {path}")


if __name__ == "__main__":
    write_html(fetch_all())
