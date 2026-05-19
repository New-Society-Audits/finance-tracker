# Security Audit — finance-tracker

## Executive Summary

`finance-tracker` is a small FastAPI + Supabase application with a sound overall structure (separated routers, session-based auth, parameterised Supabase queries). However, several **high-impact security issues** weaken the deployment posture, most notably the use of Supabase's **service-role key for all data operations** combined with **disabled CSRF protection**, an **insecure default `SESSION_SECRET`**, and **open signup** on a "single-user app." There is also a small but real **XSS** vector in the category update flow and **no file-size / rate limiting** on the CSV upload endpoint.

None of the findings indicate a compromise, but most should be fixed before any non-personal deployment.

---

## Findings (by severity)

### 🔴 H1 — Insecure default `SESSION_SECRET` fallback
**File:** `main.py`
```python
SECRET_KEY = os.environ.get("SESSION_SECRET", "change-this-before-deploying")
```
If the env var is unset (locally, in a misconfigured staging env, or if a deploy forgets it), Starlette will sign session cookies with a public, well-known string. Anyone who knows the source can forge sessions and bypass auth entirely.

**Recommendation:** Fail closed:
```python
SECRET_KEY = os.environ["SESSION_SECRET"]
assert len(SECRET_KEY) >= 32, "SESSION_SECRET must be ≥32 random bytes"
```

---

### 🔴 H2 — Service-role key used for *all* data operations, RLS effectively disabled
**Files:** `app/database.py`, `migrations/007_enable_rls.sql`
```python
def get_client() -> Client:
    # service-role client used by every route -> bypasses RLS
```
Migration 007 enables RLS but no policies exist, and the app uses the service-role key, so RLS provides **zero defense in depth**. Any SQL injection, IDOR, or logic bug in the FastAPI layer immediately becomes full-database access. The README and migration comments explicitly acknowledge this.

User isolation is enforced *only* by `.eq("user_id", user_id)` filters scattered across the code. If a future change forgets one filter, users will see each other's data. Indeed, `expense_list_partial` filters by `user_id` for the main query but the category lookup is global (acceptable, since categories are shared), and the `category` update reads categories without `.eq("user_id", ...)` — currently fine, but fragile.

**Recommendations:**
- Use the **anon key + authenticated user JWT** for data ops instead of the service-role key. Pass the access token from `sign_in_with_password` into `postgrest_client.auth(token)`.
- Add real RLS policies: `auth.uid() = user_id` on `expenses` (select/insert/update/delete).
- Keep the service-role client only for genuinely privileged ops (migrations, scheduled jobs), not request handlers.

---

### 🔴 H3 — No CSRF protection on state-changing endpoints
**Files:** `app/auth.py`, `app/expenses.py`, `app/dashboard.py`

Session auth via cookie + form POSTs (`/login`, `/signup`, `/upload/statement`, `/expense/{id}/category`, `/expense/{id}`) with no CSRF token, no `SameSite` configured explicitly, no `Origin`/`Referer` checks. `SessionMiddleware`'s default `same_site="lax"` blocks cross-site top-level POSTs but **not** all HTMX scenarios (e.g. an attacker site can still issue a DELETE if browser cooperates differently, and `lax` does not cover `fetch`/HTMX from a subdomain on the same site if any are added).

**Recommendations:**
- Add CSRF middleware (e.g. `starlette-csrf` or `fastapi-csrf-protect`) and include a token in every form / HTMX request via `hx-headers`.
- Explicitly set `SessionMiddleware(..., same_site="strict", https_only=True)`.

---

### 🟠 H4 — Open signup on a self-described single-user app
**File:** `app/auth.py`, `README.md`

`/signup` is public; anyone who finds the deployed URL can create an account and start uploading statements. The README describes the app as "single-user," so this is almost certainly unintended in production. Combined with H2 (service-role key, all data behind the same client), the threat is contained to the attacker's own row, but it still:
- Consumes Supabase Auth quotas.
- Lets attackers probe behavior (parser crashes, error messages).
- Lets attackers upload arbitrary CSVs of unbounded size (see H6).

**Recommendation:** Either disable `/signup` entirely in production (env-gate the route), gate it behind an invite code, or restrict via Supabase Auth's allow-list / disabled signups setting.

---

### 🟠 H5 — Stored XSS via category name (and minor risks elsewhere)
**File:** `app/expenses.py`
```python
return HTMLResponse(
    f'<span class="expense-category-tag" id="category-tag-{expense_id}">{tag_content}</span>'
)
```
`tag_content` is the raw `category` form value echoed back unescaped into HTML. Today categories are constrained to a known list via DB lookup — but the value returned is whatever the user submitted, *not* the resolved DB name. Submitting `category=<img src=x onerror=alert(1)>` returns reflected HTML that HTMX will inject into the DOM.

A similar reflection risk applies to the upload-success snippet (uses integers, safe) but the pattern of building HTML by f-string is dangerous and used in multiple places.

**Recommendations:**
- Render the response via Jinja autoescape (`templates.TemplateResponse(...)` with a tiny partial) or use `html.escape(tag_content)`.
- Return the canonical name fetched from the DB, not the user-submitted string.
- Audit all `HTMLResponse(f"...")` usages similarly.

---

### 🟠 H6 — Uploaded CSV: no size limit, no type validation, naive parsing
**File:** `app/expenses.py` → `upload_statement`
```python
contents = await file.read()           # entire file into memory
if filename.lower().endswith(".csv"):  # extension only
    transactions = parse_csv_statement(contents)
```
Issues:
1. `await file.read()` loads the entire upload into memory — a 2 GB CSV will OOM the worker.
2. Content-type and magic bytes are not checked; `evil.csv` could be anything.
3. For every transaction, the code issues **two synchronous Supabase requests** (dedup SELECT and INSERT). A 50k-row statement = 100k HTTP calls. Easy DoS vector.
4. `db.table("expenses").insert(...)` is called per row instead of batched; combined with no rate limiting on the route, a single user can saturate the worker.

**Recommendations:**
- Enforce a max body size in middleware or `Content-Length`/streaming check (e.g. 5 MB).
- Validate `file.content_type in {"text/csv","application/vnd.ms-excel"}`.
- Replace per-row dedup with one bulk SELECT (`in_("name", names)` filtered by date range) + one bulk `upsert(..., on_conflict=...)` using a real DB unique index on `(user_id, name, amount, date)`.
- Add a request-level rate limiter (e.g. `slowapi`) on `/upload/statement`, `/login`, `/signup`.

---

### 🟠 H7 — No login throttling / lockout
**File:** `app/auth.py`

`/login` and `/signup` accept unlimited attempts. Supabase Auth has some server-side protections, but the app does nothing. Credential stuffing against `/login` is therefore cheap.

**Recommendation:** Add IP- and email-based rate limiting (e.g. `slowapi` or Cloudflare); consider a CAPTCHA on `/signup`.

---

### 🟡 M1 — Session cookie security flags not set
**File:** `main.py`
```python
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
```
Starlette defaults: `same_site="lax"`, **`https_only=False`**, `max_age=14*24*3600`.

**Recommendation:**
```python
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="strict",
    https_only=True,        # required in production
    max_age=60 * 60 * 24 * 7,
)
```

---

### 🟡 M2 — Logout uses GET
**File:** `app/auth.py`
```python
@router.get("/logout")
def logout(request: Request): ...
```
GET logout is CSRF-able (attacker page with `<img src="https://app/logout">` can log you out). Low impact, but trivial to fix.

**Recommendation:** Switch to `POST /logout` with a CSRF token.

---

### 🟡 M3 — Server-side Supabase Auth session not invalidated on logout
**File:** `app/auth.py` → `logout`

The endpoint clears the Starlette cookie but never calls `auth_client.auth.sign_out()`. Refresh tokens issued by Supabase remain valid until they expire; if they leak (via H5, logs, etc.) they're still usable.

**Recommendation:** Store the access/refresh token from `sign_in_with_password` and call `sign_out` on logout. (Also required to make H2's RLS-based approach work.)

---

### 🟡 M4 — `migrations/006_expenses_user_id.sql` ships with a literal placeholder
```sql
UPDATE expenses SET user_id = '<YOUR_USER_UUID>' WHERE user_id IS NULL;
```
Running migrations in CI/CD blind will fail; running them via copy-paste invites mistakes (e.g. assigning all rows to the wrong account if someone replaces with an arbitrary UUID).

**Recommendation:** Split into two reviewable migrations: (a) add nullable column + index, (b) require manual backfill before applying NOT NULL.

---

### 🟢 L1 — `<script>` tag returned from `/upload/statement`
**File:** `app/expenses.py`
```python
return HTMLResponse(f"""
    <div class="upload-success">...</div>
    <script>
        setTimeout(() => {{
            document.getElementById('modal-overlay').remove();
            ...
        }}, 800);
    </script>
""")
```
HTMX won't execute inline scripts in swapped fragments by default (depends on version/config). More importantly, mixing logic into server-rendered HTML undermines a future CSP (`script-src 'self'`).

**Recommendation:** Trigger the refresh via `HX-Trigger` response header and an out-of-band swap; remove inline `<script>`.

### 🟢 L2 — Missing security headers
No middleware sets `Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, or `Strict-Transport-Security`. CDN-hosted HTMX and Chart.js will also need to be allow-listed (or self-hosted) under any future CSP.

**Recommendation:** Add a small headers middleware; self-host `htmx.org` and `chart.js` (currently loaded from `unpkg`/`jsdelivr` without SRI hashes — supply-chain risk).

### 🟢 L3 — Subresource Integrity missing on CDN scripts
**File:** `templates/dashboard.html`
```html
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
```
A CDN compromise or typosquat would inject JS into authenticated dashboards.
**Recommendation:** Add `integrity="sha384-..."` and `crossorigin="anonymous"`, or self-host.

### 🟢 L4 — Error messages from Supabase passed verbatim
**File:** `app/auth.py` (signup) — `e.message` is rendered to the user. Usually fine, but Supabase has been known to leak whether an email exists. Confirm via review.

### 🟢 L5 — `date` stored as `TEXT` not `DATE`
**File:** `migrations/001_initial_schema.sql` — no integrity, no range checks; relies on parsers being correct. Not exploitable today, but invites bugs.

---

## Pragmatic Next-Steps Checklist

**Before next deploy**
- [ ] Make `SESSION_SECRET` required; remove default fallback (H1).
- [ ] Set `SessionMiddleware(same_site="strict", https_only=True)` (M1).
- [ ] HTML-escape category name in `/expense/{id}/category` response (H5).
- [ ] Add a body-size limit + content-type check on `/upload/statement` (H6).
- [ ] Gate `/signup` behind env flag or invite code (H4).

**This sprint**
- [ ] Add CSRF protection middleware; convert `/logout` to POST (H3, M2).
- [ ] Add login/signup/upload rate limiting (H7).
- [ ] Replace per-row dedup with a bulk select + DB unique index + upsert (H6).
- [ ] Call `auth_client.auth.sign_out()` on logout (M3).
- [ ] Add security headers + SRI on CDN scripts (or self-host) (L2, L3).

**Roadmap**
- [ ] Migrate from service-role-everywhere to anon-key + per-user JWT; write real RLS policies on `expenses` (H2).
- [ ] Move CSV parsing to a streaming reader; consider a background job for large imports.
- [ ] Convert `expenses.date` to `DATE` and add `CHECK` constraints / indexes (L5).
- [ ] Add automated tests covering auth boundary cases (missing `user_id`, foreign user IDs in path params, malicious category names).