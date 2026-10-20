#!/usr/bin/env python3
"""Telegram public-channel scraper (English title -> TMDB match -> link check)."""

import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CHANNELS_FILE = Path("channels.txt")
STATE_FILE = Path("state.json")
OUTPUT_FILE = Path("output.json")
CACHE_FILE = Path("tmdb_cache.json")
REVIEW_FILE = Path("review.json")
REJECTED_FILE = Path("rejected.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
FETCH_TIMEOUT = 15
LINK_TIMEOUT = 3
LINK_DELAY = 1
TME_TIMEOUT = 6          # t.me pages are slower than normal sites
REQUIRE_ALL_LINKS = False  # True: every link in the post must work; False: at least one

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_TIMEOUT = 10
TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/multi"
MATCH_OK = 0.85        # similarity needed to accept
MATCH_OK_SHORT = 0.90  # stricter for short titles (<= 6 chars)
MATCH_REVIEW = 0.70    # between this and MATCH_OK -> review.json
MAX_CANDIDATES = 4     # English title candidates tried per post
REQUIRE_YEAR = True    # no year in the post -> goes to review.json, not auto-accepted

REVIEW = []     # near-misses, written to review.json
REJECTED = []   # skipped posts, written to rejected.json


# ---------------------------------------------------------------- JSON I/O
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Could not read {path}: {e}")
        return default


def save_json(path, data):
    """Atomic write so a crash never leaves a half-written file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_cache = load_json(CACHE_FILE, {})
if not isinstance(_cache, dict):
    _cache = {}


def read_channels():
    if not CHANNELS_FILE.exists():
        print("[ERROR] channels.txt not found")
        return []
    channels, seen = [], set()
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        name = re.sub(r"^(?:https?://)?(?:www\.)?t\.me/(?:s/)?", "", name)
        name = name.lstrip("@").split("/")[0].split("?")[0]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            channels.append(name)
    return channels


# ----------------------------------------------------------- Normalization
EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF"
    "\u2B00-\u2BFF\u2190-\u21FF\u2300-\u23FF\uFE0F\u200D\u20E3]+"
)
URL_RE = re.compile(r"https?://\S+")
RES_RE = re.compile(
    r"\b(?:2160p|1440p|1080p|720p|576p|480p|360p|240p|4k|8k|uhd|fhd|qhd|hd|"
    r"hdr|hdrip|hdcam|hdtv|webrip|web-dl|webdl|bluray|blu-ray|brrip|bdrip|"
    r"dvdrip|camrip|x264|x265|h264|h265|hevc)\b",
    re.IGNORECASE,
)

SPACED_RE = re.compile(r"\b(?:[A-Za-z0-9]\s){2,}[A-Za-z0-9]\b")
LEET_MAP = {"3": "e", "@": "a", "0": "o", "1": "l", "$": "s"}
LEET_RE = re.compile(r"(?<=[A-Za-z])[3@01$]|[3@01$](?=[A-Za-z])")

JUNK_RE = re.compile(
    r'[|\u2022\u00b7\u2605\u2606\u2726\u2727\u25ba\u25b6\u25c6\u25a0\u25a1'
    r'\u25cf\u25cb\u25aa\u203b~_*#\[\]{}<>=+/\\"\u201c\u201d]'
)


def collapse_spaced_letters(text):
    parts = re.split(r"\s{2,}", text)
    parts = [SPACED_RE.sub(lambda m: re.sub(r"\s", "", m.group(0)), p) for p in parts]
    return " ".join(parts)


def normalize_title(raw):
    if not raw:
        return ""
    t = URL_RE.sub(" ", raw)
    t = EMOJI_RE.sub(" ", t)
    t = RES_RE.sub(" ", t)
    t = collapse_spaced_letters(t)
    t = LEET_RE.sub(lambda m: LEET_MAP[m.group(0)], t)
    t = re.sub(r"\(\s*\)", " ", t)
    t = JUNK_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" -:|.,")
    return t if any(c.isalnum() for c in t) else ""


# ------------------------------------------------- English title candidates
PAREN_YEAR_RE = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
TRAIL_YEAR_RE = re.compile(r"^(.+?)\s+((?:19|20)\d{2})$")
META_LINE_RE = re.compile(
    r"^(size|quality|language|audio|genre|rating|duration|subtitles?|"
    r"format|runtime|director|cast|channel|join|share)\b",
    re.IGNORECASE,
)


def english_candidates(*texts):
    """Keep only the English/ASCII part of each line (Burmese text is dropped)."""
    out = []
    for text in texts:
        for line in (text or "").splitlines():
            line = URL_RE.sub(" ", line)
            eng = re.sub(r"[^A-Za-z0-9'’:&.,!?()\[\]@$\- ]+", " ", line)
            eng = re.sub(r"\s+", " ", eng).strip(" -:.,")
            if len(re.findall(r"[A-Za-z]", eng)) < 2 or META_LINE_RE.match(eng):
                continue
            if eng not in out:
                out.append(eng)
            if len(out) >= MAX_CANDIDATES:
                return out
    return out


def split_paren_year(text):
    m = PAREN_YEAR_RE.search(text)
    if not m:
        return "", text
    return m.group(1), PAREN_YEAR_RE.sub(" ", text, count=1)


# ------------------------------------------------------------- TMDB match
def _sim(a, b):
    def clean(s):
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s.lower())).strip()
    return SequenceMatcher(None, clean(a), clean(b)).ratio()


def match_tmdb(title, year=None):
    """Returns (status, info). status: ok | review | none | error."""
    if not TMDB_API_KEY:
        print("[WARN] TMDB_API_KEY not set.")
        return "error", None
    try:
        r = requests.get(
            TMDB_SEARCH_URL,
            params={"api_key": TMDB_API_KEY, "query": title, "include_adult": "false"},
            timeout=TMDB_TIMEOUT,
        )
        time.sleep(0.25)
        r.raise_for_status()
        results = r.json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        print(f"[WARN] TMDB failed for '{title}': {e}")
        return "error", None

    best, best_score = None, 0.0
    for it in results[:5]:
        if it.get("media_type") not in ("movie", "tv"):
            continue
        names = [it.get("title"), it.get("original_title"), it.get("name"), it.get("original_name")]
        score = max((_sim(title, n) for n in names if n), default=0.0)
        date = it.get("release_date") or it.get("first_air_date") or ""
        if year and date[:4].isdigit() and abs(int(date[:4]) - int(year)) > 1:
            continue  # year must be within +-1
        if score > best_score:
            best, best_score = it, score

    if not best:
        return "none", None
    date = best.get("release_date") or best.get("first_air_date") or ""
    info = {
        "tmdb_id": best["id"],
        "tmdb_type": best["media_type"],
        "tmdb_title": best.get("title") or best.get("name"),
        "tmdb_year": date[:4],
        "poster": best.get("poster_path"),
        "backdrop": best.get("backdrop_path"),
        "overview": best.get("overview", ""),
        "rating": best.get("vote_average"),
        "genre_ids": best.get("genre_ids", []),
        "score": round(best_score, 2),
    }
    need = MATCH_OK_SHORT if len(title) <= 6 else MATCH_OK
    if best_score >= need:
        return "ok", info
    if best_score >= MATCH_REVIEW:
        return "review", info
    return "none", None


def match_cached(title, year=None):
    key = f"{title.lower()}|{year or ''}"
    if key in _cache:
        status, info = _cache[key]
        return status, info
    status, info = match_tmdb(title, year)
    if status != "error":  # never cache outages
        _cache[key] = [status, info]
    return status, info


def resolve_title(candidates):
    """Try each English candidate. Returns (status, info, title_used)."""
    first_title, review = "", None
    for cand in candidates:
        year, base = split_paren_year(cand)
        title = normalize_title(base)
        if not title:
            continue
        first_title = first_title or title
        tries = []
        m = TRAIL_YEAR_RE.match(title)
        has_year = bool(year or m)
        if m and not year:
            tries.append((m.group(1), m.group(2)))  # "Dune 2021" -> "Dune" + 2021
        tries.append((title, year))
        for q, y in tries:
            status, info = match_cached(q, y)
            if status == "ok":
                if REQUIRE_YEAR and not has_year:
                    review = review or (info, q)  # no year: needs a human look
                    continue
                return "ok", info, q
            if status == "error":
                return "error", None, first_title
            if status == "review" and review is None:
                review = (info, q)
    if review:
        return "review", review[0], review[1]
    return "none", None, first_title


# --------------------------------------------------------------- Extraction
def extract_sources(msg):
    """Returns (raw_title, [text sources]) from preview title + message text."""
    texts = []
    pt = msg.select_one(".link_preview_title")
    if pt and pt.get_text(strip=True):
        texts.append(pt.get_text(strip=True))
    text_el = msg.select_one(".tgme_widget_message_text")
    if text_el:
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        texts.append(text_el.get_text())
    raw = ""
    for t in texts:
        for line in t.splitlines():
            if line.strip():
                raw = line.strip()[:200]
                break
        if raw:
            break
    return raw, texts


def is_bad_link(url):
    """Bot links (t.me/xxxbot) are ads/promos, never a movie card."""
    m = re.match(r"https?://(?:www\.)?t\.me/([^/?#]+)", url, re.IGNORECASE)
    return bool(m and m.group(1).lower().endswith("bot"))


def extract_links(msg, post_link):
    links = []
    selector = ".tgme_widget_message_text a[href], a.tgme_widget_message_link_preview[href]"
    for a in msg.select(selector):
        href = a["href"].strip()
        if (href.startswith(("http://", "https://")) and href != post_link
                and href not in links and not is_bad_link(href)):
            links.append(href)
    return links


# --------------------------------------------------------- Link validation
_link_cache = {}  # url -> bool, so a shared invite link is only checked once per run
TME_RE = re.compile(r"^https?://(?:www\.)?(?:t|telegram)\.me/", re.IGNORECASE)
TME_MISSING_RE = re.compile(r"Telegram:\s*Contact\s*@", re.IGNORECASE)


def tme_alive(url):
    """t.me answers 200 even for missing usernames, so also read the page title."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TME_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return False
    if not (200 <= r.status_code < 400):
        return False
    m = re.search(r"<title>(.*?)</title>", r.text, re.IGNORECASE | re.DOTALL)
    title = m.group(1).strip() if m else ""
    return not TME_MISSING_RE.match(title)


def is_alive(url):
    """Normal sites: HEAD first, then streamed GET. Alive = status 200-399."""
    if TME_RE.match(url):
        return tme_alive(url)
    try:
        r = requests.head(url, headers=HEADERS, timeout=LINK_TIMEOUT, allow_redirects=True)
        if 200 <= r.status_code < 400:
            return True
    except requests.RequestException:
        pass
    try:
        r = requests.get(url, headers=HEADERS, timeout=LINK_TIMEOUT,
                         allow_redirects=True, stream=True)
        try:
            return 200 <= r.status_code < 400
        finally:
            r.close()
    except requests.RequestException:
        return False


def check_links(links):
    """Check EVERY link in the post. Returns (alive_links, dead_links)."""
    alive, dead = [], []
    for url in links:
        if url in _link_cache:
            ok = _link_cache[url]
        else:
            ok = is_alive(url)
            _link_cache[url] = ok
            time.sleep(LINK_DELAY)
        (alive if ok else dead).append(url)
    return alive, dead


# ----------------------------------------------------------------- Scraping
def fetch_page(channel, before_id=None):
    """Fetch t.me/s/ page. If before_id is provided, fetch older posts."""
    url = f"https://t.me/s/{channel}"
    if before_id:
        url += f"?before={before_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"[ERROR] {channel}: fetch failed: {e}")
        return None


def scrape_channel(channel, last_id, known, seen_cards, max_posts=0):
    """Returns (new_results, highest_id_seen) or (None, last_id) on fetch failure."""
    results = []
    max_id = last_id
    before_id = None
    saved_count = 0

    while True:
        html = fetch_page(channel, before_id)
        if html is None:
            if before_id is None:
                return None, last_id
            break

        soup = BeautifulSoup(html, "html.parser")
        messages = soup.select("div.tgme_widget_message[data-post]")
        if not messages:
            break

        oldest_id_on_page = None

        for msg in messages:
            try:
                data_post = msg["data-post"]
                message_id = int(data_post.rsplit("/", 1)[-1])

                if oldest_id_on_page is None or message_id < oldest_id_on_page:
                    oldest_id_on_page = message_id

                if message_id <= last_id:
                    continue

                max_id = max(max_id, message_id)
                if (channel, message_id) in known:
                    continue

                post_link = f"https://t.me/{data_post}"
                raw_title, texts = extract_sources(msg)
                candidates = english_candidates(*texts)
                if not candidates:
                    continue

                # TMDB check BEFORE link validation
                status, info, movie_name = resolve_title(candidates)
                if status == "error":
                    info = {}  # TMDB outage: keep the post without TMDB data
                elif status == "review":
                    REVIEW.append({"post_link": post_link, "raw_title": raw_title,
                                   "query": movie_name, "tmdb": info})
                    print(f"[REVIEW] {movie_name} ~ {info['tmdb_title']} ({info['score']})")
                    continue
                elif status == "none":
                    if len(REJECTED) < 500:
                        REJECTED.append({"post_link": post_link, "raw_title": raw_title,
                                         "candidates": candidates})
                    print(f"[SKIP] Not on TMDB: {candidates[0]}")
                    continue
                else:
                    movie_name = info["tmdb_title"]

                links = extract_links(msg, post_link)
                if not links:
                    continue
                alive, dead = check_links(links)
                if not alive or (REQUIRE_ALL_LINKS and dead):
                    print(f"[DEADLINK] {movie_name}: {len(dead)} dead, {len(alive)} alive")
                    continue
                valid = alive[0]

                card_key = (info.get("tmdb_id"), valid)
                if info.get("tmdb_id") and card_key in seen_cards:
                    continue
                seen_cards.add(card_key)

                results.append({
                    "channel": channel,
                    "message_id": message_id,
                    "movie_name": movie_name,
                    "post_link": post_link,
                    "valid_card_link": valid,
                    "alive_links": alive,
                    "dead_links": dead,
                    "raw_title": raw_title,
                    "tmdb": info,
                })

                known.add((channel, message_id))
                saved_count += 1
                print(f"[OK] {channel}/{message_id}: {movie_name}")

                if max_posts > 0 and saved_count >= max_posts:
                    break

            except Exception as e:
                print(f"[WARN] {channel}: skipped a message: {e}")

        if max_posts > 0 and saved_count >= max_posts:
            break
        if oldest_id_on_page is None:
            break
        if oldest_id_on_page <= last_id:
            break  # reached posts processed in a previous run

        before_id = oldest_id_on_page
        time.sleep(1)

    return results, max_id


def main():
    channels = read_channels()

    target_channel = os.getenv("TARGET_CHANNEL")
    target_limit_str = os.getenv("TARGET_LIMIT")
    target_limit = int(target_limit_str) if target_limit_str and target_limit_str.isdigit() else 0

    if target_channel:
        print(f"[INFO] Manual run. Channel: {target_channel} (Limit: {target_limit})")
        name = re.sub(r"^(?:https?://)?(?:www\.)?t\.me/(?:s/)?", "", target_channel)
        name = name.lstrip("@").split("/")[0].split("?")[0]
        channels = [name]
        state, output, known = {}, [], set()
    else:
        state = load_json(STATE_FILE, {})
        output = load_json(OUTPUT_FILE, [])
        if not isinstance(state, dict):
            state = {}
        if not isinstance(output, list):
            output = []
        known = {(o.get("channel"), o.get("message_id")) for o in output}

    seen_cards = {((o.get("tmdb") or {}).get("tmdb_id"), o.get("valid_card_link")) for o in output}

    if not channels:
        print("No channels to scrape.")
        return 0

    for channel in channels:
        print(f"--- {channel} ---")
        last_id = 0 if target_channel else int(state.get(channel, 0))

        results, new_max = scrape_channel(channel, last_id, known, seen_cards, max_posts=target_limit)
        if results is None:
            continue

        output.extend(results)
        if not target_channel:
            state[channel] = new_max
            save_json(STATE_FILE, state)

        save_json(OUTPUT_FILE, output)
        save_json(CACHE_FILE, _cache)
        time.sleep(1)

    save_json(OUTPUT_FILE, output)
    save_json(CACHE_FILE, _cache)
    save_json(REVIEW_FILE, REVIEW)
    save_json(REJECTED_FILE, REJECTED)
    print(f"Done. Output: {len(output)} | review: {len(REVIEW)} | rejected: {len(REJECTED)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
