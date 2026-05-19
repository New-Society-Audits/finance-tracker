# Repository Audit — `finance-tracker`

## Executive Summary

`finance-tracker` is a small, well-structured FastAPI + HTMX + Supabase application for importing and categorizing bank statements. The codebase is generally clean, readable, and pragmatic, with thoughtful CSV parsing logic for Czech bank exports. However, several findings carry meaningful security and correctness impact, most notably the use of the Supabase **service-role key for all data operations** combined with **insufficient session-cookie hardening** and **no CSRF protection on state-changing endpoints**. There is also no automated test suite, no rate-limiting on auth, and no upload size/MIME validation, which are important gaps to close before broader use.

The recommendations below are ordered by severity.

---

## Findings

### 🔴 1. Service-role key used for all data operations (RLS bypassed by design)

`app/database.py` exposes a service-role client (`get_client()`) which **bypasses Supabase RLS**. All read/write paths in `app/dashboard.py` and `app/expenses.py` use this client. Authorization is enforced only by manually filtering on `user_id` in application code (e.g. `app/expenses.py`’s `.eq("user_id", user_id)`).

**Risks:**
- A single missing `.eq("user_id", ...)` filter becomes a cross-tenant data leak. There’s no defense-in-depth.
- The service-role key is now distributed to a long-running web process. Any RCE / SSRF / dependency compromise yields unrestricted DB access.
- `migrations/007_enable_rls.sql` enables RLS but **defines no policies** — the comment correctly notes this is only protecting against anon-key access, not application bugs.

**Recommendation:**
- Replace the service-role client for user-data ops with a per-request client that uses the user’s Supabase JWT (`postgrest.auth(token)`), and define proper RLS policies like `user_id = auth.uid()` on `expenses`.
- Reserve service-role usage for administrative tasks (migrations, scheduled jobs), and keep that client out of the request path.

---

### 🔴 2. Session middleware lacks `https_only`, `same_site`, and proper secret enforcement

`main.py`:
```python
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-this-before-deploying")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
```

Issues:
- Falls back to a hardcoded default secret if `SESSION_SECRET` is unset — production may run with a known signing key, allowing **session forgery**.
- `SessionMiddleware` defaults to `same_site="lax"` and `https_only=False`. The cookie is not marked `Secure`, exposing it on any accidental HTTP request.
- No explicit `max_age`, so sessions effectively persist as long as the browser keeps the cookie.

**Recommendation (`main.py`):**
```python
SECRET_KEY = os.environ["SESSION_SECRET"]  # fail fast if missing
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    https_only=True,
    same_site="lax",
    max_age=60 * 60 * 24 * 14,  # 14 days
)
```

---

### 🔴 3. No CSRF protection on state-changing endpoints

The app uses cookie-based session auth and exposes mutating routes (`POST /upload/statement`, `PUT /expense/{id}/category`, `DELETE /expense/{id}`, `POST /login`, `POST /signup`). There is no CSRF token, no `Origin`/`Referer` check, and `same_site` is not configured (defaults to `lax`, which **does not** protect `POST` forms cross-site against the user’s browser auto-attaching the cookie if a tricky form-encoded request is crafted; `PUT`/`DELETE` are largely browser-blocked, but `POST` is not).

**Recommendation:**
- Add a CSRF middleware (e.g. `starlette-csrf`, or a small custom double-submit token) and include the token in HTMX requests via `hx-headers`.
- At minimum, verify `Origin`/`Referer` matches the app host on all mutating endpoints.

---

### 🟠 4. `GET /logout` is dangerous (logout via image/link)

`app/auth.py`:
```python
@router.get("/logout")
def logout(request: Request):
    request.session.clear()
```

Any third-party page that loads `<img src="https://yourapp/logout">` can sign your users out (mild, but a real CSRF vector for state changes via GET).

**Recommendation:** convert `/logout` to `POST` (with a form button or HTMX `hx-post`), or require a CSRF token.

---

### 🟠 5. File upload: no size limit, no MIME validation

`app/expenses.py::upload_statement`:
```python
contents = await file.read()
```

- The entire upload is read into memory unconditionally — a large file is a trivial DoS.
- Only the filename suffix (`.csv`) is checked; the actual `content-type` and content are not validated.
- The CSV parser then loads multiple full copies of the text into memory (`text.splitlines()`, joined again, then `DictReader` over a `StringIO`).

**Recommendation:**
- Enforce a max size (e.g. 2–5 MB) and stream/chunk-read via `await file.read(MAX+1)` and reject if exceeded.
- Validate `file.content_type in {"text/csv", "application/vnd.ms-excel", "text/plain"}`.
- Consider streaming the CSV through the parser rather than buffering twice.

---

### 🟠 6. N+1 inserts and per-row dedup queries during statement import

`app/expenses.py::upload_statement` loops over each transaction and does:
1. A `SELECT` to dedup,
2. A `SELECT` for prior categories,
3. An `INSERT`.

For a year of transactions this is hundreds of round trips and meaningfully slow. It also has no transactional boundary — a failure mid-loop leaves a partial import.

**Recommendation:**
- Fetch all existing `(name, amount, date)` triples for the user once, dedup in memory.
- Fetch prior `name -> category_id` mapping once.
- Bulk-insert in a single call (`db.table("expenses").insert([...]).execute()`).

---

### 🟠 7. Dedup key is fragile

Dedup keys on `name + amount + date`. Two legitimate identical-day, identical-amount, identical-merchant charges (e.g. two coffees at the same café) will be silently dropped on re-import.

**Recommendation:** include a stable bank-provided reference (variable symbol, transaction ID, or hash of the whole row) and persist it in a `bank_ref` column with a uniqueness constraint per user.

---

### 🟠 8. `date` is stored as `TEXT`

`migrations/001_initial_schema.sql`:
```sql
date TEXT NOT NULL,  -- stored as YYYY-MM-DD string
```

This prevents proper date indexing/range filtering at the DB layer and pushes correctness onto string ordering (which happens to work for `YYYY-MM-DD` but is brittle).

**Recommendation:** migrate the column to `DATE`. Also add indexes:
```sql
CREATE INDEX expenses_user_date_idx ON expenses(user_id, date DESC);
CREATE INDEX expenses_user_name_amount_date_idx ON expenses(user_id, name, amount, date);
```

---

### 🟠 9. Migration 006 is non-idempotent and contains a placeholder

`migrations/006_expenses_user_id.sql` requires manual substitution of `<YOUR_USER_UUID>` and assumes a single user. Running it twice fails; running it on a multi-user instance is incorrect.

**Recommendation:**
- Use `ADD COLUMN IF NOT EXISTS`.
- Either skip the backfill (and clean orphaned rows separately) or parameterize via a SQL variable / migration tool.
- Adopt a migration runner (`alembic`, `supabase migration`) so migrations are tracked and re-runnable.

---

### 🟡 10. Filter/search rendered as HTML attributes without escaping in JS strings

`templates/dashboard.html` builds HTML by string-concatenation in `updateCategoryDropdown()`:
```js
'<a ... data-cat="' + c + '" onclick="selectChartFilter(this, \'' + c + '\')">' + c + '</a>'
```

If a category name ever contains `'`, `"`, `<`, or `\`, it will break the markup or execute arbitrary JS. Currently categories are admin-seeded so risk is low, but the pattern is dangerous and should be removed before any user-provided category names are supported.

Similarly, `category_name` is interpolated into HTML in `update_category` (`app/expenses.py`) without escaping:
```python
return HTMLResponse(f'<span ...>{tag_content}</span>')
```
Today this is gated by a lookup against existing categories, but the function itself does not enforce that — a category that didn’t resolve to an ID still gets echoed back into HTML.

**Recommendation:** use `html.escape()` server-side and `textContent`/DOM APIs client-side; never concatenate untrusted strings into HTML.

---

### 🟡 11. No rate-limiting on auth endpoints

`POST /login` and `POST /signup` are unprotected against brute force / account enumeration. The error message `"Invalid email or password."` is fine, but signup distinguishes existing accounts and `e.message` from Supabase is forwarded verbatim — useful for an attacker.

**Recommendation:** add a rate-limiter (e.g. `slowapi`) on `/login`, `/signup`, and consider generic error messaging on signup.

---

### 🟡 12. `print`/error handling is silent

The codebase swallows nothing explicitly but also surfaces nothing — Supabase exceptions other than `AuthApiError` will 500 with no logging. There is no logging setup at all.

**Recommendation:** add structured logging (`logging.getLogger(__name__)`) and a global exception handler that logs `exc_info=True`.

---

### 🟡 13. No tests

There is no `tests/` directory. Bank-statement parsing (`parse_csv_statement`) is the most complex code in the repo and an obvious candidate for unit tests against representative fixtures (ČS UTF-16, KB CP1250, Fio UTF-8, etc.).

**Recommendation:** add `pytest` with fixtures per bank, plus a few HTTP-level tests using `TestClient` and a mocked Supabase client.

---

### 🟢 14. Minor

- `app/dashboard.py` issues two queries per dashboard render (`expenses` and `categories`). Cache `categories` in-process — they change rarely.
- `static/dashboard.css`: `.filter-dropdown` declares `max-height` and `overflow-y` twice.
- `templates/dashboard.html` injects all expenses into the page as JSON (`{{ expenses | tojson }}`), so the dashboard scales linearly with history. Plan pagination or server-side aggregation for the charts as data grows.
- `_require_auth` in `app/expenses.py` returns `HTMLResponse("Unauthorized", status_code=401)` but in `app/dashboard.py` redirects — inconsistent. Pick one (a 401 + HTMX `HX-Redirect` header is a good pattern).
- Avoid `from __future__ import annotations` in `app/database.py` since it’s not strictly necessary on Python 3.9+ and isn’t consistently applied elsewhere.

---

## Pragmatic Next-Steps Checklist

**Security (do first)**
- [ ] Require `SESSION_SECRET` (no fallback); set `https_only=True`, `same_site="lax"`, `max_age` on `SessionMiddleware`.
- [ ] Add CSRF protection (token + HTMX `hx-headers`) for all `POST/PUT/DELETE`.
- [ ] Change `/logout` to `POST`.
- [ ] Add rate-limiting on `/login` and `/signup`; generic signup error messages.
- [ ] Enforce upload size cap and validate `content_type` on `/upload/statement`.
- [ ] Plan migration from service-role data client to user-JWT client + RLS policies on `expenses`.

**Correctness & data model**
- [ ] Migrate `expenses.date` from `TEXT` to `DATE`; add `(user_id, date DESC)` index.
- [ ] Add a `bank_ref` (or row-hash) column with `UNIQUE(user_id, bank_ref)` for robust dedup.
- [ ] Replace per-row insert loop with bulk insert; pre-fetch dedup set.
- [ ] Adopt a migration tool (Alembic or Supabase CLI migrations) and make migration 006 idempotent.

**Robustness**
- [ ] Add `logging` config and a global exception handler.
- [ ] Add `pytest` and unit tests for `parse_csv_statement` with per-bank CSV fixtures.
- [ ] Add a smoke test for each route with a mocked Supabase client.

**Cleanups**
- [ ] HTML-escape any user/dynamic strings in `update_category` and JS-built dropdowns.
- [ ] Unify `_require_auth` behavior (redirect vs. 401 + `HX-Redirect`).
- [ ] Cache `categories` per-process; remove duplicate CSS rules in `.filter-dropdown`.
- [ ] Plan dashboard pagination / server-side chart aggregation before data grows.