from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_connection

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _require_auth(request: Request):
    if not request.session.get("user"):
        return RedirectResponse(url="/login", status_code=303)
    return None


def _get_expenses_and_categories():
    conn = get_connection()
    expenses = conn.execute("""
        SELECT e.id, e.name, e.amount, e.date, c.name AS category
        FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        ORDER BY e.date DESC
    """).fetchall()
    categories = conn.execute("SELECT name FROM categories ORDER BY name").fetchall()
    conn.close()
    return [dict(e) for e in expenses], [row["name"] for row in categories]


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

    conn = get_connection()
    if category:
        expenses = conn.execute("""
            SELECT e.id, e.name, e.amount, e.date, c.name AS category
            FROM expenses e
            LEFT JOIN categories c ON e.category_id = c.id
            WHERE c.name = ?
            ORDER BY e.date DESC
        """, (category,)).fetchall()
    else:
        expenses = conn.execute("""
            SELECT e.id, e.name, e.amount, e.date, c.name AS category
            FROM expenses e
            LEFT JOIN categories c ON e.category_id = c.id
            ORDER BY e.date DESC
        """).fetchall()
    categories = conn.execute("SELECT name FROM categories ORDER BY name").fetchall()
    conn.close()

    return templates.TemplateResponse(
        "partials/expense_list.html",
        {
            "request": request,
            "expenses": [dict(e) for e in expenses],
            "categories": [row["name"] for row in categories],
        },
    )
