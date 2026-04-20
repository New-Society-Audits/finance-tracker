# Finance Tracker

A personal expense tracking app. Upload a bank statement and transactions are automatically imported, categorized, and added to your list.

## Tech Stack

- **Backend:** FastAPI + Jinja2
- **Frontend:** HTMX
- **Database:** Supabase (PostgreSQL)
- **Package Manager:** uv

## Setup

### Prerequisites

- Python 3.9+
- [uv](https://docs.astral.sh/uv/)
- A [Supabase](https://supabase.com/) project

### Install dependencies

```sh
uv sync
```

### Configure environment

Create a `.env` file in the project root:

```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_publishable_key
SUPABASE_SECRET_KEY=your_secret_key
```

### Initialize the database

Run the migrations in `migrations/` against your Supabase project's SQL Editor in order. They create the `categories` and `expenses` tables and seed default categories.

### Run the app

```sh
uv run main.py
```

The app will be available at `http://localhost:8000`.

## Project Structure

```
app/
  auth.py          # Login/logout routes
  dashboard.py     # Dashboard and expense list endpoints
  database.py      # Supabase client
  expenses.py      # Bank statement import, category updates
templates/
  login.html
  dashboard.html
  partials/        # HTMX partials (expense list, add modal)
static/
  login.css
  dashboard.css
migrations/
  001_initial_schema.sql
```

## Deployment (Render)

The app is deployed on [Render](https://render.com/) as a Web Service.

- **Runtime:** Python 3
- **Build command:** `pip install uv && uv sync --frozen`
- **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`

Set these environment variables in the Render dashboard:

| Variable | Description |
|---|---|
| `SESSION_SECRET` | Random string for signing session cookies |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Supabase publishable/anon key |
| `SUPABASE_SECRET_KEY` | Supabase service-role key |

## Features

- Single-user login with session auth
- Expense list with expandable details and category assignment
- Filter expenses by category
- CSV bank statement import with deduplication
