"""
peru_news.py
Fetches Peru-focused news from English-language RSS feeds, categorizes
stories, and writes them to docs/peru_news.json — capped at 20 per
category, max age 7 days, oldest entries replaced first.
No external APIs are used. All sources publish in English.
"""

import json
import os
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser
import feedparser

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_DIR = "docs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "peru_news.json")
MAX_PER_CATEGORY = 20
MAX_AGE_DAYS = 7
CATEGORIES = ["Diplomacy", "Military", "Energy", "Economy", "Local Events"]

# RSS feeds — all free, English-language, Peru-focused, no APIs
FEEDS = [
    # Peruvian Times — Peru's oldest English-language newspaper (since 1908)
    {"source": "Peruvian Times", "url": "https://www.peruviantimes.com/feed/"},
    {"source": "Peruvian Times", "url": "https://www.peruviantimes.com/category/news/feed/"},
    {"source": "Peruvian Times", "url": "https://www.peruviantimes.com/category/business/feed/"},
    # Peru Reports — English-language news service for foreign audience
    {"source": "Peru Reports", "url": "https://perureports.com/feed/"},
    # Latinvex Peru — Latin America business and politics, Peru-specific RSS
    {"source": "Latinvex", "url": "https://latinvex.com/rss/peru/"},
    # MercoPress — English Latin America wire, dedicated Peru section
    {"source": "MercoPress", "url": "https://en.mercopress.com/peru/rss"},
    {"source": "MercoPress", "url": "https://en.mercopress.com/economy/rss"},
    # Global Voices — English civic journalism, Peru country feed
    {"source": "Global Voices", "url": "https://globalvoices.org/feeds/country/peru/"},
    # Andina — Peru's official state news agency, English service
    {"source": "Andina", "url": "https://andina.pe/ingles/rss/rss.aspx?id=1"},
    {"source": "Andina", "url": "https://andina.pe/ingles/rss/rss.aspx?id=2"},
    {"source": "Andina", "url": "https://andina.pe/ingles/rss/rss.aspx?id=3"},
]

# ---------------------------------------------------------------------------
# Category keyword mapping (Peru-contextualised)
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "Diplomacy": [
        "diplomacy", "diplomatic", "foreign policy", "embassy", "ambassador",
        "treaty", "bilateral", "multilateral", "united nations", "oas",
        "foreign minister", "foreign affairs", "summit", "sanctions",
        "international relations", "geopolitical", "celac", "unasur",
        "trade deal", "g20", "accord", "alliance", "envoy", "consul",
        "boluarte", "canciller", "foreign ministry", "torre tagle",
        "peru and chile", "peru and bolivia", "peru and us",
        "peru and china", "latin america", "andean community",
        "can ", "trade agreement", "fta", "pacific alliance",
        "international court", "icj", "hague",
    ],
    "Military": [
        "military", "army", "navy", "air force", "defence", "defense",
        "national police", "police", "troops", "soldier", "weapons",
        "armed forces", "war", "combat", "conflict", "bomb",
        "security forces", "coast guard", "border security",
        "drug trafficking", "narcotics", "cocaine", "cartel",
        "sendero luminoso", "shining path", "mrta", "terrorism",
        "terrorist", "counter terrorism", "joint operation",
        "us southern command", "southcom", "security operation",
        "gang", "organized crime", "drug seizure", "eradication",
        "vraem", "military operation", "peruvian army",
        "peruvian navy", "peruvian air force",
    ],
    "Energy": [
        "energy", "oil", "gas", "petroleum", "petroperu", "pluspetrol",
        "renewable", "solar", "wind", "electricity", "power grid",
        "blackout", "power cut", "power outage", "hydroelectric",
        "hydro", "dam", "net zero", "carbon", "climate", "fossil fuel",
        "emissions", "coal", "lng", "pipeline", "energy crisis",
        "energy price", "fuel price", "petrol price", "diesel",
        "clean energy", "energy transition", "power plant",
        "amazon deforestation", "deforestation", "environment",
        "mining energy", "osinergmin",
    ],
    "Economy": [
        "economy", "economic", "gdp", "inflation", "interest rate",
        "central bank", "bcrp", "imf", "world bank", "budget",
        "finance minister", "treasury", "tax", "unemployment",
        "jobs", "recession", "growth", "trade", "sol", "pen",
        "bvl", "lima stock", "fiscal", "spending", "debt",
        "deficit", "wage", "cost of living", "investment",
        "business", "exports", "imports", "mining", "copper",
        "gold", "silver", "zinc", "agribusiness", "agriculture",
        "textile", "manufacturing", "tourism", "remittances",
        "sunat", "mef", "ministry of economy", "tef",
        "antamina", "southern copper", "cerro verde",
        "las bambas", "conga", "tia maria",
    ],
    "Local Events": [
        "local", "region", "province", "district", "mayor",
        "city", "town", "community", "hospital", "school",
        "crime", "police", "court", "flood", "earthquake",
        "landslide", "fire", "transport", "strike", "protest",
        "housing", "lima", "arequipa", "trujillo", "chiclayo",
        "piura", "iquitos", "cusco", "puno", "huancayo",
        "tacna", "cajamarca", "ayacucho", "junin", "loreto",
        "madre de dios", "ucayali", "san martin", "amazonas",
        "apurimac", "huanuco", "pasco", "moquegua", "tumbes",
        "election", "congress", "boluarte", "corruption",
        "impeachment", "indigenous", "native community",
        "protest", "demonstration", "blockade", "road block",
        "nhs equivalent", "essalud", "minsa", "education",
        "informal economy", "social conflict",
    ],
}


def classify(title: str, description: str):
    """Return the best-matching category for a story, or None if no match."""
    text = (title + " " + (description or "")).lower()
    scores = {cat: 0 for cat in CATEGORIES}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', text):
                scores[cat] += 1
    best_cat = max(scores, key=scores.get)
    return best_cat if scores[best_cat] > 0 else None


def strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def parse_date(entry):
    """Parse a feed entry's published date into a UTC-aware datetime."""
    raw = entry.get("published") or entry.get("updated") or entry.get("created")
    if not raw:
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if struct:
            return datetime(*struct[:6], tzinfo=timezone.utc)
        return None
    try:
        dt = dateparser.parse(raw)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc) if dt else None
    except Exception:
        return None


def fetch_feed(feed_cfg: dict) -> list:
    """Fetch a single RSS feed and return a list of story dicts."""
    source = feed_cfg["source"]
    url = feed_cfg["url"]
    stories = []
    try:
        parsed = feedparser.parse(url)
        if parsed.bozo and not parsed.entries:
            log.warning("Bozo feed (%s): %s", source, url)
            return stories
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
        for entry in parsed.entries:
            pub_date = parse_date(entry)
            if pub_date and pub_date < cutoff:
                continue
            title = strip_html(entry.get("title", "")).strip()
            desc = strip_html(entry.get("summary", "")).strip()
            if not title:
                continue
            category = classify(title, desc)
            if not category:
                continue
            story = {
                "title": title,
                "source": source,
                "url": entry.get("link", ""),
                "published_date": pub_date.isoformat() if pub_date else None,
                "category": category,
            }
            stories.append(story)
    except Exception as exc:
        log.error("Failed to fetch %s (%s): %s", source, url, exc)
    return stories


def load_existing() -> dict:
    """Load the current JSON file, grouped by category."""
    if not os.path.exists(OUTPUT_FILE):
        return {cat: [] for cat in CATEGORIES}
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {cat: [] for cat in CATEGORIES}

    grouped = {cat: [] for cat in CATEGORIES}
    stories = data.get("stories", data) if isinstance(data, dict) else data
    if isinstance(stories, list):
        for story in stories:
            cat = story.get("category")
            if cat in grouped:
                grouped[cat].append(story)
    return grouped


def merge(existing: dict, fresh: list) -> dict:
    """
    Merge fresh stories into the existing pool.
    - De-duplicate by URL.
    - Discard stories older than MAX_AGE_DAYS.
    - Replace oldest entries first when over MAX_PER_CATEGORY.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    existing_urls = set()
    for stories in existing.values():
        for s in stories:
            if s.get("url"):
                existing_urls.add(s["url"])

    for story in fresh:
        cat = story.get("category")
        if cat not in existing:
            continue
        if story["url"] in existing_urls:
            continue
        existing[cat].append(story)
        existing_urls.add(story["url"])

    for cat in CATEGORIES:
        pool = existing[cat]
        # Drop expired stories
        pool = [
            s for s in pool
            if s.get("published_date") and
               dateparser.parse(s["published_date"]).astimezone(timezone.utc) >= cutoff
        ]
        # Sort newest-first, cap at limit (oldest replaced first)
        pool.sort(key=lambda s: s.get("published_date") or "", reverse=True)
        existing[cat] = pool[:MAX_PER_CATEGORY]

    return existing


def write_output(grouped: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    flat = []
    for stories in grouped.values():
        flat.extend(stories)
    output = {
        "country": "Peru",
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "story_count": len(flat),
        "stories": flat,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)
    log.info("Wrote %d stories to %s", len(flat), OUTPUT_FILE)


def main():
    log.info("Loading existing data ...")
    existing = load_existing()

    log.info("Fetching %d RSS feeds ...", len(FEEDS))
    fresh = []
    for cfg in FEEDS:
        results = fetch_feed(cfg)
        log.info("  %s — %d stories from %s", cfg["source"], len(results), cfg["url"])
        fresh.extend(results)
        time.sleep(0.5)  # polite crawl delay

    log.info("Merging %d fresh stories ...", len(fresh))
    merged = merge(existing, fresh)

    counts = {cat: len(merged[cat]) for cat in CATEGORIES}
    log.info("Category totals: %s", counts)

    write_output(merged)


if __name__ == "__main__":
    main()
