"""
db.py  —  TikDL Bot database layer.
"""

import sqlite3
import os
import datetime
from contextlib import contextmanager

import config

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tikdl_bot.db")

def db_exists() -> bool:
    return os.path.exists(DB_PATH)


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id  INTEGER PRIMARY KEY,
            username     TEXT    DEFAULT '',
            first_name   TEXT    DEFAULT '',
            last_name    TEXT    DEFAULT '',
            first_seen   TEXT    NOT NULL,
            last_seen    TEXT    NOT NULL,
            blocked      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS licenses (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id      INTEGER NOT NULL DEFAULT 0,
            machine_id       TEXT    NOT NULL,
            license_key      TEXT    NOT NULL,
            days             INTEGER NOT NULL,
            issued_at        TEXT    NOT NULL,
            expires_at       TEXT    NOT NULL,
            revoked          INTEGER DEFAULT 0,
            revoked_at       TEXT,
            note             TEXT    DEFAULT '',
            last_checked_at  TEXT    DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_keys (
            machine_id   TEXT    PRIMARY KEY,
            license_key  TEXT    NOT NULL,
            created_at   TEXT    NOT NULL,
            claimed      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS orders (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id  INTEGER NOT NULL,
            machine_id   TEXT    NOT NULL DEFAULT '',
            plan_id      TEXT    NOT NULL,
            plan_name    TEXT    NOT NULL,
            days         INTEGER NOT NULL,
            price        REAL    NOT NULL,
            currency     TEXT    NOT NULL DEFAULT 'USD',
            status       TEXT    NOT NULL DEFAULT 'pending',
            created_at   TEXT    NOT NULL,
            approved_at  TEXT,
            note         TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS collected_urls (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            url       TEXT NOT NULL,
            user_id   INTEGER,
            user_name TEXT DEFAULT 'unknown',
            added_at  TEXT NOT NULL DEFAULT (datetime('now', '+7 hours'))
        );

        CREATE INDEX IF NOT EXISTS idx_lic_mid ON licenses(machine_id);
        CREATE INDEX IF NOT EXISTS idx_lic_exp ON licenses(expires_at);
        CREATE INDEX IF NOT EXISTS idx_ord_tid ON orders(telegram_id);
        CREATE INDEX IF NOT EXISTS idx_ord_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_collected_user_id ON collected_urls(user_id);
        CREATE INDEX IF NOT EXISTS idx_collected_user_name ON collected_urls(user_name);
        CREATE INDEX IF NOT EXISTS idx_collected_added_at ON collected_urls(added_at);
        """)
    # Auto-clean duplicates on every startup
    dedup_licenses()
    # Migration: add last_checked_at column if upgrading from older schema
    try:
        with _conn() as c:
            c.execute("ALTER TABLE licenses ADD COLUMN last_checked_at TEXT DEFAULT NULL")
    except Exception:
        pass  # Column already exists


def dedup_licenses() -> int:
    """
    Enforce 1-key-per-machine rule on existing data.
    For each machine_id that has multiple active keys,
    keep only the newest one and revoke the rest.
    Returns the number of duplicates revoked.
    """
    now   = datetime.datetime.utcnow().isoformat()
    today = datetime.date.today().isoformat()
    revoked_count = 0
    with _conn() as c:
        # Find machine IDs with more than 1 active license
        dupes = c.execute("""
            SELECT machine_id, COUNT(*) as cnt
            FROM licenses
            WHERE revoked=0 AND expires_at >= ?
            GROUP BY machine_id
            HAVING cnt > 1
        """, (today,)).fetchall()

        for row in dupes:
            mid = row["machine_id"]
            # Get all active licenses for this machine, newest first
            active = c.execute("""
                SELECT id FROM licenses
                WHERE machine_id=? AND revoked=0 AND expires_at >= ?
                ORDER BY issued_at DESC
            """, (mid, today)).fetchall()

            # Keep the first (newest), revoke the rest
            ids_to_revoke = [r["id"] for r in active[1:]]
            for lid in ids_to_revoke:
                c.execute(
                    "UPDATE licenses SET revoked=1, revoked_at=?, note=? WHERE id=?",
                    (now, "auto-dedup: 1-key-per-machine", lid)
                )
                revoked_count += 1

    if revoked_count > 0:
        import logging
        logging.getLogger(__name__).info(
            f"[dedup] Revoked {revoked_count} duplicate license(s) — 1-key-per-machine enforced."
        )
    return revoked_count


# ── Users ─────────────────────────────────────────────────────────────────────

def upsert_user(telegram_id: int, username: str = "",
                first_name: str = "", last_name: str = ""):
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute("""
            INSERT INTO users
               (telegram_id, username, first_name, last_name, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   username=excluded.username,
                   first_name=excluded.first_name,
                   last_name=excluded.last_name,
                   last_seen=excluded.last_seen
        """, (telegram_id, username, first_name, last_name, now, now))


def is_blocked(telegram_id: int) -> bool:
    with _conn() as c:
        r = c.execute(
            "SELECT blocked FROM users WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
        return bool(r and r["blocked"])


def get_all_users() -> list[dict]:
    today = datetime.date.today().isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT u.*,
                COUNT(CASE WHEN l.revoked=0 AND l.expires_at>=? THEN 1 END) AS key_count,
                COUNT(l.id) AS total_keys
            FROM users u
            LEFT JOIN licenses l ON u.telegram_id = l.telegram_id
            GROUP BY u.telegram_id
            ORDER BY u.last_seen DESC
        """, (today,)).fetchall()
        return [dict(r) for r in rows]


# ── Licenses ──────────────────────────────────────────────────────────────────

def count_active_licenses(telegram_id: int) -> int:
    today = datetime.date.today().isoformat()
    with _conn() as c:
        r = c.execute(
            "SELECT COUNT(*) FROM licenses WHERE telegram_id=? AND revoked=0 AND expires_at>=?",
            (telegram_id, today)
        ).fetchone()
        return r[0] if r else 0


def save_license(telegram_id: int, machine_id: str, license_key: str,
                 days: int, note: str = "",
                 custom_expires: str | None = None) -> int:
    """Save a license. If custom_expires is given (YYYY-MM-DD), use it instead of today+days."""
    now     = datetime.datetime.utcnow().isoformat()
    expires = custom_expires if custom_expires else (
        datetime.date.today() + datetime.timedelta(days=days)
    ).isoformat()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO licenses
               (telegram_id, machine_id, license_key, days, issued_at, expires_at, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (telegram_id, machine_id, license_key, days, now, expires, note)
        )
        return cur.lastrowid


def get_active_licenses(telegram_id: int) -> list[dict]:
    today = datetime.date.today().isoformat()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT * FROM licenses
               WHERE telegram_id=? AND revoked=0 AND expires_at>=?
               ORDER BY issued_at DESC""",
            (telegram_id, today)
        ).fetchall()]


def get_all_licenses_for_user(telegram_id: int) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM licenses WHERE telegram_id=? ORDER BY issued_at DESC",
            (telegram_id,)
        ).fetchall()]


def get_license_by_machine(machine_id: str) -> dict | None:
    """
    Get the LATEST license for a machine, regardless of status (active, expired, or revoked).
    
    This function returns the most recently issued license for a machine,
    regardless of its current state:
    - Active (not expired, not revoked)
    - Expired (past expiry date, but not revoked)
    - Revoked (admin revoked the license)
    
    This allows the bot to:
    1. Detect expired licenses → show renewal message
    2. Detect revoked licenses → show revocation message with contact info
    3. Block new key requests for any existing license
    4. Force users to renew instead of getting a new key
    """
    with _conn() as c:
        # Get the most recent license for this machine (regardless of revocation status)
        # IMPORTANT: Do NOT filter by revoked=0 — we need to see revoked licenses too!
        r = c.execute(
            """SELECT * FROM licenses WHERE machine_id=?
               ORDER BY issued_at DESC LIMIT 1""",
            (machine_id,)
        ).fetchone()
        return dict(r) if r else None


# ── Pending keys (auto-retrieval by TikDL app) ────────────────────────────────

def store_pending_key(machine_id: str, license_key: str):
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO pending_keys (machine_id, license_key, created_at, claimed)
            VALUES (?, ?, ?, 0)
        """, (machine_id, license_key, now))


def get_pending_key(machine_id: str) -> str | None:
    with _conn() as c:
        r = c.execute(
            "SELECT license_key FROM pending_keys WHERE machine_id=? AND claimed=0",
            (machine_id,)
        ).fetchone()
        return r["license_key"] if r else None


def mark_pending_claimed(machine_id: str):
    with _conn() as c:
        c.execute(
            "UPDATE pending_keys SET claimed=1 WHERE machine_id=?",
            (machine_id,)
        )


# ── Orders ────────────────────────────────────────────────────────────────────

def create_order(telegram_id: int, machine_id: str, plan: dict,
                 status: str = "pending") -> int:
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO orders
               (telegram_id, machine_id, plan_id, plan_name, days, price, currency, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (telegram_id, machine_id, plan["id"], plan["name"],
             plan["days"], plan["price"], plan["currency"], status, now)
        )
        return cur.lastrowid


def get_order(order_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(r) if r else None


def get_pending_orders() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT o.*, u.username, u.first_name FROM orders o
               LEFT JOIN users u ON o.telegram_id = u.telegram_id
               WHERE o.status='pending' ORDER BY o.created_at ASC"""
        ).fetchall()]


def approve_order(order_id: int, note: str = "") -> dict | None:
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE orders SET status='approved', approved_at=?, note=? WHERE id=?",
            (now, note, order_id)
        )
    return get_order(order_id)


def reject_order(order_id: int, note: str = ""):
    with _conn() as c:
        c.execute(
            "UPDATE orders SET status='rejected', note=? WHERE id=?",
            (note, order_id)
        )


def get_user_orders(telegram_id: int) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM orders WHERE telegram_id=? ORDER BY created_at DESC",
            (telegram_id,)
        ).fetchall()]

# ── License status management ─────────────────────────────────────────────────

def update_last_checked(license_id: int):
    """Update the last_checked_at timestamp for a license."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute("UPDATE licenses SET last_checked_at=? WHERE id=?", (now, license_id))

def revoke_license(license_id: int, note: str = "admin-revoked"):
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE licenses SET revoked=1, revoked_at=?, note=? WHERE id=?",
            (now, note, license_id)
        )

def activate_license(license_id: int, days: int | None = None):
    """Re-activate a revoked license, optionally extending expiry."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        if days:
            new_expires = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
            c.execute(
                "UPDATE licenses SET revoked=0, revoked_at=NULL, expires_at=?, note=? WHERE id=?",
                (new_expires, "admin-reactivated", license_id)
            )
        else:
            c.execute(
                "UPDATE licenses SET revoked=0, revoked_at=NULL, note=? WHERE id=?",
                ("admin-reactivated", license_id)
            )

def extend_license(license_id: int, days: int):
    """Extend a license by N days from today."""
    new_expires = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE licenses SET expires_at=?, note=? WHERE id=?",
            (new_expires, f"admin-extended-{days}d", license_id)
        )

def stack_license_expiry(license_id: int, add_days: int) -> dict:
    """
    Stack add_days on top of the existing expiry (or today if already expired).
    Returns updated license dict with new expires_at and total days_left.
    No new key generated — same key, extended expiry.
    """
    today = datetime.date.today()
    with _conn() as c:
        row = c.execute("SELECT * FROM licenses WHERE id=?", (license_id,)).fetchone()
        if not row:
            raise ValueError(f"License #{license_id} not found")
        row = dict(row)
        try:
            current_expiry = datetime.date.fromisoformat(row["expires_at"][:10])
            base = max(current_expiry, today)   # don't stack on past dates
        except Exception:
            base = today
        new_expiry  = (base + datetime.timedelta(days=add_days)).isoformat()
        days_left   = (datetime.date.fromisoformat(new_expiry) - today).days
        c.execute(
            "UPDATE licenses SET expires_at=?, note=? WHERE id=?",
            (new_expiry, f"renewed+{add_days}d", license_id)
        )
    row["expires_at"] = new_expiry
    row["days_left"]  = days_left
    return row

def get_license_by_id(license_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM licenses WHERE id=?", (license_id,)).fetchone()
        return dict(r) if r else None

# ── Bakong KHQR pending payments ──────────────────────────────────────────────

def _ensure_khqr_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS khqr_payments (
                order_id   INTEGER PRIMARY KEY,
                md5        TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)

def save_pending_payment(order_id: int, md5: str):
    _ensure_khqr_table()
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO khqr_payments (order_id,md5,status,created_at) VALUES (?,?,?,?)",
            (order_id, md5, "pending", now)
        )

def mark_pending_payment_paid(order_id: int):
    _ensure_khqr_table()
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE khqr_payments SET status='paid', updated_at=? WHERE order_id=?",
            (now, order_id)
        )

def mark_pending_payment_expired(order_id: int):
    _ensure_khqr_table()
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE khqr_payments SET status='expired', updated_at=? WHERE order_id=?",
            (now, order_id)
        )


# ── Backup settings ────────────────────────────────────────────────────────────

def _ensure_settings_table():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

def get_setting(key: str, default=None):
    _ensure_settings_table()
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

def set_setting(key: str, value: str):
    _ensure_settings_table()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))

def delete_setting(key: str):
    _ensure_settings_table()
    with _conn() as c:
        c.execute("DELETE FROM settings WHERE key=?", (key,))


# ── Collected URLs ─────────────────────────────────────────────────────────────

def _ensure_collected_urls_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS collected_urls (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                url       TEXT NOT NULL,
                user_id   INTEGER,
                machine_id TEXT,
                user_name TEXT DEFAULT 'unknown',
                added_at  TEXT NOT NULL DEFAULT (datetime('now', '+7 hours'))
            )
        """)


def store_collected_url(url: str, user_id: int = None, machine_id: str = None, user_name: str = "unknown"):
    """Store a collected URL with user info and machine_id."""
    _ensure_collected_urls_table()
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO collected_urls (url, user_id, machine_id, user_name, added_at) VALUES (?, ?, ?, ?, ?)",
            (url, user_id, machine_id, user_name, now)
        )


def get_collected_urls(user_id: int = None, machine_id: str = None, user_name: str = None, limit: int = 999999999) -> list[dict]:
    """Get collected URLs with optional filters, joined with licenses table by machine_id."""
    _ensure_collected_urls_table()
    with _conn() as c:
        # Check if licenses and users tables exist before joining
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        has_licenses = "licenses" in tables
        has_users = "users" in tables

        if has_licenses and has_users:
            query = """
                SELECT
                    cu.id,
                    cu.url,
                    COALESCE(cu.user_id, l.telegram_id) as user_id,
                    cu.machine_id,
                    COALESCE(l.machine_id, cu.machine_id) as verified_machine_id,
                    COALESCE(u.username, cu.user_name) as user_name,
                    cu.added_at
                FROM collected_urls cu
                LEFT JOIN licenses l ON cu.machine_id = l.machine_id
                LEFT JOIN users u ON l.telegram_id = u.telegram_id
                WHERE 1=1
            """
        elif has_licenses:
            query = """
                SELECT
                    cu.id, cu.url,
                    COALESCE(cu.user_id, l.telegram_id) as user_id,
                    cu.machine_id, cu.machine_id as verified_machine_id,
                    cu.user_name, cu.added_at
                FROM collected_urls cu
                LEFT JOIN licenses l ON cu.machine_id = l.machine_id
                WHERE 1=1
            """
        else:
            query = """
                SELECT
                    cu.id, cu.url, cu.user_id,
                    cu.machine_id, cu.machine_id as verified_machine_id,
                    cu.user_name, cu.added_at
                FROM collected_urls cu
                WHERE 1=1
            """
        params = []
        
        if user_id is not None:
            query += " AND cu.user_id=?"
            params.append(user_id)
        
        if machine_id is not None:
            query += " AND cu.machine_id=?"
            params.append(machine_id)
        
        if user_name is not None:
            query += " AND (u.username=? OR cu.user_name=?)"
            params.append(user_name)
            params.append(user_name)
        
        query += " ORDER BY cu.id DESC LIMIT ?"
        params.append(limit)
        
        rows = c.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def delete_collected_url(url_id: int):
    """Delete a collected URL by ID."""
    _ensure_collected_urls_table()
    with _conn() as c:
        c.execute("DELETE FROM collected_urls WHERE id=?", (url_id,))


def get_collected_urls_stats() -> dict:
    """Get statistics about collected URLs."""
    _ensure_collected_urls_table()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM collected_urls").fetchone()[0]
        unique_users = c.execute("SELECT COUNT(DISTINCT user_id) FROM collected_urls").fetchone()[0]
        unique_names = c.execute("SELECT COUNT(DISTINCT user_name) FROM collected_urls").fetchone()[0]
        return {
            "total": total,
            "unique_users_by_id": unique_users,
            "unique_users_by_name": unique_names
        }
