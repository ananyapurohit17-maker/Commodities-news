#!/usr/bin/env python3
"""
Commodities News Aggregator -> docs/index.html (hosted on GitHub Pages)

Pulls RSS + Google News feeds, tags each story with the commodity it is
about (Gold, Silver, Crude Oil ...), shows a short summary, source and
"X ago" time, and builds a filterable card page.

Local test:  pip install feedparser googlenewsdecoder
             python commodities_news.py   -> open docs/index.html
"""
import feedparser, html, json, os, re, threading, time, urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

IST = timezone(timedelta(hours=5, minutes=30))
OUT = "docs/index.html"
CACHE = "docs/summary_cache.json"
SUMMARY_MAX = 420          # ~3 sentences
MAX_ENRICH_PER_RUN = 30    # article pages fetched per run (Google News items)
MAX_STORIES = 150
ENRICH_BUDGET_SEC = 150     # hard stop for all article lookups per run
PER_ARTICLE_SEC = 15       # give up on any single article after this
MAX_CONSEC_FAILS = 6       # stop early if lookups keep failing (rate limit)
MAX_AGE_DAYS = 5           # drop stories older than this (by real publish date)

def gnews(q):
    return ("https://news.google.com/rss/search?q=" + quote_plus(q)
            + "&hl=en-IN&gl=IN&ceid=IN:en")

FEEDS = {
    "Barchart - Metals":   "https://www.barchart.com/news/rss/commodities/metals",
    "Barchart - Energies": "https://www.barchart.com/news/rss/commodities/energies",
    "Barchart - Grains":   "https://www.barchart.com/news/rss/commodities/grains",
    "Barchart - Softs":    "https://www.barchart.com/news/rss/commodities/softs",
    "SCMP - Commodities":  "https://www.scmp.com/rss/265840/feed",
    "Kitco News":          "https://www.kitco.com/rss/KitcoNews.xml",
    "Investing.com":       "https://www.investing.com/rss/commodities.rss",
    "Business Recorder":   "https://www.brecorder.com/feeds/news/1761",
    "Trading Economics":   "https://tradingeconomics.com/rss/news.aspx",
    # Indian publishers' own feeds (these carry a short description)
    "Economic Times - Commodities": "https://economictimes.indiatimes.com/markets/commodities/rssfeeds/1808152121.cms",
    "Economic Times - Markets":     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "BusinessLine - Commodities":   "https://www.thehindubusinessline.com/markets/commodities/feeder/default.rss",
    "BusinessLine - Gold & Silver": "https://www.thehindubusinessline.com/markets/gold/feeder/default.rss",
    "BusinessLine - Commodity Analysis": "https://www.thehindubusinessline.com/portfolio/commodity-analysis/feeder/default.rss",
    "BusinessLine - Agri":          "https://www.thehindubusinessline.com/economy/agri-business/feeder/default.rss",
    # Indian sources via Google News (last 2 days)
    "Economic Times":     gnews("(commodities OR gold OR silver OR crude OR copper) site:economictimes.indiatimes.com when:2d"),
    "Moneycontrol":       gnews("(commodities OR gold OR silver OR crude OR MCX) site:moneycontrol.com when:2d"),
    "Business Standard":  gnews("(commodities OR gold OR silver OR crude) site:business-standard.com when:2d"),
    "Mint":               gnews("(commodities OR gold OR silver OR crude) site:livemint.com when:2d"),
    "Hindu BusinessLine": gnews("(commodities OR gold OR silver OR crude OR spices) site:thehindubusinessline.com when:2d"),
    "MCX news":           gnews("MCX Multi Commodity Exchange when:2d"),
    "NCDEX news":         gnews("NCDEX agri commodities when:2d"),
    "Trading Economics (backup)": gnews("commodities site:tradingeconomics.com when:2d"),
}

# ---- commodity tagging ------------------------------------------------
TAGS = {
    "Gold":        r"\bgold\b|bullion",
    "Silver":      r"\bsilver\b",
    "Crude Oil":   r"\bcrude\b|\bbrent\b|\bwti\b|\bopec\b|\boil\b",
    "Natural Gas": r"natural gas|\blng\b|\bttf\b|henry hub",
    "Copper":      r"\bcopper\b",
    "Base Metals": r"alumini?um|\bzinc\b|\bnickel\b|iron ore|\bsteel\b|base metal|\blme\b",
    "Agri":        r"wheat|sugar|cotton|soy?abean|\bcorn\b|maize|\brice\b|palm oil|edible oil|coffee|cocoa|turmeric|jeera|cumin|guar|mentha|cardamom|pepper|spices|castor|chana|mustard|rapeseed|canola|oilseed|pulses",
}
AGRI_CONTEXT = r"price|futures|export|import|crop|supply|demand|msp|mcx|ncdex|mandi|rally|output|production|stocks|harvest|acreage|sowing|monsoon|tonnes|quintal|bushel|contract|duty"
NON_CRUDE_OIL = r"(palm|edible|cooking|mustard|soy|soybean|olive|coconut|sunflower|vegetable|castor) oil"
GENERIC = r"commodit|\bmcx\b|\bncdex\b"
EXCLUDE = r"gold (international|finance|loan|ltd|limited|corp)|newborn|welfare scheme|medal|asiad|olympic|asian games|bronze|athlet|cricket|tournament|gold coast|silver screen|box office|movie|film\b|actor|bollywood"

def tag_story(title, summary):
    if re.search(EXCLUDE, title, re.I):
        return []
    text = f"{title} {summary}".lower()
    tags = []
    for name, pat in TAGS.items():
        t = re.sub(NON_CRUDE_OIL, "", text) if name == "Crude Oil" else text
        if re.search(pat, t):
            if name == "Agri" and not re.search(AGRI_CONTEXT, text):
                continue
            tags.append(name)
    if not tags and re.search(GENERIC, text):
        tags.append("Commodities")
    return tags[:2]

# ---- helpers -----------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")

def clean_summary(raw, title):
    text = html.unescape(TAG_RE.sub(" ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.lower() == title.lower().strip():
        return ""
    if len(text) > SUMMARY_MAX:
        cut = text[:SUMMARY_MAX]
        end = cut.rfind(". ")
        text = cut[:end + 1] if end > SUMMARY_MAX * 0.5 else cut.rsplit(" ", 1)[0] + "…"
    return text

def time_ago(dt):
    if not dt:
        return "unknown time"
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} hr ago"
    return f"{int(s // 86400)} days ago"

def parse_dt(txt):
    """Parse ISO / RFC dates from article pages -> aware UTC datetime."""
    if not txt:
        return None
    txt = txt.strip()
    try:
        d = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except Exception:
        try:
            d = parsedate_to_datetime(txt)
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=IST)
    d = d.astimezone(timezone.utc)
    return None if d > datetime.now(timezone.utc) + timedelta(days=1) else d

META_RE = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*?content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:description|description)["\']', re.I),
]
DATE_RE = [
    re.compile(r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|og:published_time|datePublished|pubdate|publishdate)["\'][^>]*?content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name|itemprop)=["\'](?:article:published_time|og:published_time|datePublished)["\']', re.I),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
]

def fetch_article_info(gurl):
    """Best effort: decode the Google News link, then read the article's
    summary and REAL publish date. Any failure just returns blanks."""
    info = {"url": "", "summary": "", "pub": ""}
    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(gurl, interval=1)
        real = res.get("decoded_url") if res.get("status") else ""
        if not real:
            return info
        info["url"] = real
        req = urllib.request.Request(real, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            page = r.read(300000).decode("utf-8", "ignore")
        for rx in META_RE:
            m = rx.search(page)
            if m:
                info["summary"] = html.unescape(m.group(1)).strip()
                break
        for rx in DATE_RE:
            m = rx.search(page)
            d = parse_dt(html.unescape(m.group(1))) if m else None
            if d:
                info["pub"] = d.isoformat()
                break
        if not info["pub"]:
            m = re.search(r'/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:[/-]|$)', real)
            if m:
                y, mo, dd = map(int, m.groups())
                info["pub"] = datetime(y, mo, dd, tzinfo=IST).astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    return info

def timed(fn, arg, seconds):
    """Run fn(arg) but give up after `seconds` (a stuck lookup can't hang the run)."""
    box = {}
    t = threading.Thread(target=lambda: box.update(v=fn(arg)), daemon=True)
    t.start()
    t.join(seconds)
    return box.get("v") or {"url": "", "summary": "", "pub": ""}

def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            c = json.load(f)
        return c if c.get("_v") == 3 else {}
    except Exception:
        return {}

# ---- fetch -------------------------------------------------------------
MIN_DT = datetime(1970, 1, 1, tzinfo=timezone.utc)

def fetch_all():
    items = []
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[!] Could not fetch {source}: {e}")
            continue
        if not feed.entries:
            print(f"[!] {source} returned no entries — feed may have moved or blocked us.")
            continue
        is_g = "news.google.com" in url
        for e in feed.entries:
            title = e.get("title", "").strip()
            raw = e.get("summary", "")
            label = source
            if is_g:
                if " - " in title:
                    title, label = title.rsplit(" - ", 1)
                    label = label.strip()
                raw = ""
            tags = tag_story(title, html.unescape(TAG_RE.sub(" ", raw)))
            if not tags:
                continue
            st = e.get("published_parsed") or e.get("updated_parsed")
            dt = datetime(*st[:6], tzinfo=timezone.utc) if st else None
            link = e.get("link", "")
            items.append({"source": label, "title": title, "link": link, "glink": link,
                          "summary": clean_summary(raw, title), "tags": tags,
                          "dt": dt, "verified": not is_g, "google": is_g})
    best = {}
    for it in sorted(items, key=lambda x: x["dt"] or MIN_DT, reverse=True):
        k = re.sub(r"[^a-z0-9]", "", it["title"].lower())
        if k not in best or (it["summary"] and not best[k]["summary"]):
            best[k] = it
    out = sorted(best.values(), key=lambda x: x["dt"] or MIN_DT, reverse=True)[:MAX_STORIES + 60]

    # Google News stories: read summary + REAL publish date from the article
    # (Google's own timestamps are unreliable). Cached, capped per run.
    cache, fetched, fails, t0 = load_cache(), 0, 0, time.time()
    for it in out:
        if not it["google"]:
            continue
        info = cache.get(it["glink"])
        in_budget = (fetched < MAX_ENRICH_PER_RUN and fails < MAX_CONSEC_FAILS
                     and time.time() - t0 < ENRICH_BUDGET_SEC)
        if (info is None or "pub" not in info) and in_budget:
            info = cache[it["glink"]] = timed(fetch_article_info, it["glink"], PER_ARTICLE_SEC)
            fetched += 1
            fails = 0 if info.get("url") else fails + 1
            if not info.get("pub") and fetched <= 40:
                print(f"[date?] no date: {it['source']} | {it['title'][:45]} | "
                      f"{info.get('url') or 'link decode failed'}")
        if info:
            it["summary"] = clean_summary(info.get("summary", ""), it["title"])
            if info.get("url"):
                it["link"] = info["url"]
            pub = parse_dt(info.get("pub", ""))
            if pub:
                it["dt"], it["verified"] = pub, True
    try:
        keep = {i["glink"] for i in out if i["google"]}
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"_v": 3, **{k: v for k, v in cache.items() if k in keep}}, f)
    except Exception:
        pass

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    out = [i for i in out if i["dt"] is None or i["dt"] >= cutoff]
    out = sorted(out, key=lambda x: x["dt"] or MIN_DT, reverse=True)[:MAX_STORIES]
    g = [i for i in out if i["google"]]
    print(f"Google News stories with verified dates: {sum(i['verified'] for i in g)}/{len(g)}")
    for i in out:
        i["ago"] = time_ago(i["dt"]) if i["verified"] else "~" + time_ago(i["dt"])
    return out

# ---- page --------------------------------------------------------------
CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,'Segoe UI',Arial,sans-serif;background:#f1f3f6;color:#1b1f2a}
header{background:linear-gradient(135deg,#0f1b3d,#1d3468);color:#fff;padding:22px 24px 16px}
header h1{margin:0;font-size:22px;letter-spacing:.2px}
header .meta{margin-top:4px;font-size:12px;color:#b9c4e2}
.bar{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid #e3e6ec;padding:10px 16px;display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
.f{border:1px solid #d5d9e2;background:#fff;border-radius:999px;padding:5px 12px;font-size:12.5px;cursor:pointer;color:#33394a}
.f.on{background:#0f1b3d;border-color:#0f1b3d;color:#fff}
.wrap{max-width:900px;margin:18px auto;padding:0 14px;display:flex;flex-direction:column;gap:10px}
.card{display:flex;gap:16px;justify-content:space-between;background:#fff;border-radius:10px;border-left:5px solid var(--c,#8a93a6);padding:14px 16px;box-shadow:0 1px 3px rgba(20,30,60,.08)}
.main{min-width:0;flex:1}
.title{display:block;font-size:16px;font-weight:650;line-height:1.35;color:#0f1b3d;text-decoration:none}
.title:hover{text-decoration:underline}
.summary{margin:6px 0 0;font-size:13.5px;line-height:1.55;color:#4a5163}
.src{margin-top:8px;font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:#7a8296}
.side{flex:0 0 118px;display:flex;flex-direction:column;align-items:flex-end;gap:8px;text-align:right}
.chip{display:inline-block;font-size:11.5px;font-weight:600;padding:3px 9px;border-radius:5px;background:var(--bg,#eceef3);color:var(--c,#4a5163)}
.ago{font-size:12px;color:#8a92a5;white-space:nowrap}
.t-gold{--c:#8a6500;--bg:#fff2c2}.t-silver{--c:#4a5563;--bg:#e9edf2}.t-crude-oil{--c:#6b3f1d;--bg:#f1e3d3}
.t-natural-gas{--c:#0b5c8a;--bg:#dcf1fd}.t-copper{--c:#a4461a;--bg:#fde2d2}.t-base-metals{--c:#3a4a8a;--bg:#e3e7f8}
.t-agri{--c:#2b6b2a;--bg:#e0f3de}.t-commodities{--c:#444a5a;--bg:#eceef3}
.empty{text-align:center;color:#8a92a5;padding:30px}
@media(max-width:560px){.card{flex-direction:column;gap:8px}.side{flex-direction:row;align-items:center;justify-content:space-between;flex-basis:auto;text-align:left}}
"""
JS = """
const fs=[...document.querySelectorAll('.f')],cs=[...document.querySelectorAll('.card')];
fs.forEach(b=>b.onclick=()=>{fs.forEach(x=>x.classList.remove('on'));b.classList.add('on');
const t=b.dataset.tag;cs.forEach(c=>{c.style.display=(t==='All'||c.dataset.tags.split('|').includes(t))?'':'none'})});
"""
slug = lambda t: t.lower().replace(" ", "-")
esc = html.escape

def write_html(items):
    counts = {}
    for it in items:
        for t in it["tags"]:
            counts[t] = counts.get(t, 0) + 1
    btns = f'<button class="f on" data-tag="All">All ({len(items)})</button>' + "".join(
        f'<button class="f" data-tag="{esc(t)}">{esc(t)} ({n})</button>'
        for t, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    cards = []
    for it in items:
        chips = "".join(f'<span class="chip t-{slug(t)}">{esc(t)}</span>' for t in it["tags"])
        summ = f'<p class="summary">{esc(it["summary"])}</p>' if it["summary"] else ""
        cards.append(
            f'<article class="card t-{slug(it["tags"][0])}" data-tags="{esc("|".join(it["tags"]))}">'
            f'<div class="main"><a class="title" href="{esc(it["link"])}" target="_blank" rel="noopener">{esc(it["title"])}</a>'
            f'{summ}<div class="src">{esc(it["source"])}</div></div>'
            f'<div class="side"><div>{chips}</div><div class="ago">{esc(it["ago"])}</div></div></article>')
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    page = (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="1800"><title>Commodities News</title>'
            f'<style>{CSS}</style></head><body><header><h1>Commodities News</h1>'
            f'<div class="meta">Updated {now} IST · {len(items)} stories · refreshes every 30 min · “~” = approximate time (from Google News)</div></header>'
            f'<div class="bar">{btns}</div><div class="wrap">{"".join(cards) or "<div class=empty>No stories right now.</div>"}</div>'
            f'<script>{JS}</script></body></html>')
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)
    with_sum = sum(1 for i in items if i["summary"])
    print(f"Wrote {len(items)} stories to {OUT} ({with_sum} with summaries)")

if __name__ == "__main__":
    write_html(fetch_all())
