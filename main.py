import os
import requests
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()

from ad_accounts import get_records

FB_API_VERSION = os.getenv("FB_API_VERSION", "v19.0")
FB_BASE_URL = f"https://graph.facebook.com/{FB_API_VERSION}"

FACEBOOK_ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Только форум-топик (твой текущий механизм)
TELEGRAM_FORUM_CHAT_ID = os.getenv("TELEGRAM_FORUM_CHAT_ID")  # например: -1002471171046
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID")          # например: 2 (строкой из env)

WORKSHEET_ACCOUNTS = os.getenv("WORKSHEET_ACCOUNTS", "FB_accounts")

# --- Central Brain Core (мягкая интеграция) ---
CORE_URL = os.getenv("CORE_URL")  # ВАЖНО: в GitHub Actions пока может быть пусто
CORE_JOB_KEY = os.getenv("CORE_JOB_KEY", "payment_check")


def core_start(source: str = "github_action", meta: dict | None = None) -> int | None:
    """Создаёт run в Core. Если Core недоступен/не задан — ничего не ломает."""
    if not CORE_URL:
        return None
    try:
        r = requests.post(
            f"{CORE_URL.rstrip('/')}/runs/start",
            json={"job_key": CORE_JOB_KEY, "source": source, "meta": meta or {}},
            timeout=15,
        )
        r.raise_for_status()
        return int(r.json()["run_id"])
    except Exception as e:
        print(f"[Core] start failed: {e}")
        return None


def core_finish(
    run_id: int | None,
    status: str,
    summary: str | None = None,
    error: str | None = None,
    artifacts: list[dict] | None = None,
) -> None:
    """Завершает run в Core. Если Core недоступен/не задан — ничего не ломает."""
    if not CORE_URL or not run_id:
        return
    try:
        r = requests.post(
            f"{CORE_URL.rstrip('/')}/runs/finish",
            json={
                "run_id": run_id,
                "status": status,
                "summary": summary,
                "error": error,
                "artifacts": artifacts or [],
            },
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[Core] finish failed: {e}")


def send_to_forum_topic(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    thread_id = None
    if TELEGRAM_THREAD_ID is not None and str(TELEGRAM_THREAD_ID).strip() != "":
        thread_id = int(str(TELEGRAM_THREAD_ID).strip())

    payload = {
        "chat_id": TELEGRAM_FORUM_CHAT_ID,
        "message_thread_id": thread_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=20)
    r.raise_for_status()


def get_ad_account_status(account_id: str):
    url = f"{FB_BASE_URL}/{account_id}"
    params = {
        "fields": "account_id,name,account_status,balance",
        "access_token": FACEBOOK_ACCESS_TOKEN,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"Facebook API error for {account_id}: {e}")
        return None


def check_ad_accounts() -> dict:
    """
    Возвращает результат проверки, чтобы можно было красиво залогировать в Core.
    Ничего не ломает в текущей логике.
    """
    rows = get_records(WORKSHEET_ACCOUNTS)

    messages = []
    mentions = set()

    for row in rows:
        account_id = str(row.get("ad_account_id", "")).strip()
        if not account_id:
            continue

        ad_name = str(row.get("ad_name", "")).strip()
        owner_name = str(row.get("name", "")).strip()
        username = str(row.get("username", "")).strip()

        data = get_ad_account_status(account_id)
        if not data:
            continue

        status = data.get("account_status")
        balance = int(data.get("balance", 0)) / 100

        # Ошибка оплаты (оставляем твою логику как есть)
        if status == 3:
            messages.append(
                f"🔴 {ad_name} | ошибка оплаты ➡️ ${balance:.2f} — {owner_name}".strip()
            )
            if username:
                mentions.add(username)

    tz = pytz.timezone("Asia/Almaty")
    current_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    if not messages:
        print("Нет ошибок оплаты.")
        return {
            "has_errors": False,
            "count": 0,
            "text": None,
            "time": current_time,
        }

    text = f"🚨 На текущий момент - {current_time}, израсходован бюджет:\n\n" + "\n".join(messages)
    if mentions:
        text += "\n\n" + " ".join(sorted(mentions))

    send_to_forum_topic(text)

    return {
        "has_errors": True,
        "count": len(messages),
        "text": text,
        "time": current_time,
    }


if __name__ == "__main__":
    missing = []
    for k in [
        "FACEBOOK_ACCESS_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_FORUM_CHAT_ID",
        "TELEGRAM_THREAD_ID",
        "GOOGLE_SHEET_URL",
    ]:
        if not os.getenv(k):
            missing.append(k)

    if missing:
        raise SystemExit(f"Missing env: {', '.join(missing)}")

    # --- Core run wrapper (мягко) ---
    run_id = core_start(
        source="github_action",
        meta={"worksheet": WORKSHEET_ACCOUNTS},
    )

    try:
        result = check_ad_accounts()

        if result["has_errors"]:
            summary = f"Найдено ошибок оплаты: {result['count']} (time: {result['time']})"
            # опционально: в artifacts можно хранить текст/детали (но аккуратно, чтобы не спамить)
            artifacts = [
                {"type": "text", "value": "\n".join(result["text"].splitlines()[:30]), "meta": {"truncated": True}}
            ]
        else:
            summary = f"Ошибок оплаты не найдено (time: {result['time']})"
            artifacts = []

        core_finish(run_id, status="success", summary=summary, artifacts=artifacts)

    except Exception as e:
        core_finish(run_id, status="error", summary="payment_check упал", error=str(e))
        raise
