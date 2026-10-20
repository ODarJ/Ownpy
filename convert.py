#!/usr/bin/env python3
"""output.json -> data/<channel>.json (OwnPlay import format)."""
import json
from pathlib import Path

IMG = "https://image.tmdb.org/t/p/"
GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Sci-Fi", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi", 10766: "Soap", 10767: "Talk", 10768: "War",
}


def convert(item):
    t = item.get("tmdb") or {}
    gids = t.get("genre_ids") or []
    genre = next((GENRES[g] for g in gids if g in GENRES), "No Category")
    rating = t.get("rating")
    return {
        "title": t.get("tmdb_title") or item["movie_name"],
        "year": t.get("tmdb_year", ""),
        "media": "Series" if t.get("tmdb_type") == "tv" else "Movie",
        "genre": genre,
        "description_en": t.get("overview", ""),
        "description_mm": "",
        "poster": f"{IMG}w500{t['poster']}" if t.get("poster") else "",
        "backdrop": f"{IMG}w780{t['backdrop']}" if t.get("backdrop") else "",
        "rating": round(rating, 1) if rating else 0,
        # valid_card_link is the channel's group invite (same for many movies);
        # post_link is unique per movie.
        "group_link": item["valid_card_link"],
        "video_link": item["post_link"],
    }


def main():
    items = json.loads(Path("output.json").read_text(encoding="utf-8"))
    by_channel, seen = {}, set()
    for it in items:
        row = convert(it)
        key = (it["channel"], row["title"], row["year"], row["video_link"])
        if key in seen:
            continue
        seen.add(key)
        by_channel.setdefault(it["channel"], []).append(row)

    Path("data").mkdir(exist_ok=True)
    for channel, rows in by_channel.items():
        Path(f"data/{channel}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"data/{channel}.json: {len(rows)} movies")


if __name__ == "__main__":
    main()
