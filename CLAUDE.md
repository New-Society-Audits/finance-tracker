# CLAUDE.md

## Project Overview

Personal finance tracker — upload bank statements and transactions are automatically imported, categorized, and listed. Single-user app with Supabase Auth.

## Tech Stack

- **Backend:** FastAPI + Jinja2 templates
- **Frontend:** HTMX (no JS framework)
- **Database:** Supabase (PostgreSQL) with two clients — anon key for auth, service-role key for data
- **Auth:** Supabase Auth (email/password), session state in signed cookies via Starlette SessionMiddleware
- **Deployment:** Render (Web Service)

## Package Managers

- **Python:** Use `uv` (not pip). Run `uv sync` to install, `uv add <pkg>` to add dependencies.
- **JS:** Use `pnpm` (not npm), if JS packages are ever needed.

## Running Locally

```sh
uv run main.py
# → http://localhost:8000
```

## Project Structure

- `main.py` — FastAPI app setup, middleware, router registration
- `app/auth.py` — Login/logout routes (Supabase Auth)
- `app/dashboard.py` — Dashboard, expense list, add-expense modal
- `app/expenses.py` — Bank statement import, category updates
- `app/database.py` — Supabase client singletons (anon + service-role)
- `templates/` — Jinja2 templates; `partials/` subdir for HTMX fragments
- `static/` — CSS files
- `migrations/` — SQL migrations (run manually in Supabase SQL Editor)

## Environment Variables

Defined in `.env` (never committed):
- `SUPABASE_URL` — Project URL
- `SUPABASE_KEY` — Publishable/anon key (used for auth)
- `SUPABASE_SECRET_KEY` — Service-role key (used for data ops, bypasses RLS)
- `SESSION_SECRET` — Signs session cookies

## Key Patterns

- HTMX partials return HTML fragments, not JSON
- Database clients are singletons created on first use (`get_client()`, `get_auth_client()`)
- Migrations are not auto-run — apply manually via Supabase SQL Editor
