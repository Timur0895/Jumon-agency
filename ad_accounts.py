import os
import re
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# =========================
# Google Sheets connection
# =========================

def _extract_spreadsheet_id(sheet_url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
    if not match:
        raise ValueError("Invalid GOOGLE_SHEET_URL")
    return match.group(1)

def _get_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "credentials.json", scope
    )
    return gspread.authorize(creds)

def open_spreadsheet():
    """
    Открывает Google Sheet по GOOGLE_SHEET_URL
    """
    sheet_url = os.getenv("GOOGLE_SHEET_URL")
    if not sheet_url:
        raise RuntimeError("Missing env: GOOGLE_SHEET_URL")

    spreadsheet_id = _extract_spreadsheet_id(sheet_url)
    client = _get_client()
    return client.open_by_key(spreadsheet_id)

# =========================
# Public API
# =========================

def get_worksheet(name: str):
    """
    Возвращает worksheet по имени
    """
    spreadsheet = open_spreadsheet()
    return spreadsheet.worksheet(name)

def get_records(worksheet_name: str) -> list[dict]:
    """
    Возвращает все строки листа в виде списка dict
    """
    ws = get_worksheet(worksheet_name)
    return ws.get_all_records()
