"""
Dashboard routes: main page, expense list partial, and add-expense modal.

The main page (/) renders the full dashboard with all expenses.
The two partial endpoints return HTML fragments that HTMX swaps in-place
for filtering and opening the upload modal without a full page reload.
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_client

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _require_auth(request: Request):
    """Redirect to login if the user has no active session."""
    if not request.session.get("user"):
        return RedirectResponse(url="/login", status_code=303)
    return None


def _get_expenses_and_categories():
    """Fetch all expenses (joined with category name) and the category list."""
    db = get_client()
    expenses = (
        db.table("expenses")
        .select("id, name, amount, date, categories(name)")
        .order("date", desc=True)
        .execute()
        .data
    )
    categories = (
        db.table("categories")
        .select("name")
        .order("name")
        .execute()
        .data
    )

    # Supabase returns the joined category as {"categories": {"name": "..."}};
    # flatten it to a simple "category" key for easier template access.
    for e in expenses:
        cat = e.pop("categories", None)
        e["category"] = cat["name"] if cat else None

    return expenses, [row["name"] for row in categories]


@router.get("/")
def dashboard(request: Request):
    """Render the full dashboard page with expense list and filter bar."""
    redirect = _require_auth(request)
    if redirect:
        return redirect

    expenses, categories = _get_expenses_and_categories()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": request.session["user"],
            "expenses": expenses,
            "categories": categories,
        },
    )


@router.get("/modal/add-expense")
def add_expense_modal(request: Request):
    """Return the add-expense modal HTML (loaded by HTMX into #modal-container)."""
    redirect = _require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse("partials/add_expense_modal.html", {"request": request})


@router.get("/partials/expense-list")
def expense_list_partial(request: Request, category: str = ""):
    """Return the expense list HTML fragment, optionally filtered by category.

    Called by HTMX when the user selects a category filter. Swapped into
    #expense-list without a full page reload.
    """
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = get_client()

    query = db.table("expenses").select("id, name, amount, date, categories(name)").order("date", desc=True)
    if category:
        query = query.eq("categories.name", category)

    expenses = query.execute().data

    # Supabase still returns rows when the joined category doesn't match —
    # the join value is just null. Filter those out when a category is active.
    if category:
        expenses = [e for e in expenses if e.get("categories")]

    for e in expenses:
        cat = e.pop("categories", None)
        e["category"] = cat["name"] if cat else None

    categories = (
        db.table("categories")
        .select("name")
        .order("name")
        .execute()
        .data
    )

    return templates.TemplateResponse(
        "partials/expense_list.html",
        {
            "request": request,
            "expenses": expenses,
            "categories": [row["name"] for row in categories],
        },
    )
