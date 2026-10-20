#!/usr/bin/env python3
"""Продлевает токен Instagram ещё на 60 дней и кладёт новый в секрет IG_TOKEN (нужен секрет GH_PAT)."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ig  # noqa: E402


def main():
    token = os.environ.get("IG_TOKEN", "").strip()
    if not token:
        print("Нет секрета IG_TOKEN, продлевать нечего.")
        return 0
    try:
        d = ig.refresh_token(token)
    except ig.IGError as e:
        print(f"Instagram не продлил токен: {e}")
        return 1
    new = d.get("access_token", "")
    print(f"::add-mask::{new}")  # новый токен ещё не секрет: прячем из лога сразу
    os.environ["NEW_IG_TOKEN"] = new
    days = int(d.get("expires_in", 0)) // 86400
    print(f"Instagram продлил токен, новый действует {days} дн.")
    if new == token:
        print("Строка токена не изменилась, сохранять нечего.")
        return 0
    if not os.environ.get("GH_TOKEN"):
        print("Нет секрета GH_PAT: новый токен некуда сохранить. Старый действует до своих 60 дней.")
        return 1
    r = subprocess.run(["gh", "secret", "set", "IG_TOKEN", "--repo", os.environ["GITHUB_REPOSITORY"]],
                       input=new, text=True, capture_output=True)
    if r.returncode:
        print(f"Не удалось сохранить секрет: {ig.scrub(r.stderr.strip())}")
        return 1
    print("Новый токен сохранён в секрет IG_TOKEN.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
