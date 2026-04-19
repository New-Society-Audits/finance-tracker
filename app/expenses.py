"""
Expense management routes: bank statement import and category updates.

Statement import: parses a CSV bank statement, deduplicates against existing
                  expenses, and bulk-inserts new transactions.
Category update: changes the category assigned to a single expense (called by
                 the inline dropdown via HTMX PUT).
"""

from __future__ import annotations

import csv
import io
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import get_client

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _require_auth(request: Request):
    """Return a 401 response if the user has no active session."""
    if not request.session.get("user"):
        return HTMLResponse("Unauthorized", status_code=401)
    return None


# ── Bank statement upload ───────────────────────────────────

def _decode_statement_bytes(raw: bytes) -> str:
    # Česká spořitelna exports CSV as UTF-16 LE with a BOM.
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1250", errors="replace")


def _find_column(fieldnames: list[str], *needles: str) -> str | None:
    for name in fieldnames:
        low = name.lower()
        if any(n in low for n in needles):
            return name
    return None


def _parse_amount(amount_str: str) -> float | None:
    # Strip currency symbols, regular + non-breaking spaces, narrow no-break space.
    cleaned = (
        amount_str.replace("Kč", "")
        .replace("CZK", "")
        .replace("$", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .strip()
    )
    # Czech format uses comma as decimal separator (e.g. "1234,56").
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(date_str: str) -> str | None:
    # Accept DD.MM.YYYY, DD/MM/YYYY, or ISO YYYY-MM-DD.
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_csv_statement(raw: bytes) -> list[dict]:
    """Parse a CSV bank statement into a list of expense dicts.

    Supports Česká spořitelna UTF-16 exports (Czech column names, DD.MM.YYYY
    dates, comma decimals, NBSP thousands) as well as generic English CSVs.
    Positive amounts are treated as income and skipped — only outgoing
    transactions become expenses.
    """
    text = _decode_statement_bytes(raw)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []

    date_col = _find_column(reader.fieldnames, "datum", "date")
    # "protiúčtu" = counterparty name in Česká spořitelna exports.
    name_col = _find_column(reader.fieldnames, "protiúč", "protiuc", "description", "name", "memo")
    amount_col = _find_column(reader.fieldnames, "částka", "castka", "amount", "debit")

    if not (date_col and amount_col):
        return []

    transactions = []
    for row in reader:
        raw_amount = (row.get(amount_col) or "").strip()
        signed = _parse_amount(raw_amount)
        if signed is None or signed == 0:
            continue

        date_iso = _parse_date((row.get(date_col) or "").strip())
        if not date_iso:
            continue

        name = (row.get(name_col) or "").strip() if name_col else ""
        if not name:
            name = "Bank transaction"

        transactions.append({"name": name, "amount": signed, "date": date_iso})
    return transactions


@router.post("/upload/statement")
async def upload_statement(request: Request, file: UploadFile = File(...)):
    """Import transactions from a CSV bank statement.

    Each transaction is checked against existing expenses (by name + amount + date)
    to avoid duplicates. Returns an HTML snippet showing how many were imported.
    """
    auth = _require_auth(request)
    if auth:
        return auth

    contents = await file.read()
    filename = file.filename or ""

    if filename.lower().endswith(".csv"):
        transactions = parse_csv_statement(contents)
    else:
        return HTMLResponse('<div class="upload-error">Only CSV files are supported for now.</div>')

    if not transactions:
        return HTMLResponse('<div class="upload-error">No transactions found in file.</div>')

    db = get_client()
    inserted = 0
    for txn in transactions:
        # Dedup: skip if an expense with the same name, amount, and date exists
        existing = (
            db.table("expenses")
            .select("id")
            .eq("name", txn["name"])
            .eq("amount", txn["amount"])
            .eq("date", txn["date"])
            .execute()
        )
        if existing.data:
            continue

        prior_q = (
            db.table("expenses")
            .select("category_id")
            .eq("name", txn["name"])
            .not_.is_("category_id", "null")
        )
        # Only match priors with the same sign — a refund (positive) should not
        # pass its category to a later charge (negative) with the same name.
        if txn["amount"] >= 0:
            prior_q = prior_q.gte("amount", 0)
        else:
            prior_q = prior_q.lt("amount", 0)
        prior_cats = {row["category_id"] for row in prior_q.execute().data}
        category_id = prior_cats.pop() if len(prior_cats) == 1 else None

        db.table("expenses").insert({
            "name": txn["name"],
            "amount": txn["amount"],
            "date": txn["date"],
            "category_id": category_id,
        }).execute()
        inserted += 1

    return HTMLResponse(f"""
        <div class="upload-success">
            Imported {inserted} new transaction{"s" if inserted != 1 else ""}
            ({len(transactions) - inserted} duplicates skipped).
        </div>
        <script>
            setTimeout(() => {{
                document.getElementById('modal-overlay').remove();
                htmx.ajax('GET', '/partials/expense-list', {{target: '#expense-list', swap: 'innerHTML'}});
            }}, 800);
        </script>
    """)


# ── Category update ─────────────────────────────────────────

@router.put("/expense/{expense_id}/category")
async def update_category(request: Request, expense_id: int):
    """Update the category of a single expense.

    Called via HTMX PUT when the user changes the category dropdown
    on an expense item. Looks up the category ID by name and updates the row.
    """
    auth = _require_auth(request)
    if auth:
        return auth

    form = await request.form()
    category_name = form.get("category", "")

    db = get_client()
    if category_name:
        cat_result = db.table("categories").select("id").eq("name", category_name).execute()
        category_id = cat_result.data[0]["id"] if cat_result.data else None
    else:
        category_id = None

    db.table("expenses").update({"category_id": category_id}).eq("id", expense_id).execute()

    tag_content = category_name if category_name else ""
    return HTMLResponse(
        f'<span class="expense-category-tag" id="category-tag-{expense_id}">{tag_content}</span>'
    )


# ── Delete expense ─────────────────────────────────────────

@router.delete("/expense/{expense_id}")
async def delete_expense(request: Request, expense_id: int):
    auth = _require_auth(request)
    if auth:
        return auth

    db = get_client()
    db.table("expenses").delete().eq("id", expense_id).execute()

    return HTMLResponse("")
