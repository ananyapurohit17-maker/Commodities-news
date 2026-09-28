#!/usr/bin/env python3
"""
Commodity News Dashboard -> docs/index.html

Features
--------
- Aggregates commodity news from publisher RSS + Google News discovery.
- Reuters is included through Google News site:reuters.com discovery.
- Verifies publisher dates for Google News-discovered stories before showing them.
- Drops stories older than MAX_AGE_DAYS.
- Displays exact IST publication time + relative age.
- Professional Trading-Economics-inspired dashboard:
  hero story, latest-news grid, commodity/source/time filters, sidebar.
- Extracts publisher og:image where available.
"""

import feedparser
import html
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

IST = timezone(timedelta(hours=5, minutes=30))

OUT = "docs/index.html"
CACHE = "docs/summary_cache.json"

SUMMARY_MAX = 430
MAX_STORIES = 180
MAX_AGE_DAYS = 5

# Google News items must be publisher-verified before display.
MAX_ENRICH_PER_RUN = 180
ENRICH_BUDGET_SEC = 300
PER_ARTICLE_SEC = 12
MAX_CONSEC_FAILS = 10


CACHE_VERSION = 6

# These sources are important for commodity research and get verified first,
# so mainstream feeds cannot consume the entire Google-News verification budget.
PRIORITY_SOURCES = {
    "AL Circle",
    "ALCircle",
    "International Aluminium Institute",
    "SMM",
    "Shanghai Metals Market",
    "Metal.com",
    "Fibre2Fashion",
    "Agriwatch",
    "IEA",
    "International Energy Agency",
    "Forex Factory",
    "ChiniMandi",
    "USDA",
    "U.S. Department of Agriculture",
    "Reuters",
}

# Reserve a reasonable number of candidate stories per specialist publisher.
PRIORITY_PER_SOURCE = 12


# ---------------------------------------------------------------------
# FEEDS
# ---------------------------------------------------------------------

def gnews(q):
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(q)
        + "&hl=en-IN&gl=IN&ceid=IN:en"
    )


FEEDS = {
    # Direct commodity feeds
    "Barchart - Metals": "https://www.barchart.com/news/rss/commodities/metals",
    "Barchart - Energies": "https://www.barchart.com/news/rss/commodities/energies",
    "Barchart - Grains": "https://www.barchart.com/news/rss/commodities/grains",
    "Barchart - Softs": "https://www.barchart.com/news/rss/commodities/softs",
    "Kitco": "https://www.kitco.com/rss/KitcoNews.xml",
    "Investing.com": "https://www.investing.com/rss/commodities.rss",
    "Business Recorder": "https://www.brecorder.com/feeds/news/1761",
    "Trading Economics": "https://tradingeconomics.com/rss/news.aspx",

    # Indian publisher feeds
    "Economic Times - Commodities":
        "https://economictimes.indiatimes.com/markets/commodities/rssfeeds/1808152121.cms",
    "Economic Times - Markets":
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "BusinessLine - Commodities":
        "https://www.thehindubusinessline.com/markets/commodities/feeder/default.rss",
    "BusinessLine - Gold & Silver":
        "https://www.thehindubusinessline.com/markets/gold/feeder/default.rss",
    "BusinessLine - Commodity Analysis":
        "https://www.thehindubusinessline.com/portfolio/commodity-analysis/feeder/default.rss",
    "BusinessLine - Agri":
        "https://www.thehindubusinessline.com/economy/agri-business/feeder/default.rss",

    # Google News discovery. Real publisher date is verified later.
    "Reuters":
        gnews(
            '(gold OR silver OR crude OR oil OR natural gas OR LNG OR copper OR '
            'aluminium OR aluminum OR zinc OR nickel OR iron ore OR steel OR '
            'cotton OR wheat OR sugar OR soybean OR corn OR rice OR palm oil OR '
            'coffee OR cocoa OR turmeric OR cumin OR jeera OR guar) '
            'site:reuters.com when:2d'
        ),
    "Economic Times":
        gnews(
            '(commodities OR gold OR silver OR crude OR copper OR cotton OR '
            'turmeric OR jeera OR guar OR sugar OR wheat) '
            'site:economictimes.indiatimes.com when:2d'
        ),
    "Moneycontrol":
        gnews(
            '(commodities OR gold OR silver OR crude OR MCX OR NCDEX OR cotton OR '
            'turmeric OR jeera OR guar) site:moneycontrol.com when:2d'
        ),
    "Business Standard":
        gnews(
            '(commodities OR gold OR silver OR crude OR metals OR agriculture) '
            'site:business-standard.com when:2d'
        ),
    "Mint":
        gnews(
            '(commodities OR gold OR silver OR crude OR metals) '
            'site:livemint.com when:2d'
        ),
    "Hindu BusinessLine":
        gnews(
            '(commodities OR gold OR silver OR crude OR spices OR cotton OR '
            'turmeric OR jeera OR guar) site:thehindubusinessline.com when:2d'
        ),
    "MCX":
        gnews('"MCX" "Multi Commodity Exchange" when:2d'),
    "NCDEX":
        gnews('"NCDEX" agri commodities when:2d'),
    "Trading Economics (backup)":
        gnews('commodities site:tradingeconomics.com when:2d'),

    "ALCircle":
        gnews('(aluminium OR aluminum OR alumina OR bauxite OR smelter) site:alcircle.com when:3d'),
    "International Aluminium Institute":
        gnews('(aluminium OR aluminum OR alumina OR bauxite OR primary aluminium OR smelter) site:international-aluminium.org when:7d'),
    "SMM / Metal.com":
        gnews('(aluminium OR aluminum OR copper OR zinc OR nickel OR lead OR tin OR iron ore OR steel) site:news.metal.com when:3d'),
    "Fibre2Fashion":
        gnews('(cotton OR cotton yarn OR cotton crop OR cotton sowing OR cotton prices) site:fibre2fashion.com/news when:3d'),
    "Agriwatch":
        gnews('(cotton OR guar OR turmeric OR jeera OR cumin OR sugar OR wheat OR soybean OR maize OR rice OR mustard OR palm oil) site:agriwatch.com when:3d'),
    "IEA":
        gnews('(oil OR crude OR natural gas OR LNG OR refinery OR diesel OR gasoline OR energy market) site:iea.org/news when:7d'),
    "Forex Factory":
        gnews('(gold OR silver OR crude oil OR brent OR WTI OR natural gas OR LNG OR copper OR aluminium OR aluminum OR zinc OR wheat OR cotton OR sugar) site:forexfactory.com/news when:2d'),
    "ChiniMandi":
        gnews('(sugar OR ethanol OR cane OR sugarcane OR molasses OR sugar production OR sugar exports) site:chinimandi.com when:3d'),
    "USDA":
        gnews('(cotton OR wheat OR corn OR maize OR soybeans OR rice OR sugar OR crop OR production OR exports OR stocks OR acreage OR yield) site:usda.gov when:7d'),
}


# ---------------------------------------------------------------------
# CLASSIFICATION
# ---------------------------------------------------------------------

SPECIFIC_TAGS = {
    "Gold": r"\bgold\b|bullion",
    "Silver": r"\bsilver\b",
    "Crude Oil": r"\bcrude\b|\bbrent\b|\bwti\b|\bopec\+?\b|\boil prices?\b",
    "Natural Gas": r"\bnatural gas\b|\blng\b|\bttf\b|henry hub",
    "Copper": r"\bcopper\b",
    "Aluminium": r"\balumini?um\b",
    "Zinc": r"\bzinc\b",
    "Nickel": r"\bnickel\b",
    "Steel": r"\bsteel\b|\biron ore\b",
    "Cotton": r"\bcotton\b",
    "Guar": r"\bguar\b",
    "Turmeric": r"\bturmeric\b|\bhaldi\b",
    "Jeera": r"\bjeera\b|\bcumin\b",
    "Sugar": r"\bsugar\b",
    "Wheat": r"\bwheat\b",
    "Soybean": r"\bsoybeans?\b|\bsoya\b",
    "Corn": r"\bcorn\b|\bmaize\b",
    "Rice": r"\brice\b",
    "Mustard": r"\bmustard\b|\brapeseed\b|\bcanola\b",
    "Palm Oil": r"\bpalm oil\b",
    "Coffee": r"\bcoffee\b",
    "Cocoa": r"\bcocoa\b",
}

AGRI_TAGS = {
    "Cotton", "Guar", "Turmeric", "Jeera", "Sugar", "Wheat",
    "Soybean", "Corn", "Rice", "Mustard", "Palm Oil", "Coffee", "Cocoa",
}
METAL_TAGS = {"Gold", "Silver", "Copper", "Aluminium", "Zinc", "Nickel", "Steel"}
ENERGY_TAGS = {"Crude Oil", "Natural Gas"}

AGRI_CONTEXT = (
    r"price|futures|export|import|crop|supply|demand|msp|mcx|ncdex|mandi|"
    r"rally|output|production|stocks|harvest|acreage|sowing|monsoon|tonnes|"
    r"quintal|bushel|contract|duty|yield|inventory|shipment|weather"
)

GENERIC_COMMODITY = r"\bcommodit(?:y|ies)\b|\bmcx\b|\bncdex\b|\blme\b"

EXCLUDE = (
    r"gold (international|finance|loan|ltd|limited|corp)|newborn|welfare scheme|"
    r"medal|asiad|olympic|asian games|bronze|athlet|cricket|tournament|gold coast|"
    r"silver screen|box office|movie|film\b|actor|bollywood|"
    r"\bipo\b|initial public offering|share allotment|listing gains?|listing premium|"
    r"grey market premium|\bgmp\b|subscription status|"
    r"\bstock (price|market|markets|split|buyback|dividend)\b|"
    r"\bshares? (rise|rises|fall|falls|jump|jumps|surge|surges|drop|drops|gain|gains)\b|"
    r"\bquarterly results?\b|\bearnings\b|\bprofit after tax\b|\bnet profit\b|"
    r"\brevenue (rose|fell|rises|falls|growth)\b|\bebitda\b|\bdividend\b|"
    r"\bmutual funds?\b|\bsensex\b|\bnifty\b|\bbse\b|\bnse\b"
)


def classify_story(title, summary):
    """Keep only genuine commodity-market stories."""
    title_l = title.lower()
    text = f"{title} {summary}".lower()

    if re.search(EXCLUDE, text, re.I):
        return [], "Other"

    title_tags = []
    for name, pattern in SPECIFIC_TAGS.items():
        if re.search(pattern, title_l, re.I):
            title_tags.append(name)

    generic_title = re.search(GENERIC_COMMODITY, title_l, re.I)

    # Commodity keyword must be in the headline, not merely buried in summary.
    if not title_tags and not generic_title:
        return [], "Other"

    # Agriculture stories need clear commodity/crop/market context.
    if title_tags and any(tag in AGRI_TAGS for tag in title_tags):
        if not re.search(AGRI_CONTEXT, text, re.I):
            return [], "Other"

    # Reject equity/IPO/corporate-result headlines.
    if re.search(
        r"\b(ipo|stock|stocks|shares|equity|equities|sensex|nifty|dividend|"
        r"earnings|quarterly results|market cap|brokerage target|price target)\b",
        title_l, re.I
    ):
        return [], "Other"

    seen = set()
    tags = [x for x in title_tags if not (x in seen or seen.add(x))][:3]

    if any(x in AGRI_TAGS for x in tags):
        group = "Agri"
    elif any(x in ENERGY_TAGS for x in tags):
        group = "Energy"
    elif any(x in METAL_TAGS for x in tags):
        group = "Metals"
    elif generic_title:
        group = "Commodities"
        tags = ["Commodities"]
    else:
        return [], "Other"

    return tags, group


# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value):
    return html.unescape(TAG_RE.sub(" ", value or ""))


def clean_summary(raw, title):
    text = re.sub(r"\s+", " ", strip_html(raw)).strip()
    if not text or text.lower() == title.lower().strip():
        return ""
    if len(text) > SUMMARY_MAX:
        cut = text[:SUMMARY_MAX]
        end = cut.rfind(". ")
        text = (
            cut[: end + 1]
            if end > SUMMARY_MAX * 0.55
            else cut.rsplit(" ", 1)[0] + "…"
        )
    return text


def parse_dt(txt):
    """Parse common ISO/RFC publisher dates and return aware UTC datetime."""
    if not txt:
        return None

    txt = str(txt).strip()
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

    # Reject implausible future dates.
    if d > datetime.now(timezone.utc) + timedelta(hours=12):
        return None
    return d


def time_ago(dt):
    if not dt:
        return "Unknown time"

    seconds = max(0, (datetime.now(timezone.utc) - dt).total_seconds())

    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        n = int(seconds // 60)
        return f"{n} min ago"
    if seconds < 86400:
        n = int(seconds // 3600)
        return f"{n} hr ago"
    n = int(seconds // 86400)
    return f"{n} day{'s' if n != 1 else ''} ago"


def source_domain(url):
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        return domain
    except Exception:
        return ""


def source_short(name):
    name = re.sub(r"\s+\(backup\)$", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+.*$", "", name)
    return name.strip()


def entry_image(entry):
    """Best effort RSS image extraction."""
    try:
        media = entry.get("media_content") or []
        if media and media[0].get("url"):
            return media[0]["url"]
    except Exception:
        pass

    try:
        thumbs = entry.get("media_thumbnail") or []
        if thumbs and thumbs[0].get("url"):
            return thumbs[0]["url"]
    except Exception:
        pass

    try:
        for enclosure in entry.get("enclosures", []):
            if enclosure.get("type", "").startswith("image/") and enclosure.get("href"):
                return enclosure["href"]
    except Exception:
        pass

    return ""


META_DESC_RE = [
    re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:og:description|description|twitter:description)["\'][^>]*?content=["\']([^"\']+)',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:description|description|twitter:description)["\']',
        re.I,
    ),
]

DATE_RE = [
    re.compile(
        r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|og:published_time|datePublished|pubdate|publishdate)["\'][^>]*?content=["\']([^"\']+)',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name|itemprop)=["\'](?:article:published_time|og:published_time|datePublished)["\']',
        re.I,
    ),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
]

IMAGE_RE = [
    re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*?content=["\']([^"\']+)',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image|twitter:image)["\']',
        re.I,
    ),
]


def fetch_article_info(gurl):
    """
    Decode a Google News URL, then extract publisher summary/date/image.
    Google News stories are only displayed when a real publisher date is found.
    """
    info = {"url": "", "summary": "", "pub": "", "image": ""}

    try:
        from googlenewsdecoder import gnewsdecoder

        res = gnewsdecoder(gurl, interval=1)
        real = res.get("decoded_url") if res.get("status") else ""

        if not real:
            return info

        info["url"] = real

        req = urllib.request.Request(
            real,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                )
            },
        )

        with urllib.request.urlopen(req, timeout=8) as r:
            page = r.read(450000).decode("utf-8", "ignore")

        for rx in META_DESC_RE:
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

        for rx in IMAGE_RE:
            m = rx.search(page)
            if m:
                info["image"] = html.unescape(m.group(1)).strip()
                break

        # Last-resort date from publisher URL path.
        if not info["pub"]:
            m = re.search(
                r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:[/?#-]|$)", real
            )
            if m:
                y, mo, dd = map(int, m.groups())
                d = datetime(y, mo, dd, tzinfo=IST).astimezone(timezone.utc)
                info["pub"] = d.isoformat()

    except Exception:
        pass

    return info


def timed(fn, arg, seconds):
    box = {}
    t = threading.Thread(target=lambda: box.update(v=fn(arg)), daemon=True)
    t.start()
    t.join(seconds)
    return box.get("v") or {"url": "", "summary": "", "pub": "", "image": ""}


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)
        return cache if cache.get("_v") == CACHE_VERSION else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------
# FETCH
# ---------------------------------------------------------------------

MIN_DT = datetime(1970, 1, 1, tzinfo=timezone.utc)


def fetch_all():
    items = []

    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            print(f"[!] Could not fetch {source}: {exc}")
            continue

        if not feed.entries:
            print(f"[!] {source} returned no entries.")
            continue

        is_google = "news.google.com" in url

        for entry in feed.entries:
            title = entry.get("title", "").strip()
            if not title:
                continue

            raw = entry.get("summary", "") or entry.get("description", "")
            label = source

            if is_google:
                # Google News title is usually "Headline - Publisher".
                if " - " in title:
                    title, publisher = title.rsplit(" - ", 1)
                    publisher = publisher.strip()
                    if publisher:
                        label = publisher
                raw = ""

            tags, group = classify_story(title, strip_html(raw))
            if not tags:
                continue

            st = entry.get("published_parsed") or entry.get("updated_parsed")
            dt = datetime(*st[:6], tzinfo=timezone.utc) if st else None
            link = entry.get("link", "")

            items.append(
                {
                    "source": source_short(label),
                    "title": title,
                    "link": link,
                    "glink": link,
                    "summary": clean_summary(raw, title),
                    "tags": tags,
                    "group": group,
                    "dt": dt,
                    "verified": not is_google,
                    "google": is_google,
                    "image": entry_image(entry),
                }
            )

    # De-duplicate by normalized title.
    best = {}
    for item in sorted(items, key=lambda x: x["dt"] or MIN_DT, reverse=True):
        key = re.sub(r"[^a-z0-9]", "", item["title"].lower())

        if key not in best:
            best[key] = item
        elif item["summary"] and not best[key]["summary"]:
            best[key] = item

    out = sorted(
        best.values(),
        key=lambda x: x["dt"] or MIN_DT,
        reverse=True,
    )[: MAX_STORIES + 100]

    # Enrich + VERIFY Google News stories.
    # IMPORTANT: specialist commodity publishers are verified FIRST, with
    # per-source fairness, so Reuters/ET/BusinessLine cannot consume the full
    # enrichment budget before AL Circle, SMM, Fibre2Fashion, ChiniMandi, etc.
    cache = load_cache()
    fetched = 0
    fails = 0
    started = time.time()

    google_items = [i for i in out if i["google"]]

    def is_priority(item):
        s = item["source"].lower()
        keys = (
            "al circle", "alcircle", "international aluminium",
            "smm", "metal.com", "shanghai metals",
            "fibre2fashion", "agriwatch", "iea",
            "international energy agency", "forex factory",
            "chinimandi", "usda", "u.s. department of agriculture",
            "reuters",
        )
        return any(k in s for k in keys)

    priority = [i for i in google_items if is_priority(i)]
    normal = [i for i in google_items if not is_priority(i)]

    # Round-robin specialist sources so one prolific site (e.g. SMM) does not
    # crowd out the others.
    buckets = {}
    for item in priority:
        buckets.setdefault(item["source"], []).append(item)

    priority_order = []
    for n in range(PRIORITY_PER_SOURCE):
        for source_name in sorted(buckets):
            bucket = buckets[source_name]
            if n < len(bucket):
                priority_order.append(bucket[n])

    enrichment_order = priority_order + normal

    for item in enrichment_order:
        info = cache.get(item["glink"])

        within_budget = (
            fetched < MAX_ENRICH_PER_RUN
            and fails < MAX_CONSEC_FAILS
            and time.time() - started < ENRICH_BUDGET_SEC
        )

        # Cached entries are always reusable. New network lookups obey budget.
        if (info is None or "pub" not in info) and within_budget:
            info = timed(fetch_article_info, item["glink"], PER_ARTICLE_SEC)
            cache[item["glink"]] = info
            fetched += 1
            fails = 0 if info.get("url") else fails + 1

        if info:
            if info.get("url"):
                item["link"] = info["url"]

            if info.get("summary"):
                item["summary"] = clean_summary(info["summary"], item["title"])

            if info.get("image"):
                item["image"] = info["image"]

            pub = parse_dt(info.get("pub", ""))
            if pub:
                item["dt"] = pub
                item["verified"] = True

    # Save only current Google items.
    try:
        keep = {i["glink"] for i in out if i["google"]}
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "_v": CACHE_VERSION,
                    **{k: v for k, v in cache.items() if k in keep},
                },
                f,
            )
    except Exception:
        pass

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    # Critical freshness rule:
    # Direct publisher RSS is trusted.
    # Google News-discovered articles are kept ONLY if publisher date was verified.
    out = [
        i
        for i in out
        if i["dt"] is not None
        and i["dt"] >= cutoff
        and (not i["google"] or i["verified"])
    ]

    out = sorted(out, key=lambda x: x["dt"], reverse=True)[:MAX_STORIES]

    for item in out:
        item["ago"] = time_ago(item["dt"])
        item["published"] = item["dt"].astimezone(IST).strftime(
            "%d %b %Y, %I:%M %p IST"
        )
        item["domain"] = source_domain(item["link"])

    print(
        f"Kept {len(out)} stories; "
        f"verified Google stories: {sum(1 for i in out if i['google'])}"
    )

    # Helpful GitHub Actions diagnostics: show exactly which publishers made it.
    source_counts = {}
    for item in out:
        source_counts[item["source"]] = source_counts.get(item["source"], 0) + 1
    print("Sources kept:")
    for source_name, count in sorted(source_counts.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        print(f"  {source_name}: {count}")

    return out


# ---------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------

CSS = r"""
:root{
  --nav:#272727;
  --nav2:#353535;
  --ink:#171b22;
  --muted:#747b86;
  --line:#dde1e7;
  --paper:#ffffff;
  --bg:#f3f4f6;
  --blue:#153d73;
  --green:#188038;
  --red:#c62828;
  --gold:#9b7410;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;
  background:var(--bg);
  color:var(--ink);
  font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
}
.top{
  background:var(--nav);
  color:#fff;
  border-bottom:1px solid #454545;
}
.top-inner{
  max-width:1380px;
  margin:auto;
  min-height:64px;
  padding:0 22px;
  display:flex;
  align-items:center;
  gap:28px;
}
.brand{
  font-size:21px;
  font-weight:800;
  line-height:1;
  letter-spacing:-.3px;
  white-space:nowrap;
}
.brand small{
  display:block;
  margin-top:5px;
  font-size:10px;
  font-weight:600;
  letter-spacing:1.5px;
  color:#bfc4ca;
}
.nav{
  display:flex;
  align-items:center;
  gap:25px;
  margin-left:auto;
}
.nav a{
  color:#fff;
  text-decoration:none;
  font-size:14px;
}
.nav a:hover{color:#d8e7ff}
.search-shell{
  width:245px;
  border-bottom:1px solid #777;
}
.search-shell input{
  width:100%;
  padding:10px 2px;
  background:transparent;
  color:#fff;
  border:0;
  outline:none;
}
.search-shell input::placeholder{color:#aaa}
.status{
  background:#fff;
  border-bottom:1px solid var(--line);
}
.status-inner{
  max-width:1380px;
  margin:auto;
  padding:10px 22px;
  display:flex;
  justify-content:space-between;
  gap:15px;
  color:#677080;
  font-size:12px;
}
.filterbar{
  position:sticky;
  top:0;
  z-index:10;
  background:rgba(255,255,255,.97);
  border-bottom:1px solid var(--line);
  backdrop-filter:blur(10px);
}
.filters{
  max-width:1380px;
  margin:auto;
  padding:9px 22px;
  display:flex;
  gap:7px;
  overflow-x:auto;
  scrollbar-width:none;
}
.filters::-webkit-scrollbar{display:none}
.filter{
  border:1px solid #d9dde4;
  background:#fff;
  border-radius:4px;
  padding:7px 11px;
  white-space:nowrap;
  font-size:12px;
  color:#424956;
  cursor:pointer;
}
.filter:hover,.filter.on{
  background:#1f2e43;
  border-color:#1f2e43;
  color:#fff;
}
.page{
  max-width:1380px;
  margin:0 auto;
  padding:30px 22px 55px;
}
.hero-grid{
  display:grid;
  grid-template-columns:minmax(0,2fr) minmax(320px,.92fr);
  gap:20px;
  align-items:stretch;
}
.hero{
  min-height:390px;
  position:relative;
  overflow:hidden;
  background:linear-gradient(135deg,#1f334b,#0e1722);
  color:#fff;
  border:1px solid #d6dae0;
}
.hero-media{
  position:absolute;
  inset:0;
}
.hero-media img{
  width:100%;
  height:100%;
  object-fit:cover;
  display:block;
}
.hero-shade{
  position:absolute;
  inset:0;
  background:linear-gradient(90deg,rgba(8,14,22,.90) 0%,rgba(8,14,22,.64) 50%,rgba(8,14,22,.18) 100%);
}
.hero-content{
  position:relative;
  z-index:2;
  width:min(690px,78%);
  min-height:390px;
  padding:45px 34px 30px;
  display:flex;
  flex-direction:column;
  justify-content:flex-end;
}
.kicker{
  font-size:11px;
  font-weight:800;
  text-transform:uppercase;
  letter-spacing:1.1px;
  color:#dce9ff;
  margin-bottom:12px;
}
.hero h1{
  margin:0;
  font-size:35px;
  line-height:1.12;
  letter-spacing:-.65px;
}
.hero p{
  margin:16px 0 0;
  max-width:650px;
  font-size:15px;
  line-height:1.6;
  color:#eef2f6;
}
.meta{
  margin-top:18px;
  display:flex;
  flex-wrap:wrap;
  gap:9px 14px;
  font-size:11.5px;
  color:#d7dce3;
}
.hero a.cover-link{
  position:absolute;
  inset:0;
  z-index:4;
}
.sidebox{
  background:#fff;
  border:1px solid var(--line);
  padding:18px 18px 13px;
}
.sidebox h2{
  margin:0 0 8px;
  font-size:17px;
}
.side-tabs{
  display:flex;
  gap:16px;
  border-bottom:1px solid var(--line);
  margin-bottom:5px;
  overflow:auto;
}
.side-tab{
  padding:7px 0 9px;
  font-size:12px;
  white-space:nowrap;
  color:#5f6672;
  border-bottom:2px solid transparent;
}
.side-tab.on{
  color:#111;
  border-bottom-color:#111;
}
.side-story{
  display:block;
  padding:12px 0;
  border-bottom:1px solid #e9ebef;
  text-decoration:none;
  color:inherit;
}
.side-story:last-child{border-bottom:0}
.side-story strong{
  display:block;
  font-size:13.5px;
  line-height:1.4;
}
.side-story span{
  display:block;
  margin-top:5px;
  font-size:11px;
  color:#89909a;
}
.section-head{
  display:flex;
  justify-content:space-between;
  align-items:end;
  gap:15px;
  margin:30px 0 12px;
}
.section-head h2{
  margin:0;
  font-size:21px;
}
.section-head .small{
  color:#808793;
  font-size:12px;
}
.news-grid{
  display:grid;
  grid-template-columns:repeat(3,minmax(0,1fr));
  gap:18px;
}
.card{
  background:#fff;
  border:1px solid var(--line);
  min-height:245px;
  padding:20px 20px 17px;
  display:flex;
  flex-direction:column;
  transition:transform .15s ease,box-shadow .15s ease;
}
.card:hover{
  transform:translateY(-2px);
  box-shadow:0 7px 22px rgba(20,28,40,.08);
}
.chips{
  display:flex;
  gap:6px;
  flex-wrap:wrap;
  margin-bottom:12px;
}
.chip{
  display:inline-block;
  border-radius:3px;
  background:#eef2f7;
  color:#39485c;
  padding:4px 7px;
  font-size:10px;
  font-weight:700;
}
.chip.agri{background:#e8f5e9;color:#23622d}
.chip.energy{background:#fff1e5;color:#7a4418}
.chip.metals{background:#e9eefb;color:#304c88}
.card h3{
  margin:0;
  font-size:18px;
  line-height:1.35;
  letter-spacing:-.15px;
}
.card h3 a{
  color:#171b22;
  text-decoration:none;
}
.card h3 a:hover{text-decoration:underline}
.card p{
  margin:10px 0 0;
  color:#565e69;
  font-size:13.5px;
  line-height:1.55;
  display:-webkit-box;
  -webkit-line-clamp:4;
  -webkit-box-orient:vertical;
  overflow:hidden;
}
.card-foot{
  margin-top:auto;
  padding-top:16px;
  display:flex;
  justify-content:space-between;
  gap:12px;
  align-items:end;
}
.source{
  font-size:11px;
  font-weight:800;
  text-transform:uppercase;
  letter-spacing:.55px;
  color:#636b77;
}
.time{
  text-align:right;
  font-size:10.5px;
  line-height:1.45;
  color:#8b929d;
}
.empty{
  grid-column:1/-1;
  padding:50px;
  background:#fff;
  border:1px solid var(--line);
  text-align:center;
  color:#7b828d;
}
footer{
  border-top:1px solid var(--line);
  background:#fff;
  color:#707783;
  font-size:11px;
}
.footer-inner{
  max-width:1380px;
  margin:auto;
  padding:20px 22px;
}
@media(max-width:980px){
  .nav{display:none}
  .search-shell{margin-left:auto}
  .hero-grid{grid-template-columns:1fr}
  .news-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:640px){
  .top-inner{padding:0 14px}
  .brand{font-size:18px}
  .search-shell{width:145px}
  .status-inner{padding:9px 14px;display:block}
  .status-inner span{display:block;margin-top:3px}
  .filters{padding:8px 14px}
  .page{padding:18px 14px 40px}
  .hero{min-height:350px}
  .hero-content{
    min-height:350px;
    width:100%;
    padding:28px 22px;
  }
  .hero h1{font-size:28px}
  .hero-shade{
    background:linear-gradient(0deg,rgba(8,14,22,.94),rgba(8,14,22,.30));
  }
  .news-grid{grid-template-columns:1fr}
}
"""


JS = r"""
const cards=[...document.querySelectorAll('.card')];
const buttons=[...document.querySelectorAll('.filter')];
const search=document.getElementById('search');
const sourceFilter=document.getElementById('sourceFilter');

let active='All';

function applyFilters(){
  const q=(search.value||'').trim().toLowerCase();
  const selectedSource=(sourceFilter?.value||'All');

  cards.forEach(card=>{
    const tags=(card.dataset.tags||'').split('|');
    const group=card.dataset.group||'';
    const source=card.dataset.source||'';
    const blob=(card.dataset.search||'').toLowerCase();

    const categoryOK =
      active==='All' ||
      group===active ||
      tags.includes(active);

    const sourceOK = selectedSource==='All' || source===selectedSource;
    const searchOK=!q || blob.includes(q);

    card.style.display=(categoryOK && sourceOK && searchOK)?'':'none';
  });

  const visible=cards.filter(c=>c.style.display!=='none').length;
  document.getElementById('visibleCount').textContent=visible;
}

buttons.forEach(btn=>{
  btn.addEventListener('click',()=>{
    buttons.forEach(x=>x.classList.remove('on'));
    btn.classList.add('on');
    active=btn.dataset.tag;
    applyFilters();
  });
});

search.addEventListener('input',applyFilters);
if(sourceFilter) sourceFilter.addEventListener('change',applyFilters);
"""


def esc(value):
    return html.escape(str(value or ""), quote=True)


def chip_class(group):
    return group.lower() if group in {"Agri", "Energy", "Metals"} else ""


def render_side_story(item):
    return (
        f'<a class="side-story" href="{esc(item["link"])}" target="_blank" rel="noopener">'
        f'<strong>{esc(item["title"])}</strong>'
        f'<span>{esc(item["source"])} · {esc(item["ago"])}</span>'
        f"</a>"
    )


def render_card(item):
    chips = (
        f'<span class="chip {chip_class(item["group"])}">{esc(item["group"])}</span>'
        + "".join(f'<span class="chip">{esc(t)}</span>' for t in item["tags"])
    )

    summary = (
        f'<p>{esc(item["summary"])}</p>'
        if item["summary"]
        else "<p>Open the publisher link to read the full report.</p>"
    )

    search_blob = " ".join(
        [
            item["title"],
            item["summary"],
            item["source"],
            item["group"],
            *item["tags"],
        ]
    )

    return (
        f'<article class="card" '
        f'data-group="{esc(item["group"])}" data-source="{esc(item["source"])}" '
        f'data-tags="{esc("|".join(item["tags"]))}" '
        f'data-search="{esc(search_blob)}">'
        f'<div class="chips">{chips}</div>'
        f'<h3><a href="{esc(item["link"])}" target="_blank" rel="noopener">'
        f'{esc(item["title"])}</a></h3>'
        f"{summary}"
        f'<div class="card-foot">'
        f'<div class="source">{esc(item["source"])}</div>'
        f'<div class="time"><b>{esc(item["ago"])}</b><br>{esc(item["published"])}</div>'
        f"</div></article>"
    )


def write_html(items):
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    if items:
        hero = items[0]
        hero_img = (
            f'<div class="hero-media"><img src="{esc(hero["image"])}" '
            f'alt="" loading="eager" referrerpolicy="no-referrer"></div>'
            if hero["image"]
            else ""
        )
        hero_summary = hero["summary"] or "Open the publisher link for the full report."

        hero_html = (
            '<section class="hero">'
            f"{hero_img}"
            '<div class="hero-shade"></div>'
            '<div class="hero-content">'
            f'<div class="kicker">{esc(hero["group"])} · {esc(hero["source"])}</div>'
            f"<h1>{esc(hero['title'])}</h1>"
            f"<p>{esc(hero_summary)}</p>"
            '<div class="meta">'
            f"<span>{esc(hero['ago'])}</span>"
            f"<span>{esc(hero['published'])}</span>"
            f"<span>{esc(' / '.join(hero['tags']))}</span>"
            "</div></div>"
            f'<a class="cover-link" href="{esc(hero["link"])}" '
            'target="_blank" rel="noopener" aria-label="Open featured story"></a>'
            "</section>"
        )
    else:
        hero_html = (
            '<section class="hero"><div class="hero-content">'
            "<h1>No fresh stories available</h1>"
            "<p>The next scheduled refresh will try the configured sources again.</p>"
            "</div></section>"
        )

    side_items = items[1:7]
    side_html = "".join(render_side_story(x) for x in side_items)
    if not side_html:
        side_html = '<div style="padding:30px 0;color:#888">No additional stories.</div>'

    # Buttons are generated from groups + most useful commodity tags.
    groups = ["All", "Agri", "Metals", "Energy", "Commodities"]
    preferred_tags = [
        "Gold", "Silver", "Crude Oil", "Natural Gas", "Copper", "Aluminium",
        "Zinc", "Cotton", "Guar", "Turmeric", "Jeera", "Sugar", "Wheat",
        "Soybean", "Corn", "Rice", "Mustard", "Palm Oil", "Coffee", "Cocoa",
    ]

    present_groups = {i["group"] for i in items}
    present_tags = {t for i in items for t in i["tags"]}

    filters = ["All"]
    filters += [g for g in groups[1:] if g in present_groups]
    filters += [t for t in preferred_tags if t in present_tags]

    buttons = "".join(
        f'<button class="filter {"on" if name == "All" else ""}" '
        f'data-tag="{esc(name)}">{esc(name)}</button>'
        for name in filters
    )

    cards = "".join(render_card(i) for i in items[1:])
    if not cards:
        cards = '<div class="empty">No fresh commodity stories right now.</div>'

    reuters_count = sum(1 for i in items if "reuters" in i["source"].lower())

    source_names = sorted({i["source"] for i in items}, key=str.lower)
    source_options = '<option value="All">All Sources</option>' + "".join(
        f'<option value="{esc(s)}">{esc(s)}</option>' for s in source_names
    )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="1800">
<title>Commodities News Dashboard</title>
<style>{CSS}</style>
</head>
<body>

<header class="top">
  <div class="top-inner">
    <div class="brand">
      COMMODITIES NEWS
      <small>MARKETS · METALS · ENERGY · AGRI</small>
    </div>

    <nav class="nav">
      <a href="#latest">Latest</a>
      <a href="#" data-nav="Agri">Agri</a>
      <a href="#" data-nav="Metals">Metals</a>
      <a href="#" data-nav="Energy">Energy</a>
    </nav>

    <div class="search-shell">
      <input id="search" type="search" placeholder="Search news, source, commodity…">
    </div>
  </div>
</header>

<div class="status">
  <div class="status-inner">
    <div><b>Updated:</b> {esc(now)}</div>
    <span>
      {len(items)} fresh stories · Reuters {reuters_count} ·
      publisher dates verified for Google-discovered stories
    </span>
  </div>
</div>

<div class="filterbar">
  <div class="filters">{buttons}</div>
</div>

<main class="page">
  <div class="hero-grid">
    {hero_html}

    <aside class="sidebox">
      <h2>Latest Headlines</h2>
      <div class="side-tabs">
        <span class="side-tab on">Latest</span>
        <span class="side-tab">Commodities</span>
        <span class="side-tab">India + Global</span>
      </div>
      {side_html}
    </aside>
  </div>

  <div class="section-head" id="latest">
    <h2>Latest Commodity News</h2>
    <div class="small">
      <select id="sourceFilter" style="margin-right:10px;padding:6px 8px;border:1px solid #d9dde4;background:#fff">
        {source_options}
      </select>
      Showing <span id="visibleCount">{max(0, len(items)-1)}</span> stories
    </div>
  </div>

  <section class="news-grid">
    {cards}
  </section>
</main>

<footer>
  <div class="footer-inner">
    Headlines, summaries and timestamps belong to their respective publishers.
    This page links users to the original source. Google News is used only for
    discovery where configured; publisher dates are verified before those stories
    are displayed.
  </div>
</footer>

<script>{JS}</script>
<script>
document.querySelectorAll('[data-nav]').forEach(a=>{{
  a.addEventListener('click',e=>{{
    e.preventDefault();
    const name=a.dataset.nav;
    const b=[...document.querySelectorAll('.filter')].find(x=>x.dataset.tag===name);
    if(b) b.click();
    document.getElementById('latest').scrollIntoView({{behavior:'smooth'}});
  }});
}});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)

    print(f"Wrote {len(items)} stories to {OUT}")


if __name__ == "__main__":
    write_html(fetch_all())
