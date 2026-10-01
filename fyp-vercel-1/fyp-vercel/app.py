"""
FYP Repository - Flask backend
==============================
A web app to store, index, search and manage final-year engineering project records,
with student/faculty roles, file upload, analytics, CSV export and an activity log.

DATA STRUCTURES (all hand-written in ds.py, used by ProjectRepository in repository.py)
  DynamicArray  master list of Project objects              append O(1) amortised
  HashMap       id->Project, domain->ids, tech->ids, email->User     O(1) average
  Trie          title words -> ids (prefix/keyword search)  O(L + k)
  AVL tree      year -> ids (year range queries)            O(log n + k)
  Max-heap      top-K domains by project count              O(n + k log n)

LAYERS
  ds.py          data structures
  repository.py  ProjectRepository (indexes + search + analytics)
  services.py    AuthService, ActivityLog, SQLite Database
  app.py         REST controllers (this file)   ->   static/index.html is the UI layer

DESIGN NOTES
  * Auth: passwords hashed with werkzeug (PBKDF2); login stored in a signed Flask session cookie.
  * Roles: students may edit/delete only their own projects; faculty may do anything + flag + export.
  * Persistence: Postgres (Neon) is the source of truth; uploaded files live in Vercel Blob (private).
    The custom indexes (Trie, AVL, hash maps) are in-memory and are rebuilt from the DB whenever the
    `meta.v` write counter shows another serverless instance changed something. No sample data is
    created: on a fresh database the first visitor creates the faculty/admin account (/api/setup).
  * Concurrency: writers are serialised across all instances with a Postgres advisory lock, and each
    write is one transaction (committed in after_request, rolled back on any error).
"""
import csv, hmac, io, os, re, threading, time
from functools import wraps
from flask import Flask, jsonify, request, session, send_from_directory, Response, g
from werkzeug.utils import secure_filename
import psycopg
from vercel import blob

from repository import ProjectRepository
from services import AuthService, ActivityLog, Database

BASE = os.path.dirname(os.path.abspath(__file__))
ON_VERCEL = bool(os.environ.get("VERCEL"))
ALLOWED = {".pdf", ".ppt", ".pptx", ".doc", ".docx"}
DOMAINS = ["AI", "IoT", "Web", "Security", "Mobile", "Data"]
MAX_UPLOAD = 4 * 1024 * 1024          # Vercel rejects request bodies above ~4.5 MB, so cap at 4 MB

SECRET = os.environ.get("SECRET_KEY", "")
if not SECRET:
    if ON_VERCEL: raise RuntimeError("Set the SECRET_KEY environment variable in Vercel.")
    SECRET = "dev-secret-change-me"
DSN = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DSN: raise RuntimeError("Set DATABASE_URL to your Neon connection string.")
SETUP_KEY = os.environ.get("SETUP_KEY", "")
# Without an email server, the reset token is returned in the API response, which lets anyone reset any
# account. So it is ON for local demos and OFF on Vercel unless you deliberately set DEMO_RESET=1.
DEMO_RESET = os.environ.get("DEMO_RESET", "0" if ON_VERCEL else "1") == "1"

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="")
app.secret_key = SECRET
app.config.update(MAX_CONTENT_LENGTH=MAX_UPLOAD, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=ON_VERCEL)

db = Database(DSN)
repo, auth, log = ProjectRepository(), AuthService(SECRET), ActivityLog()
_lock = threading.RLock()
_state = {"v": None}          # version of the database that the in-memory indexes reflect


# ------------------------------------------------------------------ persistence
def rebuild():
    """Load rows from Postgres and rebuild every index from scratch (Trie / AVL / hash maps)."""
    global repo, auth, log
    repo, auth, log = ProjectRepository(), AuthService(SECRET), ActivityLog()
    log.on_add = db.add_activity                              # activity log writes through to Postgres
    auth.load(db.load_users())
    for row in db.load_projects(): repo.load(row)
    log.items = db.load_activity()


def sync():
    """Rebuild the indexes only if another instance (or a failed request) made them stale."""
    v = db.version()
    if _state["v"] != v:
        rebuild(); _state["v"] = v


def _needs_db():
    return request.path.startswith(("/api/", "/uploads/"))


@app.before_request
def _begin():
    if _needs_db():
        _lock.acquire(); g.locked = True
        g.write = request.method not in ("GET", "HEAD", "OPTIONS")
        db.begin(write=g.write)
        sync()


@app.after_request
def _finish(resp):
    if g.get("locked") and db.conn is not None:
        if resp.status_code < 400:
            if db.dirty: _state["v"] = db.commit()
        elif g.write:
            _state["v"] = None       # request failed midway: in-memory indexes may be ahead of the DB
    return resp


@app.teardown_request
def _release(_exc):
    if g.pop("locked", False):
        if _exc is not None or (db.dirty and db.conn is not None): _state["v"] = None
        try: db.close()
        finally: _lock.release()


# ---------------------------------------------------------------------- helpers
class ApiError(Exception):
    def __init__(self, message, status=400): self.message, self.status = message, status


@app.errorhandler(ApiError)
def _api_err(e): return jsonify(error=e.message), e.status


@app.errorhandler(413)
def _too_big(_e): return jsonify(error="File is too large (4 MB max)."), 413


@app.errorhandler(psycopg.OperationalError)
def _db_down(_e): return jsonify(error="Database unavailable, please try again."), 503


@app.errorhandler(ValueError)
def _val_err(e): return jsonify(error="Validation failed", fields=e.args[0]), 400


def current_user():
    return auth.by_id.get(session.get("uid")) if session.get("uid") else None


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not current_user(): raise ApiError("Login required", 401)
        return f(*a, **k)
    return w


def faculty_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if current_user().role != "faculty": raise ApiError("Faculty access only", 403)
        return f(*a, **k)
    return w


def body():
    return (request.get_json(silent=True) or {}) if request.is_json else request.form.to_dict()


def int_arg(name):
    v = request.args.get(name, "")
    return int(v) if v.strip().lstrip("-").isdigit() else None


def show(p):
    d = p.to_dict()
    u = auth.by_id.get(p.uploaded_by)
    d["uploader_name"], d["uploader_dept"] = (u.name, u.department) if u else ("Unknown", "-")
    d["file_url"] = f"/uploads/{p.id}" if p.file_path else ""
    return d


def validate_project(d, partial=False):
    """Returns cleaned fields; raises ValueError({field: message})."""
    err, out = {}, {}
    if not partial or "title" in d:
        t = (d.get("title") or "").strip()
        if len(t) < 5: err["title"] = "Title must be at least 5 characters."
        out["title"] = t
    if not partial or "abstract" in d:
        a = (d.get("abstract") or "").strip()
        if len(a) < 30: err["abstract"] = "Abstract must be at least 30 characters."
        out["abstract"] = a
    if not partial or "domain" in d:
        m = [x for x in DOMAINS if x.lower() == (d.get("domain") or "").strip().lower()]
        if not m: err["domain"] = "Choose one of: " + ", ".join(DOMAINS)
        else: out["domain"] = m[0]
    if not partial or "year" in d:
        try: y = int(d.get("year"))
        except (TypeError, ValueError): y = 0
        if not 2015 <= y <= 2030: err["year"] = "Year must be between 2015 and 2030."
        out["year"] = y
    if not partial or "technologies" in d:
        t = d.get("technologies") or []
        if isinstance(t, str): t = t.split(",")
        out["technologies"] = [x.strip() for x in t if x.strip()]
    if not partial or "source_url" in d:
        u = (d.get("source_url") or "").strip()
        if u and not re.match(r"^https?://", u): err["source_url"] = "URL must start with http:// or https://"
        out["source_url"] = u
    if err: raise ValueError(err)
    return out


def check_upload():
    """Validate the optional uploaded file *before* anything is created/changed."""
    f = request.files.get("file")
    if not f or not f.filename: return None
    if os.path.splitext(f.filename)[1].lower() not in ALLOWED:
        raise ValueError({"file": "Allowed types: " + ", ".join(sorted(ALLOWED))})
    return f


def blob_delete(pathname):
    if pathname:
        try: blob.delete(pathname)
        except Exception: pass                               # a leftover file must never break a request


def save_upload(p, f):
    if not f: return
    old = p.file_path
    name = f"fyp/{p.id}_{int(time.time())}_{secure_filename(f.filename)}"
    res = blob.put(name, f.read(), access="private", content_type=f.mimetype or "application/octet-stream",
                   add_random_suffix=True)
    p.file_name, p.file_path = f.filename, res.pathname
    blob_delete(old)


def can_edit(p):
    u = current_user()
    return u.role == "faculty" or p.uploaded_by == u.id


def run_search(mine=False):
    a = request.args
    dept = a.get("department", "").strip().lower()
    uid = current_user().id

    def pred(p):
        if mine and p.uploaded_by != uid: return False
        if dept:
            u = auth.by_id.get(p.uploaded_by)
            return bool(u) and u.department.lower() == dept
        return True
    dom = next((d for d in DOMAINS if d.lower() == a.get("domain", "").lower()), "")
    return repo.search(a.get("q", ""), dom, a.get("tech", ""), int_arg("year_from"), int_arg("year_to"), pred)


# ------------------------------------------------------------------------ auth
@app.post("/api/signup")
def signup():
    d = body()
    u = auth.register(d.get("name"), d.get("email"), d.get("password"), d.get("department"), d.get("year"))
    db.save_user(u); log.add(f"New student {u.name} registered")
    return jsonify(message="Account created"), 201


@app.get("/api/setup-status")
def setup_status():
    return jsonify(needed=len(auth.by_id) == 0)


@app.post("/api/setup")
def setup():
    """First-run only: create the first faculty/admin account. Refused once any user exists."""
    if len(auth.by_id) > 0: raise ApiError("Setup already completed", 403)
    d = body()
    if SETUP_KEY and not hmac.compare_digest(str(d.get("setup_key") or ""), SETUP_KEY):
        raise ApiError("Invalid setup key", 403)
    u = auth.register(d.get("name"), d.get("email"), d.get("password"), d.get("department"), 0, role="faculty")
    db.save_user(u); log.add(f"Admin account created for {u.name}")
    session["uid"] = u.id
    return jsonify(user=u.to_dict(private=False)), 201


@app.post("/api/login")
def login():
    d = body()
    u = auth.login(d.get("email"), d.get("password"))
    if not u: raise ApiError("Invalid email or password", 401)
    session["uid"] = u.id
    return jsonify(user=u.to_dict(private=False))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(message="Logged out")


@app.get("/api/me")
@login_required
def me():
    return jsonify(user=current_user().to_dict(private=False))


@app.post("/api/forgot")
def forgot():
    token = auth.create_reset_token(body().get("email"))
    # Mock flow: a real system would email the link. The token is returned for demo purposes.
    return jsonify(message="If the email exists, a reset link was sent.", demo_token=token if DEMO_RESET else None)


@app.post("/api/reset")
def reset():
    d = body()
    u = auth.reset_password(d.get("token"), d.get("password")); db.save_user(u)
    return jsonify(message="Password updated")


# -------------------------------------------------------------------- projects
@app.get("/api/meta")
@login_required
def meta():
    m = repo.meta(); m["domains"] = DOMAINS
    m["departments"] = sorted({u.department for u in auth.all()})
    return jsonify(m)


@app.get("/api/projects")
@login_required
def list_projects():
    rows = run_search(mine=request.args.get("mine") == "1")
    per = min(max(int_arg("per_page") or 6, 1), 50)
    pages = max(1, -(-len(rows) // per))
    page = min(max(int_arg("page") or 1, 1), pages)
    return jsonify(items=[show(p) for p in rows[(page - 1) * per: page * per]],
                   total=len(rows), page=page, pages=pages)


@app.get("/api/projects/<int:pid>")
@login_required
def get_project(pid):
    p = repo.get(pid)
    if not p: raise ApiError("Project not found", 404)
    d = show(p); d["can_edit"] = can_edit(p)
    return jsonify(d)


@app.post("/api/projects")
@login_required
def create_project():
    fields, f = validate_project(body()), check_upload()
    p = repo.add(fields, current_user().id)
    save_upload(p, f); db.save_project(p)
    log.add(f"Project '{p.title}' added by {current_user().name}")
    return jsonify(show(p)), 201


@app.put("/api/projects/<int:pid>")
@login_required
def update_project(pid):
    p = repo.get(pid)
    if not p: raise ApiError("Project not found", 404)
    if not can_edit(p): raise ApiError("You can only edit your own projects", 403)
    fields, f = validate_project(body(), partial=True), check_upload()
    repo.update(pid, fields)                                  # un-index old keys, apply, re-index
    save_upload(p, f); db.save_project(p)
    log.add(f"Project '{p.title}' updated by {current_user().name}")
    return jsonify(show(p))


@app.delete("/api/projects/<int:pid>")
@login_required
def delete_project(pid):
    p = repo.get(pid)
    if not p: raise ApiError("Project not found", 404)
    if not can_edit(p): raise ApiError("You can only delete your own projects", 403)
    repo.delete(pid); db.delete_project(pid)
    blob_delete(p.file_path)
    log.add(f"Project '{p.title}' deleted by {current_user().name}")
    return jsonify(message="Deleted")


@app.post("/api/projects/<int:pid>/flag")
@faculty_required
def flag_project(pid):
    field = body().get("field")
    p = repo.get(pid)
    if not p: raise ApiError("Project not found", 404)
    if field not in ("featured", "verified"): raise ApiError("field must be 'featured' or 'verified'")
    setattr(p, field, not getattr(p, field)); p.updated_at = time.time()
    db.save_project(p)
    log.add(f"Project '{p.title}' {'marked' if getattr(p, field) else 'unmarked'} {field} by {current_user().name}")
    return jsonify(show(p))


@app.get("/uploads/<int:pid>")
@login_required
def uploaded(pid):
    """Files are private in Blob; only logged-in users can fetch them, through this route."""
    p = repo.get(pid)
    if not p or not p.file_path: raise ApiError("File not found", 404)
    res = blob.get(p.file_path, access="private")
    return Response(res.content, mimetype=res.content_type or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{secure_filename(p.file_name) or "file"}"'})


# ------------------------------------------------------- analytics / export / log
@app.get("/api/analytics")
@login_required
def analytics():
    return jsonify(repo.analytics(int_arg("year_from"), int_arg("year_to")))


@app.get("/api/activity")
@login_required
def activity():
    return jsonify(items=log.recent(int_arg("limit") or 15))


@app.get("/api/export/csv")
@faculty_required
def export_csv():
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["id", "title", "domain", "year", "technologies", "source_url", "uploaded_by", "department", "featured", "verified"])
    for p in run_search():                                    # same filters as the list endpoint
        s = show(p)
        w.writerow([p.id, p.title, p.domain, p.year, ";".join(p.technologies), p.source_url,
                    s["uploader_name"], s["uploader_dept"], p.featured, p.verified])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=projects.csv"})


@app.get("/api/export/report")
@faculty_required
def export_report():
    years = [y for y, _ in repo.year_idx.items()]
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["domain"] + years)
    for d in DOMAINS:
        w.writerow([d] + [len(repo.search(domain=d, year_from=y, year_to=y)) for y in years])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=report.csv"})


# --------------------------------------------------------------------------- UI
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)      # local dev; needs DATABASE_URL (a free Neon database works fine)
