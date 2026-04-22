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
from datetime import datetime

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import get_client

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _require_auth(request: Request):
    """Return a 401 response if the user has no active session."""
    if not request.session.get("user_id"):
        return HTMLResponse("Unauthorized", status_code=401)
    return None


# ── Bank statement upload ───────────────────────────────────

# Column-name vocabulary. Czech banks (ČS, ČSOB, KB, Raiffeisen, Fio, mBank,
# Air Bank, Moneta) all ship slightly different headers, sometimes with and
# sometimes without diacritics — match on substring so either form works.
_DATE_KEYWORDS = ("datum", "date")
# When a statement has several date columns, prefer the one that reflects when
# money actually moved (posting / debit / credit) over due or value date.
_DATE_PREFERENCE = (
    "zaúčt", "zauct",
    "odepsán", "odepsan", "připsán", "pripsan",
    "provedení", "proveden",
    "book", "process", "posting",
)
_AMOUNT_KEYWORDS = ("částka", "castka", "amount", "objem", "suma", "hodnota")
# Some exports (older KB, ČSOB statement CSVs) split outflows and inflows.
_DEBIT_KEYWORDS = ("vrub", "výdaj", "vydaj", "debit")
_CREDIT_KEYWORDS = ("prospěch", "prospech", "příjem", "prijem", "credit")
# Name-column tiers. Banks disagree about which field holds the useful label:
# card payments put the merchant in Popis/Poznámka; transfers put a party in
# "Název protistrany"; Fio often leaves "Zpráva pro příjemce" blank and puts
# the card merchant in "Poznámka". We collect every matching column ordered
# by tier and, per row, pick the first non-empty value.
_NAME_KEYWORD_TIERS = (
    # Tier 1: authoritative identifiers — counterparty name, explicit description,
    # message-to-recipient. These tend to be stable across re-imports.
    ("název protistrany", "nazev protistrany",
     "název protiúčtu", "nazev protiuctu",
     "popis", "description",
     "zpráva pro příjemce", "zprava pro prijemce",
     "merchant", "payee", "beneficiary", "memo"),
    # Tier 2: personal notes and purpose — Fio card payments land here.
    ("účel", "ucel", "purpose",
     "poznámka", "poznamka", "note",
     "zpráva pro mě", "zprava pro me",
     "zpráva", "zprava", "message"),
    # Tier 3: transaction type / category — last meaningful fallback.
    ("typ", "druh", "type", "kategorie", "category"),
    # Tier 4: counter-account number / IBAN — not human-friendly, but still
    # better than a generic "Bank transaction" placeholder.
    ("protiúč", "protiuc", "protistrana", "protistrany", "iban protistrany"),
)


def _decode_statement_bytes(raw: bytes) -> str:
    """Decode a statement blob using whichever encoding actually works.

    Czech banks export in a mix of UTF-16 LE (Česká spořitelna), CP1250 (older
    KB/ČSOB) and UTF-8 (most newer exports). BOMs first, then a null-byte
    heuristic for BOM-less UTF-16, then each candidate encoding in turn.
    """
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le").lstrip("\ufeff")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be").lstrip("\ufeff")
    sample = raw[:2000]
    if sample and sample.count(b"\x00") > len(sample) // 3:
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1250", "iso-8859-2"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _detect_delimiter(sample: str) -> str:
    """Pick the most likely column delimiter — Czech banks usually use ';'."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        pass
    lines = [l for l in sample.splitlines() if l.strip()]
    counts = {d: sum(l.count(d) for l in lines) for d in (";", ",", "\t", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def _find_header_row(text: str, delimiter: str) -> int | None:
    """Index of the first row that looks like a CSV header.

    Bank statements commonly start with preamble rows (account number, IBAN,
    period) before the real header. Scan until we find a row mentioning both
    a date-like and an amount-like column.
    """
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    amount_like = _AMOUNT_KEYWORDS + _DEBIT_KEYWORDS + _CREDIT_KEYWORDS
    for idx, row in enumerate(reader):
        if idx > 50:
            break
        if len(row) < 2:
            continue
        lowers = [(c or "").strip().lower() for c in row]
        has_date = any(any(k in c for k in _DATE_KEYWORDS) for c in lowers)
        has_amount = any(any(k in c for k in amount_like) for c in lowers)
        if has_date and has_amount:
            return idx
    return None


def _find_column(fieldnames: list[str], *needles: str) -> str | None:
    for name in fieldnames:
        if not name:
            continue
        low = name.lower()
        if any(n in low for n in needles):
            return name
    return None


def _find_date_column(fieldnames: list[str]) -> str | None:
    candidates = [n for n in fieldnames if n and any(k in n.lower() for k in _DATE_KEYWORDS)]
    if not candidates:
        return None
    for marker in _DATE_PREFERENCE:
        for name in candidates:
            if marker in name.lower():
                return name
    return candidates[0]


def _find_name_columns(fieldnames: list[str]) -> list[str]:
    """Columns to try for a human-readable label, ordered by tier then fieldname order.

    Returning a list (not a single column) lets us fall back per row — an
    empty Popis on a card payment defers to Poznámka, and so on.
    """
    result: list[str] = []
    seen: set[str] = set()
    for tier in _NAME_KEYWORD_TIERS:
        for name in fieldnames:
            if not name or name in seen:
                continue
            low = name.lower()
            if any(k in low for k in tier):
                result.append(name)
                seen.add(name)
    return result


def _row_name(row: dict, name_cols: list[str]) -> str:
    for col in name_cols:
        val = (row.get(col) or "").strip()
        if _is_meaningful_label(val):
            return val
    return "Bank transaction"


def _is_meaningful_label(val: str) -> bool:
    """Reject empty strings and pure-numeric codes as labels.

    Tier 3/4 fields often hold codes ("Typ"="0", an IBAN, a variable symbol)
    for rows where no real description exists. Those are worse than the
    "Bank transaction" fallback, so skip them and keep looking.
    """
    if not val:
        return False
    alnum = "".join(ch for ch in val if ch.isalnum())
    return bool(alnum) and not alnum.isdigit()


def _parse_amount(amount_str: str) -> float | None:
    cleaned = amount_str
    for sym in ("Kč", "CZK", "EUR", "USD", "GBP", "€", "$", "£"):
        cleaned = cleaned.replace(sym, "")
    # Strip regular + non-breaking + narrow no-break spaces (thousands sep).
    cleaned = cleaned.replace("\u00a0", "").replace("\u202f", "").replace(" ", "").strip()
    if not cleaned:
        return None
    # Mixed separators ("1.234,56" EU or "1,234.56" US): whichever is last is decimal.
    if "," in cleaned and "." in cleaned:
        if cleaned.rindex(",") > cleaned.rindex("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(date_str: str) -> str | None:
    s = date_str.strip()
    if not s:
        return None
    # Drop a trailing time component ("15.03.2026 10:22" or ISO "...T...").
    s = s.split(" ")[0].split("T")[0]
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d",
                "%d.%m.%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _row_amount(row: dict, amount_col: str | None,
                debit_col: str | None, credit_col: str | None) -> float | None:
    """Signed amount from a row, combining split debit/credit columns if needed."""
    if amount_col:
        return _parse_amount((row.get(amount_col) or "").strip())
    debit = _parse_amount((row.get(debit_col) or "").strip()) if debit_col else None
    credit = _parse_amount((row.get(credit_col) or "").strip()) if credit_col else None
    if credit:
        return abs(credit)
    if debit:
        # Split-column exports store debit as a positive magnitude; re-sign it.
        return -abs(debit)
    return None


def parse_csv_statement(raw: bytes) -> list[dict]:
    """Parse a CSV bank statement into a list of {name, amount, date} dicts.

    Tolerates quirks found across Czech bank exports: UTF-16/CP1250/UTF-8
    encodings, comma/semicolon/tab delimiters, preamble rows before the real
    header, Czech column names with or without diacritics, DD.MM.YYYY dates,
    comma decimal separators, NBSP thousands separators, and split
    debit/credit columns. Negative amounts = outflows, positive = inflows.
    """
    text = _decode_statement_bytes(raw)
    if not text.strip():
        return []

    sample = "\n".join(text.splitlines()[:40])
    delimiter = _detect_delimiter(sample)

    header_idx = _find_header_row(text, delimiter)
    if header_idx is None:
        return []

    body = "\n".join(text.splitlines()[header_idx:])
    reader = csv.DictReader(io.StringIO(body), delimiter=delimiter)
    if not reader.fieldnames:
        return []

    date_col = _find_date_column(reader.fieldnames)
    amount_col = _find_column(reader.fieldnames, *_AMOUNT_KEYWORDS)
    debit_col = _find_column(reader.fieldnames, *_DEBIT_KEYWORDS) if not amount_col else None
    credit_col = _find_column(reader.fieldnames, *_CREDIT_KEYWORDS) if not amount_col else None
    name_cols = _find_name_columns(reader.fieldnames)

    if not date_col or not (amount_col or debit_col or credit_col):
        return []

    transactions = []
    for row in reader:
        signed = _row_amount(row, amount_col, debit_col, credit_col)
        if signed is None or signed == 0:
            continue

        date_iso = _parse_date((row.get(date_col) or "").strip())
        if not date_iso:
            continue

        transactions.append({
            "name": _row_name(row, name_cols),
            "amount": signed,
            "date": date_iso,
        })
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

    user_id = request.session["user_id"]

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
        # Dedup: skip if this user already has an expense with the same name, amount, and date
        existing = (
            db.table("expenses")
            .select("id")
            .eq("user_id", user_id)
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
            .eq("user_id", user_id)
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
            "user_id": user_id,
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

    user_id = request.session["user_id"]

    form = await request.form()
    category_name = form.get("category", "")

    db = get_client()
    if category_name:
        cat_result = db.table("categories").select("id").eq("name", category_name).execute()
        category_id = cat_result.data[0]["id"] if cat_result.data else None
    else:
        category_id = None

    (
        db.table("expenses")
        .update({"category_id": category_id})
        .eq("id", expense_id)
        .eq("user_id", user_id)
        .execute()
    )

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

    user_id = request.session["user_id"]

    db = get_client()
    (
        db.table("expenses")
        .delete()
        .eq("id", expense_id)
        .eq("user_id", user_id)
        .execute()
    )

    return HTMLResponse("")
