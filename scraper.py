#!/usr/bin/env python3
"""Telegram public-channel scraper for GitHub Actions."""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CHANNELS_FILE = Path("channels.txt")
STATE_FILE = Path("state.json")
OUTPUT_FILE = Path("output.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
FETCH_TIMEOUT = 15   # seconds, for the t.me page
LINK_TIMEOUT = 3     # seconds, for link validation
LINK_DELAY = 1       # seconds between link checks


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

# Single characters separated by whitespace: "T h e" -> "The"
SPACED_RE = re.compile(r"\b(?:[A-Za-z0-9]\s){2,}[A-Za-z0-9]\b")

# Leetspeak map and regex (only chars touching a letter: keeps "2019" safe)
LEET_MAP = {"3": "e", "@": "a", "0": "o", "1": "l", "$": "s"}
LEET_RE = re.compile(r"(?<=[A-Za-z])[3@01$]|[3@01$](?=[A-Za-z])")

# Decorative symbols to strip (ASCII-safe \u escapes)
JUNK_RE = re.compile(
    r'[|\u2022\u00b7\u2605\u2606\u2726\u2727\u25ba\u25b6\u25c6\u25a0\u25a1'
    r'\u25cf\u25cb\u25aa\u203b~_*#\[\]{}<>=+/\\"\u201c\u201d]'
)


def collapse_spaced_letters(text):
    # Words separated by 2+ spaces keep one space between them.
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
    t = re.sub(r"\(\s*\)", " ", t)          # empty brackets left by removed tags
    t = JUNK_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" -:|.,")
    return t if any(c.isalnum() for c in t) else ""


# --------------------------------------------------------------- Extraction
def extract_raw_title(msg):
    preview_title = msg.select_one(".link_preview_title")
    if preview_title and preview_title.get_text(strip=True):
        return preview_title.get_text(strip=True)
    text_el = msg.select_one(".tgme_widget_message_text")
    if text_el:
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        for line in text_el.get_text().splitlines():
            if line.strip():
                return line.strip()[:200]
    return ""


def extract_links(msg, post_link):
    links = []
    selector = ".tgme_widget_message_text a[href], a.tgme_widget_message_link_preview[href]"
    for a in msg.select(selector):
        href = a["href"].strip()
        # Only absolute links (hashtag links are relative "?q=..." and are skipped)
        if href.startswith(("http://", "https://")) and href != post_link and href not in links:
            links.append(href)
    return links


# --------------------------------------------------------- Link validation
def is_alive(url):
    """HEAD first; fall back to streamed GET. Alive = status 200-399."""
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


def first_alive_link(links):
    for url in links:
        alive = is_alive(url)
        time.sleep(LINK_DELAY)
        if alive:
            return url
    return None


# ----------------------------------------------------------------- Scraping
def fetch_page(channel):
    try:
        r = requests.get(f"https://t.me/s/{channel}", headers=HEADERS, timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"[ERROR] {channel}: fetch failed: {e}")
        return None


def scrape_channel(channel, last_id, known):
    """Returns (new_results, highest_id_seen) or (None, last_id) on fetch failure."""
    html = fetch_page(channel)
    if html is None:
        return None, last_id

    soup = BeautifulSoup(html, "html.parser")
    results, max_id = [], last_id

    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            data_post = msg["data-post"]               # e.g. "channel/123"
            message_id = int(data_post.rsplit("/", 1)[-1])
            if message_id <= last_id:
                continue
            max_id = max(max_id, message_id)
            if (channel, message_id) in known:
                continue

            post_link = f"https://t.me/{data_post}"
            raw_title = extract_raw_title(msg)
            movie_name = normalize_title(raw_title)
            if not movie_name:
                continue

            links = extract_links(msg, post_link)
            valid = first_alive_link(links) if links else None
            if not valid:
                continue

            results.append({
                "channel": channel,
                "message_id": message_id,
                "movie_name": movie_name,
                "post_link": post_link,
                "valid_card_link": valid,
                "raw_title": raw_title,
            })
            print(f"[OK] {channel}/{message_id}: {movie_name}")
        except Exception as e:  # one bad message must not kill the run
            print(f"[WARN] {channel}: skipped a message: {e}")

    return results, max_id


def main():
    channels = read_channels()
    if not channels:
        print("No channels to scrape.")
        return 0

    state = load_json(STATE_FILE, {})
    output = load_json(OUTPUT_FILE, [])
    if not isinstance(state, dict):
        state = {}
    if not isinstance(output, list):
        output = []
    known = {(o.get("channel"), o.get("message_id")) for o in output}

    for channel in channels:
        print(f"--- {channel} ---")
        last_id = int(state.get(channel, 0))
        results, new_max = scrape_channel(channel, last_id, known)
        if results is None:
            continue  # fetch failed: keep old state so we retry next run
        output.extend(results)
        known.update((r["channel"], r["message_id"]) for r in results)
        state[channel] = new_max
        # Save after every channel so progress survives a crash/timeout
        save_json(OUTPUT_FILE, output)
        save_json(STATE_FILE, state)
        time.sleep(1)

    save_json(OUTPUT_FILE, output)
    save_json(STATE_FILE, state)
    print(f"Done. Total items in output.json: {len(output)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())