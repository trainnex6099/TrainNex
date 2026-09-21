import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "summit.db"

DEFAULT_MESSAGES = {
    "trainer_offer": "{trainee} has been approved and is ready for training.\n\n🔗 [View Approval & Results]({approval_url})\n\n⏰ This offer will expire {expires}.",
    "trainer_claimed": "You have claimed {trainee} for Field Training.\n\nPlease contact the trainee and begin the training process.",
    "trainee_accepted": "Your application has been approved. A Field Training Officer will contact you when one becomes available.",
    "offer_expired": "You did not claim {trainee} in time. The trainee has been passed to the next trainer.",
    "fallback_announcement": "{trainee} has been approved and is awaiting a Field Training Officer.\n\nAny Field Training Officer may claim this trainee.\n\n🔗 [View Approval & Results]({approval_url})",
    "claim_notification": "{trainer} has claimed {trainee} for Field Training.",
}

CONFIG_FIELDS = [
    "application_server_id", "approval_channel_id", "melony_bot_id",
    "training_server_id", "fto_role_id", "announcement_channel_id",
    "claim_notification_channel_id", "fto_commander_role_id",
    "fto_overseer_role_id",
]


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def _column_names(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(db, table, name, declaration):
    if name not in _column_names(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def init_db():
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS trainnex_config (
            id INTEGER PRIMARY KEY CHECK(id=1),
            application_server_id TEXT DEFAULT '',
            approval_channel_id TEXT DEFAULT '',
            melony_bot_id TEXT DEFAULT '',
            training_server_id TEXT DEFAULT '',
            fto_role_id TEXT DEFAULT '',
            announcement_channel_id TEXT DEFAULT '',
            claim_notification_channel_id TEXT DEFAULT '',
            fto_commander_role_id TEXT DEFAULT '',
            fto_overseer_role_id TEXT DEFAULT ''
        )""")
        db.execute("INSERT OR IGNORE INTO trainnex_config(id) VALUES(1)")
        db.execute("""CREATE TABLE IF NOT EXISTS message_templates (
            message_key TEXT PRIMARY KEY,
            content TEXT NOT NULL
        )""")
        for key, value in DEFAULT_MESSAGES.items():
            db.execute(
                "INSERT OR IGNORE INTO message_templates(message_key,content) VALUES(?,?)",
                (key, value),
            )
        db.execute("""CREATE TABLE IF NOT EXISTS discord_cache (
            guild_id TEXT NOT NULL,
            item_type TEXT NOT NULL,
            item_id TEXT NOT NULL,
            name TEXT NOT NULL,
            position INTEGER DEFAULT 0,
            PRIMARY KEY(guild_id,item_type,item_id)
        )""")


def get_trainnex_config():
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM trainnex_config WHERE id=1").fetchone()
    return dict(row) if row else {}


def save_trainnex_config(values):
    init_db()
    clean = {k: str(values.get(k, "")).strip() for k in CONFIG_FIELDS}
    sets = ", ".join(f"{k}=?" for k in CONFIG_FIELDS)
    with connect() as db:
        db.execute(
            f"UPDATE trainnex_config SET {sets} WHERE id=1",
            [clean[k] for k in CONFIG_FIELDS],
        )


def get_message_template(key):
    init_db()
    with connect() as db:
        row = db.execute(
            "SELECT content FROM message_templates WHERE message_key=?", (key,)
        ).fetchone()
    return row["content"] if row else None


def get_all_messages():
    init_db()
    with connect() as db:
        rows = db.execute(
            "SELECT message_key,content FROM message_templates ORDER BY message_key"
        ).fetchall()
    return {r["message_key"]: r["content"] for r in rows}


def save_message_template(key, content):
    init_db()
    with connect() as db:
        db.execute(
            """INSERT INTO message_templates(message_key,content) VALUES(?,?)
               ON CONFLICT(message_key) DO UPDATE SET content=excluded.content""",
            (key, content),
        )


def replace_cache(guild_id, item_type, items):
    init_db()
    with connect() as db:
        db.execute(
            "DELETE FROM discord_cache WHERE guild_id=? AND item_type=?",
            (str(guild_id), item_type),
        )
        db.executemany(
            "INSERT INTO discord_cache(guild_id,item_type,item_id,name,position) VALUES(?,?,?,?,?)",
            [
                (str(guild_id), item_type, str(x["id"]), x["name"], int(x.get("position", 0)))
                for x in items
            ],
        )


def get_cache(guild_id, item_type):
    init_db()
    with connect() as db:
        rows = db.execute(
            """SELECT item_id AS id,name,position FROM discord_cache
               WHERE guild_id=? AND item_type=? ORDER BY position DESC,name""",
            (str(guild_id), item_type),
        ).fetchall()
    return [dict(r) for r in rows]


# ---- Summit Shifts -------------------------------------------------------
def init_shift_db():
    init_db()
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS shift_settings (
            guild_id TEXT PRIMARY KEY,
            admin_role_id TEXT DEFAULT ''
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS shift_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            name TEXT NOT NULL,
            on_shift_role_id TEXT DEFAULT '',
            on_break_role_id TEXT DEFAULT '',
            log_channel_id TEXT DEFAULT '',
            is_default INTEGER DEFAULT 0,
            UNIQUE(guild_id,name)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS shifts (
            shift_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL,
            rank_name TEXT DEFAULT '',
            shift_type_id INTEGER NOT NULL,
            shift_type_name TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            break_started_at INTEGER,
            break_seconds INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            admin_adjustment TEXT DEFAULT '',
            adjusted_by TEXT DEFAULT '',
            adjusted_at INTEGER
        )""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_active ON shifts(guild_id,user_id,status)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_guild_type ON shifts(guild_id,shift_type_name)"
        )


def get_shift_settings(guild_id):
    init_shift_db()
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO shift_settings(guild_id) VALUES(?)", (str(guild_id),)
        )
        row = db.execute(
            "SELECT * FROM shift_settings WHERE guild_id=?", (str(guild_id),)
        ).fetchone()
    return dict(row)


def save_shift_settings(guild_id, values):
    init_shift_db()
    with connect() as db:
        db.execute(
            """INSERT INTO shift_settings(guild_id,admin_role_id) VALUES(?,?)
               ON CONFLICT(guild_id) DO UPDATE SET admin_role_id=excluded.admin_role_id""",
            (str(guild_id), str(values.get("admin_role_id", "")).strip()),
        )


def list_shift_guild_ids():
    init_shift_db()
    with connect() as db:
        rows = db.execute("SELECT DISTINCT guild_id FROM shift_types").fetchall()
    return [int(r["guild_id"]) for r in rows if str(r["guild_id"]).isdigit()]


def list_shift_types(guild_id):
    init_shift_db()
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM shift_types WHERE guild_id=? ORDER BY is_default DESC,name",
            (str(guild_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_shift_type(type_id):
    init_shift_db()
    with connect() as db:
        row = db.execute("SELECT * FROM shift_types WHERE id=?", (int(type_id),)).fetchone()
    return dict(row) if row else None


def get_shift_type_by_name(guild_id, name):
    init_shift_db()
    with connect() as db:
        row = db.execute(
            "SELECT * FROM shift_types WHERE guild_id=? AND lower(name)=lower(?) LIMIT 1",
            (str(guild_id), str(name).strip()),
        ).fetchone()
    return dict(row) if row else None


def save_shift_type(guild_id, values):
    init_shift_db()
    type_id = values.get("id")
    default = 1 if values.get("is_default") else 0
    with connect() as db:
        if default:
            db.execute("UPDATE shift_types SET is_default=0 WHERE guild_id=?", (str(guild_id),))
        if type_id:
            db.execute(
                """UPDATE shift_types
                   SET name=?,on_shift_role_id=?,on_break_role_id=?,log_channel_id=?,is_default=?
                   WHERE id=? AND guild_id=?""",
                (
                    values.get("name", "").strip(),
                    str(values.get("on_shift_role_id", "")),
                    str(values.get("on_break_role_id", "")),
                    str(values.get("log_channel_id", "")),
                    default,
                    int(type_id),
                    str(guild_id),
                ),
            )
            return int(type_id)
        cur = db.execute(
            """INSERT INTO shift_types(guild_id,name,on_shift_role_id,on_break_role_id,log_channel_id,is_default)
               VALUES(?,?,?,?,?,?)""",
            (
                str(guild_id),
                values.get("name", "").strip(),
                str(values.get("on_shift_role_id", "")),
                str(values.get("on_break_role_id", "")),
                str(values.get("log_channel_id", "")),
                default,
            ),
        )
        return cur.lastrowid


def delete_shift_type(guild_id, type_id):
    init_shift_db()
    with connect() as db:
        db.execute(
            "DELETE FROM shift_types WHERE guild_id=? AND id=?",
            (str(guild_id), int(type_id)),
        )


def new_shift_id():
    return "S-" + secrets.token_hex(4).upper()


def get_active_shift(guild_id, user_id):
    init_shift_db()
    with connect() as db:
        row = db.execute(
            """SELECT * FROM shifts
               WHERE guild_id=? AND user_id=? AND status IN ('active','break')
               ORDER BY started_at DESC LIMIT 1""",
            (str(guild_id), str(user_id)),
        ).fetchone()
    return dict(row) if row else None


def list_active_shifts(guild_id):
    init_shift_db()
    with connect() as db:
        rows = db.execute(
            """SELECT * FROM shifts
               WHERE guild_id=? AND status IN ('active','break')
               ORDER BY started_at""",
            (str(guild_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def start_shift(guild_id, user_id, username, rank_name, type_id):
    shift_type = get_shift_type(type_id)
    if not shift_type or str(shift_type["guild_id"]) != str(guild_id):
        raise ValueError("Invalid shift type")
    if get_active_shift(guild_id, user_id):
        raise ValueError("You already have an active shift")
    shift_id = new_shift_id()
    now = int(time.time())
    with connect() as db:
        db.execute(
            """INSERT INTO shifts(
                   shift_id,guild_id,user_id,username,rank_name,shift_type_id,
                   shift_type_name,started_at,status
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                shift_id,
                str(guild_id),
                str(user_id),
                username,
                rank_name,
                int(type_id),
                shift_type["name"],
                now,
                "active",
            ),
        )
    return get_shift(shift_id)


def get_shift(shift_id):
    init_shift_db()
    with connect() as db:
        row = db.execute("SELECT * FROM shifts WHERE shift_id=?", (shift_id,)).fetchone()
    return dict(row) if row else None


def get_last_shift(guild_id, user_id):
    init_shift_db()
    with connect() as db:
        row = db.execute(
            """SELECT * FROM shifts
               WHERE guild_id=? AND user_id=?
               ORDER BY started_at DESC LIMIT 1""",
            (str(guild_id), str(user_id)),
        ).fetchone()
    return dict(row) if row else None


def get_shift_user_stats(guild_id, user_id):
    init_shift_db()
    now = int(time.time())
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM shifts WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        ).fetchall()
    total = 0
    for row in rows:
        s = dict(row)
        end = int(s.get("ended_at") or now)
        breaks = int(s.get("break_seconds") or 0)
        if s.get("status") == "break" and s.get("break_started_at"):
            breaks += max(0, now - int(s["break_started_at"]))
        total += max(0, end - int(s["started_at"]) - breaks)
    count = len(rows)
    return {
        "shift_count": count,
        "total_seconds": total,
        "average_seconds": (total // count) if count else 0,
    }


def set_shift_break(shift_id, on_break):
    shift = get_shift(shift_id)
    now = int(time.time())
    if not shift:
        raise ValueError("Shift not found")
    with connect() as db:
        if on_break:
            if shift["status"] != "active":
                raise ValueError("Shift is not active")
            db.execute(
                "UPDATE shifts SET status='break',break_started_at=? WHERE shift_id=?",
                (now, shift_id),
            )
        else:
            if shift["status"] != "break":
                raise ValueError("Shift is not on break")
            extra = max(0, now - int(shift["break_started_at"] or now))
            db.execute(
                """UPDATE shifts
                   SET status='active',break_seconds=break_seconds+?,break_started_at=NULL
                   WHERE shift_id=?""",
                (extra, shift_id),
            )
    return get_shift(shift_id)


def end_shift(shift_id, adjustment="", adjusted_by=""):
    shift = get_shift(shift_id)
    now = int(time.time())
    if not shift:
        raise ValueError("Shift not found")
    extra = (
        max(0, now - int(shift["break_started_at"] or now))
        if shift["status"] == "break"
        else 0
    )
    with connect() as db:
        db.execute(
            """UPDATE shifts
               SET status='ended',ended_at=?,break_seconds=break_seconds+?,break_started_at=NULL,
                   admin_adjustment=?,adjusted_by=?,adjusted_at=?
               WHERE shift_id=?""",
            (
                now,
                extra,
                adjustment,
                adjusted_by,
                now if adjustment else None,
                shift_id,
            ),
        )
    return get_shift(shift_id)


def shift_leaderboard(guild_id, limit=25, shift_type_name=None):
    init_shift_db()
    now = int(time.time())
    params = [str(guild_id)]
    sql = "SELECT * FROM shifts WHERE guild_id=?"
    if shift_type_name:
        sql += " AND lower(shift_type_name)=lower(?)"
        params.append(str(shift_type_name))
    with connect() as db:
        rows = db.execute(sql, params).fetchall()

    totals = {}
    for row in rows:
        s = dict(row)
        end = int(s.get("ended_at") or now)
        breaks = int(s.get("break_seconds") or 0)
        if s.get("status") == "break" and s.get("break_started_at"):
            breaks += max(0, now - int(s["break_started_at"]))
        worked = max(0, end - int(s["started_at"]) - breaks)
        uid = str(s["user_id"])
        entry = totals.setdefault(uid, {"user_id": uid, "seconds": 0, "shift_count": 0})
        entry["seconds"] += worked
        entry["shift_count"] += 1

    result = sorted(totals.values(), key=lambda r: r["seconds"], reverse=True)
    return result[: int(limit)]


# ---- Summit Leave of Absence --------------------------------------------
def init_loa_db():
    init_db()
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS loa_settings (
            guild_id TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            request_channel_id TEXT DEFAULT '',
            log_channel_id TEXT DEFAULT '',
            on_leave_role_id TEXT DEFAULT ''
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS loa_requests (
            loa_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL,
            reason TEXT NOT NULL,
            duration_text TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL,
            start_at INTEGER NOT NULL,
            end_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at INTEGER NOT NULL,
            reviewed_at INTEGER,
            reviewed_by TEXT DEFAULT '',
            denial_reason TEXT DEFAULT '',
            ended_at INTEGER,
            ended_by TEXT DEFAULT '',
            request_message_id TEXT DEFAULT '',
            request_channel_id TEXT DEFAULT ''
        )""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_loa_user ON loa_requests(guild_id,user_id,requested_at DESC)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_loa_due ON loa_requests(status,end_at)"
        )


def list_loa_guild_ids():
    init_loa_db()
    with connect() as db:
        rows = db.execute("SELECT guild_id FROM loa_settings WHERE enabled=1").fetchall()
    return [int(r["guild_id"]) for r in rows if str(r["guild_id"]).isdigit()]


def get_loa_settings(guild_id):
    init_loa_db()
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO loa_settings(guild_id) VALUES(?)", (str(guild_id),)
        )
        row = db.execute(
            "SELECT * FROM loa_settings WHERE guild_id=?", (str(guild_id),)
        ).fetchone()
    return dict(row)


def save_loa_settings(guild_id, values):
    init_loa_db()
    with connect() as db:
        db.execute(
            """INSERT INTO loa_settings(guild_id,enabled,request_channel_id,log_channel_id,on_leave_role_id)
               VALUES(?,?,?,?,?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   enabled=excluded.enabled,
                   request_channel_id=excluded.request_channel_id,
                   log_channel_id=excluded.log_channel_id,
                   on_leave_role_id=excluded.on_leave_role_id""",
            (
                str(guild_id),
                1 if values.get("enabled", True) else 0,
                str(values.get("request_channel_id", "")).strip(),
                str(values.get("log_channel_id", "")).strip(),
                str(values.get("on_leave_role_id", "")).strip(),
            ),
        )


def new_loa_id():
    return "LOA-" + secrets.token_hex(4).upper()


def create_loa_request(guild_id, user_id, username, duration_text, duration_seconds, reason):
    init_loa_db()
    if get_current_loa(guild_id, user_id):
        raise ValueError("You already have a pending or active leave of absence.")
    now = int(time.time())
    loa_id = new_loa_id()
    with connect() as db:
        db.execute(
            """INSERT INTO loa_requests(
                   loa_id,guild_id,user_id,username,reason,duration_text,duration_seconds,
                   start_at,end_at,status,requested_at
               ) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)""",
            (
                loa_id,
                str(guild_id),
                str(user_id),
                username,
                reason.strip(),
                duration_text.strip().upper(),
                int(duration_seconds),
                now,
                now + int(duration_seconds),
                now,
            ),
        )
    return get_loa(loa_id)


def get_loa(loa_id):
    init_loa_db()
    with connect() as db:
        row = db.execute("SELECT * FROM loa_requests WHERE loa_id=?", (loa_id,)).fetchone()
    return dict(row) if row else None


def get_current_loa(guild_id, user_id):
    init_loa_db()
    with connect() as db:
        row = db.execute(
            """SELECT * FROM loa_requests
               WHERE guild_id=? AND user_id=? AND status IN ('pending','approved')
               ORDER BY requested_at DESC LIMIT 1""",
            (str(guild_id), str(user_id)),
        ).fetchone()
    return dict(row) if row else None


def list_user_loas(guild_id, user_id, limit=25):
    init_loa_db()
    with connect() as db:
        rows = db.execute(
            """SELECT * FROM loa_requests
               WHERE guild_id=? AND user_id=?
               ORDER BY requested_at DESC LIMIT ?""",
            (str(guild_id), str(user_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def list_pending_loas():
    init_loa_db()
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM loa_requests WHERE status='pending' AND request_message_id!=''"
        ).fetchall()
    return [dict(r) for r in rows]


def list_active_loas(guild_id):
    init_loa_db()
    with connect() as db:
        rows = db.execute(
            """SELECT * FROM loa_requests
               WHERE guild_id=? AND status='approved'
               ORDER BY end_at""",
            (str(guild_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def set_loa_request_message(loa_id, channel_id, message_id):
    init_loa_db()
    with connect() as db:
        db.execute(
            "UPDATE loa_requests SET request_channel_id=?,request_message_id=? WHERE loa_id=?",
            (str(channel_id), str(message_id), loa_id),
        )
    return get_loa(loa_id)


def approve_loa(loa_id, reviewer_id):
    init_loa_db()
    now = int(time.time())
    with connect() as db:
        cur = db.execute(
            """UPDATE loa_requests
               SET status='approved',reviewed_at=?,reviewed_by=?,denial_reason=''
               WHERE loa_id=? AND status='pending'""",
            (now, str(reviewer_id), loa_id),
        )
        if cur.rowcount != 1:
            raise ValueError("This leave request is no longer pending.")
    return get_loa(loa_id)


def deny_loa(loa_id, reviewer_id, denial_reason):
    init_loa_db()
    now = int(time.time())
    with connect() as db:
        cur = db.execute(
            """UPDATE loa_requests
               SET status='denied',reviewed_at=?,reviewed_by=?,denial_reason=?
               WHERE loa_id=? AND status='pending'""",
            (now, str(reviewer_id), denial_reason.strip(), loa_id),
        )
        if cur.rowcount != 1:
            raise ValueError("This leave request is no longer pending.")
    return get_loa(loa_id)


def end_loa(loa_id, ended_by="system", ended_at=None):
    init_loa_db()
    now = int(ended_at or time.time())
    with connect() as db:
        cur = db.execute(
            """UPDATE loa_requests
               SET status='ended',ended_at=?,ended_by=?
               WHERE loa_id=? AND status='approved'""",
            (now, str(ended_by), loa_id),
        )
        if cur.rowcount != 1:
            raise ValueError("This leave of absence is not active.")
    return get_loa(loa_id)


def list_due_loas(now=None):
    init_loa_db()
    now = int(now or time.time())
    with connect() as db:
        rows = db.execute(
            """SELECT * FROM loa_requests
               WHERE status='approved' AND end_at<=?
               ORDER BY end_at""",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


init_shift_db()
init_loa_db()
