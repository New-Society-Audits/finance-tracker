"""
Finance Tracker — application entry point.

Creates the FastAPI app, registers middleware and routers,
and starts the dev server when run directly.
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

from app.auth import router as auth_router
from app.dashboard import router as dashboard_router
from app.expenses import router as expenses_router

load_dotenv()

# Used to sign session cookies — read from env for security
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-this-before-deploying")

app = FastAPI()

# Serve CSS and other static assets from /static
app.mount("/static", StaticFiles(directory="static"), name="static")

# Session middleware stores login state in a signed cookie
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# Register route groups
app.include_router(auth_router)       # /login, /logout
app.include_router(dashboard_router)  # /, /modal/add-expense, /partials/expense-list
app.include_router(expenses_router)   # /upload/statement, /expense/{id}/category


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
