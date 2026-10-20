#!/usr/bin/env python3
"""Ставит Reels в очередь выкладки (для сессий Claude с клоном этого репозитория).

  python3 tools/enqueue.py --id appetit-v1 --at "2026-10-21 18:00" \
      --caption-file podpis.txt [--regular] [--cover-ms 1500] video.mp4
  python3 tools/enqueue.py --id appetit-v1 --at "2026-10-21 18:00" --url https://…/video.mp4 --caption "…"

Время без пояса — Новосибирск. По умолчанию пробный Reels (graduation MANUAL).
Локальный файл кладётся отдельным коммитом в ветку media/<id> (не в main) и отдаётся
Instagram по raw-ссылке; после публикации программа удаляет эту ветку сама.
Очередь — queue/<id>.json в main; итог появится в status/<id>.json.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = "savcov89-hub/insta"
NSK = dt.timezone(dt.timedelta(hours=7))
LIMIT = 100_000_000  # GitHub не принимает файлы больше 100 МБ


def git(*args, inp=None):
    r = subprocess.run(["git", *args], cwd=ROOT, input=inp, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def push_media(path, item_id):
    """Коммит без родителя с одним видео → ветка media/<id>. Возвращает (ветка, raw-ссылка)."""
    if path.stat().st_size >= LIMIT:
        sys.exit(f"{path.name} больше 100 МБ: перекодируй (CRF 19, maxrate 6500k)")
    name = re.sub(r"[^\w.-]", "_", path.name)
    blob = git("hash-object", "-w", str(path))
    tree = git("mktree", inp=f"100644 blob {blob}\t{name}\n")
    commit = git("commit-tree", tree, "-m", f"media: {item_id}")
    branch = f"media/{item_id}"
    git("push", "-q", "--force", "origin", f"{commit}:refs/heads/{branch}")
    return branch, f"https://raw.githubusercontent.com/{REPO}/{commit}/{name}"


def check_url(url):
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", nargs="?", help="локальный mp4")
    ap.add_argument("--url", help="готовая публичная ссылка на mp4 вместо файла")
    ap.add_argument("--id", required=True, help="латиница, цифры, дефис: appetit-v1-2110")
    ap.add_argument("--at", required=True, help='"2026-10-21 18:00" (Новосибирск) или ISO с поясом')
    ap.add_argument("--caption", default="")
    ap.add_argument("--caption-file")
    ap.add_argument("--regular", action="store_true", help="обычный Reels, не пробный")
    ap.add_argument("--auto-share", action="store_true", help="пробный сам уйдёт подписчикам, если зайдёт (SS_PERFORMANCE)")
    ap.add_argument("--cover-ms", type=int, help="кадр обложки, мс от начала")
    ap.add_argument("--no-push", action="store_true", help="только записать queue/<id>.json")
    a = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", a.id):
        sys.exit("id: только латиница в нижнем регистре, цифры и дефис")
    if bool(a.video) == bool(a.url):
        sys.exit("нужен либо файл, либо --url")
    when = dt.datetime.fromisoformat(a.at.replace(" ", "T"))
    when = when if when.tzinfo else when.replace(tzinfo=NSK)
    caption = pathlib.Path(a.caption_file).read_text().strip() if a.caption_file else a.caption
    if len(caption) > 2200:
        sys.exit(f"подпись {len(caption)} символов, Instagram берёт до 2200")

    item = {"publish_at": when.isoformat(), "trial": not a.regular}
    if not a.regular:
        item["graduation"] = "SS_PERFORMANCE" if a.auto_share else "MANUAL"
    if a.cover_ms is not None:
        item["cover_ms"] = a.cover_ms
    if a.url:
        item["video_url"] = a.url
    else:
        branch, url = push_media(pathlib.Path(a.video), a.id)
        item["video_url"], item["media_branch"] = url, branch
    item["caption"] = caption
    print(f"видео: {check_url(item['video_url'])} {item['video_url']}")

    f = ROOT / "queue" / f"{a.id}.json"
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n")
    print(f"в очереди: {f.relative_to(ROOT)} на {when.astimezone(NSK):%d.%m %H:%M} Новосибирск")
    if a.no_push:
        return
    git("add", str(f))
    git("commit", "-q", "-m", f"queue: {a.id} {when.astimezone(NSK):%d.%m %H:%M}")
    git("pull", "-q", "--rebase", "origin", "main")
    git("push", "-q", "origin", "HEAD:main")
    print("запушено в main; программа возьмёт пост за 40 минут до времени")


if __name__ == "__main__":
    main()
