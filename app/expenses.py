import csv
import io
import os
from datetime import datetime

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import get_client

router = APIRouter()
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _require_auth(request: Request):
    if not request.session.get("user"):
        return HTMLResponse("Unauthorized", status_code=401)
    return None


# ── Receipt upload ──────────────────────────────────────────

async def extract_receipt_data(file_path: str) -> dict:
    """Placeholder for receipt image extraction.

    Will be replaced with an OpenRouter-based vision model call.
    Returns dict with keys: name, amount, date, category.
    """
    # TODO: Replace with OpenRouter API call
    return {
        "name": "Receipt expense",
        "amount": 0.00,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "category": "Other",
    }


@router.post("/upload/receipt")
async def upload_receipt(request: Request, file: UploadFile = File(...)):
    auth = _require_auth(request)
    if auth:
        return auth

    # Save uploaded file
    contents = await file.read()
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    with open(file_path, "wb") as f:
        f.write(contents)

    # Extract data from receipt image
    data = await extract_receipt_data(file_path)

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
    """Parse a CSV bank statement. Expects columns: date, description, amount."""
    transactions = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        # Normalize column names to lowercase
        row_lower = {k.lower().strip(): v.strip() for k, v in row.items()}
        name = row_lower.get("description", row_lower.get("name", row_lower.get("memo", "")))
        amount_str = row_lower.get("amount", row_lower.get("debit", "0"))
        date_str = row_lower.get("date", "")

        try:
            amount = abs(float(amount_str.replace(",", "").replace("$", "")))
        except (ValueError, AttributeError):
            continue

        if name and date_str:
            transactions.append({"name": name, "amount": amount, "date": date_str})
    return transactions


@router.post("/upload/statement")
async def upload_statement(request: Request, file: UploadFile = File(...)):
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
        # Dedup: skip if same name, amount, and date already exist
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
