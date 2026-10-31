# FYP Repository — Final Year Project Repository

A full-stack web app to store, index, search and manage final-year project records.
**Python + Flask + SQLite**, with a hand-written data-structures layer and a single-page UI.

## Run locally
```
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."        # free Neon database
export BLOB_READ_WRITE_TOKEN="vercel_blob_rw_..."   # only needed for file uploads
python app.py
```
Open **http://127.0.0.1:5000**. The app starts completely empty; the first visit shows a Welcome screen
to create the Faculty/Admin account (enter your `SETUP_KEY` if you set one).

## Architecture
```
static/index.html  UI (fetch -> REST API)
app.py             REST controllers, validation, roles
services.py        AuthService, ActivityLog, SQLite Database
repository.py      ProjectRepository (search, analytics)
ds.py              DynamicArray, HashMap, Trie, AVLTree, MaxHeap (hand-written)
```
**SQLite is the source of truth.** Every change is written to it immediately. The custom data
structures are in-memory indexes rebuilt from the database at start-up, and all searching goes through them.

## Features
Signup/login/logout, hashed passwords, password reset (demo token) · student/faculty roles ·
project CRUD with file upload (pdf/ppt/doc, 10 MB) · Trie title search + domain/tech/year filters ·
server-side pagination · dashboard analytics with year filter · activity log · featured/verified flags ·
CSV export (respects filters) and per-domain-per-year report · dark mode.

## API (JSON, session cookie)
- `GET /api/setup-status`, `POST /api/setup` (first run only), `POST /api/signup | login | logout | forgot | reset`, `GET /api/me`
- `GET /api/projects?q=&domain=&tech=&year_from=&year_to=&department=&mine=1&page=&per_page=`
- `GET|PUT|DELETE /api/projects/<id>`, `POST /api/projects` (multipart with optional `file`)
- `POST /api/projects/<id>/flag {"field":"featured"|"verified"}` (faculty)
- `GET /api/analytics`, `/api/activity`, `/api/meta`, `/api/export/csv`, `/api/export/report`

## Data structures
| Structure | Used for | Cost |
|---|---|---|
| Dynamic array | master project list | append O(1) amortised |
| Hash map | id / domain / tech / email lookups | O(1) avg |
| Trie | title prefix + keyword search | O(L + k) |
| AVL tree | year index, range queries | O(log n + k) |
| Max-heap | top-K domains | O(n + k log n) |
##Demo Video
https://github.com/user-attachments/assets/4c9948ce-79a3-4423-b529-2c8a0b0ff612

## Viva summary
Each search narrows a set of project ids through the Trie, hash-map and AVL indexes, then intersects
them, so no filter scans the whole list. Around that core sit authentication, role checks, file upload,
analytics, export, an activity log and a SQLite database. The UI shows all of it working live.

## Deploying on Vercel (Neon + Blob)
1. Push this folder to GitHub, then **Add New -> Project** on vercel.com and import it.
2. In the project, open **Storage**: create/connect a **Neon** (Postgres) database and a **Blob** store (choose *Private*).
   This adds `DATABASE_URL` and `BLOB_READ_WRITE_TOKEN` automatically.
3. Add environment variables `SECRET_KEY` (long random string) and `SETUP_KEY` (any password you choose).
4. Deploy, open the link, create the admin account with your `SETUP_KEY`.

Notes: uploads are limited to 4 MB (Vercel request limit). Password-reset links are disabled on Vercel because there is
no email server (set `DEMO_RESET=1` only for demos). Indexes are rebuilt from Postgres whenever another instance changes data.
