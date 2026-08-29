import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http import cookies
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

SOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
STATIC_ROOT = SOURCE_ROOT / "static"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RCT_DB_PATH", APP_DIR / "rct_clean.db"))
HOST = os.environ.get("RCT_HOST", "127.0.0.1")
PORT = int(os.environ.get("RCT_PORT", "8000"))
COOKIE_SECURE = os.environ.get("RCT_COOKIE_SECURE", "0") == "1"
SESSIONS = {}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}:{digest.hex()}"


def password_ok(password, stored):
    salt, expected = stored.split(":")
    actual = password_hash(password, bytes.fromhex(salt)).split(":")[1]
    return hmac.compare_digest(actual, expected)


ALLOCATIONS_SCHEMA = """(
  owner TEXT NOT NULL, position INTEGER NOT NULL, treatment TEXT NOT NULL,
  center INTEGER NOT NULL, patient_id TEXT, patient_initials TEXT, sex TEXT,
  age INTEGER, note TEXT, randomized_at TEXT, randomized_by TEXT,
  voided_at TEXT, voided_by TEXT, void_reason TEXT, legacy_position INTEGER,
  PRIMARY KEY(owner, position)
)"""
LEGACY_COLUMNS = ("center", "position", "treatment", "patient_id", "patient_initials", "sex", "age",
                  "note", "randomized_at", "randomized_by", "voided_at", "voided_by", "void_reason")


def make_block():
    # One permuted block at a time: A/B 1:1, length re-drawn per block so the boundary stays hidden.
    size = secrets.choice((4, 6))
    block = ["A"] * (size // 2) + ["B"] * (size // 2)
    secrets.SystemRandom().shuffle(block)
    return block


def migrate_allocations(conn):
    # Blocks used to belong to a center; they now belong to a doctor account. Only rows a patient has
    # actually seen are carried over — unused pre-generated slots were future sequence and are dropped.
    rows = conn.execute(f"SELECT {', '.join(LEGACY_COLUMNS)} FROM allocations "
                        "WHERE patient_id IS NOT NULL ORDER BY randomized_at, position").fetchall()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"CREATE TABLE allocations_new {ALLOCATIONS_SCHEMA}")
    next_position = {}
    for row in rows:
        owner = row["randomized_by"] or "legacy-unknown"
        position = next_position[owner] = next_position.get(owner, 0) + 1
        values = {name: row[name] for name in LEGACY_COLUMNS}
        values.update(owner=owner, position=position, legacy_position=row["position"])
        names = list(values)
        conn.execute(f"INSERT INTO allocations_new({', '.join(names)}) "
                     f"VALUES ({', '.join(':' + name for name in names)})", values)
    conn.execute("DROP TABLE allocations")
    conn.execute("ALTER TABLE allocations_new RENAME TO allocations")
    conn.commit()
    print(f"随机序列已按医生账号重建：迁移 {len(rows)} 条随机记录，未使用的预生成名额已丢弃")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS users (
          username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
          center INTEGER, display_name TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS centers (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS allocations {ALLOCATIONS_SCHEMA};
        """)
        allocation_columns = {row[1] for row in conn.execute("PRAGMA table_info(allocations)")}
        for name, sql_type in (("patient_initials", "TEXT"), ("sex", "TEXT"),
                               ("age", "INTEGER"), ("note", "TEXT"),
                               ("voided_at", "TEXT"), ("voided_by", "TEXT"),
                               ("void_reason", "TEXT")):
            if name not in allocation_columns:
                conn.execute(f"ALTER TABLE allocations ADD COLUMN {name} {sql_type}")
        if "owner" not in allocation_columns:
            migrate_allocations(conn)
        # Duplicate study numbers stay blocked per center, not per account: two doctors in one center
        # must never be able to randomize the same patient twice.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_alloc_center_patient ON allocations(center, patient_id)")
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            admin_username = os.environ.get("RCT_ADMIN_USERNAME", "admin")
            admin_password = os.environ.get("RCT_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
            conn.execute("INSERT INTO users VALUES (?, ?, NULL, ?, 1)",
                         (admin_username, password_hash(admin_password), "项目管理员"))
            if not os.environ.get("RCT_ADMIN_PASSWORD"):
                print(f"未设置 RCT_ADMIN_PASSWORD，已生成初始管理员密码：{admin_password}")
        desired_admin = os.environ.get("RCT_ADMIN_USERNAME")
        desired_password = os.environ.get("RCT_ADMIN_PASSWORD")
        if desired_admin and desired_password:
            admins = conn.execute("SELECT username FROM users WHERE is_admin=1").fetchall()
            desired_exists = conn.execute("SELECT 1 FROM users WHERE username=?", (desired_admin,)).fetchone()
            if len(admins) == 1 and not desired_exists:
                conn.execute("UPDATE users SET username=?, password_hash=? WHERE username=? AND is_admin=1",
                             (desired_admin, password_hash(desired_password), admins[0]["username"]))


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        relative = urlparse(path).path.lstrip("/") or "index.html"
        return str(STATIC_ROOT / relative)

    def json_body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError):
            return {}

    def send_json(self, data, status=200, headers=None):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def user(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie"))
        token = jar.get("session")
        return SESSIONS.get(token.value) if token else None

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            body = self.json_body()
            with db() as conn:
                row = conn.execute("SELECT * FROM users WHERE username=?", (body.get("username", ""),)).fetchone()
            if not row or not password_ok(body.get("password", ""), row["password_hash"]):
                return self.send_json({"error": "账号或密码错误"}, 401)
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = dict(row)
            secure = "; Secure" if COOKIE_SECURE else ""
            return self.send_json({"ok": True}, headers={"Set-Cookie": f"session={token}; HttpOnly; SameSite=Strict; Path=/{secure}"})
        if path == "/api/logout":
            jar = cookies.SimpleCookie(self.headers.get("Cookie"))
            if jar.get("session"):
                SESSIONS.pop(jar["session"].value, None)
            return self.send_json({"ok": True}, headers={"Set-Cookie": "session=; Max-Age=0; Path=/"})
        if path == "/api/randomize":
            user = self.user()
            if not user or user["is_admin"]:
                return self.send_json({"error": "请使用中心账号登录"}, 403)
            body = self.json_body()
            patient_id = body.get("patient_id", "").strip().upper()
            if not patient_id or len(patient_id) > 40 or not all(c.isalnum() or c in "-_" for c in patient_id):
                return self.send_json({"error": "研究编号只能包含字母、数字、- 和 _，最长40位"}, 400)
            initials = body.get("patient_initials", "").strip().upper()
            sex = body.get("sex", "").strip()
            note = body.get("note", "").strip()
            try:
                age = int(body["age"]) if str(body.get("age", "")).strip() else None
            except (TypeError, ValueError):
                return self.send_json({"error": "年龄格式不正确"}, 400)
            if len(initials) > 20 or sex not in ("", "男", "女", "其他/未知") or (age is not None and not 0 <= age <= 120) or len(note) > 200:
                return self.send_json({"error": "患者资料格式不正确，请检查长度和取值"}, 400)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                with db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute("SELECT 1 FROM allocations WHERE center=? AND patient_id=?",
                                            (user["center"], patient_id)).fetchone()
                    if existing:
                        return self.send_json({"error": "该研究编号已经随机，不能重复操作"}, 409)
                    slot = conn.execute("SELECT position, treatment FROM allocations WHERE owner=? AND patient_id IS NULL ORDER BY position LIMIT 1",
                                        (user["username"],)).fetchone()
                    if not slot:
                        # This account's block is exhausted: append the next one. No total cap.
                        start = conn.execute("SELECT COALESCE(MAX(position), 0) FROM allocations WHERE owner=?",
                                             (user["username"],)).fetchone()[0]
                        conn.executemany("INSERT INTO allocations(owner, position, treatment, center) VALUES (?, ?, ?, ?)",
                                         [(user["username"], start + offset, arm, user["center"])
                                          for offset, arm in enumerate(make_block(), start=1)])
                        slot = conn.execute("SELECT position, treatment FROM allocations WHERE owner=? AND patient_id IS NULL ORDER BY position LIMIT 1",
                                            (user["username"],)).fetchone()
                    conn.execute("UPDATE allocations SET patient_id=?, patient_initials=?, sex=?, age=?, note=?, randomized_at=?, randomized_by=? WHERE owner=? AND position=?",
                                 (patient_id, initials, sex, age, note, now, user["username"], user["username"], slot["position"]))
                # position is deliberately withheld: it would tell the doctor where the block ends.
                return self.send_json({"patient_id": patient_id, "treatment": slot["treatment"], "randomized_at": now})
            except sqlite3.IntegrityError:
                return self.send_json({"error": "该研究编号已经随机"}, 409)
            except sqlite3.OperationalError:
                return self.send_json({"error": "系统正忙，请稍后重试，不要重复点击"}, 503)
        if path == "/api/users":
            user = self.user()
            if not user or not user["is_admin"]:
                return self.send_json({"error": "仅管理员可以添加账号"}, 403)
            body = self.json_body()
            username = body.get("username", "").strip().lower()
            display_name = body.get("display_name", "").strip()
            password = body.get("password", "")
            center_name = body.get("center_name", "").strip()
            if not (4 <= len(username) <= 30) or not all(c.isalnum() or c in "-_" for c in username):
                return self.send_json({"error": "账号需为4～30位字母、数字、- 或 _"}, 400)
            if not center_name or len(center_name) > 60 or not display_name or len(display_name) > 40 or len(password) < 8:
                return self.send_json({"error": "请填写中心名称、姓名，并设置至少8位密码"}, 400)
            try:
                with db() as conn:
                    center_row = conn.execute("SELECT id FROM centers WHERE name=?", (center_name,)).fetchone()
                    if center_row:
                        center = center_row["id"]
                    else:
                        center = conn.execute("INSERT INTO centers(name) VALUES (?)", (center_name,)).lastrowid
                    # No sequence is pre-generated: each account gets its first block on its first randomization.
                    conn.execute("INSERT INTO users VALUES (?, ?, ?, ?, 0)",
                                 (username, password_hash(password), center, display_name))
            except sqlite3.IntegrityError:
                return self.send_json({"error": "该账号已存在"}, 409)
            return self.send_json({"ok": True, "username": username})
        if path == "/api/centers/delete":
            user = self.user()
            if not user or not user["is_admin"]:
                return self.send_json({"error": "仅管理员可以删除中心"}, 403)
            try:
                center = int(self.json_body().get("center"))
            except (TypeError, ValueError):
                return self.send_json({"error": "中心参数不正确"}, 400)
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT name FROM centers WHERE id=?", (center,)).fetchone()
                if not row:
                    return self.send_json({"error": "中心不存在"}, 404)
                used = conn.execute("SELECT COUNT(*) FROM allocations WHERE center=? AND patient_id IS NOT NULL", (center,)).fetchone()[0]
                if used:
                    return self.send_json({"error": "该中心已有患者随机记录，不能删除"}, 409)
                conn.execute("DELETE FROM users WHERE center=? AND is_admin=0", (center,))
                conn.execute("DELETE FROM allocations WHERE center=?", (center,))
                conn.execute("DELETE FROM centers WHERE id=?", (center,))
            return self.send_json({"ok": True})
        if path == "/api/records/delete":
            user = self.user()
            if not user:
                return self.send_json({"error": "未登录"}, 401)
            body = self.json_body()
            reason = body.get("reason", "").strip()
            patient_id = body.get("patient_id", "").strip().upper()
            try:
                center = int(body.get("center"))
            except (TypeError, ValueError):
                return self.send_json({"error": "记录参数不正确"}, 400)
            if not patient_id:
                return self.send_json({"error": "记录参数不正确"}, 400)
            if not reason or len(reason) > 200:
                return self.send_json({"error": "请填写200字以内的删除原因"}, 400)
            if not user["is_admin"] and user["center"] != center:
                return self.send_json({"error": "无权删除其他中心的患者"}, 403)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with db() as conn:
                row = conn.execute("SELECT owner, position, voided_at FROM allocations WHERE center=? AND patient_id=?",
                                   (center, patient_id)).fetchone()
                if not row:
                    return self.send_json({"error": "患者记录不存在"}, 404)
                if row["voided_at"]:
                    return self.send_json({"error": "该患者记录已经删除"}, 409)
                conn.execute("UPDATE allocations SET voided_at=?, voided_by=?, void_reason=? WHERE owner=? AND position=?",
                             (now, user["username"], reason, row["owner"], row["position"]))
            return self.send_json({"ok": True})
        self.send_error(404)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.send_json({"status": "ok"})
        if not path.startswith("/api/"):
            return super().do_GET()
        user = self.user()
        if not user:
            return self.send_json({"error": "未登录"}, 401)
        if path == "/api/me":
            center_name = None
            if user["center"]:
                with db() as conn:
                    row = conn.execute("SELECT name FROM centers WHERE id=?", (user["center"],)).fetchone()
                    center_name = row["name"] if row else None
            return self.send_json({"username": user["username"], "name": user["display_name"], "center": user["center"], "center_name": center_name, "is_admin": bool(user["is_admin"])})
        if path == "/api/records":
            with db() as conn:
                if user["is_admin"]:
                    rows = conn.execute("SELECT c.name center_name, a.center, a.owner, a.position, a.patient_id, a.patient_initials, a.sex, a.age, a.note, a.treatment, a.randomized_at, a.randomized_by FROM allocations a JOIN centers c ON c.id=a.center WHERE a.patient_id IS NOT NULL AND a.voided_at IS NULL ORDER BY c.name, a.owner, a.position").fetchall()
                else:
                    # owner and position are withheld: they would reveal how far into the block this account is.
                    rows = conn.execute("SELECT c.name center_name, a.center, a.patient_id, a.patient_initials, a.sex, a.age, a.note, a.treatment, a.randomized_at, a.randomized_by FROM allocations a JOIN centers c ON c.id=a.center WHERE a.center=? AND a.patient_id IS NOT NULL AND a.voided_at IS NULL ORDER BY a.randomized_at", (user["center"],)).fetchall()
            return self.send_json({"records": [dict(r) for r in rows]})
        if path == "/api/users":
            if not user["is_admin"]:
                return self.send_json({"error": "仅管理员可以查看账号"}, 403)
            with db() as conn:
                rows = conn.execute("SELECT u.username, u.display_name, u.center, c.name center_name FROM users u JOIN centers c ON c.id=u.center WHERE u.is_admin=0 ORDER BY c.name, u.username").fetchall()
            return self.send_json({"users": [dict(r) for r in rows]})
        if path == "/api/progress":
            with db() as conn:
                # LEFT JOIN: nothing is pre-generated any more, so a center with 0 patients has no allocation rows.
                rows = conn.execute("SELECT c.id center, c.name center_name, COALESCE(SUM(CASE WHEN a.patient_id IS NOT NULL AND a.voided_at IS NULL THEN 1 ELSE 0 END), 0) used FROM centers c LEFT JOIN allocations a ON a.center=c.id GROUP BY c.id, c.name ORDER BY c.name").fetchall()
            visible = rows if user["is_admin"] else [r for r in rows if r["center"] == user["center"]]
            return self.send_json({"progress": [dict(r) for r in visible]})
        if path == "/api/export":
            patient_columns = ["患者研究编号", "姓名缩写", "性别", "年龄", "备注", "分组", "随机时间(UTC)", "操作者"]
            with db() as conn:
                if user["is_admin"]:
                    rows = conn.execute("SELECT c.name, a.owner, a.position, a.patient_id, a.patient_initials, a.sex, a.age, a.note, a.treatment, a.randomized_at, a.randomized_by FROM allocations a JOIN centers c ON c.id=a.center WHERE a.patient_id IS NOT NULL AND a.voided_at IS NULL ORDER BY c.name, a.owner, a.position").fetchall()
                    header = ["中心", "医生账号", "账号内序号"] + patient_columns
                    filename = "rct-all-records.csv"
                else:
                    rows = conn.execute("SELECT c.name, a.patient_id, a.patient_initials, a.sex, a.age, a.note, a.treatment, a.randomized_at, a.randomized_by FROM allocations a JOIN centers c ON c.id=a.center WHERE a.center=? AND a.patient_id IS NOT NULL AND a.voided_at IS NULL ORDER BY a.randomized_at", (user["center"],)).fetchall()
                    header = ["中心"] + patient_columns
                    filename = "rct-center-records.csv"
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows([tuple(r) for r in rows])
            payload = ("\ufeff" + stream.getvalue()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f"attachment; filename={filename}")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        self.send_error(404)


if __name__ == "__main__":
    init_db()
    print(f"RCT系统已启动：http://{HOST}:{PORT}")
    print("管理员账号和密码由 RCT_ADMIN_USERNAME / RCT_ADMIN_PASSWORD 配置")
    print("使用结束后请关闭此窗口。")
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
