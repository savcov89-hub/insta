#!/usr/bin/env python3
"""Собирает комментарии под Reels раз в сутки и отмечает, на какие ещё нет ответа Михаила.

GitHub Actions запускает его раз в сутки (.github/workflows/comments.yml) и по кнопке.
Ничего в Instagram не пишет: только читает. Отвечает publisher/reply.py, и только на одобренные ответы.

Итог:
  comments/comments.json  все комментарии за KEEP_DAYS дней: текст, автор, ответы в ветке,
                          need_reply = последнее слово в ветке не за нами. Его читает календарь (вкладка «Комменты»).
  comments/drafts.json    черновики ответов. Пишет их Claude, этот скрипт их не трогает,
                          только помечает posted, если ответ уже виден в Instagram.

В лог пишет только числа: репозиторий публичный.

Переменные окружения:
  IG_TOKEN     долгоживущий токен Instagram (секрет репозитория)
  WINDOW_DAYS  ролики моложе этого перечитываются всегда (по умолчанию 30), старые — только если вырос счётчик
  KEEP_DAYS    сколько дней хранить комментарии, на которые уже ответили (по умолчанию 90)
  GIT_PUSH=0   не коммитить (для локальной проверки)
"""
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ig  # noqa: E402
from stats import parse_time, NSK  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "comments"
WINDOW = dt.timedelta(days=int(os.environ.get("WINDOW_DAYS", "30")))
KEEP = dt.timedelta(days=int(os.environ.get("KEEP_DAYS", "90")))
PUSH = os.environ.get("GIT_PUSH", "1") != "0"
FIELDS = ("id,text,username,timestamp,like_count,hidden,"
          "replies{id,text,username,timestamp,like_count}")


def now():
    return dt.datetime.now(dt.timezone.utc)


def log(msg):
    print(f"[{now().astimezone(NSK):%d.%m %H:%M:%S}] {ig.scrub(msg)}", flush=True)


def read(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def fetch(token, media_id):
    out, after = [], None
    while True:
        p = {"fields": FIELDS, "limit": "50"}
        if after:
            p["after"] = after
        d = ig.call("GET", f"{media_id}/comments", token, p)
        out += d.get("data", [])
        paging = d.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not after:
            return out


def thread_of(c, me):
    replies = sorted(((c.get("replies") or {}).get("data") or []), key=lambda r: r.get("timestamp", ""))
    msgs = [{"id": c["id"], "username": c.get("username"), "text": c.get("text", ""),
             "timestamp": c.get("timestamp"), "like_count": c.get("like_count", 0)}]
    msgs += [{"id": r["id"], "username": r.get("username"), "text": r.get("text", ""),
              "timestamp": r.get("timestamp"), "like_count": r.get("like_count", 0)} for r in replies]
    last = msgs[-1]
    return {
        "id": c["id"], "username": c.get("username"), "text": c.get("text", ""),
        "timestamp": c.get("timestamp"), "like_count": c.get("like_count", 0),
        "hidden": bool(c.get("hidden")),
        "replies": msgs[1:],
        # Отвечать надо, если последнее сообщение ветки не наше. Свои комментарии верхнего уровня не в счёт.
        "need_reply": last["username"] != me and c.get("username") != me and not c.get("hidden"),
        "last_id": last["id"], "last_from": last["username"],
    }


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def commit_push(message):
    if not PUSH:
        return
    git("add", "comments")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-q", "-m", message)
    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    for attempt in range(5):
        if git("pull", "-q", "--rebase", "origin", branch, check=False).returncode != 0:
            git("rebase", "--abort", check=False)
        if git("push", "-q", "origin", f"HEAD:{branch}", check=False).returncode == 0:
            return
        time.sleep(3 * (attempt + 1))
    log("не получилось запушить комментарии")


def main():
    token = os.environ.get("IG_TOKEN", "").strip()
    if not token:
        log("нет секрета IG_TOKEN")
        return 1
    t0 = now()
    me = ig.me(token).get("username")
    reels = read(ROOT / "stats" / "reels.json", {}).get("reels", {})
    state = read(OUT / "comments.json", {})
    seen = state.get("seen", {})            # media_id → comments_count при прошлом чтении
    old = state.get("comments", {})
    drafts = read(OUT / "drafts.json", {})

    comments, read_n, errors, empty = {}, 0, 0, 0
    for mid, r in reels.items():
        if r.get("deleted") or not r.get("timestamp"):
            continue
        count = r.get("comments_count") or 0
        young = t0 - parse_time(r["timestamp"]) < WINDOW
        if not count or (not young and seen.get(mid) == count):
            continue
        try:
            got = fetch(token, mid)
        except ig.IGError as e:
            errors += 1
            log(f"ролик {mid}: ошибка Instagram {e}")
            continue
        read_n += 1
        if got:
            seen[mid] = count
        else:
            empty += 1   # счётчик не ноль, а пусто: перечитаем завтра
        reel = {"media_id": mid, "permalink": r.get("permalink"), "caption": (r.get("caption") or "").split("\n")[0][:140],
                "queue_id": r.get("queue_id"), "reel_time": r.get("timestamp")}
        for c in got:
            item = thread_of(c, me)
            item.update(reel)
            item["first_seen"] = (old.get(c["id"]) or {}).get("first_seen") or t0.isoformat(timespec="seconds")
            comments[c["id"]] = item

    # Ролики, которые сейчас не перечитывали: их комментарии берём из прошлого файла.
    reread = {v["media_id"] for v in comments.values()}
    for cid, c in old.items():
        if cid not in comments and c.get("media_id") not in reread:
            comments[cid] = c
    # Старые отвеченные чистим (обещание на странице удаления данных).
    for cid in [k for k, c in comments.items()
                if not c.get("need_reply") and t0 - parse_time(c.get("timestamp") or t0.isoformat()) > KEEP]:
        del comments[cid]

    # Черновик, на который ответ уже виден в Instagram, помечаем отправленным.
    changed_drafts = False
    for cid, d in drafts.items():
        c = comments.get(cid)
        if c and not c["need_reply"] and d.get("status") in ("draft", "approved"):
            d["status"] = "answered"
            changed_drafts = True

    waiting = sum(1 for c in comments.values() if c["need_reply"])
    new = sum(1 for c in comments.values() if c["first_seen"] == t0.isoformat(timespec="seconds"))
    OUT.mkdir(exist_ok=True)
    (OUT / "comments.json").write_text(json.dumps({
        "updated": t0.isoformat(timespec="seconds"), "username": me,
        "waiting": waiting, "empty_reels": empty, "seen": seen,
        "comments": dict(sorted(comments.items(), key=lambda kv: kv[1].get("timestamp") or "", reverse=True)),
    }, ensure_ascii=False, indent=1) + "\n")
    if changed_drafts:
        (OUT / "drafts.json").write_text(json.dumps(drafts, ensure_ascii=False, indent=1) + "\n")
    log(f"перечитано роликов {read_n}, ошибок {errors}; комментариев {len(comments)}, новых {new}, ждут ответа {waiting}")
    if empty:
        log(f"у {empty} роликов счётчик комментариев не ноль, а Instagram не отдал ни одного: "
            "приложение Meta, скорее всего, ещё в режиме разработки")
    commit_push(f"comments: {len(comments)}, ждут ответа {waiting}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # без трейсбека: в нём мог бы оказаться адрес запроса с токеном
        log(f"сбой: {type(e).__name__}: {e}")
        sys.exit(1)
