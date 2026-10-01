"""services.py - business logic: users/auth, activity log, Postgres (Neon) persistence."""
import json, re, time
import psycopg
from psycopg.rows import dict_row
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash
from ds import HashMap


class User:
    def __init__(self, id, name, email, password_hash, role, department, year):
        self.id, self.name, self.email = id, name, email
        self.password_hash, self.role, self.department, self.year = password_hash, role, department, year

    def to_dict(self, private=True):
        d = dict(id=self.id, name=self.name, email=self.email, role=self.role,
                 department=self.department, year=self.year)
        if private: d["password_hash"] = self.password_hash
        return d


class AuthService:
    """Registration, login, password hashing and reset tokens."""

    def __init__(self, secret="dev-secret-change-me"):
        self.by_id, self.by_email = HashMap(), HashMap()      # O(1) lookups
        self.next_id = 1
        self._signer = URLSafeTimedSerializer(secret, salt="password-reset")

    def register(self, name, email, password, department, year, role="student"):
        name, email = (name or "").strip(), (email or "").strip().lower()
        errors = {}
        if len(name) < 2: errors["name"] = "Name is required."
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email): errors["email"] = "Enter a valid email."
        elif email in self.by_email: errors["email"] = "Email already registered."
        if len(password or "") < 6: errors["password"] = "Password must be at least 6 characters."
        if not (department or "").strip(): errors["department"] = "Department is required."
        try: year = int(year)
        except (TypeError, ValueError): year = 0
        if role == "student" and not 1 <= year <= 4: errors["year"] = "Year must be 1-4."
        if errors: raise ValueError(errors)
        u = User(self.next_id, name, email, generate_password_hash(password), role, department.strip(), year)
        self.next_id += 1
        self.by_id.put(u.id, u); self.by_email.put(email, u)
        return u

    def login(self, email, password):
        u = self.by_email.get((email or "").strip().lower())
        return u if u and check_password_hash(u.password_hash, password or "") else None

    def create_reset_token(self, email):
        """Signed, expiring token. It embeds a fragment of the current password hash,
        so it stops working the moment the password changes (single use)."""
        u = self.by_email.get((email or "").strip().lower())
        if not u: return None
        return self._signer.dumps({"uid": u.id, "h": u.password_hash[-12:]})

    def reset_password(self, token, new_password):
        try: data = self._signer.loads(token or "", max_age=900)      # valid 15 minutes
        except (BadSignature, SignatureExpired): raise ValueError({"token": "Invalid or expired token."})
        u = self.by_id.get(data.get("uid"))
        if not u or u.password_hash[-12:] != data.get("h"): raise ValueError({"token": "Invalid or expired token."})
        if len(new_password or "") < 6: raise ValueError({"password": "Password must be at least 6 characters."})
        u.password_hash = generate_password_hash(new_password)
        return u

    def all(self): return [u for _, u in self.by_id.items()]

    def load(self, rows):
        for r in rows:
            u = User(**r)
            self.by_id.put(u.id, u); self.by_email.put(u.email, u)
            self.next_id = max(self.next_id, u.id + 1)


class ActivityLog:
    """Newest-first list kept in memory; every entry is also written through `on_add` (the DB)."""
    def __init__(self, cap=200):
        self.items, self.cap, self.on_add = [], cap, None

    def add(self, message, ts=None):
        entry = {"message": message, "at": ts or time.time()}
        self.items.insert(0, entry)
        del self.items[self.cap:]
        if self.on_add: self.on_add(entry)

    def recent(self, n=15): return self.items[:n]


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY,
    v BIGINT NOT NULL DEFAULT 0            -- bumped on every write; lets each instance know when to reload
);
INSERT INTO meta (id, v) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('student', 'faculty')),
    department TEXT NOT NULL,
    year INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    domain TEXT NOT NULL,
    technologies TEXT NOT NULL DEFAULT '[]',      -- JSON list of strings
    year INTEGER NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    uploaded_by INTEGER NOT NULL REFERENCES users(id),
    file_name TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',           -- Vercel Blob pathname
    featured INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS activity (
    id BIGSERIAL PRIMARY KEY,
    message TEXT NOT NULL,
    at DOUBLE PRECISION NOT NULL
);
"""

WRITE_LOCK = 748291      # advisory-lock key: serialises all writers across every serverless instance


class Database:
    """
    Postgres (Neon) persistence via psycopg 3.

    Design: Postgres is the durable source of truth; the custom data structures
    (Trie / AVL / hash maps) are in-memory *indexes* rebuilt from these tables whenever the
    `meta.v` counter shows another instance changed something. One short-lived connection
    per request (serverless-friendly, works with Neon's pooled endpoint).
    """

    def __init__(self, dsn):
        self.dsn, self.conn, self.dirty = dsn, None, False
        with psycopg.connect(dsn, prepare_threshold=None) as c:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))   # cold starts may race
            c.execute(SCHEMA)

    # ---- per-request transaction ----
    def begin(self, write=False):
        self.conn = psycopg.connect(self.dsn, row_factory=dict_row, prepare_threshold=None)
        self.dirty = False
        if write: self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))

    def version(self):
        return self.conn.execute("SELECT v FROM meta WHERE id = 1").fetchone()["v"]

    def commit(self):
        """Bump the version and commit. Returns the new version."""
        v = self.conn.execute("UPDATE meta SET v = v + 1 WHERE id = 1 RETURNING v").fetchone()["v"]
        self.conn.commit(); self.dirty = False
        return v

    def close(self):
        if self.conn is not None:
            try: self.conn.rollback()
            finally: self.conn.close(); self.conn = None
        self.dirty = False

    # ---- reads (used when the indexes need rebuilding) ----
    def load_users(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM users ORDER BY id")]

    def load_projects(self):
        rows = []
        for r in self.conn.execute("SELECT * FROM projects ORDER BY id"):
            d = dict(r)
            d["technologies"] = json.loads(d["technologies"])
            rows.append(d)
        return rows

    def load_activity(self, limit=200):
        return [dict(r) for r in self.conn.execute(
            "SELECT message, at FROM activity ORDER BY at DESC, id DESC LIMIT %s", (limit,))]

    # ---- writes (inside the request's transaction; committed in app.py after_request) ----
    def save_user(self, u):
        self.dirty = True
        self.conn.execute(
            """INSERT INTO users (id,name,email,password_hash,role,department,year)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, email=EXCLUDED.email,
                 password_hash=EXCLUDED.password_hash, role=EXCLUDED.role,
                 department=EXCLUDED.department, year=EXCLUDED.year""",
            (u.id, u.name, u.email, u.password_hash, u.role, u.department, u.year))

    def save_project(self, p):
        self.dirty = True
        self.conn.execute(
            """INSERT INTO projects (id,title,abstract,domain,technologies,year,source_url,
                 uploaded_by,file_name,file_path,featured,verified,created_at,updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, abstract=EXCLUDED.abstract,
                 domain=EXCLUDED.domain, technologies=EXCLUDED.technologies, year=EXCLUDED.year,
                 source_url=EXCLUDED.source_url, uploaded_by=EXCLUDED.uploaded_by,
                 file_name=EXCLUDED.file_name, file_path=EXCLUDED.file_path,
                 featured=EXCLUDED.featured, verified=EXCLUDED.verified,
                 created_at=EXCLUDED.created_at, updated_at=EXCLUDED.updated_at""",
            (p.id, p.title, p.abstract, p.domain, json.dumps(p.technologies), p.year, p.source_url or "",
             p.uploaded_by, p.file_name or "", p.file_path or "", int(p.featured), int(p.verified),
             p.created_at, p.updated_at))

    def delete_project(self, pid):
        self.dirty = True
        self.conn.execute("DELETE FROM projects WHERE id = %s", (pid,))

    def add_activity(self, entry, cap=200):
        self.dirty = True
        self.conn.execute("INSERT INTO activity (message, at) VALUES (%s, %s)", (entry["message"], entry["at"]))
        self.conn.execute("DELETE FROM activity WHERE id NOT IN "
                          "(SELECT id FROM activity ORDER BY at DESC, id DESC LIMIT %s)", (cap,))
