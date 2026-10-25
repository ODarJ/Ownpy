#!/usr/bin/env python3
"""Read new Telegram channel links from a (secret) PUBLIC channel and add them to channels.txt.

  SOURCE_CHANNEL   username of the public channel you post links into (GitHub secret)
  CHANNELS_KEY     Fernet key: channels.txt is written encrypted when it is set (GitHub secret)
  MAX_PAGES        how many preview pages (~20 posts each) to read at most (default 10)

Flow: fetch https://t.me/s/<SOURCE_CHANNEL> -> find t.me links in the posts -> drop the ones that
are already in seen_links.json -> channels_crypto.add_channel() for each new one -> save
seen_links.json.  Channel names are never printed (Actions logs of a public repo are public).
"""

import hashlib
import hmac
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from channels_crypto import ChannelsFileError, add_channel, clean_channel

SOURCE_CHANNEL = clean_channel(os.getenv("SOURCE_CHANNEL", ""))
CHANNELS_KEY = os.getenv("CHANNELS_KEY", "").strip()
try:
    MAX_PAGES = max(1, int(os.getenv("MAX_PAGES", "10") or 10))
except ValueError:
    MAX_PAGES = 10

CHANNELS_FILE = Path("channels.txt")
SEEN_FILE = Path("seen_links.json")
FETCH_TIMEOUT = 15
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# seen_links.json is committed to the repo. With HASH_SEEN=True it holds keyed hashes
# (HMAC-SHA256 with CHANNELS_KEY) instead of channel names, so a public repo does not leak
# the list you just encrypted. Set it to False for a plain, readable list of usernames.
HASH_SEEN = True
SKIP_BOT_NAMES = True  # t.me/xxxbot is a bot, not a channel

# t.me/<name>, t.me/s/<name>, t.me/<name>/123 ... but never t.me/+invite or t.me/c/<id>/<post>
LINK_RE = re.compile(
    r"(?<![\w.\-])(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
RESERVED = {"joinchat", "addstickers", "addemoji", "addlist", "share", "proxy", "socks", "login",
            "setlanguage", "iv", "contact", "boost", "invoice", "giftcode"}


# ------------------------------------------------------------------- helpers
def extract_names(text):
    """All channel usernames found in `text` (unique, in order of appearance)."""
    names, lowered = [], set()
    for m in LINK_RE.finditer(text or ""):
        name = m.group("name")
        low = name.lower()
        if low in RESERVED or low in lowered or low == SOURCE_CHANNEL.lower():
            continue
        if SKIP_BOT_NAMES and low.endswith("bot"):
            continue
        lowered.add(low)
        names.append(name)
    return names


def seen_id(name):
    low = name.lower()
    if HASH_SEEN and CHANNELS_KEY:
        return hmac.new(CHANNELS_KEY.encode(), low.encode(), hashlib.sha256).hexdigest()[:32]
    return low


def load_seen():
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Could not read {SEEN_FILE}: {e}")
        return []


def save_seen(items):
    tmp = SEEN_FILE.with_name(SEEN_FILE.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SEEN_FILE)


def fetch_page(before_id=None):
    """Preview page of the source channel (posts older than before_id). Retries 429/5xx."""
    url = f"https://t.me/s/{SOURCE_CHANNEL}"
    if before_id:
        url += f"?before={before_id}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"[ERROR] could not fetch the source channel: {type(e).__name__}")
    return None


def parse_page(html):
    """-> (list of post ids, text of every post incl. the targets of hidden hyperlinks)."""
    soup = BeautifulSoup(html, "html.parser")
    ids, texts = [], []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            ids.append(int(msg["data-post"].rsplit("/", 1)[-1]))
        except (KeyError, ValueError):
            continue
        for el in msg.select(".tgme_widget_message_text"):
            texts.append(el.get_text(" "))
            texts.extend(a["href"] for a in el.select("a[href]"))  # [text](t.me/x) style links
    return ids, texts


# ---------------------------------------------------------------------- main
def main():
    if not SOURCE_CHANNEL:
        print("[ERROR] SOURCE_CHANNEL is not set")
        return 1
    if not CHANNELS_KEY:
        print("[WARN] CHANNELS_KEY is not set: channels.txt will NOT be encrypted")

    seen_list = load_seen()
    seen = set(seen_list)

    found, found_low = [], set()      # every link found (unique), newest first
    new_names = []                    # the ones not in seen_links.json yet
    before_id, pages = None, 0

    for _ in range(MAX_PAGES):
        html = fetch_page(before_id)
        if html is None:
            if pages == 0:
                return 1
            break
        ids, texts = parse_page(html)
        if not ids:
            break
        pages += 1

        new_on_page = 0
        for name in extract_names("\n".join(texts)):
            if name.lower() in found_low:
                continue
            found_low.add(name.lower())
            found.append(name)
            if seen_id(name) not in seen:
                new_names.append(name)
                new_on_page += 1

        # a page without anything new means we reached what the last run already processed
        oldest = min(ids)
        if new_on_page == 0 or oldest == before_id:
            break
        before_id = oldest
        time.sleep(1)

    print(f"Pages read: {pages}")
    print(f"Links found: {len(found)}")
    print(f"New links: {len(new_names)}")

    added = already = 0
    done = []
    try:
        for name in reversed(new_names):          # oldest post first
            if add_channel(name, CHANNELS_FILE):
                added += 1
            else:
                already += 1
            done.append(seen_id(name))
    except ChannelsFileError as e:
        # nothing is lost: only the links that really were handled are marked as seen
        print(f"[ERROR] channels.txt: {e}")
        if done:
            save_seen(seen_list + done)
        print(f"Added: {added} (stopped early)")
        return 1

    if done:
        save_seen(seen_list + done)
    print(f"Added to channels.txt: {added}")
    print(f"Already in channels.txt: {already}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
