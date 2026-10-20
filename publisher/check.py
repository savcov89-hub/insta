#!/usr/bin/env python3
"""Проверяет токен и аккаунт. Ничего не публикует.

С TEST_VIDEO_URL ещё отдаёт видео в Instagram и ждёт обработки:
так видно, что Instagram забирает видео по нашей ссылке. Контейнер невидим и сам истекает через сутки.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ig  # noqa: E402


def main():
    token = os.environ.get("IG_TOKEN", "").strip()
    if not token:
        print("Нет секрета IG_TOKEN: Settings → Secrets and variables → Actions → New repository secret.")
        return 1
    try:
        acc = ig.me(token)
        user_id = acc.get("user_id") or acc.get("id")
        print(f"Аккаунт: @{acc.get('username')} ({acc.get('account_type')}), id {user_id}")
        lim = ig.publishing_limit(token, user_id)
        cfg = lim.get("config", {})
        print(f"Публикаций через API за сутки: {lim.get('quota_usage')} из {cfg.get('quota_total')}")
        url = os.environ.get("TEST_VIDEO_URL", "").strip()
        if url:
            trial = os.environ.get("TRIAL", "1") == "1"
            cid = ig.create_reel(token, user_id, url, caption="", trial=trial)
            print(f"Видео отдано в Instagram ({'пробный' if trial else 'обычный'} Reels), контейнер {cid}, жду обработку…")
            print(f"Обработка: {ig.wait_ready(token, cid)}. Публикацию не запускаю.")
        print("Всё в порядке.")
        return 0
    except ig.IGError as e:
        print(f"Ошибка Instagram: {e}")
        if e.raw:
            print(e.raw[:1500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
