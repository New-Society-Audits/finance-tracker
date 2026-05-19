# Security Audit — finance-tracker

## Executive Summary

`finance-tracker` is a single-tenant FastAPI app backed by Supabase Auth and Postgres. The codebase is small, readable, and the author has been thoughtful about a few security basics (RLS migration, signed cookies, service-role key kept server-side). However, several **high-severity issues** stand out: the app uses the Supabase **service-role key for all data operations** (bypassing RLS), session cookies are **not hardened**, signup is **open to the public** despite the README describing this as a single-user app, there is **no CSRF protection** on state-changing routes, and the `SESSION_SECRET` falls back to a hard-coded default. A few smaller issues (open redirect surface, missing security headers, public CDN script tags without SRI, unbounded file upload, README leaks the secret-key env var name as "publishable") round out the list.

This audit is scoped to security only. The application logic, parsing code, and template structure are otherwise sound.

---

## Findings (by severity)

### 🔴 Critical — `SESSION_SECRET` has an insecure default
**File:** `main.py:18`
```python
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-this-before-deploying")
```
If the env var is ever missing in production (typo, misconfigured Render service, local `.env` copied to a deployed box), session cookies are signed with a publicly-known string. Anyone can forge a valid session for any `user_id`.

**Recommendation:** Fail closed.
```python
SECRET_KEY = os.environ["SESSION_SECRET"]
if len(SECRET_KEY) < 32:
    raise RuntimeError("SESSION_SECRET must be at least 32 chars")
```

---

### 🔴 Critical — All data operations use the service-role key, bypassing RLS
**Files:** `app/database.py:30-34`, `app/dashboard.py`, `app/expenses.py`

Every database call goes through `get_client()`, which is built with `SUPABASE_SECRET_KEY` (service role). Authorization is enforced **only** by `.eq("user_id", user_id)` in application code. Migration `007_enable_rls.sql` enables RLS but adds no policies, explicitly relying on the service-role bypass.

Consequences:
1. Any missing `.eq("user_id", ...)` filter — present or future — silently exposes or corrupts other users' data. There is no defense-in-depth.
2. If the service-role key ever leaks (logs, error pages, env dump, supply-chain), the entire database is compromised. There is no DB-side check that requests originate from the right user.
3. The `categories` table queries (`app/dashboard.py:36`, `app/expenses.py:316`) have no user filter — fine today, but the pattern invites mistakes.

**Recommendation:**
- Switch to per-request authenticated clients using the user's Supabase JWT (`auth.set_session(...)` or `postgrest`'s `Authorization: Bearer <user_jwt>` header) and add proper RLS policies:
  ```sql
  CREATE POLICY "users see own expenses" ON expenses
    FOR ALL USING (auth.uid() = user_id);
  ```
- Persist the access token in the session at login time (`response.session.access_token`).
- Reserve the service-role key for admin scripts only.

---

### 🔴 High — No CSRF protection on state-changing routes
**Files:** `app/auth.py` (POST /login, /signup), `app/expenses.py` (POST /upload/statement, PUT /expense/{id}/category, DELETE /expense/{id})

The app relies on cookie-based session auth, accepts `application/x-www-form-urlencoded` / `multipart/form-data`, and has no CSRF token, `SameSite` configuration, or `Origin`/`Referer` check. A malicious site can trigger uploads, category changes, or deletes for any logged-in user.

**Recommendation:**
- Set `SameSite=Lax` (or `Strict`) and `Secure` on the session cookie:
  ```python
  app.add_middleware(
      SessionMiddleware,
      secret_key=SECRET_KEY,
      same_site="lax",
      https_only=True,
      max_age=60 * 60 * 24 * 7,
  )
  ```
- Add a CSRF token for state-changing endpoints (e.g. `starlette-csrf` or a double-submit cookie checked in an HTMX-friendly header).
- Verify `Origin`/`Referer` matches the deployment host for non-GET requests as a backstop.

---

### 🔴 High — Open public signup on a "single-user" app
**Files:** `app/auth.py:46-66`, `README.md` ("Single-user login with session auth")

`GET/POST /signup` is publicly reachable, so anyone on the internet can create an account, log in, upload statements, and consume Supabase quota. Multi-tenant code paths still rely on the service-role-key model above, so the threat surface is larger than intended.

**Recommendation:** If single-user is intended:
- Remove `/signup` routes and templates.
- Optionally gate signup with an invite code or allow-list (`ALLOWED_EMAILS` env var).
- Add an explicit allow-list check in `login()` even when signup is removed (in case users exist in Supabase from prior testing).

---

### 🟠 Medium — Missing cookie & response security headers
**File:** `main.py`

`SessionMiddleware` is added without `https_only`, `same_site`, or `max_age`. There is no middleware for HSTS, `X-Content-Type-Options`, `Referrer-Policy`, or a Content-Security-Policy. The dashboard loads `unpkg.com` and `cdn.jsdelivr.net` scripts without Subresource Integrity, so a CDN compromise lets attackers execute arbitrary JS against authenticated sessions.

**Recommendation:**
- Configure cookie flags as in the CSRF section above.
- Add a small middleware that injects:
  ```
  Strict-Transport-Security: max-age=31536000; includeSubDomains
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Content-Security-Policy: default-src 'self'; script-src 'self' https://unpkg.com https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'
  ```
- Pin and SRI-hash the CDN scripts in `templates/dashboard.html:8-9`, or vendor them under `/static/`.

---

### 🟠 Medium — Unbounded, unvalidated file upload
**File:** `app/expenses.py:235-249`

`upload_statement` calls `await file.read()` with no size cap, no content-type check (only filename extension), and no per-user rate limit. A user can POST a multi-gigabyte file, exhausting memory in the Render dyno (FastAPI buffers `UploadFile.read()` into memory once you call `.read()`). The parser also walks every row and issues a synchronous Supabase query per transaction (`_inserted` loop), which is amplified into an easy DoS.

**Recommendation:**
- Enforce a max body size via a middleware (e.g. 5 MB) and reject before reading.
- Stream-parse the CSV via `file.file` instead of loading the whole blob.
- Replace the per-transaction `existing` + `insert` loop with a single batched `upsert` (with a uniqueness constraint on `(user_id, name, amount, date)`).
- Validate `file.content_type in {"text/csv", "application/vnd.ms-excel"}` in addition to the extension.

---

### 🟠 Medium — No rate limiting on login or signup
**File:** `app/auth.py`

`POST /login` calls Supabase directly with no throttling or lockout, and `POST /signup` is similarly exposed. Credential stuffing and signup spam are both unmitigated.

**Recommendation:** Add `slowapi` or a reverse-proxy rate limit (e.g. 5 attempts / 10 min / IP for login). Track failed attempts per email as well.

---

### 🟠 Medium — `migrations/006_expenses_user_id.sql` runs an inline template placeholder
**File:** `migrations/006_expenses_user_id.sql:14`

`<YOUR_USER_UUID>` is meant to be replaced by hand. If anyone executes this verbatim, Postgres will error, but the migration file itself isn't safely idempotent (multiple SQL files execute non-transactionally in the SQL editor by default). A misapplied migration can leave the table in a half-migrated state where `user_id` is nullable and some rows lack ownership — a security-relevant condition given the app-layer authorization model.

**Recommendation:** Wrap each migration file in `BEGIN; ... COMMIT;` and replace the hand-edit with `INSERT ... SELECT auth.uid() ...` or remove migration `006` in favor of a fresh setup script for new deployments.

---

### 🟡 Low — Information disclosure in signup errors
**File:** `app/auth.py:58-63`

`e.message` from `AuthApiError` is rendered straight into the page. Supabase auth messages can leak the existence of an account ("User already registered"), enabling email enumeration.

**Recommendation:** Map Supabase errors to a generic message ("Could not create account. Check your email for next steps.") and rely on the existing "check your email" flow.

---

### 🟡 Low — `RedirectResponse` used as the result of a 401 check
**File:** `app/expenses.py:33-37`

`_require_auth` returns an `HTMLResponse("Unauthorized", status_code=401)` for HTMX-aware endpoints, which is fine. But `app/dashboard.py:_require_auth` returns a 303 redirect to `/login` for HTMX partials — the client may swap `/login` HTML into a fragment target. Minor, but the inconsistency is worth aligning. More importantly, an unauthenticated `GET /partials/expense-list` returning a redirect is observable; ensure no HTMX response leaks server-side rendered content for unauthenticated users.

**Recommendation:** Return `401` with an `HX-Redirect: /login` header for HTMX endpoints and let the browser navigate.

---

### 🟡 Low — README documents `SUPABASE_KEY` as "publishable" but also lists a "secret" key
**File:** `README.md` (env table)

Worth a short note that `SUPABASE_SECRET_KEY` is the **service-role** key, must never appear in any client-side bundle, and grants full DB access. Helps a future contributor not accidentally expose it.

---

### 🟡 Low — `date` stored as `TEXT`
**File:** `migrations/001_initial_schema.sql`

Not a direct vulnerability, but `date TEXT` plus client-trusted format strings is brittle. The dashboard JS guards against bad dates (`templates/dashboard.html`), confirming the author knows this can go wrong. A malformed date written via a future code path can break chart rendering for the whole user.

**Recommendation:** `ALTER TABLE expenses ALTER COLUMN date TYPE date USING date::date;`

---

## Next-Steps Checklist

- [ ] **Required (week 1):**
  - [ ] Remove the `"change-this-before-deploying"` fallback for `SESSION_SECRET` (`main.py:18`).
  - [ ] Set `SameSite=Lax`, `Secure`, and `max_age` on session cookies.
  - [ ] Add CSRF protection (token or Origin check) to all non-GET routes.
  - [ ] Remove or gate `/signup` if the app is single-user.
  - [ ] Add a max-body-size limit (e.g. 5 MB) on `/upload/statement`.

- [ ] **Important (week 2–3):**
  - [ ] Stop using the service-role key for request-scoped operations. Issue authenticated PostgREST calls per user and add real RLS policies on `expenses` (and `categories` if scoped).
  - [ ] Add login rate limiting (per IP and per email).
  - [ ] Add security response headers and a basic CSP; pin and SRI the CDN scripts.
  - [ ] Make signup error messages generic to prevent email enumeration.

- [ ] **Hardening (later):**
  - [ ] Convert `expenses.date` to a real `date` column with a check constraint.
  - [ ] Replace per-row dedup loop with a unique index and batched upsert.
  - [ ] Wrap all migrations in transactions; remove the placeholder UUID in `006`.
  - [ ] Add an integration test that asserts user A cannot read/write user B's rows (the test will fail today only by lucky code paths, not by DB enforcement).