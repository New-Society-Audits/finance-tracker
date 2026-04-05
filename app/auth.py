"""
Authentication routes: login page, credential check, and logout.

Uses Supabase Auth (email/password) for authentication.
On success the user's email is stored in the session cookie.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from supabase import AuthApiError

from app.database import get_auth_client

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/login")
def login_page(request: Request):
    """Render the login form."""
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    """Authenticate via Supabase Auth and create a session on success."""
    try:
        response = get_auth_client().auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except AuthApiError:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid email or password."},
            status_code=401,
        )

    # Store the email in the signed session cookie
    request.session["user"] = response.user.email
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    """Clear the session and redirect back to login."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
