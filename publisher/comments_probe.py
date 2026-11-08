#!/usr/bin/env python3
"""Разовая проверка: отдаёт ли Instagram тексты комментариев. В лог пишет только числа (репозиторий публичный)."""
import json, os, pathlib, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ig
tok = os.environ["IG_TOKEN"].strip()
acc = ig.me(tok); my = acc.get("username")
reels = json.loads((pathlib.Path(__file__).resolve().parent.parent / "stats/reels.json").read_text())["reels"]
top = sorted(reels.items(), key=lambda kv: kv[1].get("comments_count") or 0, reverse=True)[:6]
for mid, r in top:
    try:
        d = ig.call("GET", f"{mid}/comments", tok, {"fields": "id,username,timestamp,replies{id,username}", "limit": "50"})
        data = d.get("data", [])
        mine = sum(1 for c in data if c.get("username") == my)
        reps = sum(len((c.get("replies") or {}).get("data", [])) for c in data)
        print(f"{mid}: comments_count={r.get('comments_count')} получено={len(data)} (моих {mine}), ответов внутри={reps}")
    except ig.IGError as e:
        print(f"{mid}: ошибка {e}")
