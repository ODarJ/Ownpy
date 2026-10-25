#!/usr/bin/env python3
"""Read / write an encrypted channels.txt (Fernet = AES-128-CBC + HMAC, from `cryptography`).

The key comes from the CHANNELS_KEY environment variable (a GitHub secret in Actions, an env var
for the Telegram bot). A plain-text channels.txt still works, so you can migrate step by step.

Python API (for scraper.py, the bot, ...)
    read_channels(path)        -> ["chan1", "chan2"]   (decrypts when needed)
    add_channel(name, path)    -> True if added, False if it was already there
    write_channels(names, path)
    maybe_decrypt(text)        -> text (decrypted when it is a Fernet token)
    encrypt_text(text) / decrypt_text(token)

Command line
    python channels_crypto.py keygen              print a new key
    python channels_crypto.py encrypt [file]      plain channels.txt -> encrypted (in place)
    python channels_crypto.py decrypt [file]      print the plain list (does not touch the file)
    python channels_crypto.py list [file]         print the channels as JSON
    python channels_crypto.py add NAME [file]     add one channel (encrypted when CHANNELS_KEY is set)
"""

import json
import os
import re
import sys
from pathlib import Path

DEFAULT_FILE = Path("channels.txt")
TOKEN_PREFIX = "gAAAAA"  # every Fernet token starts like this


class ChannelsFileError(Exception):
    """Wrong / missing key or a damaged file."""


def clean_channel(value):
    name = re.sub(r"^(?:https?://)?(?:www\.)?t\.me/(?:s/)?", "", (value or "").strip())
    return name.lstrip("@").split("/")[0].split("?")[0]


def parse_channels(text):
    """Plain text (one channel per line, # comments allowed) -> unique, cleaned list."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        name = clean_channel(line)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def looks_encrypted(text):
    t = (text or "").strip()
    return len(t) > 60 and t.startswith(TOKEN_PREFIX) and not re.search(r"\s", t)


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise ChannelsFileError("the 'cryptography' package is not installed (pip install cryptography)")
    key = os.getenv("CHANNELS_KEY", "").strip()
    if not key:
        raise ChannelsFileError("CHANNELS_KEY is not set")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise ChannelsFileError("CHANNELS_KEY is not a valid Fernet key (run: python channels_crypto.py keygen)")


def encrypt_text(text):
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_text(token):
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.strip().encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ChannelsFileError("could not decrypt: wrong CHANNELS_KEY or damaged file")


def maybe_decrypt(text):
    """Decrypt when `text` is a Fernet token, otherwise return it unchanged."""
    return decrypt_text(text) if looks_encrypted(text) else (text or "")


def read_channels(path=DEFAULT_FILE):
    path = Path(path)
    if not path.exists():
        return []
    return parse_channels(maybe_decrypt(path.read_text(encoding="utf-8")))


def write_channels(names, path=DEFAULT_FILE):
    """Encrypted when CHANNELS_KEY is set, plain text otherwise. Atomic write."""
    path = Path(path)
    plain = "\n".join(names) + ("\n" if names else "")
    content = (encrypt_text(plain) if os.getenv("CHANNELS_KEY", "").strip() else plain) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def add_channel(name, path=DEFAULT_FILE):
    name = clean_channel(name)
    if not name:
        raise ChannelsFileError("empty channel name")
    names = read_channels(path)
    if name.lower() in {n.lower() for n in names}:
        return False
    write_channels(names + [name], path)
    return True


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "keygen":
            from cryptography.fernet import Fernet
            print(Fernet.generate_key().decode())
        elif cmd == "encrypt":
            path = Path(argv[2] if len(argv) > 2 else DEFAULT_FILE)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if looks_encrypted(text):
                print("already encrypted")
                return 0
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(encrypt_text(text) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            print(f"encrypted {path} ({len(parse_channels(text))} channels)")
        elif cmd == "decrypt":
            path = Path(argv[2] if len(argv) > 2 else DEFAULT_FILE)
            print(maybe_decrypt(path.read_text(encoding="utf-8")), end="")
        elif cmd == "list":
            print(json.dumps(read_channels(argv[2] if len(argv) > 2 else DEFAULT_FILE)))
        elif cmd == "add" and len(argv) > 2:
            added = add_channel(argv[2], argv[3] if len(argv) > 3 else DEFAULT_FILE)
            print("added" if added else "already in the list")
        else:
            print(__doc__)
            return 1
    except ChannelsFileError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
