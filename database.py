import os
import json
import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

# Use Railway volume mount if present, otherwise local file
DB_DIR = "/data" if os.path.exists("/data") else os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "leeds.db")
PROFILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "business_profiles")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id TEXT NOT NULL,
            business_name TEXT,
            name TEXT,
            contact TEXT,
            address TEXT,
            issue TEXT,
            description TEXT,
            urgency TEXT,
            preferred_time TEXT,
            email_sent INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id TEXT UNIQUE NOT NULL,
            config_json TEXT NOT NULL,
            login_email TEXT UNIQUE,
            password_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    seed_businesses_from_json()


# =========================
# Businesses
# =========================

def seed_businesses_from_json():
    """Import any JSON business profiles that aren't already in the DB."""
    if not os.path.exists(PROFILES_DIR):
        return
    conn = get_conn()
    for fname in os.listdir(PROFILES_DIR):
        if not fname.endswith(".json"):
            continue
        bid = fname[:-5]
        existing = conn.execute(
            "SELECT id FROM businesses WHERE business_id = ?", (bid,)
        ).fetchone()
        if existing:
            continue
        try:
            with open(os.path.join(PROFILES_DIR, fname), encoding="utf-8") as fh:
                raw = fh.read()
                json.loads(raw)
            now = datetime.utcnow().isoformat()
            conn.execute(
                "INSERT INTO businesses (business_id, config_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (bid, raw, now, now)
            )
            print(f"[seed] Imported business: {bid}")
        except Exception as e:
            print(f"[seed] Failed to import {bid}: {e}")
    conn.commit()
    conn.close()


def get_business_config(business_id):
    """Get a business's config (the chat-flow JSON). Returns dict or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT config_json FROM businesses WHERE business_id = ?", (business_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        return json.loads(row["config_json"])
    except Exception:
        return None


def get_business_record(business_id):
    """Full row including login info — for admin views."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM businesses WHERE business_id = ?", (business_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_businesses():
    conn = get_conn()
    rows = conn.execute(
        "SELECT business_id, config_json, login_email FROM businesses ORDER BY business_id"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            cfg = json.loads(r["config_json"])
        except Exception:
            cfg = {}
        out.append({
            "business_id": r["business_id"],
            "name": cfg.get("business_name", r["business_id"]),
            "login_email": r["login_email"],
            "has_login": bool(r["login_email"]),
        })
    return out


def create_business(business_id, config):
    conn = get_conn()
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO businesses (business_id, config_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (business_id, json.dumps(config, indent=2), now, now)
    )
    conn.commit()
    conn.close()


def update_business(business_id, config):
    conn = get_conn()
    conn.execute(
        "UPDATE businesses SET config_json = ?, updated_at = ? WHERE business_id = ?",
        (json.dumps(config, indent=2), datetime.utcnow().isoformat(), business_id)
    )
    conn.commit()
    conn.close()


def set_business_credentials(business_id, login_email, password):
    conn = get_conn()
    conn.execute(
        "UPDATE businesses SET login_email = ?, password_hash = ? WHERE business_id = ?",
        (login_email.strip().lower(), generate_password_hash(password), business_id)
    )
    conn.commit()
    conn.close()


def verify_business_login(login_email, password):
    """Returns business_id if login is valid, otherwise None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT business_id, password_hash FROM businesses WHERE login_email = ?",
        (login_email.strip().lower(),)
    ).fetchone()
    conn.close()
    if not row or not row["password_hash"]:
        return None
    if check_password_hash(row["password_hash"], password):
        return row["business_id"]
    return None


# =========================
# Leads
# =========================

def save_lead(business_id, business_name, name, contact, address, issue,
              description, urgency, preferred_time, email_sent):
    conn = get_conn()
    conn.execute("""
        INSERT INTO leads (business_id, business_name, name, contact, address,
                           issue, description, urgency, preferred_time,
                           email_sent, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (business_id, business_name, name, contact, address, issue,
          description, urgency, preferred_time, 1 if email_sent else 0,
          datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def get_all_leads(business_id=None, limit=200):
    conn = get_conn()
    if business_id:
        rows = conn.execute(
            "SELECT * FROM leads WHERE business_id = ? ORDER BY id DESC LIMIT ?",
            (business_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_lead_stats():
    conn = get_conn()
    rows = conn.execute("""
        SELECT business_id,
               business_name,
               COUNT(*) AS total,
               SUM(CASE WHEN created_at >= datetime('now','-7 days') THEN 1 ELSE 0 END) AS this_week
        FROM leads
        GROUP BY business_id
        ORDER BY total DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]
