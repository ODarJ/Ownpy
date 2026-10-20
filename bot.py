#!/usr/bin/env python3
"""Telegram bot: collects public channel links into channels.txt."""

import logging
import os
import re
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

TOKEN = os.environ["BOT_TOKEN"]  # export BOT_TOKEN="123:abc"
CHANNELS_FILE = Path(os.getenv("CHANNELS_FILE", "channels.txt"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Public usernames start with a letter. "t.me/+xxxx" never matches.
LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
# t.me paths that are not channel usernames
RESERVED = {"joinchat", "addstickers", "addemoji", "addlist", "share", "proxy",
            "socks", "login", "setlanguage", "iv", "contact", "boost"}


def extract_channels(text):
    found = []
    for m in LINK_RE.finditer(text):
        name = m.group("name")
        if name.lower() not in RESERVED and name.lower() not in [f.lower() for f in found]:
            found.append(name)
    return found


def add_channel(name):
    """Append to channels.txt. Returns True if added, False if already present."""
    existing = set()
    if CHANNELS_FILE.exists():
        existing = {l.strip().lstrip("@").lower()
                    for l in CHANNELS_FILE.read_text(encoding="utf-8").splitlines()}
    if name.lower() in existing:
        return False
    needs_newline = (CHANNELS_FILE.exists() and CHANNELS_FILE.stat().st_size > 0
                     and not CHANNELS_FILE.read_text(encoding="utf-8").endswith("\n"))
    with open(CHANNELS_FILE, "a", encoding="utf-8") as f:
        f.write(("\n" if needs_newline else "") + name + "\n")
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    # Include hidden hyperlinks (text_link entities) as well as visible text
    text = msg.text + " " + " ".join(e.url for e in (msg.entities or []) if e.url)
    if "t.me/" not in text.lower() and "telegram.me/" not in text.lower():
        return

    names = extract_channels(text)
    if not names:
        return  # only private invite links / non-channel links

    try:
        results = [add_channel(n) for n in names]  # evaluate all, not short-circuit
    except OSError as e:
        logging.error("Could not write channels.txt: %s", e)
        await msg.reply_text("⚠️ Could not save the channel. Try again later.")
        return

    if any(results):
        await msg.reply_text("✅ Channel added to the daily scrape list!")
    else:
        await msg.reply_text("ℹ️ Already in the list.")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
