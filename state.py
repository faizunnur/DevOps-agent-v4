"""
State Manager — SQLite
Tracks every deployment step so we can resume, stop, and audit.
"""
import sqlite3
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

DB_PATH = os.getenv("STATE_DB", str(Path(tempfile.gettempdir()) / "devops-agent" / "state.db"))


def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS deployments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project     TEXT NOT NULL UNIQUE,
            app         TEXT,
            repo        TEXT,
            cloud       TEXT DEFAULT 'AWS',
            region      TEXT DEFAULT 'us-east-1',
            branch      TEXT DEFAULT 'main',
            status      TEXT DEFAULT 'pending',
            ec2_ip      TEXT,
            created_at  TEXT,
            updated_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS steps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project     TEXT NOT NULL,
            step        TEXT NOT NULL,
            status      TEXT DEFAULT 'pending',
            result      TEXT,
            error       TEXT,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project     TEXT NOT NULL,
            path        TEXT NOT NULL,
            content     TEXT,
            updated_at  TEXT,
            UNIQUE(project, path)
        );
        """)
        # Add branch column if it doesn't exist (for migration)
        try:
            conn.execute("ALTER TABLE deployments ADD COLUMN branch TEXT DEFAULT 'main'")
        except sqlite3.OperationalError:
            pass  # column already exists


def save_deployment(project, app, repo, cloud="AWS", region="us-east-1", branch="main"):
    now = datetime.utcnow().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO deployments (project,app,repo,cloud,region,branch,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'pending',?,?) "
            "ON CONFLICT(project) DO UPDATE SET app=excluded.app, repo=excluded.repo, "
            "cloud=excluded.cloud, region=excluded.region, branch=excluded.branch, updated_at=excluded.updated_at",
            (project, app, repo, cloud, region, branch, now, now)
        )


def update_deployment(project, **kwargs):
    now = datetime.utcnow().isoformat()
    kwargs["updated_at"] = now
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [project]
    with _conn() as conn:
        conn.execute(f"UPDATE deployments SET {sets} WHERE project=?", vals)


def get_deployment(project):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM deployments WHERE project=?", (project,)).fetchone()
        return dict(row) if row else None


def get_deployment_by_repo(repo):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM deployments WHERE repo=? ORDER BY updated_at DESC LIMIT 1", (repo,)).fetchone()
        return dict(row) if row else None


def list_projects():
    with _conn() as conn:
        rows = conn.execute("SELECT project, status, ec2_ip FROM deployments ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def log_step(project, step, status, result=None, error=None):
    now = datetime.utcnow().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO steps (project,step,status,result,error,created_at) VALUES (?,?,?,?,?,?)",
            (project, step, status, str(result)[:2000] if result else None,
             str(error)[:2000] if error else None, now)
        )


def get_steps(project):
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM steps WHERE project=? ORDER BY id", (project,)).fetchall()
        return [dict(r) for r in rows]


def step_done(project, step):
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM steps WHERE project=? AND step=? AND status='done' ORDER BY id DESC LIMIT 1",
            (project, step)
        ).fetchone()
        return row is not None


def save_file(project, path, content):
    if not path:
        return  # Guard against None/empty paths from GitHub API tree objects
        
    now = datetime.utcnow().isoformat()
    # Also write to local filesystem (only if project and path are valid)
    if project and path:
        try:
            local_path = Path(tempfile.gettempdir()) / "devops-agent" / str(project) / str(path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(content, encoding='utf-8')
        except Exception:
            pass  # filesystem cache is best-effort; DB is the source of truth
    with _conn() as conn:
        conn.execute(
            "INSERT INTO files (project,path,content,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(project,path) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
            (project, path, content, now)
        )


def get_file(project, path):
    # Try local filesystem first (only if project and path are valid)
    if project and path:
        try:
            local_path = Path(tempfile.gettempdir()) / "devops-agent" / str(project) / str(path)
            if local_path.exists():
                return local_path.read_text(encoding='utf-8')
        except Exception:
            pass
    # Fall back to DB
    with _conn() as conn:
        row = conn.execute(
            "SELECT content FROM files WHERE project=? AND path=?", (project, path)
        ).fetchone()
        return row["content"] if row else None


def get_all_files(project):
    with _conn() as conn:
        rows = conn.execute("SELECT path, content FROM files WHERE project=?", (project,)).fetchall()
        return {r["path"]: r["content"] for r in rows}


def list_projects():
    with _conn() as conn:
        rows = conn.execute("SELECT project, app, status, ec2_ip, updated_at FROM deployments ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


init_db()