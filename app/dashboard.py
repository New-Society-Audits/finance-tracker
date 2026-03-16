from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_connection

router = APIRouter()
templates = Jinja2Templates(directory="templates")

PLACEHOLDER_EXPENSES = [
    {"name": "Grocery Shopping", "date": "2026-03-15", "amount": 54.30},
    {"name": "Uber Ride", "date": "2026-03-14", "amount": 12.50},
    {"name": "Netflix Subscription", "date": "2026-03-13", "amount": 15.99},
    {"name": "Coffee Shop", "date": "2026-03-12", "amount": 6.80},
    {"name": "Gym Membership", "date": "2026-03-10", "amount": 35.00},
    {"name": "Electricity Bill", "date": "2026-03-08", "amount": 89.40},
    {"name": "Amazon Order", "date": "2026-03-05", "amount": 43.20},
]


@router.get("/")
def dashboard(request: Request):
    if not request.session.get("user"):
        return RedirectResponse(url="/login", status_code=303)

    conn = get_connection()
    categories = conn.execute("SELECT name FROM categories ORDER BY name").fetchall()
    conn.close()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": request.session["user"],
            "expenses": PLACEHOLDER_EXPENSES,
            "categories": [row["name"] for row in categories],
        },
    )
