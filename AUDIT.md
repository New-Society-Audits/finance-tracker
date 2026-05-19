# Repository Audit: finance-tracker

## Executive Summary

`finance-tracker` is a small FastAPI + HTMX personal finance app backed by Supabase. The code is readable, well-commented, and the architecture (routers + singleton DB clients + Jinja partials) is appropriate for its scope. However, the app has several **security weaknesses that are notable now that it supports multiple authenticated users**: a default session secret fallback, exclusive use of the Supabase service-role key (bypassing RLS), missing CSRF protection on state-changing HTMX endpoints, an unrendered server-controlled string injected into HTML, and an unbounded file upload. There are also a few correctness/perf issues worth addressing (N+1 inserts on import, brittle dedup, missing DB indexes).

None of the issues are catastrophic in isolation, but together they meaningfully reduce the safety margin of a deployed multi-user app.

---

## Findings (by severity)

### 🔴 High

#### 1. Default `SESSION_SECRET` fallback allows session forgery
`main.py` falls back to a hardcoded literal if the env var is missing:
```python
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-this-before-deploying")
```
If `SESSION_SECRET` is ever unset in production (misconfiguration, new environment, rollback), session cookies become forgeable by anyone — they can impersonate any user by signing a cookie with the known secret.

**Recommendation:** Fail loudly. Replace with:
```python
SECRET_KEY = os.environ["SESSION_SECRET"]
```
…and document it as required in `README.md` / Render setup.

---

#### 2. All data queries use the service-role key, bypassing RLS
`app/database.py` uses `SUPABASE_SECRET_KEY` (service role) for every data operation. Migration `007_enable_rls.sql` enables RLS but defines no policies, so the only thing standing between a user and another user's expenses is the application code's `.eq("user_id", user_id)` filters.

This is currently correct, but it is a single mistake away from data leakage. For example, on `PUT /expense/{expense_id}/category` the update is correctly scoped, but a future endpoint that forgets to filter would silently expose all rows.

**Recommendation:**
- Long-term: switch to per-user JWT-authenticated Supabase clients and define real RLS policies (`USING (auth.uid() = user_id)`). This makes the database itself enforce isolation.
- Short-term: add an integration test that confirms user A cannot read/modify user B's expenses.

---

#### 3. No CSRF protection on state-changing endpoints
Authentication is session-cookie based, but `POST /upload/statement`, `PUT /expense/{id}/category`, `DELETE /expense/{id}`, `POST /login`, `POST /signup` have no CSRF token. A malicious site visited by a logged-in user could trigger uploads, category edits, or deletions cross-origin.

`SessionMiddleware` defaults to `same_site="lax"`, which mitigates simple cases but not all (e.g., top-level form POSTs, `multipart/form-data` from forms).

**Recommendation:**
- Add `same_site="strict"` and `https_only=True` to `SessionMiddleware` (`main.py`).
- Add a CSRF token (e.g., `starlette-csrf` or `fastapi-csrf-protect`) and include it in every HTMX request via `hx-headers` on `<body>`.

---

#### 4. Unrendered server data injected into HTML/JS via `tojson` without strict escaping
`templates/dashboard.html`:
```js
const allExpenses = {{ expenses | tojson }}.filter(...)
```
`expense.name` comes from user-uploaded CSVs (untrusted). Jinja's `tojson` is generally safe for embedding in `<script>` blocks, but the same data is also rendered in the partial:
```html
<span class="expense-name">{{ expense.name }}</span>
```
That side is correctly auto-escaped, but `confirmDelete`/`toggleExclude` template into JS with literal IDs (`onclick="toggleExclude({{ expense.id }})"`). IDs are integers from the DB, so safe today — but the pattern is fragile.

Additionally, in `app/expenses.py::update_category`, `category_name` is interpolated directly into HTML:
```python
tag_content = category_name if category_name else ""
return HTMLResponse(f'<span ...>{tag_content}</span>')
```
`category_name` comes from a form post and is not sanitized before HTML insertion. It is constrained by the dropdown options client-side, but the endpoint trusts the client. A crafted POST with `category=<img onerror=...>` would render unescaped HTML into the user's own page — limited blast radius (self-XSS) but still worth fixing.

**Recommendation:**
- In `update_category`, validate `category_name` against the categories table before responding, and use `html.escape()` (or, better, render via a Jinja template fragment).
- Add a `Content-Security-Policy` header forbidding inline event handlers / external scripts beyond what you actually need.

---

#### 5. Unbounded file upload + full read into memory
`app/expenses.py::upload_statement`:
```python
contents = await file.read()
```
There's no size limit, no MIME validation beyond extension, and the entire file is loaded into RAM and decoded multiple times. A 500 MB upload OOMs the dyno; a slow upload ties up workers.

**Recommendation:**
- Enforce a max size (e.g., 5 MB) by checking `file.size` (Starlette ≥0.36) or reading in chunks up to a cap.
- Validate `file.content_type` in addition to extension.
- Consider streaming the parse via `file.file` instead of `await file.read()`.

---

### 🟠 Medium

#### 6. N+1 queries on every statement import
`upload_statement` issues 2–3 queries **per row**: dedup check, prior-category lookup, and insert. A 500-row statement = ~1,500 round trips to Supabase. This is slow and increases the chance of partial-failure midway through.

**Recommendation:**
- Batch the dedup check: one query that loads `(name, amount, date)` tuples for the user, then check in Python.
- Batch the prior-category lookup with a single grouped query.
- Use a single bulk `insert(rows_list)` at the end.

#### 7. Dedup key is fragile
`app/expenses.py` dedups on `(name, amount, date)`. Banks often emit two real transactions with identical descriptions and amounts on the same day (e.g., two coffees at the same shop). These will silently be dropped on re-import.

**Recommendation:** Add a stable external ID where available (most Czech bank exports include a transaction ID column), and fall back to including a row index or `bank_ref` field. Store it as a unique-per-user column and dedup on it.

#### 8. Missing index for the dedup query
The hot lookup is `WHERE user_id=? AND name=? AND amount=? AND date=?`. Only `user_id` is indexed (migration 006).

**Recommendation:** Add `CREATE INDEX expenses_user_dedup_idx ON expenses(user_id, date, amount);` (or a true unique constraint once external IDs are introduced).

#### 9. `date` stored as TEXT
`migrations/001_initial_schema.sql` stores date as `TEXT`. The app already parses to ISO `YYYY-MM-DD` so sorts work, but range queries lose the planner benefit of a real `DATE`, and validation is purely application-side.

**Recommendation:** Migrate to `DATE`. Add a CHECK constraint or use the proper type.

#### 10. `update_category` does not validate ownership of `category_id` lookups, and silently no-ops on missing rows
If `category_name` doesn't match any row, `category_id` becomes `None` and the expense is silently uncategorized — indistinguishable from the user intentionally clearing it. No error is returned.

**Recommendation:** Return 400 when an unknown category is submitted.

#### 11. `expenses_user_id` migration backfill is broken-by-design
`migrations/006_expenses_user_id.sql` requires hand-editing `<YOUR_USER_UUID>` in the SQL before running. If applied as-is in a new environment, it errors out mid-migration with `ALTER COLUMN ... SET NOT NULL` failing on NULLs. There is no rollback.

**Recommendation:** Wrap the migration in a transaction and either delete all existing rows in fresh environments or document a flag clearly at the top. Add a comment that says "do not run on a fresh DB."

---

### 🟡 Low

#### 12. No rate limiting on `/login` or `/signup`
Brute-force attacks are unmitigated. Supabase enforces some limits at its end, but adding `slowapi` middleware is trivial and worthwhile.

#### 13. Error info leakage from `signup`
`return e.message or "Could not create account."` — Supabase auth error messages can reveal whether an email is registered. Consider normalizing to a generic message.

#### 14. `migrations/` is not idempotent and not versioned in code
The README says "run them in order in the SQL Editor" — there's no tracking of which migrations have been applied. For a single-user hobby tool this is fine, but consider adopting `supabase migration` CLI or `alembic`.

#### 15. Categories are global, not per-user
All users see the same category list (`categories` table has no `user_id`). This is a product decision, but worth flagging — it surprises users and means one user renaming a category affects all.

#### 16. `confirmDelete` uses a fragile 3-second timeout pattern
`templates/dashboard.html` toggles a "Confirm?" state without locking the button. Double-clicks during the confirmation window race the HTMX request. Minor UX bug.

#### 17. CDN scripts pinned only to major versions
`<script src="https://unpkg.com/htmx.org@2.0.4">` is fine, but `chart.js@4` floats. Use SRI hashes for both to defend against CDN compromise.

#### 18. Dead/unused schema field
`expenses.receipt_path` is in the schema (migration 001) but never written or read.

---

## Concrete Recommendations Checklist

### Must-do before broader use
- [ ] Make `SESSION_SECRET` required (`main.py`) — no fallback.
- [ ] Set `SessionMiddleware(..., same_site="strict", https_only=True)`.
- [ ] Add CSRF protection to all `POST`/`PUT`/`DELETE` endpoints.
- [ ] Cap upload size and validate content-type in `upload_statement` (`app/expenses.py`).
- [ ] HTML-escape (or template-render) the response in `update_category` (`app/expenses.py`).
- [ ] Add an integration test confirming user A cannot read/modify user B's data.

### Should-do soon
- [ ] Replace the per-row dedup/insert loop in `upload_statement` with batched queries + bulk insert.
- [ ] Add a composite index on `expenses(user_id, date, amount)`; prefer a unique `(user_id, bank_ref)` once available.
- [ ] Migrate `expenses.date` from `TEXT` to `DATE`.
- [ ] Define real RLS policies and switch data ops to user-scoped JWT clients (long-term path away from the service-role key).
- [ ] Validate `category` against allowed values in `update_category`; return 400 on unknown.
- [ ] Add rate limiting on `/login` and `/signup`.

### Nice-to-have
- [ ] Drop `expenses.receipt_path` or implement it.
- [ ] Add SRI to CDN script tags.
- [ ] Adopt a migration tool (Supabase CLI or alembic) so applied migrations are tracked.
- [ ] Generic, identical error messages on signup to avoid account enumeration.
- [ ] Consider per-user categories or a flag distinguishing "shared defaults" from user-created.

---

## Architecture Notes (overall positive)

- Module split (`auth`, `dashboard`, `expenses`, `database`) is clean and easy to navigate.
- Comments are unusually good — the bank CSV parser's `_NAME_KEYWORD_TIERS` and `_decode_statement_bytes` are clear about *why*, not just *what*.
- HTMX-partial pattern is consistent and avoids client-side state sprawl.
- The `fillGaps` 4000-iteration guard in `dashboard.html` shows defensive thinking; that mindset just needs to extend to the server-side boundary cases above.