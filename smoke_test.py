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
status, _, _ = request("POST", "/api/records/delete", {"center": used_center, "position": allocation["position"], "reason": "录入错误"}, doctor_cookie)
assert status == 200
status, records, _ = request("GET", "/api/records", cookie=doctor_cookie)
assert status == 200 and records["records"] == []
status, center_csv, _ = request("GET", "/api/export", cookie=doctor_cookie)
assert status == 200 and b"C02-TEST" not in center_csv

server.shutdown()
server.server_close()
tmp.cleanup()
print("Full permission and randomization smoke test passed")
