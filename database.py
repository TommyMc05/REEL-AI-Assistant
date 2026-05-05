import os
import sqlite3
from datetime import datetime

# Use Railway volume mount if present, otherwise local file
DB_DIR = "/data" if os.path.exists("/data") else os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "leeds.db")


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
    conn.commit()
    conn.close()


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
    """Return {business_id: {'total': N, 'this_week': N}} for the dashboard."""
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
