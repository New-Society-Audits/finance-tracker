# Repository Audit — `finance-tracker`

## Executive Summary

`finance-tracker` is a small, well-organized FastAPI + HTMX app that uses Supabase for auth and storage. The code is readable, the CSV parser is thoughtfully engineered for Czech bank exports, and the architecture is appropriate for the scope. However, the app has **several material security weaknesses**: all data access uses the Supabase service-role key (bypassing RLS) and trusts a session-cookie `user_id` for authorization, there is no CSRF protection on state-changing requests, session cookies are not hardened, and category-update inputs are rendered into HTML without escaping (XSS). There are also reliability issues in the CSV importer (per-row N+1 queries, slow synchronous I/O in async endpoints) and missing tests/CI. The fixes are mostly small and high-leverage.

---

## Findings (highest impact first)

### 1. 🔴 Critical — Service-role key used for all data ops; RLS is effectively disabled

`app/database.py` exposes a single `get_client()` built with `SUPABASE_SECRET_KEY`, and every route in `dashboard.py`/`expenses.py` filters by `user_id` taken from the session cookie. `migrations/007_enable_rls.sql` enables RLS but adds **no policies**, and the comment explicitly notes that the service key bypasses it.

Consequences:
- Any bug that forgets `.eq("user_id", ...)` (or any future endpoint that takes an `expense_id` without scoping) leaks/edits other users' data.
- If the service-role key leaks (env exfiltration, log capture, dependency compromise), the entire database is open.
- Defense-in-depth promised by migration 007 is not real.

**Recommendation**
- Switch user-scoped data operations to a **per-request client created with the user's Supabase access token** (`create_client(URL, ANON_KEY)` + `postgrest.auth(jwt)`), and add RLS policies (`auth.uid() = user_id`) on `expenses`. Keep the service-role client only for trusted server-side jobs (e.g., admin tasks), not request handlers.
- Add a `select` policy on `categories` for `authenticated` role.
- Persist the Supabase `access_token` / `refresh_token` in the session (server-side or signed) and refresh as needed.

Files: `app/database.py`, `app/auth.py`, `app/dashboard.py`, `app/expenses.py`, `migrations/007_enable_rls.sql`.

---

### 2. 🔴 High — XSS via category name rendered as raw HTML

In `app/expenses.py::update_category`:

```python
return HTMLResponse(
    f'<span class="expense-category-tag" id="category-tag-{expense_id}">{tag_content}</span>'
)
```

`tag_content` is `form.get("category", "")` — user-controlled (HTMX-driven select), unescaped, and the same field is also looked up against the DB without validating membership. If an attacker (or a future feature allowing custom categories) submits `<img onerror=...>`, it is injected verbatim and runs on the dashboard via `htmx-swap`.

The success snippet from `/upload/statement` returning a `<script>` block via `HTMLResponse` (executed by HTMX) is also a footgun: any future interpolation into that string would similarly bypass Jinja autoescape.

**Recommendation**
- Render fragments via Jinja templates (autoescape on), not f-strings: e.g., `templates.TemplateResponse("partials/category_tag.html", {...})`.
- Validate `category_name` against the categories table and reject unknowns with a 400.
- Avoid returning inline `<script>`; use HX-Trigger response headers (`HX-Trigger: refresh-expenses`) and bind handlers on the client.

Files: `app/expenses.py` (`update_category`, end of `upload_statement`).

---

### 3. 🔴 High — Session cookie not hardened; weak default secret

In `main.py`:

```python
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-this-before-deploying")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
```

- The fallback secret is committed and would let anyone forge sessions if env is misconfigured.
- `SessionMiddleware` defaults to `https_only=False`, no `same_site`, no max age. The cookie containing `user_id` is trivially replayable over plain HTTP.

**Recommendation**
- Fail fast: `SECRET_KEY = os.environ["SESSION_SECRET"]` (raise on missing in production).
- Configure: `SessionMiddleware(secret_key=..., https_only=True, same_site="lax", max_age=60*60*24*14)`.
- Add `app.add_middleware(HTTPSRedirectMiddleware)` behind Render (or trust `X-Forwarded-Proto`), and `TrustedHostMiddleware`.

Files: `main.py`.

---

### 4. 🔴 High — No CSRF protection on POST/PUT/DELETE

All state-changing endpoints (`/login`, `/signup`, `/upload/statement`, `/expense/{id}/category`, `/expense/{id}`) rely solely on the session cookie. With `same_site` unset and no CSRF token, a malicious site can perform cross-origin form posts and trigger uploads or deletes.

**Recommendation**
- Set `same_site="strict"` (or at least `"lax"`) on the session cookie.
- Add a CSRF token (e.g., `starlette-csrf` or a small custom dependency) checked on all non-GET endpoints. HTMX integrates well via `hx-headers` or a meta-tag pattern.

Files: `main.py`, all routers.

---

### 5. 🟠 Medium — N+1 queries and synchronous Supabase calls in async handlers

`upload_statement` runs **two synchronous Supabase HTTP requests per transaction** (existence check + insert) inside an `async def`, blocking the event loop:

```python
for txn in transactions:
    existing = db.table("expenses").select("id")...execute()
    prior_q = db.table("expenses").select("category_id")...execute()
    db.table("expenses").insert({...}).execute()
```

A 500-row statement = ~1500 sequential network calls on the only event-loop thread.

**Recommendation**
- Fetch existing rows once per upload with a single query (`select(name, amount, date) where user_id=...`), build an in-memory dedup set, and then **bulk insert** new ones (`.insert([...])`).
- Compute prior-category mapping with a single grouped query (`select name, category_id where user_id=...`).
- Wrap the blocking client calls with `await run_in_threadpool(...)` or use the async Supabase client; alternatively make these endpoints `def` (not `async def`) so Starlette runs them in a threadpool.
- Add a unique index `(user_id, name, amount, date)` and rely on `ON CONFLICT DO NOTHING` to make dedup race-free.

Files: `app/expenses.py::upload_statement`, new migration.

---

### 6. 🟠 Medium — Unbounded upload size & no MIME validation

`upload_statement` calls `await file.read()` without a size cap and only checks the `.csv` extension on the filename. A 2 GB upload will OOM the worker; a `.csv` with mismatched content is parsed regardless.

**Recommendation**
- Configure a hard limit at the proxy (Render) and explicitly check `file.size` / read in chunks with a max byte count.
- Validate the declared `content-type` and reject non-text payloads.
- Consider scanning for the BOM/header signature before full parse.

Files: `app/expenses.py`.

---

### 7. 🟠 Medium — `categories` table is global, not per-user

`categories` is shared across all users (`migrations/001`, `003`), and dashboards/category lookups join by name without `user_id`. Adding custom categories per user later will require a schema change; today, the category list leaks across tenants if you ever add more than one real user.

**Recommendation**
- Either commit to single-tenant (document it) or add `user_id NULL` (NULL = system defaults) and update queries + RLS.

Files: `migrations/001`, `003`, `app/dashboard.py`, `app/expenses.py`.

---

### 8. 🟠 Medium — `expenses.date` stored as TEXT

`migrations/001_initial_schema.sql`:

```sql
date TEXT NOT NULL,  -- stored as YYYY-MM-DD string
```

This works for sort/filter because the format is lexicographic, but it defeats indexing semantics, breaks if any non-ISO data slips in, and complicates future timezone handling.

**Recommendation**
- Migrate to `DATE`. Add a check constraint or trigger if you want to preserve format guarantees.

Files: `migrations/001_initial_schema.sql`.

---

### 9. 🟡 Low/Medium — Singleton clients hold connections forever; module-import-time env reads

`app/database.py` reads `os.environ[...]` at import (will crash on import if any var is missing, but only after `load_dotenv()` runs in two different modules — fragile). The singletons can also outlive token lifetimes once you move to per-user JWT auth.

**Recommendation**
- Centralize env loading and validation (pydantic-settings).
- Build clients lazily per request when user-scoped, keep service-role client as singleton only for admin paths.

Files: `app/database.py`, `main.py`.

---

### 10. 🟡 Low — Search/filter logic split between client and server

`filterBySearch()` filters DOM nodes by `name` only, while the server-side category filter ignores the search input. Result: when a category filter is active and you re-search, you only see the already-rendered subset. Also, `excludedIds` is `localStorage`-only — invisible to the server and lost across browsers.

**Recommendation**
- Move filtering server-side (single endpoint accepting `category` and `q`); keep HTMX URL-based for shareable state.
- Persist exclusions on the row (`excluded boolean`).

Files: `templates/dashboard.html`, `app/dashboard.py`.

---

### 11. 🟡 Low — Logging, error visibility, and observability

- No logging of failed login attempts, upload errors, or Supabase exceptions (`update_category` silently ignores DB errors).
- Bare exception handling in `auth.py` swallows the original error message.
- No request ID, no structured logs.

**Recommendation**
- Add `logging` config, log security-relevant events (login success/failure, signup, upload counts) with user id.
- Surface a generic 500 page; log details server-side.

Files: `app/auth.py`, `app/expenses.py`, `main.py`.

---

### 12. 🟡 Low — Missing security headers, CORS posture undefined

No `Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy`, etc. CDN-loaded HTMX/Chart.js without SRI hashes.

**Recommendation**
- Add `secure-headers` middleware (e.g., `starlette` middleware setting CSP) and SRI for the two CDN scripts (`htmx.org@2.0.4`, `chart.js@4`). Pin exact versions.

Files: `templates/dashboard.html`, `main.py`.

---

### 13. 🟡 Low — No tests, no CI, no linter config

The CSV parser in particular has rich, bank-specific heuristics that would benefit from unit tests (UTF-16 ČS, CP1250 KB, split debit/credit, NBSP thousands, Fio Poznámka tier, etc.). No `ruff`/`mypy`/`pytest` configuration in evidence.

**Recommendation**
- Add `pytest` with fixture CSVs per bank.
- Add `ruff` + `mypy --strict` for `app/` and a minimal GitHub Actions workflow.

---

### 14. 🟡 Minor

- `app/dashboard.py::expense_list_partial` filters rows post-query (`if e.get("categories")`) because PostgREST nested filter doesn't restrict the parent — clearer to use an inner join or filter via `category_id` after resolving the name once.
- `templates/dashboard.html` uses inline `onclick="..."` handlers everywhere — incompatible with a strict CSP. Migrate to event listeners.
- `applyExcludedState` keys off DOM IDs that can collide if `expense_id` reuses across users (will once you fix #1 properly).
- `confirmDelete` mutates `allExpenses` optimistically but doesn't roll back on failure.
- `migrations/006_expenses_user_id.sql` has a literal `<YOUR_USER_UUID>` placeholder — easy to run accidentally; consider parameterizing or splitting backfill from schema change.
- `Python 3.9+` in README, but `app/expenses.py` uses `int | None` PEP 604 syntax (requires 3.10+). Update README or `requires-python`.

---

## Next-Steps Checklist (pragmatic order)

**Security hardening (do first)**
- [ ] Make `SESSION_SECRET` required; configure `SessionMiddleware(https_only=True, same_site="lax", max_age=...)`.
- [ ] Add CSRF protection on all non-GET endpoints.
- [ ] Replace f-string HTML responses in `expenses.py` with Jinja templates; validate `category` against the DB.
- [ ] Add baseline security headers + SRI for CDN scripts; pin versions.

**Authorization model (do next)**
- [ ] Persist Supabase access/refresh tokens in session; build per-request clients with the user's JWT for `expenses` reads/writes.
- [ ] Add RLS policies on `expenses` (`auth.uid() = user_id`) and `categories` (read for `authenticated`).
- [ ] Restrict the service-role client to admin/maintenance paths only.

**Reliability / performance**
- [ ] Refactor `upload_statement` to bulk-fetch existing rows and bulk-insert new ones; add `UNIQUE(user_id, name, amount, date)` and use `ON CONFLICT DO NOTHING`.
- [ ] Run blocking Supabase calls off the event loop (sync route or `run_in_threadpool`).
- [ ] Cap upload size; validate MIME.

**Data model**
- [ ] Migrate `expenses.date` to `DATE`.
- [ ] Decide single-tenant vs multi-tenant for `categories`; adjust schema and queries.

**Quality engineering**
- [ ] Add `pytest` with bank-specific CSV fixtures for `parse_csv_statement`.
- [ ] Add `ruff` + `mypy` and a GitHub Actions CI pipeline (lint, type, test).
- [ ] Add structured logging and basic audit events.

**Polish**
- [ ] Move inline event handlers in `dashboard.html` to event listeners (CSP-friendly).
- [ ] Unify search + category filter server-side; persist chart exclusions in DB.
- [ ] Update README Python version (3.10+).