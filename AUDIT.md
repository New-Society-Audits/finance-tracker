# Repository Audit: finance-tracker

## Executive Summary

`finance-tracker` is a small but well-organized FastAPI + HTMX + Supabase application for importing and categorizing bank statements. The code is readable, well-commented, and demonstrates good taste in separation of concerns (auth / dashboard / expenses / database). However, there are several **critical security issues** that should block production use by anyone other than the original author, plus a handful of correctness, performance, and architectural concerns that would matter as the app grows beyond a single user.

The most pressing problems are:

1. **CSRF protection is entirely absent** despite cookie-based sessions and state-changing GET/PUT/DELETE routes.
2. **All data access uses the Supabase service-role key**, bypassing RLS — meaning the app, not the database, is the only thing standing between any logged-in user and every other user's data.
3. **Session cookies are not marked Secure/HttpOnly/SameSite explicitly**, and a hardcoded default `SESSION_SECRET` exists.
4. **The CSV import does N+1 SELECT/INSERTs per transaction** (no batching), which will hurt UX and cost on large statements.

None of these are hard to fix; the codebase is small enough that a focused security pass would pay for itself quickly.

---

## Findings (Ordered by Severity)

### 🔴 Critical

#### 1. No CSRF protection on state-changing routes
**Files:** `app/auth.py`, `app/expenses.py`, `app/dashboard.py`, `main.py`

The app uses cookie-based sessions (`SessionMiddleware`) but has no CSRF token verification. Routes like `POST /upload/statement`, `PUT /expense/{id}/category`, `DELETE /expense/{id}`, and `GET /logout` (state-changing via GET!) can be triggered cross-origin by any malicious page the user visits while logged in.

**Recommendation:**
- Add a CSRF middleware (e.g. `starlette-csrf` or `fastapi-csrf-protect`).
- Move `/logout` from `GET` to `POST` (`app/auth.py:67`).
- Set the session cookie `SameSite=Lax` (default in Starlette but verify) or `Strict`.

#### 2. Service-role key used for all data access — RLS is a no-op
**Files:** `app/database.py:27-33`, `migrations/007_enable_rls.sql`

`get_client()` returns a service-role client, which **bypasses Row-Level Security entirely**. Migration 007 enables RLS but adds no policies, and the app never uses the user's JWT. Authorization is enforced only by `eq("user_id", user_id)` clauses scattered throughout `app/expenses.py` and `app/dashboard.py`. A single missing `.eq("user_id", …)` anywhere = full cross-tenant data leak.

For example, `update_category` (`app/expenses.py:281-287`) correctly scopes to `user_id`, but the category lookup at `app/expenses.py:277` does not validate that the user can use that category (low risk today since categories are shared, but the pattern is fragile).

**Recommendation:**
- Use the **anon key + the user's access token** for data operations, and write proper RLS policies (e.g. `USING (user_id = auth.uid())`). Store the Supabase session in the cookie, not just `user_id`.
- Short-term mitigation: write a `db_for_user(user_id)` helper that auto-applies `.eq("user_id", user_id)` to every query, so individual handlers can't forget.

#### 3. Hardcoded fallback `SESSION_SECRET`
**File:** `main.py:23`

```python
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-this-before-deploying")
```

If `SESSION_SECRET` is ever missing in any environment, sessions become trivially forgeable. There is no startup check.

**Recommendation:** Fail loudly:
```python
SECRET_KEY = os.environ["SESSION_SECRET"]
```
…matching the pattern already used for Supabase keys in `app/database.py:20-22`.

---

### 🟠 High

#### 4. `GET /logout` is state-changing
**File:** `app/auth.py:67`

A `<img src="/logout">` on any site logs the user out (annoying, not catastrophic, but a real CSRF). Change to `POST` with a CSRF token.

#### 5. Category-filter query trusts client-supplied string in `.eq()`
**File:** `app/dashboard.py:97-101`

```python
if category:
    query = query.eq("categories.name", category)
```

This is safe from SQL injection (Supabase parameterizes), but there is **no validation that `category` is a real category name**, and the filtering logic at line 109 (`expenses = [e for e in expenses if e.get("categories")]`) is a workaround for the fact that Supabase returns all rows when joining and filtering on a related table — the filter is applied *post-fetch in Python*, which means **pagination cannot be added later without re-architecture**, and large datasets will pull every expense into memory.

**Recommendation:** Resolve `category_name → category_id` first, then filter on `category_id` directly on the `expenses` table.

#### 6. N+1 queries during statement import
**File:** `app/expenses.py:328-368`

For each transaction parsed from a CSV (potentially hundreds):
- 1× `SELECT` for dedup
- 1× `SELECT` for prior-category lookup
- 1× `INSERT`

That's 3 round-trips × N transactions. A 500-line statement = 1500 round-trips, taking many seconds and blocking the request worker.

**Recommendation:**
- Fetch all existing `(name, amount, date)` tuples for the user in **one query** and dedup in Python.
- Fetch all prior `(name, sign, category_id)` mappings in **one query**.
- Use a **single bulk `insert([...])`** call at the end.

#### 7. Cookie security flags not set
**File:** `main.py:32`

`SessionMiddleware` defaults are OK on HTTPS but should be explicit. There's no `https_only=True` and no SameSite specified.

**Recommendation:**
```python
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    https_only=True,            # in production
    same_site="lax",
    max_age=60 * 60 * 24 * 14,  # 14 days, not "forever"
)
```

---

### 🟡 Medium

#### 8. `date` stored as `TEXT` instead of `DATE`
**Files:** `migrations/001_initial_schema.sql:16`, `app/expenses.py` throughout

Dates are stored as `TEXT` (`YYYY-MM-DD`). This works only because the format is lexicographically sortable, but loses type safety, prevents range arithmetic, and makes future features (e.g. "expenses this month") awkward. Migrate to `DATE`.

#### 9. `amount` stored as `DOUBLE PRECISION`
**File:** `migrations/001_initial_schema.sql:15`

Money in floats is a known antipattern (e.g. `0.1 + 0.2 != 0.3`). Use `NUMERIC(12, 2)`.

#### 10. Deduplication on `(name, amount, date)` is brittle
**File:** `app/expenses.py:333-344`

Two legitimate transactions on the same day to the same merchant for the same amount are silently dropped. Most banks expose a per-transaction reference/ID — capture it when present and dedup on that.

#### 11. Inline JS in templates is large and unstructured
**File:** `templates/dashboard.html:163-450+`

~300 lines of JS inline in the template, including state management, chart rendering, and event handlers. Hard to test, hard to lint, and grows fast. Extract to `static/dashboard.js`.

#### 12. `confirmDelete` has a UX race condition
**File:** `templates/dashboard.html:206-225`

The 3-second "confirm window" resets but `btn.dataset.confirm` is read synchronously without protection — and the optimistic chart update at line 211 happens *before* the server confirms deletion. If the DELETE fails the chart is out of sync. Listen to `htmx:responseError` and revert.

#### 13. Bare `except` / broad exception handling on auth
**File:** `app/auth.py:27, 50`

Only `AuthApiError` is caught; everything else (network failure, JSON decode, Supabase outage) bubbles up as a 500. Wrap in a broader try/except that returns a friendly "service unavailable" message.

#### 14. No rate limiting on login/signup
**File:** `app/auth.py`

Brute-force resistance is delegated entirely to Supabase. Add per-IP throttling (e.g. `slowapi`) for `/login` and `/signup`.

#### 15. CSV upload has no size limit
**File:** `app/expenses.py:312-313`

`await file.read()` loads the whole file into memory. A malicious user could upload a multi-GB "CSV" to OOM the worker.

**Recommendation:** Enforce a max size (e.g. 2 MB) via `Content-Length` check before reading, and stream parsing if larger files are needed.

---

### 🟢 Low

#### 16. Module-level env var access prevents testability
**File:** `app/database.py:20-22`

`os.environ["SUPABASE_URL"]` at import time means importing the module without env vars raises `KeyError` — impossible to unit-test handlers in isolation. Move into a settings object (e.g. `pydantic-settings`) loaded lazily.

#### 17. Singletons are not thread/process safe in async contexts
**File:** `app/database.py:25-26`

Module-level mutable singletons are fine here (FastAPI under uvicorn is single-process per worker, and creation is idempotent), but the pattern doesn't generalize. Use FastAPI's `Depends()`.

#### 18. No tests
There are no unit or integration tests visible in the tree. The CSV parser in `app/expenses.py` is the most logic-dense and bug-prone code in the project and has extensive edge cases (encodings, delimiters, name-column tiers, sign conventions) — it badly wants a pytest suite.

#### 19. `migrations/006_expenses_user_id.sql` requires hand-editing
The placeholder `<YOUR_USER_UUID>` is fine for a personal project, but won't run automatically anywhere. Either remove the backfill (if the table is empty for new deployments) or document the procedure more strongly.

#### 20. `migrations/` has no migration runner
Migrations are applied by hand via the Supabase SQL editor. Consider Supabase CLI migrations or a tool like `yoyo-migrations` for repeatable deploys.

#### 21. Type hints are partial
`app/dashboard.py` and `app/expenses.py` mix `dict`, untyped `list`, and `dict[str, ...]`. Adding a `TypedDict` for `expense`/`category` would catch issues like the `e["category"]` mutation in `_get_expenses_and_categories` (`app/dashboard.py:49-50`).

#### 22. `expenses.receipt_path` is unused
**File:** `migrations/001_initial_schema.sql:18`

Either implement receipt upload or drop the column. Schema sprawl encourages staleness.

#### 23. Chart click → highlight relies on a 4000-iteration safety cap
**File:** `templates/dashboard.html:344-348`

The comment is honest about this being a guard against a runaway loop, but the underlying issue is that `fillGaps` uses string comparison on date keys for the termination condition. Reworking around `Date` objects directly would remove the need for a cap.

---

## Recommendations: Concrete Next Steps

### Must do before any multi-user deployment
- [ ] Replace the service-role client with the user's JWT + write proper RLS policies (`app/database.py`, `migrations/`).
- [ ] Add CSRF middleware and migrate `/logout` to POST (`app/auth.py`, `main.py`).
- [ ] Remove the `SESSION_SECRET` default — fail at startup if absent (`main.py:23`).
- [ ] Set explicit `https_only`, `same_site`, and `max_age` on `SessionMiddleware` (`main.py:32`).
- [ ] Enforce upload size limit on `POST /upload/statement` (`app/expenses.py`).

### Should do soon
- [ ] Batch the statement-import queries into 1 SELECT + 1 INSERT (`app/expenses.py:328-368`).
- [ ] Change schema: `expenses.date → DATE`, `expenses.amount → NUMERIC(12,2)`.
- [ ] Add rate limiting to `/login` and `/signup`.
- [ ] Extract dashboard JS to `static/dashboard.js`.
- [ ] Add a pytest suite for `parse_csv_statement` with fixture CSVs for each Czech bank format.

### Nice to have
- [ ] Move Supabase config to `pydantic-settings`.
- [ ] Add `Depends()` injection for the DB client instead of module-level singletons.
- [ ] Adopt a migration runner (Supabase CLI).
- [ ] Capture a bank-supplied transaction reference for robust dedup.
- [ ] Drop unused `receipt_path` column, or implement the feature.
- [ ] Add `mypy` / `ruff` to CI.

---

## Closing Note

The codebase is unusually pleasant to read for a personal project — comments explain *why*, not *what*, and the CSV parser in particular reflects real-world knowledge of Czech bank export quirks. The architectural bones are good. The headline risks are concentrated in the security model (RLS bypass + missing CSRF), and fixing those would put this in solid shape.