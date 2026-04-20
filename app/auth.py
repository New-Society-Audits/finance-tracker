"""
Authentication routes: login, signup, and logout.

Uses Supabase Auth (email/password) for authentication.
On success the user's id and email are stored in the session cookie.
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
    """Render the login form, consuming any one-shot flash message from signup."""
    info = request.session.pop("flash_info", None)
    return templates.TemplateResponse("login.html", {"request": request, "info": info})


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

    request.session["user"] = response.user.email
    request.session["user_id"] = response.user.id
    return RedirectResponse(url="/", status_code=303)


@router.get("/signup")
def signup_page(request: Request):
    """Render the signup form."""
    return templates.TemplateResponse("signup.html", {"request": request})


@router.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    """Create a new Supabase Auth user and sign them in."""
    try:
        response = get_auth_client().auth.sign_up(
            {"email": email, "password": password}
        )
    except AuthApiError as e:
        return templates.TemplateResponse(
            "signup.html",
            {"request": request, "error": e.message or "Could not create account."},
            status_code=400,
        )

    # If email confirmation is enabled in Supabase, there may be no session yet.
    # Send the user to the login page with a one-shot flash message.
    if not response.session:
        request.session["flash_info"] = "Check your email to confirm your account, then sign in."
        return RedirectResponse(url="/login", status_code=303)

    request.session["user"] = response.user.email
    request.session["user_id"] = response.user.id
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    """Clear the session and redirect back to login."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
