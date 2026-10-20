#!/usr/bin/env python3
"""Выкладывает Reels из очереди queue/*.json, когда подходит их время.

GitHub Actions запускает его каждые 10 минут (.github/workflows/publish.yml).
Каждый запуск берёт посты, до которых осталось меньше LOOKAHEAD_MIN минут,
заранее отдаёт видео в Instagram (контейнер ещё невидим), ждёт обработки
и ровно в назначенное время публикует. Итог пишет в status/<id>.json.

Переменные окружения:
  IG_TOKEN        долгоживущий токен Instagram (секрет репозитория)
  LOOKAHEAD_MIN   за сколько минут до публикации начинать (по умолчанию 40)
  MAX_LATE_MIN    если запуск опоздал сильнее, пост не выкладывается (180)
  TRIAL_CAP       сколько пробных за скользящие сутки пускать (10, как у Instagram)
  ONLY            выложить только пост с этим id
  DRY_RUN=1       только показать план, ничего не отправлять
  GIT_PUSH=0      не коммитить статусы (для локальной проверки)
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

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
STATUS = ROOT / "status"
NSK = dt.timezone(dt.timedelta(hours=7), "Новосибирск")

LOOKAHEAD = dt.timedelta(minutes=int(os.environ.get("LOOKAHEAD_MIN", "40")))
MAX_LATE = dt.timedelta(minutes=int(os.environ.get("MAX_LATE_MIN", "180")))
TRIAL_CAP = int(os.environ.get("TRIAL_CAP", "10"))
CAP_RETRY = dt.timedelta(minutes=60)
CAP_GIVE_UP = dt.timedelta(hours=24)
DAY = dt.timedelta(hours=24)
DRY = os.environ.get("DRY_RUN") == "1"
PUSH = os.environ.get("GIT_PUSH", "1") != "0"
ONLY = os.environ.get("ONLY", "").strip()
DONE = {"published", "failed", "missed", "cancelled"}


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(s):
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=NSK)  # без пояса — время Новосибирска


def nsk(t):
    return t.astimezone(NSK).strftime("%d.%m %H:%M:%S")


def log(msg):
    print(f"[{nsk(now())}] {ig.scrub(msg)}", flush=True)


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def commit_push(message):
    if not PUSH or DRY:
        return
    git("add", "status")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-q", "-m", message)
    for attempt in range(5):
        git("pull", "-q", "--rebase", "origin", "main", check=False)
        if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            return
        time.sleep(3 * (attempt + 1))
    log("не получилось запушить статус, он останется только в логе")


def load_status(item_id):
    p = STATUS / f"{item_id}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_status(item, st, note=None):
    st["updated"] = now().isoformat(timespec="seconds")
    if note:
        st.setdefault("history", []).append(f"{nsk(now())} {note}")
    if DRY:
        log(f"{item['id']}: [план] {st.get('state')} {note or ''}")
        return
    STATUS.mkdir(exist_ok=True)
    (STATUS / f"{item['id']}.json").write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n")
    commit_push(f"status: {item['id']} → {st['state']}")


def load_queue():
    items = []
    for f in sorted(QUEUE.glob("*.json")):
        try:
            item = json.loads(f.read_text())
        except ValueError as e:
            log(f"{f.name}: битый JSON, пропускаю ({e})")
            continue
        item["id"] = f.stem
        if not item.get("video_url") or not item.get("publish_at"):
            log(f"{f.name}: нет video_url или publish_at, пропускаю")
            continue
        item["when"] = parse_time(item["publish_at"])
        items.append(item)
    return items


def trials_last_day(items, t):
    """Пробные, которые эта программа выложила за сутки до момента t."""
    out = []
    for it in items:
        st = load_status(it["id"])
        if st.get("state") == "published" and it.get("trial", True) and st.get("published_at"):
            p = parse_time(st["published_at"])
            if t - DAY < p <= t:
                out.append(p)
    return sorted(out)


def pick_due(items):
    due, t = [], now()
    for it in items:
        if ONLY and it["id"] != ONLY:
            continue
        st = load_status(it["id"])
        state = st.get("state", "queued")
        if state in DONE:
            continue
        if it.get("cancel"):
            save_status(it, {**st, "state": "cancelled"}, "отменён в очереди")
            continue
        when = it["when"]
        if state == "cap_wait":
            if t - when > CAP_GIVE_UP:
                save_status(it, {**st, "state": "failed"}, "сутки ждал лимит пробных, сдаюсь")
                continue
            when = max(when, parse_time(st["retry_after"]))
        elif t - when > MAX_LATE and not ONLY:
            save_status(it, {**st, "state": "missed"},
                        f"запуск опоздал на {int((t - when).total_seconds() // 60)} мин, не выкладываю")
            continue
        if when - t <= LOOKAHEAD or ONLY:
            due.append((when, it, st))
    due.sort(key=lambda x: x[0])
    return due


def cap_wait(it, st, retry_after, why):
    """Откладывает пост до свободного окна. True, если это новость (а не очередной повтор)."""
    first = st.get("state") != "cap_wait"
    st = {**st, "state": "cap_wait", "retry_after": retry_after.isoformat(timespec="seconds")}
    save_status(it, st, f"{why}; попробую в {nsk(retry_after)}")
    return first


def main():
    items = load_queue()
    due = pick_due(items)
    if not due:
        log("в ближайшие минуты выкладывать нечего")
        return 0
    token = os.environ.get("IG_TOKEN", "").strip()
    if not token and not DRY:
        # не красим запуск в красный каждые 10 минут: пост сам станет missed через MAX_LATE_MIN
        log("нет секрета IG_TOKEN — выложить не могу, жду, когда его добавят")
        return 0

    user_id = None
    if not DRY:
        acc = ig.me(token)
        user_id = acc.get("user_id") or acc.get("id")
        log(f"аккаунт @{acc.get('username')}")

    bad = False
    ready = []
    # 1. Заранее отдаём видео в Instagram: контейнер невидим до публикации
    for when, it, st in due:
        log(f"{it['id']}: время {nsk(when)}, {'пробный' if it.get('trial', True) else 'обычный'} Reels")
        if DRY:
            ready.append((when, it, st))
            continue
        try:
            cid = st.get("container_id")
            if cid:
                code, _ = ig.container_status(token, cid)
                if code == "PUBLISHED":
                    save_status(it, {**st, "state": "published"}, "контейнер уже был опубликован")
                    continue
                if code in ("ERROR", "EXPIRED"):
                    cid = None
            if not cid:
                cid = ig.create_reel(token, user_id, it["video_url"], it.get("caption", ""),
                                     trial=it.get("trial", True),
                                     graduation=it.get("graduation", "MANUAL"),
                                     cover_ms=it.get("cover_ms"),
                                     share_to_feed=it.get("share_to_feed", True))
                st = {**st, "state": "uploading", "container_id": cid}
                save_status(it, st, "видео отдано в Instagram")
            ready.append((when, it, st))
        except ig.IGError as e:
            if e.trial_limit:
                bad |= cap_wait(it, st, now() + CAP_RETRY, "Instagram: лимит пробных")
            else:
                save_status(it, {**st, "state": "failed", "error": str(e)}, f"ошибка: {e}")
                bad = True

    # 2. Ждём обработку и публикуем точно по времени
    for when, it, st in ready:
        if DRY:
            log(f"{it['id']}: [план] выложил бы в {nsk(when)}")
            continue
        try:
            ig.wait_ready(token, st["container_id"])
            if it.get("trial", True):
                recent = trials_last_day(items, max(now(), when))
                if len(recent) >= TRIAL_CAP:
                    bad |= cap_wait(it, st, recent[0] + DAY + dt.timedelta(minutes=2),
                                    f"за сутки уже {len(recent)} пробных")
                    continue
            wait = (when - now()).total_seconds()
            if wait > 0:
                log(f"{it['id']}: видео готово, жду {int(wait // 60)} мин до {nsk(when)}")
                time.sleep(wait)
            media_id = ig.publish(token, user_id, st["container_id"])
            link = ig.permalink(token, media_id)
            st = {**st, "state": "published", "media_id": media_id, "permalink": link,
                  "published_at": now().isoformat(timespec="seconds")}
            st.pop("error", None)
            st.pop("retry_after", None)
            save_status(it, st, f"опубликован {link or media_id}")
            log(f"{it['id']}: ОПУБЛИКОВАН {link or media_id}")
            if it.get("media_branch") and PUSH:
                git("push", "-q", "origin", "--delete", it["media_branch"], check=False)
        except ig.IGError as e:
            if e.trial_limit:
                bad |= cap_wait(it, st, now() + CAP_RETRY, "Instagram: лимит пробных")
            else:
                save_status(it, {**st, "state": "failed", "error": str(e)}, f"ошибка: {e}")
                bad = True
            log(f"{it['id']}: ОШИБКА {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # без трейсбека: в нём мог бы оказаться адрес запроса с токеном
        log(f"сбой: {type(e).__name__}: {e}")
        sys.exit(1)
