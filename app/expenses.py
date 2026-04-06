"""
Expense management routes: receipt upload, bank statement import, and category updates.

Receipt upload:  saves the image to disk, extracts expense data (AI — TODO),
                 and inserts a new expense row.
Statement import: parses a CSV bank statement, deduplicates against existing
                  expenses, and bulk-inserts new transactions.
Category update: changes the category assigned to a single expense (called by
                 the inline dropdown via HTMX PUT).
"""

import base64
import csv
import io
import json
import logging
import mimetypes
import os
from datetime import datetime

import httpx
from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import get_client

logger = logging.getLogger(__name__)

def _openrouter_api_key():
    return os.getenv("OPENROUTER_API_KEY", "")

def _openrouter_model():
    return os.getenv("OPENROUTER_MODEL", "google/gemini-flash-2.0")

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Directory where uploaded receipt images are saved
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _require_auth(request: Request):
    """Return a 401 response if the user has no active session."""
    if not request.session.get("user"):
        return HTMLResponse("Unauthorized", status_code=401)
    return None


# ── Receipt upload ──────────────────────────────────────────

VALID_CATEGORIES = ["Food", "Transport", "Shopping", "Entertainment", "Health", "Utilities", "Other"]

RECEIPT_SYSTEM_PROMPT = f"""\
You are a receipt parser. Given a photo of a receipt, extract:
- name: a short description of the purchase (e.g. "Grocery run at Trader Joe's")
- amount: the total amount paid as a number (e.g. 42.50)
- date: the date on the receipt in YYYY-MM-DD format
- category: one of {VALID_CATEGORIES}

Respond ONLY with a JSON object, no markdown fences or extra text.
Example: {{"name": "Coffee at Starbucks", "amount": 5.75, "date": "2026-03-15", "category": "Food"}}
"""


async def extract_receipt_data(file_path: str) -> dict:
    """Extract expense fields from a receipt image via OpenRouter vision model."""
    fallback = {
        "name": "Receipt expense",
        "amount": 0.00,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "category": "Other",
    }

    if not _openrouter_api_key():
        logger.warning("OPENROUTER_API_KEY not set — using fallback receipt data")
        return fallback

    # Base64-encode the image
    mime_type = mimetypes.guess_type(file_path)[0] or "image/jpeg"
    with open(file_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {_openrouter_api_key()}"},
                json={
                    "model": _openrouter_model(),
                    "messages": [
                        {"role": "system", "content": RECEIPT_SYSTEM_PROMPT},
                        {"role": "user", "content": [
                            {"type": "image_url", "image_url": {
                                "url": f"data:{mime_type};base64,{image_b64}",
                            }},
                            {"type": "text", "text": "Extract the expense details from this receipt."},
                        ]},
                    ],
                },
            )
            result = resp.json()
            if resp.status_code != 200:
                logger.error("OpenRouter error %s: %s", resp.status_code, result)
                return fallback
            content = result["choices"][0]["message"]["content"]
            # Strip markdown fences if the model wraps the JSON
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1]  # remove opening fence
                content = content.rsplit("```", 1)[0]  # remove closing fence
            data = json.loads(content)

            # Validate and sanitize
            return {
                "name": str(data.get("name", fallback["name"]))[:200],
                "amount": round(float(data.get("amount", 0)), 2),
                "date": str(data.get("date", fallback["date"])),
                "category": data.get("category") if data.get("category") in VALID_CATEGORIES else "Other",
            }
        except Exception as e:
            logger.error("Receipt extraction failed: %s", e)
            return fallback


@router.post("/upload/receipt")
async def upload_receipt(request: Request, file: UploadFile = File(...)):
    """Save a receipt image, extract expense data, and insert into the database.

    Returns an HTML snippet that HTMX swaps into the modal to show a success
    message, then auto-closes the modal and refreshes the expense list.
    """
    auth = _require_auth(request)
    if auth:
        return auth

    # Save the uploaded file with a timestamped filename
    contents = await file.read()
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    with open(file_path, "wb") as f:
        f.write(contents)

    # Extract structured data from the receipt image
    data = await extract_receipt_data(file_path)

    # Look up the category ID by name so we can store the foreign key
    db = get_client()
    cat_result = db.table("categories").select("id").eq("name", data["category"]).execute()
    category_id = cat_result.data[0]["id"] if cat_result.data else None

    db.table("expenses").insert({
        "name": data["name"],
        "amount": data["amount"],
        "date": data["date"],
        "category_id": category_id,
        "receipt_path": file_path,
    }).execute()

    # Return inline HTML: success message → auto-close modal → refresh list
    return HTMLResponse("""
        <div class="upload-success">
            Receipt processed and expense added.
        </div>
        <script>
            setTimeout(() => {
                document.getElementById('modal-overlay').remove();
                htmx.ajax('GET', '/partials/expense-list', {target: '#expense-list', swap: 'innerHTML'});
            }, 800);
        </script>
    """)


# ── Bank statement upload ───────────────────────────────────

def parse_csv_statement(content: str) -> list[dict]:
    """Parse a CSV bank statement into a list of transaction dicts.

    Handles common column names: date, description/name/memo, amount/debit.
    Strips currency symbols and commas from amounts.
    """
    transactions = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        # Normalize column names to lowercase for flexible matching
        row_lower = {k.lower().strip(): v.strip() for k, v in row.items()}
        name = row_lower.get("description", row_lower.get("name", row_lower.get("memo", "")))
        amount_str = row_lower.get("amount", row_lower.get("debit", "0"))
        date_str = row_lower.get("date", "")

        try:
            amount = abs(float(amount_str.replace(",", "").replace("$", "")))
        except (ValueError, AttributeError):
            continue  # Skip rows with unparseable amounts

        if name and date_str:
            transactions.append({"name": name, "amount": amount, "date": date_str})
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
        text = contents.decode("utf-8", errors="replace")
        transactions = parse_csv_statement(text)
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
        db.table("expenses").insert({
            "name": txn["name"],
            "amount": txn["amount"],
            "date": txn["date"],
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

    return HTMLResponse(status_code=200)
