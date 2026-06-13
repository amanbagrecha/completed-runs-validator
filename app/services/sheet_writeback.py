from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.config import ROOT_DIR, SHEET_CSV_URL


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_CREDENTIALS_PATH = ROOT_DIR / "vast-sheet-sync-32aedaa23a0f.json"
DEFAULT_SHEET_TITLE = "check_pano_bkp_counts"
APP_SHEET_COLUMNS = [
    "compltd_status",
    "compltd_validator",
    "compltd_started_at",
    "compltd_completed_at",
    "compltd_outcome",
    "compltd_reviewed_images",
    "compltd_failed_images",
    "compltd_updated_at",
]


class SheetClient(Protocol):
    def read_values(self) -> list[list[str]]:
        ...

    def write_values(self, cell_range: str, values: list[list[str]]) -> None:
        ...

    def batch_write_values(self, updates: list[tuple[str, list[list[str]]]]) -> None:
        ...


@dataclass(frozen=True)
class CompletionWriteback:
    run_id: str
    validator: str
    completed_at: str
    outcome: str
    reviewed_images: int
    failed_images: int


@dataclass(frozen=True)
class SheetWriteResult:
    status: str
    row_number: int | None = None
    detail: str = ""


class GoogleSheetClient:
    def __init__(self, spreadsheet_id: str, sheet_id: int, credentials_path: Path):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_id = sheet_id
        self.credentials_path = credentials_path
        self._access_token: str | None = None
        self._sheet_title: str | None = None

    @classmethod
    def from_config(cls) -> GoogleSheetClient | None:
        credentials_path = _credentials_path()
        if not credentials_path or not credentials_path.exists():
            return None
        spreadsheet_id = _spreadsheet_id_from_url(SHEET_CSV_URL)
        sheet_id = _sheet_id_from_url(SHEET_CSV_URL)
        if spreadsheet_id is None or sheet_id is None:
            raise RuntimeError("Could not parse Google Sheet id/gid from SHEET_CSV_URL")
        return cls(spreadsheet_id, sheet_id, credentials_path)

    def read_values(self) -> list[list[str]]:
        data = self._request(
            "GET",
            f"/values/{urllib.parse.quote(self._a1_range('A:ZZ'), safe='')}?majorDimension=ROWS",
        )
        return data.get("values", [])

    def write_values(self, cell_range: str, values: list[list[str]]) -> None:
        encoded_range = urllib.parse.quote(self._a1_range(cell_range), safe="")
        self._request(
            "PUT",
            f"/values/{encoded_range}?valueInputOption=USER_ENTERED",
            {"values": values},
        )

    def batch_write_values(self, updates: list[tuple[str, list[list[str]]]]) -> None:
        if not updates:
            return
        self._request(
            "POST",
            "/values:batchUpdate",
            {
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": self._a1_range(cell_range), "values": values}
                    for cell_range, values in updates
                ],
            },
        )

    def _a1_range(self, cell_range: str) -> str:
        title = self._sheet_title_value().replace("'", "''")
        return f"'{title}'!{cell_range}"

    def _sheet_title_value(self) -> str:
        if self._sheet_title is None:
            metadata = self._request("GET", "?fields=sheets.properties(sheetId,title)")
            fallback_title = os.getenv("COMPLTD_SHEET_TITLE", DEFAULT_SHEET_TITLE)
            available_titles: list[str] = []
            for sheet in metadata.get("sheets", []):
                properties = sheet.get("properties", {})
                title = properties.get("title")
                if title:
                    available_titles.append(str(title))
                if int(properties.get("sheetId", -1)) == self.sheet_id:
                    self._sheet_title = str(title)
                    break
            if self._sheet_title is None and fallback_title in available_titles:
                self._sheet_title = fallback_title
            if self._sheet_title is None:
                raise RuntimeError(
                    f"Could not find sheet tab with gid {self.sheet_id} or title {fallback_title!r}"
                )
        return self._sheet_title

    def _request(self, method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}{path}"
        headers = {"Authorization": f"Bearer {self._token()}"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=45) as response:
            response_data = response.read().decode("utf-8")
        return json.loads(response_data) if response_data else {}

    def _token(self) -> str:
        if self._access_token is None:
            try:
                from google.auth.transport.requests import Request
                from google.oauth2.service_account import Credentials
            except ImportError as exc:
                raise RuntimeError("Install google-auth to use Google Sheets writeback") from exc

            credentials = Credentials.from_service_account_file(
                str(self.credentials_path),
                scopes=[SHEETS_SCOPE],
            )
            credentials.refresh(Request())
            self._access_token = credentials.token
        return self._access_token


def write_run_completion(
    payload: CompletionWriteback,
    *,
    client: SheetClient | None = None,
) -> SheetWriteResult:
    if client is None:
        client = GoogleSheetClient.from_config()
    if client is None:
        return SheetWriteResult(status="disabled", detail="Google service-account JSON not found")

    return _write_fields(
        payload.run_id,
        {
            "compltd_status": "completed",
            "compltd_validator": payload.validator,
            "compltd_completed_at": payload.completed_at,
            "compltd_outcome": payload.outcome,
            "compltd_reviewed_images": str(payload.reviewed_images),
            "compltd_failed_images": str(payload.failed_images),
            "compltd_updated_at": payload.completed_at,
        },
        client,
    )


def _write_fields(run_id: str, fields: dict[str, str], client: SheetClient) -> SheetWriteResult:
    values = client.read_values()
    if not values:
        raise RuntimeError("Google Sheet has no header row")

    headers = [str(value).strip() for value in values[0]]
    _ensure_app_headers(headers, client)
    header_index = {header: index for index, header in enumerate(headers)}
    if "folder" not in header_index:
        raise RuntimeError("Google Sheet is missing required 'folder' column")

    folder_index = header_index["folder"]
    row_number = _find_run_row(values, folder_index, run_id)
    if row_number is None:
        raise RuntimeError(f"Run {run_id} was not found in Google Sheet")

    updates = [
        (f"{_column_letter(header_index[column] + 1)}{row_number}", [[value]])
        for column, value in fields.items()
        if column in APP_SHEET_COLUMNS
    ]
    client.batch_write_values(updates)
    return SheetWriteResult(status="updated", row_number=row_number)


def _ensure_app_headers(headers: list[str], client: SheetClient) -> None:
    missing = [column for column in APP_SHEET_COLUMNS if column not in headers]
    if not missing:
        return
    start = len(headers) + 1
    end = start + len(missing) - 1
    client.write_values(f"{_column_letter(start)}1:{_column_letter(end)}1", [missing])
    headers.extend(missing)


def _find_run_row(values: list[list[str]], folder_index: int, run_id: str) -> int | None:
    for row_number, row in enumerate(values[1:], start=2):
        value = row[folder_index] if folder_index < len(row) else ""
        if str(value).strip() == run_id:
            return row_number
    return None


def _column_letter(index: int) -> str:
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _credentials_path() -> Path | None:
    value = os.getenv("COMPLTD_GOOGLE_CREDENTIALS") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    return Path(value).expanduser() if value else DEFAULT_CREDENTIALS_PATH


def _spreadsheet_id_from_url(url: str) -> str | None:
    match = re.search(r"/spreadsheets/d/([^/]+)", url)
    return match.group(1) if match else None


def _sheet_id_from_url(url: str) -> int | None:
    match = re.search(r"[?&]gid=(\d+)", url)
    return int(match.group(1)) if match else None
