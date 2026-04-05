from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_client

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _require_auth(request: Request):
    if not request.session.get("user"):
        return RedirectResponse(url="/login", status_code=303)
    return None


def _get_expenses_and_categories():
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

    # Flatten the joined category name
    for e in expenses:
        cat = e.pop("categories", None)
        e["category"] = cat["name"] if cat else None

    return expenses, [row["name"] for row in categories]


@router.get("/")
def dashboard(request: Request):
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
    redirect = _require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse("partials/add_expense_modal.html", {"request": request})


@router.get("/partials/expense-list")
def expense_list_partial(request: Request, category: str = ""):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    db = get_client()

    query = db.table("expenses").select("id, name, amount, date, categories(name)").order("date", desc=True)
    if category:
        query = query.eq("categories.name", category)

    expenses = query.execute().data

    # Filter out rows where the category join returned null (when filtering)
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
