"""
Authentication routes: login page, credential check, and logout.

Uses a simple username/password lookup against the Supabase `users` table.
On success the username is stored in the session cookie.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_client

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/login")
def login_page(request: Request):
    """Render the login form."""
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Validate credentials and create a session on success."""
    result = (
        get_client()
        .table("users")
        .select("id")
        .eq("username", username)
        .eq("password", password)
        .execute()
    )

    if not result.data:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username or password."},
            status_code=401,
        )

    # Store the username in the signed session cookie
    request.session["user"] = username
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    """Clear the session and redirect back to login."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
