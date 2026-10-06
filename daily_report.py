import os
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pytz

from ad_accounts import get_records

load_dotenv()

FACEBOOK_ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN")

# Telegram (старый режим — fallback, если CORE_URL не задан)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_FORUM_CHAT_ID = os.getenv("TELEGRAM_FORUM_CHAT_ID")  # например: -1002471171046
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID")          # например: 182

# Core (новый режим)
CORE_URL = os.getenv("CORE_URL")  # пример: http://127.0.0.1:8000
CORE_API_KEY = os.getenv("CORE_API_KEY")  # опционально

FB_API_VERSION = os.getenv("FB_API_VERSION", "v19.0")
BASE_URL = f"https://graph.facebook.com/{FB_API_VERSION}"

WORKSHEET_ACCOUNTS = os.getenv("WORKSHEET_ACCOUNTS", "FB_accounts")

MONTHS_RU = {
    "January": "января", "February": "февраля", "March": "марта",
    "April": "апреля", "May": "мая", "June": "июня",
    "July": "июля", "August": "августа", "September": "сентября",
    "October": "октября", "November": "ноября", "December": "декабря"
}

GRAVO_NAMES = {"Gravo", "Gravo 2"}

# Переписки могут приходить из разных objectives (ENGAGEMENT, TRAFFIC и т.д.)
# Поэтому ищем по action_type, а не по objective.
MESSAGING_ACTION_TYPES = {
    "onsite_conversion.messaging_conversation_started_7d",
    "onsite_conversion.messaging_conversation_started",
    "messaging_conversation_started_7d",
    "messaging_conversation_started",
}

JOB_KEY = "daily_report"


def get_yesterday_date_ru():
    tz = pytz.timezone("Asia/Almaty")
    yesterday = datetime.now(tz) - timedelta(days=1)
    day = yesterday.strftime("%d")
    month_ru = MONTHS_RU[yesterday.strftime("%B")]
    return f"{day} {month_ru}"


def tg_send_to_forum_topic(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_FORUM_CHAT_ID,
        "message_thread_id": TELEGRAM_THREAD_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()


# -------------------------
# Core client
# -------------------------
def _core_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if CORE_API_KEY:
        headers["X-API-KEY"] = CORE_API_KEY
    return headers


def core_start_run(job_key: str, source: str = "daily_report") -> str:
    url = f"{CORE_URL.rstrip('/')}/runs/start"
    payload = {"job_key": job_key, "source": source}
    r = requests.post(url, headers=_core_headers(), json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    run_id = data.get("run_id") or data.get("id")
    if not run_id:
        raise RuntimeError(f"Core /runs/start: no run_id in response: {data}")
    return str(run_id)


def core_finish_run(
    run_id: str,
    status: str,
    summary: str,
    error: str | None = None,
    artifacts: list[dict] | None = None,
) -> None:
    url = f"{CORE_URL.rstrip('/')}/runs/finish"
    payload = {
        "run_id": int(run_id) if str(run_id).isdigit() else run_id,
        "status": status,
        "summary": summary,
        "error": error,               # важно: поле называется error (как в твоём runs.py)
        "artifacts": artifacts or [],
    }
    r = requests.post(url, headers=_core_headers(), json=payload, timeout=30)
    r.raise_for_status()


def get_account_data(account_id: str) -> dict:
    url = f"{BASE_URL}/{account_id}/insights"
    params = {
        "access_token": FACEBOOK_ACCESS_TOKEN,
        "level": "campaign",
        "date_preset": "yesterday",
        "fields": "campaign_name,spend,actions,cost_per_action_type,objective",
        "limit": 200,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _extract_int(value) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def campaign_has_messaging(campaign: dict) -> bool:
    actions = campaign.get("actions") or []
    cpat = campaign.get("cost_per_action_type") or []

    for a in actions:
        if a.get("action_type") in MESSAGING_ACTION_TYPES:
            return True

    for a in cpat:
        if a.get("action_type") in MESSAGING_ACTION_TYPES:
            return True

    return False


def campaign_messaging_conversations(campaign: dict) -> int:
    actions = campaign.get("actions") or []
    total = 0
    for a in actions:
        if a.get("action_type") in MESSAGING_ACTION_TYPES:
            total += _extract_int(a.get("value", 0))
    return total


def build_accounts_dict(rows: list[dict]) -> dict:
    """
    Превращаем строки листа FB_accounts в dict:
    { ad_account_id: {ad_name, name, username} }
    """
    accounts = {}
    for row in rows:
        ad_id = str(row.get("ad_account_id", "")).strip()
        if not ad_id:
            continue
        accounts[ad_id] = {
            "ad_name": str(row.get("ad_name", "")).strip(),
            "name": str(row.get("name", "")).strip(),
            "username": str(row.get("username", "")).strip(),
        }
    return accounts


def get_filtered_data(ad_accounts: dict) -> dict:
    total_spend = 0.0
    total_conversations = 0
    cpa_per_account = []

    total_accounts = len(ad_accounts)
    active_ad_accounts = set()

    cabinets_with_conversations = {}
    cabinets_with_sales = {}

    campaigns_count = {}
    sales_campaigns_count = {}

    cabinets_no_conversations = []
    cabinets_no_messaging_ads = []

    inactive_ad_accounts_names = []

    for account_id, info in ad_accounts.items():
        ad_name = info["ad_name"] or account_id

        data = get_account_data(account_id)
        campaigns = data.get("data") or []

        if not campaigns:
            inactive_ad_accounts_names.append(ad_name)
            continue

        spend_for_messaging = 0.0
        spend_for_sales = 0.0
        conversations_total = 0
        sales_total = 0

        active_campaigns_count = 0

        campaigns_count[ad_name] = 0
        sales_campaigns_count[ad_name] = 0

        has_messaging_ads = False
        has_sales_objective = False

        cabinet_is_gravo = ad_name in GRAVO_NAMES

        for campaign in campaigns:
            objective = campaign.get("objective")
            spend = float(campaign.get("spend", 0) or 0)

            if spend > 0:
                active_ad_accounts.add(account_id)
                active_campaigns_count += 1

            is_messaging_campaign = cabinet_is_gravo or campaign_has_messaging(campaign)

            if is_messaging_campaign:
                has_messaging_ads = True
                campaigns_count[ad_name] += 1
                spend_for_messaging += spend
                conversations_total += campaign_messaging_conversations(campaign)

            elif objective == "OUTCOME_SALES":
                has_sales_objective = True
                sales_campaigns_count[ad_name] += 1
                spend_for_sales += spend

                actions = campaign.get("actions") or []
                purchases = 0
                for a in actions:
                    if a.get("action_type") == "purchase":
                        purchases = _extract_int(a.get("value", 0))
                        break
                sales_total += purchases

        if conversations_total > 0:
            avg_cpa = spend_for_messaging / conversations_total
            cpa_per_account.append(avg_cpa)

            cabinets_with_conversations[ad_name] = {
                "conversations": conversations_total,
                "avg_cpa": avg_cpa,
                "active_campaigns": active_campaigns_count,
            }

            total_spend += spend_for_messaging
            total_conversations += conversations_total

        elif has_messaging_ads:
            cabinets_no_conversations.append(ad_name)

        elif not has_messaging_ads and not has_sales_objective:
            cabinets_no_messaging_ads.append(ad_name)

        if has_sales_objective:
            avg_sale_cpa = (spend_for_sales / sales_total) if sales_total > 0 else 0
            cabinets_with_sales[ad_name] = {
                "sales": sales_total,
                "avg_cpa": avg_sale_cpa,
                "active_campaigns": sales_campaigns_count[ad_name],
            }

    avg_cpa_weighted = (total_spend / total_conversations) if total_conversations > 0 else 0
    avg_cpa_by_accounts = (sum(cpa_per_account) / len(cpa_per_account)) if cpa_per_account else 0

    return {
        "total_accounts": total_accounts,
        "active_ad_accounts": list(active_ad_accounts),  # чтобы в json нормально сериализовалось
        "cabinets_with_conversations": cabinets_with_conversations,
        "cabinets_no_conversations": cabinets_no_conversations,
        "cabinets_no_messaging_ads": cabinets_no_messaging_ads,
        "cabinets_with_sales": cabinets_with_sales,
        "total_spend": total_spend,
        "total_conversations": total_conversations,
        "avg_cpa_weighted": avg_cpa_weighted,
        "avg_cpa_by_accounts": avg_cpa_by_accounts,
        "campaigns_count": campaigns_count,
        "inactive_ad_accounts": inactive_ad_accounts_names,
    }


def build_message(report: dict) -> str:
    yesterday_date = get_yesterday_date_ru()

    message = f"📊 Отчёт по рекламе за {yesterday_date}\n\n"
    message += f"📌 Всего рекламных кабинетов: {report['total_accounts']}\n"
    message += f"📢 Активных кабинетов: {len(report['active_ad_accounts'])}\n\n"

    message += f"💬 Кабинеты с переписками ({len(report['cabinets_with_conversations'])}):\n"
    for name, data in report["cabinets_with_conversations"].items():
        message += (
            f"➤ *{name}* - {data['conversations']} переписок | "
            f"CPA: {data['avg_cpa']:.2f} $ | Кампаний: {data['active_campaigns']}\n"
        )

    message += f"\n🛒 Кабинеты с продажами ({len(report['cabinets_with_sales'])}):\n"
    for name, data in report["cabinets_with_sales"].items():
        message += (
            f"➤ *{name}* - {data['sales']} продаж | "
            f"CPA: {data['avg_cpa']:.2f} $ | Кампаний: {data['active_campaigns']}\n"
        )

    message += f"\n⚠️ Кабинеты с рекламой, но без переписок ({len(report['cabinets_no_conversations'])}):\n"
    for name in report["cabinets_no_conversations"]:
        message += f"➤ *{name}*\n"

    message += f"\n🚫 Кабинеты без рекламы на переписки ({len(report['cabinets_no_messaging_ads'])}):\n"
    message += ", ".join(report["cabinets_no_messaging_ads"]) if report["cabinets_no_messaging_ads"] else "-"

    message += f"\n\n🛑 *Неактивные кабинеты ({len(report['inactive_ad_accounts'])}):*\n"
    message += ", ".join(report["inactive_ad_accounts"]) if report["inactive_ad_accounts"] else "-"

    message += f"\n\n💰 Всего потрачено: {report['total_spend']:.2f} $\n"
    message += f"📨 Всего переписок: {report['total_conversations']}\n"
    message += (
        f"⚡ Средняя цена за переписку (взвешенная): {report['avg_cpa_weighted']:.2f} $\n"
        f"  ➤  рассчитывается по формуле общие расходы / общее число переписок\n"
    )
    message += (
        f"🧮 Средняя цена по кабинетам: {report['avg_cpa_by_accounts']:.2f} $\n"
        f"  ➤  это простое среднее арифметическое всех CPA по кабинетам\n"
    )

    return message


def send_telegram_report():
    rows = get_records(WORKSHEET_ACCOUNTS)
    ad_accounts = build_accounts_dict(rows)

    report = get_filtered_data(ad_accounts)
    message = build_message(report)

    # 1) Новый режим: через Core
    if CORE_URL:
        run_id = None
        try:
            run_id = core_start_run(JOB_KEY, source="local_test" if "127.0.0.1" in CORE_URL or "localhost" in CORE_URL else "github_actions")

            summary = f"Daily report ok: spend ${report['total_spend']:.2f}, conversations={report['total_conversations']}, accounts={report['total_accounts']}"
            artifacts = [
                {"type": "text", "value": message, "meta": {"format": "telegram_markdown"}},
                {"type": "json", "value": json.dumps(report, ensure_ascii=False), "meta": {"name": "report"}},
            ]

            core_finish_run(
                run_id=run_id,
                status="success",
                summary=summary,
                artifacts=artifacts,
            )
            return

        except Exception as e:
            if run_id:
                try:
                    core_finish_run(
                        run_id=run_id,
                        status="error",
                        summary="Daily report failed",
                        error=str(e),
                        artifacts=[
                            {"type": "text", "value": "Daily report failed before delivery", "meta": {}}
                        ],
                    )
                except Exception:
                    pass
            raise

    # 2) Старый режим: напрямую в Telegram
    tg_send_to_forum_topic(message)


if __name__ == "__main__":
    required = ["FACEBOOK_ACCESS_TOKEN", "GOOGLE_SHEET_URL"]

    # Если Core не используем — Telegram обязателен
    if not CORE_URL:
        required += ["TELEGRAM_BOT_TOKEN", "TELEGRAM_FORUM_CHAT_ID", "TELEGRAM_THREAD_ID"]

    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing env: {', '.join(missing)}")

    send_telegram_report()
