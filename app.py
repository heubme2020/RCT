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
SESSION_TIMEOUT = int(os.environ.get("RCT_SESSION_TIMEOUT", "1800"))  # 默认30分钟
RANDOMIZATION_ALGORITHM = "sha256-random-block4-6-v3"
LEGACY_FIXED6_ALGORITHM = "sha256-fixed-block6-v2"
APP_VERSION = "2026.08.29-r5"
APP_UPDATED_AT = "2026-08-29"
PARTICIPANT_STATUSES = {"已随机", "治疗中", "完成随访", "脱落", "撤回知情同意", "失访", "方案违背", "误随机"}


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


def cleanup_sessions():
    now = datetime.now(timezone.utc).timestamp()
    expired = [t for t, v in SESSIONS.items() if now - v["created"] > SESSION_TIMEOUT]
    for t in expired:
        del SESSIONS[t]


class SeededRandom:
    """Version-stable deterministic generator used only for randomization lists."""

    def __init__(self, seed):
        self.seed = seed.encode("utf-8")
        self.counter = 0

    def randbelow(self, upper):
        limit = (1 << 256) - ((1 << 256) % upper)
        while True:
            block = hashlib.sha256(self.seed + self.counter.to_bytes(8, "big")).digest()
            self.counter += 1
            value = int.from_bytes(block, "big")
            if value < limit:
                return value % upper

    def shuffle(self, values):
        for index in range(len(values) - 1, 0, -1):
            other = self.randbelow(index + 1)
            values[index], values[other] = values[other], values[index]


ALLOCATIONS_SCHEMA = """(
  owner TEXT NOT NULL, position INTEGER NOT NULL, treatment TEXT NOT NULL,
  center INTEGER NOT NULL, patient_id TEXT, patient_initials TEXT, sex TEXT,
  age INTEGER, note TEXT, randomized_at TEXT, randomized_by TEXT,
  voided_at TEXT, voided_by TEXT, void_reason TEXT,
  consent_confirmed INTEGER, eligibility_confirmed INTEGER,
  participant_status TEXT NOT NULL DEFAULT '已随机',
  status_changed_at TEXT, status_changed_by TEXT, status_reason TEXT,
  block_index INTEGER, legacy_position INTEGER,
  PRIMARY KEY(owner, position)
)"""
LEGACY_COLUMNS = ("center", "position", "treatment", "patient_id", "patient_initials", "sex", "age",
                  "note", "randomized_at", "randomized_by", "voided_at", "voided_by", "void_reason",
                  "consent_confirmed", "eligibility_confirmed", "participant_status",
                  "status_changed_at", "status_changed_by", "status_reason")


def make_block(seed, username, block_index):
    rng = SeededRandom(f"{seed}\0doctor\0{username}\0block4or6\0{block_index}")
    size = (4, 6)[rng.randbelow(2)]
    block = ["A"] * (size // 2) + ["B"] * (size // 2)
    rng.shuffle(block)
    return block


def make_fixed6_block(seed, username, block_index):
    rng = SeededRandom(f"{seed}\0doctor\0{username}\0block6\0{block_index}")
    block = ["A"] * 3 + ["B"] * 3
    rng.shuffle(block)
    return block


def make_sequence(seed, username, block_count=1):
    return [arm for block_index in range(block_count)
            for arm in make_block(seed, username, block_index)]


def sequence_hash(sequence):
    return hashlib.sha256("".join(sequence).encode("ascii")).hexdigest()


def extend_sequence(conn, owner, center):
    metadata = conn.execute(
        "SELECT random_seed, algorithm_version FROM centers WHERE id=?", (center,)
    ).fetchone()
    if not metadata or not metadata["random_seed"]:
        return False
    last_position = conn.execute(
        "SELECT COALESCE(MAX(position), 0) FROM allocations WHERE owner=?", (owner,)
    ).fetchone()[0]
    if metadata["algorithm_version"] == RANDOMIZATION_ALGORITHM:
        block_index = conn.execute(
            "SELECT COALESCE(MAX(block_index), -1) + 1 FROM allocations WHERE owner=?", (owner,)
        ).fetchone()[0]
        block = make_block(metadata["random_seed"], owner, block_index)
    elif metadata["algorithm_version"] == LEGACY_FIXED6_ALGORITHM:
        block_index = last_position // 6
        block = make_fixed6_block(metadata["random_seed"], owner, block_index)
    else:
        return False
    conn.executemany(
        "INSERT INTO allocations(owner, position, treatment, center, block_index) VALUES (?, ?, ?, ?, ?)",
        [(owner, last_position + offset, arm, center, block_index) for offset, arm in enumerate(block, 1)]
    )
    return True


def audit(conn, actor, action, entity_type, entity_id, details=None):
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    previous = conn.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    previous_hash = previous[0] if previous else ""
    material = "|".join((previous_hash, created_at, actor or "", action, entity_type,
                         str(entity_id or ""), details_json))
    entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    conn.execute("INSERT INTO audit_log(created_at, actor, action, entity_type, entity_id, details, previous_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (created_at, actor, action, entity_type, str(entity_id or ""), details_json, previous_hash, entry_hash))


def migrate_allocations(conn):
    # Blocks used to belong to a center; they now belong to a doctor account. Only rows a patient has
    # actually seen are carried over — unused pre-generated slots were future sequence and are dropped.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = conn.execute(
        f"SELECT {', '.join(LEGACY_COLUMNS)} FROM allocations "
        "WHERE patient_id IS NOT NULL ORDER BY randomized_at, position").fetchall()
    missing_seed = [r["id"] for r in conn.execute(
        "SELECT id FROM centers WHERE random_seed IS NULL").fetchall()]
    conn.execute("BEGIN IMMEDIATE")
    for center in missing_seed:
        conn.execute(
            "UPDATE centers SET random_seed=?, algorithm_version=?, sequence_created_at=?, sequence_created_by=? WHERE id=?",
            (secrets.token_urlsafe(24), RANDOMIZATION_ALGORITHM, now, "system-migration", center))
    conn.execute(f"CREATE TABLE allocations_new {ALLOCATIONS_SCHEMA}")
    next_position = {}
    for row in rows:
        owner = row["randomized_by"] or "legacy-unknown"
        position = next_position[owner] = next_position.get(owner, 0) + 1
        values = {name: row[name] for name in LEGACY_COLUMNS}
        values.update(owner=owner, position=position, legacy_position=row["position"], block_index=None)
        names = list(values)
        conn.execute(f"INSERT INTO allocations_new({', '.join(names)}) "
                     f"VALUES ({', '.join(':' + name for name in names)})", values)
    conn.execute("DROP TABLE allocations")
    conn.execute("ALTER TABLE allocations_new RENAME TO allocations")
    conn.commit()
    print(f"随机序列已按医生账号重建：迁移 {len(rows)} 条随机记录，补齐 {len(missing_seed)} 个中心种子")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS users (
          username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
          center INTEGER, display_name TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS centers (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
          random_seed TEXT, algorithm_version TEXT, sequence_hash TEXT,
          sequence_created_at TEXT, sequence_created_by TEXT, next_block_index INTEGER
        );
        CREATE TABLE IF NOT EXISTS allocations {ALLOCATIONS_SCHEMA};
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, actor TEXT,
          action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
          details TEXT NOT NULL, previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL UNIQUE
        );
        """)
        center_columns = {row[1] for row in conn.execute("PRAGMA table_info(centers)")}
        for name, sql_type in (("random_seed", "TEXT"), ("algorithm_version", "TEXT"),
                               ("sequence_hash", "TEXT"), ("sequence_created_at", "TEXT"),
                               ("sequence_created_by", "TEXT"), ("next_block_index", "INTEGER")):
            if name not in center_columns:
                conn.execute(f"ALTER TABLE centers ADD COLUMN {name} {sql_type}")
        allocation_columns = {row[1] for row in conn.execute("PRAGMA table_info(allocations)")}
        for name, sql_type in (("patient_initials", "TEXT"), ("sex", "TEXT"),
                               ("age", "INTEGER"), ("note", "TEXT"),
                               ("voided_at", "TEXT"), ("voided_by", "TEXT"),
                               ("void_reason", "TEXT"), ("consent_confirmed", "INTEGER"),
                               ("eligibility_confirmed", "INTEGER"),
                               ("participant_status", "TEXT NOT NULL DEFAULT '已随机'"),
                               ("status_changed_at", "TEXT"), ("status_changed_by", "TEXT"),
                               ("status_reason", "TEXT")):
            if name not in allocation_columns:
                conn.execute(f"ALTER TABLE allocations ADD COLUMN {name} {sql_type}")
        if "owner" not in allocation_columns:
            migrate_allocations(conn)
        # Duplicate study numbers stay blocked per center, not per account: two doctors in one center
        # must never be able to randomize the same patient twice.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_alloc_center_patient ON allocations(center, patient_id)")
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            admin_username = os.environ.get("RCT_ADMIN_USERNAME", "admin")
            admin_password = os.environ.get("RCT_ADMIN_PASSWORD", "admin123")
            conn.execute("INSERT INTO users VALUES (?, ?, NULL, ?, 1)",
                         (admin_username, password_hash(admin_password), "项目管理员"))
        desired_admin = os.environ.get("RCT_ADMIN_USERNAME")
        desired_password = os.environ.get("RCT_ADMIN_PASSWORD")
        if desired_admin:
            admins = conn.execute("SELECT username FROM users WHERE is_admin=1").fetchall()
            desired_exists = conn.execute("SELECT 1 FROM users WHERE username=?", (desired_admin,)).fetchone()
            if len(admins) == 1 and not desired_exists:
                conn.execute("UPDATE users SET username=?, password_hash=? WHERE username=? AND is_admin=1",
                             (desired_admin, password_hash(desired_password or "admin123"), admins[0]["username"]))


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        if urlparse(self.path).path.endswith((".html", ".js", ".css", "/")):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

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
        if not token or token.value not in SESSIONS:
            return None
        entry = SESSIONS[token.value]
        if datetime.now(timezone.utc).timestamp() - entry["created"] > SESSION_TIMEOUT:
            del SESSIONS[token.value]
            return None
        return entry["data"]

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            cleanup_sessions()
            body = self.json_body()
            with db() as conn:
                row = conn.execute("SELECT * FROM users WHERE username=?", (body.get("username", ""),)).fetchone()
            if not row or not password_ok(body.get("password", ""), row["password_hash"]):
                with db() as conn:
                    audit(conn, body.get("username", ""), "LOGIN_FAILED", "session", "", {})
                return self.send_json({"error": "账号或密码错误"}, 401)
            with db() as conn:
                audit(conn, row["username"], "LOGIN_SUCCESS", "session", "", {})
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = {"data": dict(row), "created": datetime.now(timezone.utc).timestamp()}
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
            consent_confirmed = body.get("consent_confirmed") is True
            eligibility_confirmed = body.get("eligibility_confirmed") is True
            if not consent_confirmed or not eligibility_confirmed:
                return self.send_json({"error": "随机前必须确认已签署知情同意且符合全部入组条件"}, 400)
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
                    existing = conn.execute("SELECT * FROM allocations WHERE center=? AND patient_id=?",
                                            (user["center"], patient_id)).fetchone()
                    if existing:
                        return self.send_json({"error": "该研究编号已经随机，不能重复操作"}, 409)
                    slot = conn.execute("SELECT position, treatment FROM allocations WHERE owner=? AND patient_id IS NULL ORDER BY position LIMIT 1",
                                        (user["username"],)).fetchone()
                    if not slot and extend_sequence(conn, user["username"], user["center"]):
                        slot = conn.execute("SELECT position, treatment FROM allocations WHERE owner=? AND patient_id IS NULL ORDER BY position LIMIT 1",
                                            (user["username"],)).fetchone()
                    if not slot:
                        return self.send_json({"error": "本账号随机序列生成失败，请联系随机化管理员"}, 409)
                    conn.execute("UPDATE allocations SET patient_id=?, patient_initials=?, sex=?, age=?, note=?, randomized_at=?, randomized_by=?, consent_confirmed=1, eligibility_confirmed=1, participant_status='已随机' WHERE owner=? AND position=?",
                                 (patient_id, initials, sex, age, note, now, user["username"], user["username"], slot["position"]))
                    audit(conn, user["username"], "RANDOMIZE", "allocation",
                          f"{user['center']}:{user['username']}:{slot['position']}",
                          {"patient_id": patient_id, "treatment": slot["treatment"], "consent_confirmed": True, "eligibility_confirmed": True})
                # position is deliberately withheld from the doctor: it would reveal where the block ends.
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
            seed = body.get("random_seed", "").strip()
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
                        if not 8 <= len(seed) <= 128:
                            return self.send_json({"error": "新建中心必须填写8～128位固定随机种子"}, 400)
                        if conn.execute("SELECT 1 FROM centers WHERE random_seed=?", (seed,)).fetchone():
                            return self.send_json({"error": "该随机种子已被其他中心使用，请更换"}, 409)
                        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        center = conn.execute(
                            "INSERT INTO centers(name, random_seed, algorithm_version, sequence_created_at, sequence_created_by) VALUES (?, ?, ?, ?, ?)",
                            (center_name, seed, RANDOMIZATION_ALGORITHM, created_at, user["username"])
                        ).lastrowid
                        # No sequence is pre-generated: each account derives its own blocks lazily from the center seed.
                    conn.execute("INSERT INTO users VALUES (?, ?, ?, ?, 0)",
                                 (username, password_hash(password), center, display_name))
                    audit(conn, user["username"], "CREATE_USER", "user", username,
                          {"center": center, "center_name": center_name, "created_new_center": center_row is None})
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
                audit(conn, user["username"], "DELETE_EMPTY_CENTER", "center", center, {"name": row["name"]})
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
                audit(conn, user["username"], "VOID_ALLOCATION", "allocation", f"{center}:{row['owner']}:{row['position']}",
                      {"patient_id": patient_id, "reason": reason})
            return self.send_json({"ok": True})
        if path == "/api/records/status":
            user = self.user()
            if not user:
                return self.send_json({"error": "未登录"}, 401)
            body = self.json_body()
            status = body.get("status", "").strip()
            reason = body.get("reason", "").strip()
            patient_id = body.get("patient_id", "").strip().upper()
            try:
                center = int(body.get("center"))
            except (TypeError, ValueError):
                return self.send_json({"error": "记录参数不正确"}, 400)
            if not patient_id:
                return self.send_json({"error": "记录参数不正确"}, 400)
            if status not in PARTICIPANT_STATUSES:
                return self.send_json({"error": "患者状态不正确"}, 400)
            if status in {"脱落", "撤回知情同意", "失访", "方案违背", "误随机"} and not reason:
                return self.send_json({"error": "该状态必须填写原因"}, 400)
            if len(reason) > 200:
                return self.send_json({"error": "原因不能超过200字"}, 400)
            if not user["is_admin"] and user["center"] != center:
                return self.send_json({"error": "无权修改其他中心患者"}, 403)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT participant_status FROM allocations WHERE center=? AND patient_id=?",
                                   (center, patient_id)).fetchone()
                if not row:
                    return self.send_json({"error": "患者记录不存在"}, 404)
                conn.execute("UPDATE allocations SET participant_status=?, status_changed_at=?, status_changed_by=?, status_reason=? WHERE center=? AND patient_id=?",
                             (status, now, user["username"], reason, center, patient_id))
                audit(conn, user["username"], "CHANGE_PARTICIPANT_STATUS", "allocation", f"{center}:{patient_id}",
                      {"patient_id": patient_id, "from": row["participant_status"], "to": status, "reason": reason})
            return self.send_json({"ok": True})
        self.send_error(404)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.send_json({"status": "ok", "version": APP_VERSION, "updated_at": APP_UPDATED_AT})
        if path == "/api/version":
            return self.send_json({"version": APP_VERSION, "updated_at": APP_UPDATED_AT})
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
                    rows = conn.execute("SELECT c.name center_name, a.center, a.owner, a.position, a.patient_id, a.patient_initials, a.sex, a.age, a.note, a.treatment, a.randomized_at, a.randomized_by, a.participant_status, a.status_reason FROM allocations a JOIN centers c ON c.id=a.center WHERE a.patient_id IS NOT NULL AND a.voided_at IS NULL ORDER BY c.name, a.owner, a.position").fetchall()
                else:
                    # owner and position are withheld: they would reveal how far into the block this account is.
                    rows = conn.execute("SELECT c.name center_name, a.center, a.patient_id, a.patient_initials, a.sex, a.age, a.note, a.treatment, a.randomized_at, a.randomized_by, a.participant_status, a.status_reason FROM allocations a JOIN centers c ON c.id=a.center WHERE a.center=? AND a.patient_id IS NOT NULL AND a.voided_at IS NULL ORDER BY a.randomized_at", (user["center"],)).fetchall()
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
            patient_columns = ["患者研究编号", "姓名缩写", "性别", "年龄", "备注", "分组", "随机时间(UTC)", "操作者", "患者状态", "状态原因"]
            with db() as conn:
                if user["is_admin"]:
                    rows = conn.execute("SELECT c.name, a.owner, a.position, a.patient_id, a.patient_initials, a.sex, a.age, a.note, a.treatment, a.randomized_at, a.randomized_by, a.participant_status, a.status_reason FROM allocations a JOIN centers c ON c.id=a.center WHERE a.patient_id IS NOT NULL AND a.voided_at IS NULL ORDER BY c.name, a.owner, a.position").fetchall()
                    header = ["中心", "医生账号", "账号内序号"] + patient_columns
                    filename = "rct-all-records.csv"
                else:
                    rows = conn.execute("SELECT c.name, a.patient_id, a.patient_initials, a.sex, a.age, a.note, a.treatment, a.randomized_at, a.randomized_by, a.participant_status, a.status_reason FROM allocations a JOIN centers c ON c.id=a.center WHERE a.center=? AND a.patient_id IS NOT NULL AND a.voided_at IS NULL ORDER BY a.randomized_at", (user["center"],)).fetchall()
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
        if path == "/api/audit-export":
            if not user["is_admin"]:
                return self.send_json({"error": "仅管理员可以导出审计日志"}, 403)
            with db() as conn:
                rows = conn.execute("SELECT id, created_at, actor, action, entity_type, entity_id, details, previous_hash, entry_hash FROM audit_log ORDER BY id").fetchall()
                audit(conn, user["username"], "EXPORT_AUDIT_LOG", "audit_log", "", {"exported_entries": len(rows)})
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(["ID", "时间(UTC)", "操作者", "动作", "对象类型", "对象ID", "详情JSON", "前序哈希", "本条哈希"])
            writer.writerows([tuple(r) for r in rows])
            payload = ("\ufeff" + stream.getvalue()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=rct-audit-log.csv")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        if path == "/api/randomization-document":
            if not user["is_admin"]:
                return self.send_json({"error": "仅独立随机化管理员可以下载原始序列"}, 403)
            with db() as conn:
                rows = conn.execute("""
                    SELECT c.name, c.random_seed, c.algorithm_version, c.sequence_created_at,
                           c.sequence_created_by, a.owner, a.position, a.treatment
                    FROM centers c JOIN allocations a ON a.center=c.id
                    ORDER BY c.name, a.owner, a.position
                """).fetchall()
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(["中心", "固定随机种子", "算法版本", "生成时间(UTC)",
                             "序列生成者", "医生账号", "账号内序号", "预设分组"])
            writer.writerows([tuple(r) for r in rows])
            payload = ("\ufeff" + stream.getvalue()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=rct-original-randomization-document.csv")
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
    # 每5分钟清理过期session
    def periodic_cleanup():
        while True:
            threading.Event().wait(300)
            cleanup_sessions()
    threading.Thread(target=periodic_cleanup, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
