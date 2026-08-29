import http.client
import json
import os
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import app


tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
app.DB_PATH = Path(tmp.name) / "test.db"
os.environ["RCT_ADMIN_USERNAME"] = "admin"
os.environ["RCT_ADMIN_PASSWORD"] = "test-admin-password"
app.init_db()
server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def request(method, path, body=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = conn.getresponse()
    payload = response.read()
    content_type = response.getheader("Content-Type", "")
    data = json.loads(payload) if payload and "application/json" in content_type else payload
    return response.status, data, response.getheader("Set-Cookie")


def randomize(pid, cookie):
    return request("POST", "/api/randomize",
                   {"patient_id": pid, "patient_initials": "ZS", "sex": "男", "age": 45,
                    "consent_confirmed": True, "eligibility_confirmed": True}, cookie)


# 管理员登录
status, _, header = request("POST", "/api/login", {"username": "admin", "password": "test-admin-password"})
assert status == 200
admin_cookie = header.split(";", 1)[0]

# 建医生账号（新中心，带 seed）
status, _, _ = request("POST", "/api/users",
                       {"username": "doctor88", "display_name": "测试医生", "center_name": "测试医院",
                        "password": "test-password-1", "random_seed": "test-seed-8888"}, admin_cookie)
assert status == 200

# 医生登录
status, _, header = request("POST", "/api/login", {"username": "doctor88", "password": "test-password-1"})
assert status == 200
doctor_cookie = header.split(";", 1)[0]

# 随机（需知情同意/资格确认），响应不得含 position
status, allocation, _ = randomize("C02-TEST", doctor_cookie)
assert status == 200 and allocation["treatment"] in ("A", "B")
assert "position" not in allocation and "owner" not in allocation, "医生响应不能下发序号/账号"

# 缺少知情同意确认应被拒
status, error, _ = request("POST", "/api/randomize", {"patient_id": "C02-NOCONSENT"}, doctor_cookie)
assert status == 400 and "知情同意" in error["error"]

# 单账号连录 12 例：自动续区组，不再有 50 上限
for index in range(12):
    status, _, _ = randomize(f"C02-BLK-{index:02d}", doctor_cookie)
    assert status == 200, f"第 {index + 1} 例失败：{status}"

# 同中心第二位医生（已有中心不填 seed）
status, _, _ = request("POST", "/api/users",
                       {"username": "doctor99", "display_name": "同中心第二位医生", "center_name": "测试医院",
                        "password": "test-password-1", "random_seed": ""}, admin_cookie)
assert status == 200
status, _, header = request("POST", "/api/login", {"username": "doctor99", "password": "test-password-1"})
assert status == 200
doctor99_cookie = header.split(";", 1)[0]

# 跨医生在同一中心用同一研究编号必须被拒
status, error, _ = randomize("C02-TEST", doctor99_cookie)
assert status == 409, f"同中心跨医生重复编号未被拦截：{status} {error}"

# 医生记录：可见范围中心级（能看到同事），但不含 position/owner
status, records, _ = request("GET", "/api/records", cookie=doctor99_cookie)
assert status == 200
assert all("position" not in r and "owner" not in r for r in records["records"]), "医生不应看到序号/账号"
assert {"C02-TEST", "C02-BLK-00"} <= {r["patient_id"] for r in records["records"]}, "医生应看到本中心全部记录"

# 管理员记录：含 owner + position
status, records, _ = request("GET", "/api/records", cookie=admin_cookie)
assert status == 200 and all("position" in r and "owner" in r for r in records["records"])
mine = sorted((r for r in records["records"] if r["owner"] == "doctor88"), key=lambda r: r["position"])
assert [r["position"] for r in mine] == list(range(1, 14)), "账号内序号应连续编号"
# 区组不变式：A/B 偏差不超 3，第一个区组（4 或 6 例）结束归零
running, diffs = 0, []
for record in mine:
    running += 1 if record["treatment"] == "A" else -1
    diffs.append(running)
assert all(abs(d) <= 3 for d in diffs), f"区组不变式被破坏：{diffs}"
assert diffs[3] == 0 or diffs[5] == 0, f"第一个区组结束未归零：{diffs}"

# 删除记录：入参 patient_id（医生可删本中心，含同事）
status, _, _ = request("POST", "/api/records/delete",
                       {"center": mine[0]["center"], "patient_id": "C02-BLK-00", "reason": "录入错误"}, doctor99_cookie)
assert status == 200

# 修改状态：入参 patient_id
status, _, _ = request("POST", "/api/records/status",
                       {"center": mine[0]["center"], "patient_id": "C02-BLK-01", "status": "治疗中", "reason": ""},
                       doctor_cookie)
assert status == 200

# 零入组中心仍出现在进度里
status, _, _ = request("POST", "/api/users",
                       {"username": "zero88", "display_name": "零入组医生", "center_name": "零入组中心",
                        "password": "test-password-1", "random_seed": "zero-seed-9999"}, admin_cookie)
assert status == 200
status, progress, _ = request("GET", "/api/progress", cookie=admin_cookie)
zero = next((p for p in progress["progress"] if p["center_name"] == "零入组中心"), None)
assert status == 200 and zero and zero["used"] == 0, f"零入组中心从进度消失：{progress}"

# 管理员导出：含医生账号/账号内序号；医生导出：无序号列
status, all_csv, _ = request("GET", "/api/export", cookie=admin_cookie)
assert status == 200 and "医生账号".encode() in all_csv and "账号内序号".encode() in all_csv
status, center_csv, _ = request("GET", "/api/export", cookie=doctor_cookie)
assert status == 200 and "序号".encode() not in center_csv, "医生 CSV 不应含序号列"

# 随机化文档（管理员可下载，含 seed 与账号）
status, doc_csv, _ = request("GET", "/api/randomization-document", cookie=admin_cookie)
assert status == 200 and "医生账号".encode() in doc_csv and b"test-seed-8888" in doc_csv

server.shutdown()
server.server_close()
tmp.cleanup()
print("Full permission and randomization smoke test passed")
