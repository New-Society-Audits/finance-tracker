# Repository Audit: finance-tracker

## Executive Summary

`finance-tracker` is a small but well-structured FastAPI + HTMX personal finance app backed by Supabase. The codebase is clean, documented, and the CSV statement parser is impressively pragmatic about Czech bank quirks. However, several **security issues** materially weaken the multi-user model: the app uses the Supabase service-role key for all data operations (bypassing RLS), session secrets fall back to a hardcoded default, and there is no CSRF protection on state-changing routes. There are also reliability concerns (N+1 inserts during statement import, no tests, no input validation on uploads) and a few correctness bugs in the `expenses` table schema (date stored as `TEXT`, no unique constraint to enforce dedupe at the DB level).

Risk-adjusted, the highest priorities are: (1) rotate to per-request Supabase auth + real RLS policies, (2) harden session/secret handling, and (3) add a test suite covering the statement parser.

---

## Findings (ordered by severity)

### 🔴 1. Service-role key used for all data operations — RLS is effectively disabled

**File:** `app/database.py`, `app/expenses.py`, `app/dashboard.py`

`get_client()` returns a Supabase client built with `SUPABASE_SECRET_KEY` (service role). Every read/write filters by `user_id` *in Python*. Migration `007_enable_rls.sql` enables RLS but defines no policies, explicitly relying on the bypass.

**Risks:**
- A single missed `.eq("user_id", user_id)` filter (e.g. the `expenses.categories.name` join filter in `expense_list_partial` already has subtle behavior) leaks or mutates other users' data.
- The service-role key sits in app memory and env; any RCE/SSRF compromises every user's data globally.
- `update_category` looks up a category by **name only** with no `user_id` scope (categories are global today, but if categories ever become per-user, this silently breaks).

**Recommendation:**
- Switch to a per-request Postgres client using the user's JWT (set via `client.postgrest.auth(jwt)`), and write RLS policies like `auth.uid() = user_id` on `expenses`.
- Keep service-role usage to a narrow, explicit admin path (e.g., user-deletion cleanup), not the request-handling path.

---

### 🔴 2. Weak session secret default

**File:** `main.py:23`

```python
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-this-before-deploying")
```

If the env var is missing in any environment (staging, a forgotten Render preview, local dev exposed via tunnel), session cookies are signed with a publicly known string — trivial session forgery.

**Recommendation:** Fail hard on missing `SESSION_SECRET`:
```python
SECRET_KEY = os.environ["SESSION_SECRET"]
```
Also set `SessionMiddleware(..., https_only=True, same_site="lax")` and a sensible `max_age`.

---

### 🟠 3. No CSRF protection on state-changing routes

**Files:** `app/expenses.py`, `app/auth.py`

`POST /upload/statement`, `PUT /expense/{id}/category`, `DELETE /expense/{id}`, and `POST /login` are session-cookie authenticated with no CSRF token. A malicious site can trigger statement uploads or category changes for a logged-in user.

**Recommendation:** Add a CSRF middleware (e.g. `starlette-csrf` or `fastapi-csrf-protect`) and emit hidden tokens / `hx-headers` from templates. At minimum, set cookie `SameSite=Lax` (default) and verify `Origin`/`Referer` on POST/PUT/DELETE.

---

### 🟠 4. Statement upload has no size, type, or rate limits

**File:** `app/expenses.py:204-220`

`await file.read()` loads the entire upload into memory, file extension is the only validation (`.endswith(".csv")` — bypassable), and there's no per-user rate limit. A single user can OOM the dyno or spam imports.

**Recommendation:**
- Enforce a max body size (FastAPI/Starlette doesn't out-of-the-box; add a middleware or a reverse-proxy limit on Render).
- Validate `file.content_type` plus extension.
- Stream-parse instead of full-read for large CSVs.
- Add a basic rate limiter (e.g. `slowapi`).

---

### 🟠 5. N+1 queries during CSV import

**File:** `app/expenses.py:226-258`

For each transaction, the loop runs (a) a dedupe SELECT, (b) a "prior category" SELECT, and (c) an INSERT. A 500-row statement = ~1500 round-trips and a multi-second hang.

**Recommendation:**
- Pre-fetch existing `(name, amount, date)` tuples for the user once, dedupe in memory.
- Pre-fetch prior `(name, sign) -> category_id` once.
- Use a single bulk `insert(rows)` call.
- Add a DB-level uniqueness guard: `UNIQUE (user_id, name, amount, date)` to make dedupe correctness independent of app logic.

---

### 🟡 6. `expenses.date` stored as `TEXT`

**File:** `migrations/001_initial_schema.sql`

```sql
date TEXT NOT NULL,  -- stored as YYYY-MM-DD string
```

This works because dates are ISO-formatted but it precludes proper indexing for range queries, allows malformed values, and forces front-end defensive checks like the regex filter in `dashboard.html`:
```js
.filter(e => /^\d{4}-\d{2}-\d{2}$/.test(e.date))
```

**Recommendation:** Migrate to `DATE`. Add an index on `(user_id, date DESC)` for the common dashboard query.

---

### 🟡 7. Category filter uses a fragile join filter

**File:** `app/dashboard.py:88-100`

```python
query = query.eq("categories.name", category)
...
expenses = [e for e in expenses if e.get("categories")]
```

PostgREST's behavior on filtering across embedded resources is subtle (the comment acknowledges null rows still come back). This is correctness-by-postprocessing and easy to break.

**Recommendation:** Resolve `category_id` first via a single lookup, then filter with `.eq("category_id", id)`. Predictable, indexable, and avoids the post-filter.

---

### 🟡 8. No tests

The CSV parser in `app/expenses.py` has dozens of hand-tuned heuristics (encodings, delimiters, header detection, name tier fallback, mixed thousands/decimal separators). This is exactly the code most likely to regress silently.

**Recommendation:** Add `pytest` with fixtures for at least one real export per supported bank (anonymized). Cover:
- Encoding detection (`_decode_statement_bytes`)
- Delimiter detection
- Amount parsing (the EU/US separator branch, NBSP, currency symbols)
- Date format permutations
- Split debit/credit columns
- Tier-fallback for `_row_name` (Fio Poznámka case)

---

### 🟡 9. No `users` table reference but app trusts session-stored `user_id`

**File:** `app/dashboard.py`, `app/expenses.py`

If a Supabase user is deleted out-of-band but their session cookie is still valid, queries proceed against a non-existent `auth.users.id`. With service-role + no FK validation on read, this is silently a no-op rather than a 401.

**Recommendation:** Periodically validate the session against Supabase (e.g. token refresh on each request) or shorten session lifetime.

---

### 🟢 10. Minor / hygiene

- **`templates/dashboard.css` duplicated rule** — `static/dashboard.css:144-149` declares `max-height: 260px; overflow-y: auto;` twice in `.filter-dropdown`.
- **Unpinned CDN JS** — `dashboard.html` loads `htmx.org@2.0.4` and `chart.js@4` from unpinned/major-floated CDNs without SRI hashes. Pin exact versions and add `integrity=`.
- **`load_dotenv()` called twice** — in `main.py` and `app/database.py`. Harmless but confusing; centralize.
- **Inline `<script>` in `dashboard.html`** is large and hard to test; consider moving to `static/dashboard.js`.
- **`_get_expenses_and_categories` and `expense_list_partial`** duplicate the category-flattening + tier split. Extract a helper.
- **`upload/statement` returns embedded `<script>` that calls `htmx.ajax`** — works, but coupling response HTML to client-side behavior makes the endpoint hard to reuse. Prefer `HX-Trigger` response header to fire a custom event the page listens for.
- **No logging.** Failures in the parser, Supabase errors, and dedupe skips are silent. Add structured logging (`logging` module) early — you'll need it the first time a user reports "my upload didn't work".
- **`migrations/006_expenses_user_id.sql`** requires manual UUID substitution. Document this more loudly or provide a templated runner.
- **`migrations/` are run by hand** with no record of which have been applied. Consider `alembic` or at least a `schema_migrations` table.

---

## Pragmatic Next-Steps Checklist

**Must-fix before any wider rollout:**
- [ ] Replace the service-role-key default path with per-request user JWT + write real RLS policies on `expenses` and (if/when scoped) `categories`.
- [ ] `SESSION_SECRET` must be required (no fallback). Configure cookie flags (`https_only`, `same_site`, `max_age`).
- [ ] Add CSRF protection on POST/PUT/DELETE routes.
- [ ] Cap upload size + verify `content_type` in `upload_statement`.

**Reliability & correctness:**
- [ ] Bulk-insert on CSV import; pre-fetch existing rows for dedupe.
- [ ] Add `UNIQUE (user_id, name, amount, date)` constraint and an index on `(user_id, date DESC)`.
- [ ] Migrate `expenses.date` from `TEXT` to `DATE`.
- [ ] Replace `categories.name` join filter with id-based filter in `expense_list_partial`.

**Engineering hygiene:**
- [ ] Set up `pytest` and write fixtures for the statement parser; commit at least 5 anonymized real-bank CSVs.
- [ ] Add `ruff` + `mypy` (or `pyright`) to CI.
- [ ] Pin CDN script versions with SRI; or self-host `htmx` and `chart.js` from `static/`.
- [ ] Introduce a migration tool (Alembic or a tracking table).
- [ ] Add `logging.getLogger(__name__)` in `app/expenses.py` for parse failures and dedupe stats.

**Nice to have:**
- [ ] Extract the inline `<script>` in `dashboard.html` to a static file.
- [ ] Replace embedded `<script>` in `upload_statement` response with `HX-Trigger`.
- [ ] Deduplicate `_get_expenses_and_categories` vs `expense_list_partial`.