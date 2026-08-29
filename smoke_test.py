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


status, _, header = request("POST", "/api/login", {"username": "admin", "password": "test-admin-password"})
assert status == 200
admin_cookie = header.split(";", 1)[0]
status, _, _ = request("POST", "/api/users", {"username": "doctor88", "display_name": "测试医生", "center_name": "测试医院", "password": "test-password-1"}, admin_cookie)
assert status == 200

status, _, header = request("POST", "/api/login", {"username": "doctor88", "password": "test-password-1"})
assert status == 200
doctor_cookie = header.split(";", 1)[0]
patient = {"patient_id": "C02-TEST", "patient_initials": "ZS", "sex": "男", "age": 45, "note": "测试"}
status, allocation, _ = request("POST", "/api/randomize", patient, doctor_cookie)
assert status == 200 and allocation["treatment"] in ("A", "B")
status, records, _ = request("GET", "/api/records", cookie=doctor_cookie)
assert status == 200 and len(records["records"]) == 1 and records["records"][0]["center_name"] == "测试医院"
status, center_csv, _ = request("GET", "/api/export", cookie=doctor_cookie)
assert status == 200 and b"C02-TEST" in center_csv
status, users, _ = request("GET", "/api/users", cookie=admin_cookie)
assert status == 200 and any(u["username"] == "doctor88" for u in users["users"])
used_center = next(u["center"] for u in users["users"] if u["username"] == "doctor88")
status, error, _ = request("POST", "/api/centers/delete", {"center": used_center}, admin_cookie)
assert status == 409 and "不能删除" in error["error"]
status, _, _ = request("POST", "/api/users", {"username": "empty88", "display_name": "空中心医生", "center_name": "空中心", "password": "test-password-1"}, admin_cookie)
assert status == 200
status, users, _ = request("GET", "/api/users", cookie=admin_cookie)
empty_center = next(u["center"] for u in users["users"] if u["username"] == "empty88")
status, _, _ = request("POST", "/api/centers/delete", {"center": empty_center}, admin_cookie)
assert status == 200
status, users, _ = request("GET", "/api/users", cookie=admin_cookie)
assert not any(u["username"] == "empty88" for u in users["users"])
status, records, _ = request("GET", "/api/records", cookie=admin_cookie)
assert status == 200 and len(records["records"]) == 1
status, all_csv, _ = request("GET", "/api/export", cookie=admin_cookie)
assert status == 200 and b"C02-TEST" in all_csv
status, _, _ = request("POST", "/api/records/delete", {"center": used_center, "patient_id": "C02-TEST", "reason": "录入错误"}, doctor_cookie)
assert status == 200
status, records, _ = request("GET", "/api/records", cookie=doctor_cookie)
assert status == 200 and records["records"] == []
status, center_csv, _ = request("GET", "/api/export", cookie=doctor_cookie)
assert status == 200 and b"C02-TEST" not in center_csv

# 区组按医生账号分层后的回归断言
status, _, _ = request("POST", "/api/users", {"username": "doctor99", "display_name": "同中心第二位医生", "center_name": "测试医院", "password": "test-password-1"}, admin_cookie)
assert status == 200
status, _, header = request("POST", "/api/login", {"username": "doctor99", "password": "test-password-1"})
assert status == 200
doctor99_cookie = header.split(";", 1)[0]

status, allocation, _ = request("POST", "/api/randomize", {"patient_id": "C02-DUP"}, doctor_cookie)
assert status == 200 and "position" not in allocation and "owner" not in allocation, "随机响应体不能下发序号"
status, error, _ = request("POST", "/api/randomize", {"patient_id": "C02-DUP"}, doctor99_cookie)
assert status == 409, f"同中心跨医生重复使用研究编号未被拦截：{status} {error}"

for index in range(12):
    status, _, _ = request("POST", "/api/randomize", {"patient_id": f"C02-99-{index:02d}"}, doctor99_cookie)
    assert status == 200, f"第 {index + 1} 例随机失败（不应再有名额上限）：{status}"

status, records, _ = request("GET", "/api/records", cookie=doctor99_cookie)
assert status == 200 and all("position" not in r and "owner" not in r for r in records["records"]), "医生不应看到序号或账号"
assert {"C02-DUP", "C02-99-00"} <= {r["patient_id"] for r in records["records"]}, "可见范围应保持中心级"

status, records, _ = request("GET", "/api/records", cookie=admin_cookie)
assert status == 200 and all("position" in r and "owner" in r for r in records["records"]), "管理员应看到账号与序号"
mine = sorted((r for r in records["records"] if r["owner"] == "doctor99"), key=lambda r: r["position"])
assert [r["position"] for r in mine] == list(range(1, 13)), "账号内序号应连续编号"
running, diffs = 0, []
for record in mine:
    running += 1 if record["treatment"] == "A" else -1
    diffs.append(running)
assert all(abs(d) <= 3 for d in diffs), f"区组内 A/B 偏差超过 3：{diffs}"
assert diffs[3] == 0 or diffs[5] == 0, f"第一个区组（4 或 6 例）结束时未归零：{diffs}"
his = [r for r in records["records"] if r["owner"] == "doctor88"]
assert [r["patient_id"] for r in his] == ["C02-DUP"], "另一账号的序列不应受影响"
assert his[0]["position"] == 2, f"作废的名额不回收，本例序号应为 2，实际 {his[0]['position']}"

status, _, _ = request("POST", "/api/users", {"username": "zero88", "display_name": "零入组医生", "center_name": "零入组中心", "password": "test-password-1"}, admin_cookie)
assert status == 200
status, progress, _ = request("GET", "/api/progress", cookie=admin_cookie)
zero = next((p for p in progress["progress"] if p["center_name"] == "零入组中心"), None)
assert status == 200 and zero and zero["used"] == 0, f"0 入组的中心从进度里消失了：{progress}"

status, _, header = request("POST", "/api/login", {"username": "zero88", "password": "test-password-1"})
status, _, _ = request("POST", "/api/randomize", {"patient_id": "C02-DUP"}, header.split(";", 1)[0])
assert status == 200, "不同中心之间应允许使用相同研究编号"

status, all_csv, _ = request("GET", "/api/export", cookie=admin_cookie)
assert status == 200 and "医生账号".encode() in all_csv and "账号内序号".encode() in all_csv
status, center_csv, _ = request("GET", "/api/export", cookie=doctor99_cookie)
assert status == 200 and "序号".encode() not in center_csv, "医生 CSV 不应含序号列"

server.shutdown()
server.server_close()
tmp.cleanup()
print("Full permission and randomization smoke test passed")
