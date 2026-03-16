from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_connection

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@router.get("/")
def dashboard(request: Request):
    if not request.session.get("user"):
        return RedirectResponse(url="/login", status_code=303)

    conn = get_connection()
    expenses = conn.execute("""
        SELECT e.id, e.name, e.amount, e.date, c.name AS category
        FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        ORDER BY e.date DESC
    """).fetchall()
    categories = conn.execute("SELECT name FROM categories ORDER BY name").fetchall()
    conn.close()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": request.session["user"],
            "expenses": [dict(e) for e in expenses],
            "categories": [row["name"] for row in categories],
        },
    )
