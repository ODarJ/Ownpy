#!/usr/bin/env python3
"""Telegram public-channel scraper for GitHub Actions (Matrix: one job per channel).

Flow per post: English title -> TMDB match -> link check -> TMDB details (backdrop, trailer)
               -> Burmese overview (local WindyWord/Marian model) -> Turso table `movies` (or json_data/<channel>/output.json
               when TURSO_DATABASE_URL / TURSO_AUTH_TOKEN are not set)

Environment variables
  TARGET_CHANNEL       channel username(s) for this job: "a" or "a,b,c" (default "default" = every
                       channel in channels.txt)
  TARGET_LIMIT         max NEW posts saved per run (default 100, 0 = unlimited); the next run resumes
  MAX_RUNTIME_MIN      whole-run time budget in minutes (default 100)
  CHANNEL_BUDGET_MIN   per-channel time budget in minutes (default 30, 0 = no limit)
  JSON_DIR             folder for state/review/rejected files (default "json_data")
  TURSO_DATABASE_URL   libsql://<db>-<org>.turso.io   } when both are set the movies are saved in
  TURSO_AUTH_TOKEN     database token (write access) } Turso instead of output.json
  TMDB_API_KEY         TMDB v3 API key
  MM_ENGINE            "windyword" (default, local model) | "google" (deep-translator) | "none"
  MT_MODEL             Hugging Face repo of the model (default WindyWord/translate-en-sit)
  MT_SUBFOLDER         sub-folder inside the repo (default "lora")
  MT_LANG_TOKEN        target-language token the model needs (default ">>mya<<" = Burmese)
  CHANNELS_KEY         Fernet key to decrypt channels.txt (only needed in "default" mode and when
                       the file is encrypted; matrix jobs get their channels via TARGET_CHANNEL)
Per-channel files (created automatically):
  json_data/<channel>/{state,review,rejected,nolinks}.json   (+ output.json without Turso)
Shared files (repo root, merged on save):
  tmdb_cache.json, mm_cache.json
"""

import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from channels_crypto import ChannelsFileError, read_channels as read_channels_file


# ------------------------------------------------------------------ settings
def env_number(name, default, cast=int):
    """Read a number from the environment; empty/invalid values fall back to the default."""
    try:
        return cast(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def normalize_channel(value):
    name = re.sub(r"^(?:https?://)?(?:www\.)?t\.me/(?:s/)?", "", (value or "").strip())
    return name.lstrip("@").split("/")[0].split("?")[0]


def parse_targets(raw):
    out, seen = [], set()
    for part in re.split(r"[,\s]+", raw or ""):
        name = normalize_channel(part)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out or ["default"]


TARGET_CHANNELS = parse_targets(os.getenv("TARGET_CHANNEL", "default"))
TARGET_CHANNEL = ",".join(TARGET_CHANNELS)
TARGET_LIMIT = env_number("TARGET_LIMIT", 100)
MAX_RUNTIME_MIN = env_number("MAX_RUNTIME_MIN", 100, float)
CHANNEL_BUDGET_MIN = env_number("CHANNEL_BUDGET_MIN", 30, float)
RUN_DEADLINE = time.time() + MAX_RUNTIME_MIN * 60

JSON_DIR = Path(os.getenv("JSON_DIR", "json_data"))
CHANNELS_FILE = Path("channels.txt")
CACHE_FILE = Path("tmdb_cache.json")      # shared, repo root
MM_CACHE_FILE = Path("mm_cache.json")     # shared, repo root

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
FETCH_TIMEOUT = 15
LINK_TIMEOUT = 3
TME_TIMEOUT = 6            # t.me pages are slower than normal sites
LINK_DELAY = 1
KEEP_NO_LINK_POSTS = True  # no links in the card (links in comments/buttons): keep the post

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_TIMEOUT = 10
TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/multi"
TMDB_DETAILS_URL = "https://api.themoviedb.org/3/{kind}/{tid}"
BACKDROP_BASE = "https://image.tmdb.org/t/p/w1280"
MATCH_OK = 0.85            # similarity needed to accept
MATCH_OK_SHORT = 0.90      # stricter for short titles (<= 6 chars)
MATCH_REVIEW = 0.70        # between this and MATCH_OK -> review.json
MAX_CANDIDATES = 4         # English title candidates tried per post
REQUIRE_YEAR = True        # no year in the post -> review.json, never auto-accepted

MM_ENGINE = os.getenv("MM_ENGINE", "windyword").strip().lower()
MT_MODEL = os.getenv("MT_MODEL", "WindyWord/translate-en-sit").strip()
MT_SUBFOLDER = os.getenv("MT_SUBFOLDER", "lora").strip()
MT_LANG_TOKEN = os.getenv("MT_LANG_TOKEN", ">>mya<<").strip()  # Sino-Tibetan model: target language is chosen by this prefix
MT_BATCH = 8               # sentences translated together
MT_BEAMS = 4               # beam search width (higher = slower, slightly better)
MM_MAX_FAILS = 5           # consecutive failures before translation is switched off for this run


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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def save_shared_json(path, data):
    """Shared cache: merge with what is on disk (ours wins) and never crash on failure."""
    try:
        merged = load_json(path, {})
        if not isinstance(merged, dict):
            merged = {}
        merged.update(data)
        save_json(path, merged)
    except Exception as e:
        print(f"[WARN] Could not save shared file {path}: {e}")


def load_list(path):
    data = load_json(path, [])
    return data if isinstance(data, list) else []


# ------------------------------------------------------------------- Turso
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT,
    message_id INTEGER,
    movie_name TEXT,
    post_link TEXT,
    tmdb_id INTEGER,
    tmdb_data TEXT, -- Store the whole TMDB dictionary as a JSON string
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel, message_id) -- Avoid duplicates
)
"""
# same post again (e.g. a run was repeated) -> update the row instead of failing or duplicating
UPSERT_SQL = """
INSERT INTO movies (channel, message_id, movie_name, post_link, tmdb_id, tmdb_data)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(channel, message_id) DO UPDATE SET
    movie_name = excluded.movie_name,
    post_link  = excluded.post_link,
    tmdb_id    = excluded.tmdb_id,
    tmdb_data  = excluded.tmdb_data
"""


def connect_turso():
    """Global Turso client, created only when both variables are set (else local JSON mode)."""
    if not (TURSO_DATABASE_URL and TURSO_AUTH_TOKEN):
        if TURSO_DATABASE_URL or TURSO_AUTH_TOKEN:
            print("[WARN] Only one of TURSO_DATABASE_URL / TURSO_AUTH_TOKEN is set: using local JSON files")
        return None
    try:
        import libsql_client
        url = TURSO_DATABASE_URL
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]  # HTTP transport: simpler and sturdier in CI
        return libsql_client.create_client_sync(url, auth_token=TURSO_AUTH_TOKEN)
    except ImportError:
        print("[WARN] libsql-client is not installed (pip install libsql-client): using local JSON files")
    except Exception as e:  # never print the URL or the token
        print(f"[WARN] Could not create the Turso client ({type(e).__name__}): using local JSON files")
    return None


db = connect_turso()


def db_execute(sql, args=()):
    """One statement, 3 attempts with a growing pause."""
    last = None
    for attempt in range(3):
        try:
            return db.execute(sql, list(args))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def init_db():
    db_execute(CREATE_TABLE_SQL)


def load_known_ids(channel):
    rs = db_execute("SELECT message_id FROM movies WHERE channel = ?", [channel])
    return {int(row[0]) for row in rs.rows if row[0] is not None}


_cache = load_json(CACHE_FILE, {})        # TMDB search + details cache
if not isinstance(_cache, dict):
    _cache = {}
_mm_cache = load_json(MM_CACHE_FILE, {})  # tmdb_id -> Burmese overview
if not isinstance(_mm_cache, dict):
    _mm_cache = {}


class ChannelData:
    """Every file of one channel lives in json_data/<channel>/ (created automatically)."""

    def __init__(self, channel):
        self.channel = channel
        self.dir = JSON_DIR / channel
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.output_file = self.dir / "output.json"
        self.review_file = self.dir / "review.json"
        self.rejected_file = self.dir / "rejected.json"
        self.nolinks_file = self.dir / "nolinks.json"

        st = load_json(self.state_file, {})
        st = st if isinstance(st, dict) else {}
        try:
            self.last_id = int(st.get("last_id") or 0)
        except (TypeError, ValueError):
            self.last_id = 0
        self.pending = st.get("pending")

        self.output = [] if db else load_list(self.output_file)  # output.json only without Turso
        self.pending_rows = []   # movies waiting to be written to Turso (flushed after every page)
        self.saved = 0           # movies added in this run
        self.review = load_list(self.review_file)      # near-misses and posts without a year
        self.rejected = load_list(self.rejected_file)  # skipped posts (capped)
        self.nolinks = load_list(self.nolinks_file)    # HTML samples of link-less posts
        if db:
            self.known = load_known_ids(channel)   # what Turso already has for this channel
        else:
            self.known = {o.get("message_id") for o in self.output}
        self.seen_posts = {e.get("post_link") for e in self.review + self.rejected}

    def add_unique(self, bucket, entry, cap):
        if entry["post_link"] in self.seen_posts or len(bucket) >= cap:
            return
        self.seen_posts.add(entry["post_link"])
        bucket.append(entry)

    def add_movie(self, record):
        if db:
            self.pending_rows.append(record)
        else:
            self.output.append(record)
        self.known.add(record["message_id"])
        self.saved += 1

    def flush(self):
        """Write the waiting movies to Turso. False = failed (the caller must not advance the state)."""
        if not db or not self.pending_rows:
            return True
        try:
            for r in self.pending_rows:
                t = r.get("tmdb") or {}
                db_execute(UPSERT_SQL, [r["channel"], r["message_id"], r["movie_name"], r["post_link"],
                                        t.get("tmdb_id"), json.dumps(t, ensure_ascii=False)])
        except Exception as e:
            print(f"[ERROR] {self.channel}: could not write to Turso ({type(e).__name__}); "
                  "this page will be retried on the next run")
            return False
        self.pending_rows = []
        return True

    def save_state(self):
        save_json(self.state_file, {"last_id": self.last_id, "pending": self.pending})

    def save_files(self):
        """Called after every page: output (local mode) + side files + shared caches."""
        if not db:
            save_json(self.output_file, self.output)
        save_json(self.review_file, self.review)
        save_json(self.rejected_file, self.rejected)
        save_json(self.nolinks_file, self.nolinks)
        save_shared_json(CACHE_FILE, _cache)
        save_shared_json(MM_CACHE_FILE, _mm_cache)


def read_channels():
    """channels.txt may be encrypted (needs CHANNELS_KEY) or plain text."""
    if not CHANNELS_FILE.exists():
        print("[ERROR] channels.txt not found")
        return []
    try:
        return read_channels_file(CHANNELS_FILE)
    except ChannelsFileError as e:
        print(f"[ERROR] channels.txt: {e}")
        sys.exit(1)  # fail loudly instead of silently scraping nothing


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


def fetch_details(info):
    """Backdrop + YouTube trailer via /movie/{id}?append_to_response=videos (TV: /tv/{id})."""
    tid = info.get("tmdb_id")
    if not tid or not TMDB_API_KEY:
        return {}
    kind = "tv" if info.get("tmdb_type") == "tv" else "movie"
    key = f"details:{kind}:{tid}"
    if key in _cache:
        return _cache[key]
    try:
        r = requests.get(
            TMDB_DETAILS_URL.format(kind=kind, tid=tid),
            params={"api_key": TMDB_API_KEY, "append_to_response": "videos"},
            timeout=TMDB_TIMEOUT,
        )
        time.sleep(0.25)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[WARN] TMDB details failed for {kind}/{tid}: {e}")
        return {}

    videos = [v for v in (data.get("videos") or {}).get("results") or []
              if v.get("site") == "YouTube" and v.get("key")]
    pick = (next((v for v in videos if v.get("type") == "Trailer" and v.get("official")), None)
            or next((v for v in videos if v.get("type") == "Trailer"), None)
            or next((v for v in videos if v.get("type") == "Teaser"), None))
    details = {
        "backdrop_path": data.get("backdrop_path") or "",
        "trailer": f"https://www.youtube.com/watch?v={pick['key']}" if pick else "",
    }
    _cache[key] = details
    return details


# ------------------------------------------------ Burmese translation (inline)
_mt = None              # (tokenizer, model, device, torch) once the local model is loaded
_translator = None      # only used when MM_ENGINE=google
_mm_fails = 0           # consecutive failures
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def load_mt():
    """Load WindyWord (MarianMT) once per process; the weights come from the Hugging Face cache."""
    global _mt
    if _mt is None:
        import torch
        from transformers import MarianMTModel, MarianTokenizer
        tokenizer = MarianTokenizer.from_pretrained(MT_MODEL, subfolder=MT_SUBFOLDER)
        model = MarianMTModel.from_pretrained(MT_MODEL, subfolder=MT_SUBFOLDER)
        if MT_LANG_TOKEN not in tokenizer.get_vocab():
            raise RuntimeError(f"the model has no {MT_LANG_TOKEN} language token")
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(device).eval()
        _mt = (tokenizer, model, device, torch)
        print(f"[INFO] translation model loaded on {device}")
    return _mt


def translate_windyword(text):
    """English -> Burmese with the local model, sentence by sentence (Marian works best on short inputs)."""
    tokenizer, model, device, torch = load_mt()
    sentences = [x.strip() for x in SENTENCE_SPLIT_RE.split(text[:4500]) if x.strip()]
    out = []
    for i in range(0, len(sentences), MT_BATCH):
        batch = [f"{MT_LANG_TOKEN} {x}" for x in sentences[i:i + MT_BATCH]]
        enc = tokenizer(batch, return_tensors="pt", padding=True, truncation=True, max_length=256).to(device)
        with torch.no_grad():
            generated = model.generate(**enc, num_beams=MT_BEAMS, max_new_tokens=256)
        out.extend(tokenizer.decode(g, skip_special_tokens=True).strip() for g in generated)
    return " ".join(x for x in out if x)


def translate_google(text):
    global _translator
    if _translator is None:
        from deep_translator import GoogleTranslator
        _translator = GoogleTranslator(source="en", target="my")
    time.sleep(0.6)
    return (_translator.translate(text[:4500]) or "").strip()


def looks_burmese(text):
    """True when most of the letters are Myanmar script (rejects Chinese/Latin/empty output)."""
    mm = sum(1 for c in text if "\u1000" <= c <= "\u109f" or "\uaa60" <= c <= "\uaa7f")
    other = sum(1 for c in text if c.isalpha() and not ("\u1000" <= c <= "\u109f" or "\uaa60" <= c <= "\uaa7f"))
    return mm >= 5 and mm / (mm + other) >= 0.6


def translate_mm(info):
    """English TMDB overview -> Burmese (cached per tmdb_id). Returns "" on failure."""
    global _mm_fails
    overview = (info.get("overview") or "").strip()
    tid = str(info.get("tmdb_id") or "")
    if not overview or not tid or MM_ENGINE == "none":
        return ""
    if tid in _mm_cache:
        return _mm_cache[tid]
    if _mm_fails >= MM_MAX_FAILS:
        return ""  # the model/engine keeps failing: stop trying for the rest of this run
    try:
        if MM_ENGINE == "google":
            mm = translate_google(overview)
        else:
            try:
                load_mt()
            except ImportError:
                raise
            except Exception as e:  # download failed, wrong repo, no language token ...
                print(f"[WARN] could not load the translation model ({type(e).__name__}): {str(e)[:120]}")
                _mm_fails = MM_MAX_FAILS  # do not retry the (slow) load for every movie
                return ""
            mm = translate_windyword(overview)
    except ImportError as e:
        print(f"[WARN] translation libraries missing ({e.name}): skipping Burmese translation")
        _mm_fails = MM_MAX_FAILS
        return ""
    except Exception as e:
        _mm_fails += 1
        print(f"[WARN] translate failed for {tid} ({_mm_fails}/{MM_MAX_FAILS}): {type(e).__name__}: {str(e)[:120]}")
        return ""
    if not looks_burmese(mm):
        # a multi-language model can answer in the wrong script: never store that
        _mm_fails += 1
        print(f"[WARN] translation for {tid} is not Burmese text ({_mm_fails}/{MM_MAX_FAILS}), not saved")
        return ""
    _mm_fails = 0
    _mm_cache[tid] = mm
    return mm


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
    selector = (".tgme_widget_message_text a[href], a.tgme_widget_message_link_preview[href], "
                ".tgme_widget_message_inline_keyboard a[href]")  # inline buttons
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
    """Fetch t.me/s/ page (posts OLDER than before_id). Retries on 429/5xx with backoff."""
    url = f"https://t.me/s/{channel}"
    if before_id:
        url += f"?before={before_id}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                try:
                    wait = int(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 5 * (attempt + 1)
                wait = max(1, min(wait, 60))
                print(f"[RETRY] {channel}: HTTP {r.status_code}, waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"[ERROR] {channel}: fetch failed: {e}")
    return None


def scrape_channel(data, max_posts=0):
    """Scrape one channel, newest -> oldest, resumable.

    state.json = {"last_id": N, "pending": None | {"top_id": T, "before": B}}
      last_id  everything up to this id was fully processed by earlier completed passes
      pending  an unfinished pass: posts newer than `before` (up to top_id) are done;
               the next run continues from `before` downwards until it reaches last_id

    Returns "done" (pass complete, last_id advanced), "partial" (stopped early, resumes next
    run) or "fail" (first page could not be fetched).
    """
    channel = data.channel
    last_id, pending = data.last_id, data.pending
    before_id = pending["before"] if pending else None
    top_id = pending["top_id"] if pending else None
    saved_count = 0

    chan_deadline = float("inf")
    if CHANNEL_BUDGET_MIN > 0:
        chan_deadline = time.time() + CHANNEL_BUDGET_MIN * 60

    def time_up():
        return time.time() >= min(RUN_DEADLINE, chan_deadline)

    def remember(before):
        data.pending = {"top_id": top_id, "before": before}
        data.save_state()

    if pending:
        print(f"[RESUME] {channel}: continuing below id {before_id} (until {last_id})")

    while True:
        if time_up():
            print(f"[TIME] {channel}: time budget reached, progress saved")
            if top_id is not None:
                remember(before_id)
            return "partial"

        html = fetch_page(channel, before_id)
        if html is None:
            if top_id is None:
                return "fail"
            remember(before_id)
            return "partial"

        soup = BeautifulSoup(html, "html.parser")
        parsed = []
        for msg in soup.select("div.tgme_widget_message[data-post]"):
            try:
                parsed.append((int(msg["data-post"].rsplit("/", 1)[-1]), msg))
            except (KeyError, ValueError):
                continue
        if not parsed:
            break  # start of the channel reached

        parsed.sort(key=lambda x: x[0])  # oldest -> newest
        page_min, page_max = parsed[0][0], parsed[-1][0]
        if top_id is None:
            top_id = max(last_id, page_max)  # first page of a fresh pass

        interrupted = False
        for message_id, msg in parsed:
            if message_id <= last_id or message_id in data.known:
                continue
            if time_up() or (max_posts > 0 and saved_count >= max_posts):
                interrupted = True
                break
            try:
                post_link = f"https://t.me/{msg['data-post']}"
                raw_title, texts = extract_sources(msg)
                candidates = english_candidates(*texts)
                if not candidates:
                    continue

                # TMDB check BEFORE link validation
                status, info, movie_name = resolve_title(candidates)
                if status == "error":
                    info = {}  # TMDB outage: keep the post without TMDB data
                elif status == "review":
                    data.add_unique(data.review, {"post_link": post_link, "raw_title": raw_title,
                                                  "query": movie_name, "tmdb": info}, 5000)
                    print(f"[REVIEW] {movie_name} ~ {info['tmdb_title']} ({info['score']})")
                    continue
                elif status == "none":
                    data.add_unique(data.rejected, {"post_link": post_link, "raw_title": raw_title,
                                                    "candidates": candidates}, 500)
                    print(f"[SKIP] Not on TMDB: {candidates[0]}")
                    continue
                else:
                    movie_name = info["tmdb_title"]

                # Links are only used to decide keep/skip (not stored): one alive link is enough
                links = extract_links(msg, post_link)
                if links:
                    alive, dead = check_links(links)
                    if not alive:
                        print(f"[DEADLINK] {movie_name}: {len(dead)} dead links")
                        continue
                elif KEEP_NO_LINK_POSTS:
                    if len(data.nolinks) < 5:
                        data.nolinks.append({"post_link": post_link, "html": str(msg)[:3000]})
                    print(f"[NOLINK] kept: {movie_name}")
                else:
                    continue

                # TMDB details (backdrop + trailer) and Burmese overview
                # (info is copied so the TMDB search cache stays untouched)
                tmdb_out = dict(info)
                details = fetch_details(info)
                backdrop = details.get("backdrop_path") or info.get("backdrop") or ""
                tmdb_out["backdrop"] = f"{BACKDROP_BASE}{backdrop}" if backdrop else ""
                tmdb_out["trailer"] = details.get("trailer", "")
                tmdb_out["overview_mm"] = translate_mm(info)

                data.add_movie({
                    "channel": channel,
                    "channel_link": f"https://t.me/{channel}",
                    "message_id": message_id,
                    "movie_name": movie_name,
                    "post_link": post_link,
                    "raw_title": raw_title,
                    "tmdb": tmdb_out,
                })
                saved_count += 1
                print(f"[OK] {channel}/{message_id}: {movie_name}")
            except Exception as e:
                print(f"[WARN] {channel}: skipped a message: {e}")

        if not data.flush():       # Turso write failed: keep the old resume point
            data.save_files()
            return "partial"
        data.save_files()          # after every page

        if interrupted:
            why = "limit reached" if max_posts > 0 and saved_count >= max_posts else "time budget reached"
            print(f"[STOP] {channel}: {why}, will resume next run")
            remember(page_max + 1)  # redo this page next time (saved posts are skipped quickly)
            return "partial"

        if page_min <= last_id:
            break  # reached the part processed in earlier runs

        before_id = page_min
        remember(before_id)  # crash-safe: state always points at the next page
        time.sleep(1)

    data.last_id = top_id if top_id is not None else last_id
    data.pending = None
    data.save_state()
    return "done"


def write_github_output(done):
    """Lets a workflow read steps.<id>.outputs.done."""
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"done={'true' if done else 'false'}\n")
    except OSError as e:
        print(f"[WARN] Could not write GITHUB_OUTPUT: {e}")


def main():
    channels = read_channels() if TARGET_CHANNELS == ["default"] else TARGET_CHANNELS
    print(f"[INFO] job={TARGET_CHANNEL} | limit={TARGET_LIMIT} | budget={MAX_RUNTIME_MIN}m/"
          f"{CHANNEL_BUDGET_MIN}m per channel | store={'Turso' if db else 'local JSON'}")

    if db:
        try:
            init_db()          # CREATE TABLE IF NOT EXISTS movies (...)
        except Exception as e:
            print(f"[ERROR] Turso is not reachable ({type(e).__name__}); stopping so nothing is lost")
            return 1

    if not channels:
        print("No channels to scrape.")
        write_github_output(True)
        return 0

    unfinished, total = [], 0
    for channel in channels:
        if time.time() >= RUN_DEADLINE:
            unfinished.append(channel)
            continue
        print(f"--- {channel} ---")
        try:
            data = ChannelData(channel)
        except Exception as e:      # e.g. Turso read failed: skip this channel, keep going
            print(f"[ERROR] {channel}: could not start ({type(e).__name__})")
            unfinished.append(channel)
            continue
        status = scrape_channel(data, max_posts=TARGET_LIMIT)
        data.save_files()
        total += data.saved
        if status != "done":
            unfinished.append(channel)
        time.sleep(1)

    print(f"Done. New movies this run: {total}")
    if unfinished:
        print(f"[INFO] Unfinished (will resume next run): {', '.join(unfinished)}")
    write_github_output(not unfinished)
    if db:
        try:
            db.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
