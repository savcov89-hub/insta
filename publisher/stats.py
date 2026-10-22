#!/usr/bin/env python3
"""Собирает статистику по вышедшим Reels: просмотры, охват, досмотр, лайки, сохранения, репосты.

GitHub Actions запускает его раз в 3 часа (.github/workflows/stats.yml).
Берёт ролики из status/*.json (всё, что выложила наша программа, в том числе пробные)
и из ленты аккаунта (/me/media: ролики, выложенные из приложения или Metricool).
Итог:
  stats/reels.json     последние цифры по каждому ролику (его читает страница-календарь)
  stats/history.jsonl  снимок цифр на каждый запуск по роликам младше HISTORY_DAYS дней
  stats/audience.json  последний снимок подписчиков по городам, полу и возрасту
                       и тех, кто реагировал на контент за месяц (раз в AUDIENCE_HOURS часов)
  stats/audience.jsonl все такие снимки подряд, чтобы видеть прирост по городам

В лог пишет только количество роликов и какие метрики отдал Instagram, сами цифры — только в файлы.

Переменные окружения:
  IG_TOKEN       долгоживущий токен Instagram (секрет репозитория)
  HISTORY_DAYS   сколько дней после выхода писать историю (по умолчанию 14)
  AUDIENCE_HOURS как часто снимать город, пол и возраст аудитории (по умолчанию 20 ч, то есть раз в сутки)
  DRY_RUN=1      ничего не записывать, только проверить, какие метрики доступны
  GIT_PUSH=0     не коммитить (для локальной проверки)
"""
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ig  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
STATUS = ROOT / "status"
STATS = ROOT / "stats"
NSK = dt.timezone(dt.timedelta(hours=7), "Новосибирск")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "14"))
DRY = os.environ.get("DRY_RUN") == "1"
PUSH = os.environ.get("GIT_PUSH", "1") != "0"
AUDIENCE_EVERY = dt.timedelta(hours=float(os.environ.get("AUDIENCE_HOURS", "20")))

# Метрики Reels в Instagram API. Что аккаунту или ролику недоступно, отсеивается по ответу Instagram.
METRICS = [
    "views", "reach", "likes", "comments", "shares", "saved", "total_interactions",
    "ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "reels_skip_rate",
    "follows", "profile_visits",
]
MEDIA_FIELDS = "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count"
# Аудитория аккаунта: (ключ в файле, метрика, разбивка). Город и пол Instagram считает отдельно;
# «city,gender» — попытка получить их вместе, если Instagram откажет, просто не будет этого ключа.
AUDIENCE = [
    ("followers", "follower_demographics", "city"),
    ("followers", "follower_demographics", "gender"),
    ("followers", "follower_demographics", "age"),
    ("followers", "follower_demographics", "city,gender"),
    ("engaged", "engaged_audience_demographics", "gender"),
    ("engaged", "engaged_audience_demographics", "age"),
    ("engaged", "engaged_audience_demographics", "city"),
]
# Период для «кто реагировал»; у разных версий API свои значения, пробуем по очереди.
TIMEFRAMES = {"follower_demographics": [None, "this_month", "last_30_days"],
              "engaged_audience_demographics": ["this_month", "last_30_days"]}
PERMISSION_CODES = {10, 200, 3}   # «нет разрешения» у Instagram: нужен instagram_business_manage_insights


class NoPermission(Exception):
    pass


class NoInsights(Exception):
    """Instagram не отдаёт по ролику ни одной метрики (так бывает со старыми роликами)."""


def now():
    return dt.datetime.now(dt.timezone.utc)


def log(msg):
    print(f"[{now().astimezone(NSK):%d.%m %H:%M:%S}] {ig.scrub(msg)}", flush=True)


def parse_time(s):
    s = str(s).replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", s):          # Instagram пишет +0000
        s = s[:-2] + ":" + s[-2:]
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=NSK)


def our_reels():
    """media_id → что знаем о ролике из нашей очереди."""
    out = {}
    for f in sorted(STATUS.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            st = json.loads(f.read_text())
        except ValueError:
            continue
        if st.get("state") != "published" or not st.get("media_id"):
            continue
        q = {}
        qf = QUEUE / f.name
        if qf.exists():
            try:
                q = json.loads(qf.read_text())
            except ValueError:
                pass
        out[str(st["media_id"])] = {
            "queue_id": f.stem, "permalink": st.get("permalink"), "timestamp": st.get("published_at"),
            "trial": q.get("trial", True), "caption": q.get("caption", ""),
        }
    return out


def account_media(token, pages=2):
    """Последние ролики ленты (до 50 × pages)."""
    out, after = [], None
    for _ in range(pages):
        params = {"fields": MEDIA_FIELDS, "limit": "50"}
        if after:
            params["after"] = after
        d = ig.call("GET", "me/media", token, params)
        out += d.get("data", [])
        after = (d.get("paging") or {}).get("cursors", {}).get("after")
        if not after or not (d.get("paging") or {}).get("next"):
            break
    return out


def media_info(token, media_id):
    try:
        return ig.call("GET", media_id, token, {"fields": MEDIA_FIELDS})
    except ig.IGError as e:
        log(f"ролик {media_id}: не открылся ({e})")
        return None


def _value(d):
    if "total_value" in d:
        return d["total_value"].get("value")
    vals = d.get("values") or [{}]
    return vals[-1].get("value")


def _names(text):
    return set(re.findall(r"[a-z_]+", text))


def gone(e):
    """Ролика больше нет (удалён) — это не про метрики."""
    t = f"{e} {e.raw or ''}"
    return e.subcode == 33 or "does not exist" in t or "Unsupported get request" in t


def insights(token, media_id, metrics, unsupported, skipped):
    """Цифры ролика. Метрику, от которой Instagram отказался для этого ролика, пропускает
    (и считает в skipped); метрику, которой нет вообще, кладёт в unsupported на весь запуск."""
    ask = [m for m in metrics if m not in unsupported]
    while ask:
        try:
            data = ig.call("GET", f"{media_id}/insights", token, {"metric": ",".join(ask)}).get("data", [])
            return {d["name"]: _value(d) for d in data}
        except ig.IGError as e:
            if e.code in PERMISSION_CODES:
                raise NoPermission(str(e)) from None
            if gone(e):
                raise
            msg = f"{e} {e.raw or ''}"
            m = re.search(r"must be one of the following values:?(.*)", msg, re.S)
            if m:   # Instagram перечислил, какие метрики вообще бывают
                bad = [x for x in ask if x not in _names(m.group(1))]
                unsupported.update(bad)
            else:   # отказ назвал метрики («…does not support the follows metric…»)
                bad = [x for x in ask if x in _names(msg)]
            if not bad:
                return insights_one_by_one(token, media_id, ask, skipped)
            for x in bad:
                skipped[x] = skipped.get(x, 0) + 1
            ask = [x for x in ask if x not in bad]
    return {}


def insights_one_by_one(token, media_id, metrics, skipped):
    out = {}
    for m in metrics:
        try:
            data = ig.call("GET", f"{media_id}/insights", token, {"metric": m}).get("data", [])
            for d in data:
                out[d["name"]] = _value(d)
        except ig.IGError as e:
            if e.code in PERMISSION_CODES:
                raise NoPermission(str(e)) from None
            if gone(e):
                raise
            skipped[m] = skipped.get(m, 0) + 1
    if not out and metrics:
        raise NoInsights()
    return out


def demographics(token, metric, breakdown):
    """{значение разбивки: число}. Для «city,gender» ключ «Novosibirsk, Novosibirsk Oblast | F»."""
    err = None
    for tf in TIMEFRAMES[metric]:
        params = {"metric": metric, "period": "lifetime", "metric_type": "total_value", "breakdown": breakdown}
        if tf:
            params["timeframe"] = tf
        try:
            data = ig.call("GET", "me/insights", token, params).get("data", [])
        except ig.IGError as e:
            if e.code in PERMISSION_CODES:
                raise
            err = e
            continue
        out = {}
        for item in data:
            for b in (item.get("total_value") or {}).get("breakdowns", []):
                for r in b.get("results", []):
                    out[" | ".join(str(x) for x in r.get("dimension_values", []))] = r.get("value")
        return out
    raise err


def last_audience_time():
    p = STATS / "audience.json"
    try:
        return parse_time(json.loads(p.read_text())["t"])
    except (OSError, ValueError, KeyError):
        return None


def audience(token, t0, followers):
    """Снимок аудитории, если прошлый старше AUDIENCE_EVERY; иначе None. Сбой здесь не мешает статистике роликов."""
    last = last_audience_time()
    if last and t0 - last < AUDIENCE_EVERY:
        return None
    snap = {"t": t0.isoformat(timespec="seconds"), "followers_count": followers, "followers": {}, "engaged": {}}
    failed = []
    for key, metric, breakdown in AUDIENCE:
        try:
            snap[key][breakdown] = demographics(token, metric, breakdown)
        except ig.IGError as e:
            failed.append(f"{key}/{breakdown} ({e})")
    got = [f"{k}/{b}" for k in ("followers", "engaged") for b in snap[k]]
    log(f"аудитория: есть {', '.join(got) or 'ничего'}")
    if failed:
        log(f"аудитория: не отдал {'; '.join(failed)}")
    return snap if got else None


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def commit_push(message):
    if not PUSH or DRY:
        return
    git("add", "stats")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-q", "-m", message)
    for attempt in range(5):
        git("pull", "-q", "--rebase", "origin", "main", check=False)
        if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            return
        time.sleep(3 * (attempt + 1))
    log("не получилось запушить статистику")


def main():
    token = os.environ.get("IG_TOKEN", "").strip()
    if not token:
        log("нет секрета IG_TOKEN")
        return 1
    t0 = now()
    acc = ig.call("GET", "me", token, {"fields": "user_id,username,followers_count,follows_count,media_count"})
    log(f"аккаунт @{acc.get('username')}")

    ours = our_reels()
    feed = {}
    try:
        for m in account_media(token):
            if m.get("media_product_type") == "REELS" or m.get("media_type") == "VIDEO":
                feed[str(m["id"])] = m
    except ig.IGError as e:
        log(f"лента не открылась ({e}), беру только наши ролики")

    ids = list(dict.fromkeys(list(ours) + list(feed)))
    old_path = STATS / "reels.json"
    old = json.loads(old_path.read_text()) if old_path.exists() else {}
    unsupported, skipped = set(), {}
    reels, errors = {}, 0
    for media_id in ids:
        info = feed.get(media_id) or media_info(token, media_id) or {}
        mine = ours.get(media_id, {})
        prev = old.get("reels", {}).get(media_id) or {}
        deleted = no_insights = False
        try:
            if prev.get("no_insights"):
                raise NoInsights()
            mine_skipped = {}
            nums = insights(token, media_id, METRICS, unsupported, mine_skipped)
            for k, v in mine_skipped.items():
                skipped[k] = skipped.get(k, 0) + v
        except NoPermission as e:
            log(f"Instagram не даёт статистику: {e}")
            log("нужно разрешение instagram_business_manage_insights в приложении Meta и новый токен")
            return 2
        except NoInsights:
            no_insights, nums = True, {}
        except ig.IGError as e:
            if gone(e):
                deleted = True
            else:
                log(f"ролик {media_id}: статистика не пришла ({e})")
                errors += 1
            nums = prev.get("insights", {})   # последние известные цифры
        if deleted and not prev and not mine:
            continue
        caption = info.get("caption") or mine.get("caption") or ""
        reels[media_id] = {
            "id": media_id,
            "queue_id": mine.get("queue_id"),
            "permalink": info.get("permalink") or mine.get("permalink"),
            "timestamp": info.get("timestamp") or mine.get("timestamp"),
            "trial": mine.get("trial") if mine else None,
            "caption": caption.strip().split("\n", 1)[0][:200],
            "like_count": info.get("like_count"),
            "comments_count": info.get("comments_count"),
            "insights": nums,
        }
        if deleted:
            reels[media_id]["deleted"] = True
        if no_insights:
            reels[media_id]["no_insights"] = True
        for k in ("permalink", "timestamp", "caption", "like_count", "comments_count"):
            if reels[media_id][k] in (None, "") and prev.get(k) not in (None, ""):
                reels[media_id][k] = prev[k]

    for media_id, r in (old.get("reels") or {}).items():
        reels.setdefault(media_id, r)   # ушёл из ленты (старше 100 постов) — оставляем последние цифры
    got = sorted({k for r in reels.values() for k in r["insights"]})
    blind = sum(1 for r in reels.values() if r.get("no_insights"))
    log(f"роликов: {len(reels)} (наших {len(ours)}, из ленты {len(feed)}), без статистики у Instagram: {blind}, "
        f"ошибок: {errors}")
    log(f"Instagram отдал метрики: {', '.join(got) or 'никаких'}")
    if unsupported:
        log(f"таких метрик у Instagram нет: {', '.join(sorted(unsupported))}")
    if skipped:
        log("не отдал для части роликов: " + ", ".join(f"{k} ({v})" for k, v in sorted(skipped.items())))
    try:
        aud = audience(token, t0, acc.get("followers_count"))
    except Exception as e:  # noqa: BLE001 — аудитория не должна ломать статистику роликов
        log(f"аудитория не снялась: {type(e).__name__}: {e}")
        aud = None
    if DRY:
        log("пробный запуск: ничего не записываю")
        return 0

    STATS.mkdir(exist_ok=True)
    stamp = t0.isoformat(timespec="seconds")
    summary = {
        "updated": stamp,
        "account": {k: acc.get(k) for k in ("username", "followers_count", "follows_count", "media_count")},
        "reels": dict(sorted(reels.items(), key=lambda kv: kv[1].get("timestamp") or "", reverse=True)),
    }
    old_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    with open(STATS / "history.jsonl", "a") as h:
        h.write(json.dumps({"t": stamp, "followers": acc.get("followers_count")}, ensure_ascii=False) + "\n")
        for r in reels.values():
            ts = r.get("timestamp")
            if ts and not r.get("deleted") and t0 - parse_time(ts) <= dt.timedelta(days=HISTORY_DAYS):
                h.write(json.dumps({"t": stamp, "id": r["id"], **r["insights"]}, ensure_ascii=False) + "\n")
    if aud:
        (STATS / "audience.json").write_text(json.dumps(aud, ensure_ascii=False, indent=1) + "\n")
        with open(STATS / "audience.jsonl", "a") as h:
            h.write(json.dumps(aud, ensure_ascii=False) + "\n")
    commit_push(f"stats: {len(reels)} роликов" + (", аудитория" if aud else ""))
    return 1 if errors and errors == len(reels) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # без трейсбека: в нём мог бы оказаться адрес запроса с токеном
        log(f"сбой: {type(e).__name__}: {e}")
        sys.exit(1)
