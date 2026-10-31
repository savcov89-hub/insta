#!/usr/bin/env python3
"""Читает комментарии к Reels за последние DAYS дней и печатает их в лог (в файлы ничего не пишет)."""
import datetime as dt
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ig  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DAYS = int(os.environ.get("DAYS", "7"))
F = "id,text,username,timestamp,like_count,replies{id,text,username,timestamp,like_count}"


def main():
    token = os.environ.get("IG_TOKEN", "").strip()
    reels = json.loads((ROOT / "stats" / "reels.json").read_text())["reels"]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)
    for mid, r in reels.items():
        ts = str(r.get("timestamp") or "").replace("Z", "+00:00")
        if not ts or r.get("deleted"):
            continue
        if len(ts) > 5 and ts[-5] in "+-" and ts[-3] != ":":
            ts = ts[:-2] + ":" + ts[-2:]
        if dt.datetime.fromisoformat(ts) < cutoff:
            continue
        out, after = [], None
        try:
            while True:
                p = {"fields": F, "limit": "50"}
                if after:
                    p["after"] = after
                d = ig.call("GET", f"{mid}/comments", token, p)
                out += d.get("data", [])
                after = (d.get("paging") or {}).get("cursors", {}).get("after")
                if not (d.get("paging") or {}).get("next"):
                    break
        except ig.IGError as e:
            print("REEL", json.dumps({"id": mid, "error": str(e)}, ensure_ascii=False))
            continue
        print("REEL", json.dumps({"id": mid, "permalink": r.get("permalink"), "time": r.get("timestamp"),
                                 "caption": r.get("caption"), "count": r.get("comments_count"),
                                 "comments": out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
