import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

exe = Path(__file__).parent / "dist" / "RCT-Randomizer.exe"
env = os.environ.copy()
env["RCT_ADMIN_USERNAME"] = "admin"
env["RCT_ADMIN_PASSWORD"] = "test-admin-password"
process = subprocess.Popen([str(exe)], cwd=exe.parent, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
try:
    deadline = time.time() + 20
    last_error = None
    while time.time() < deadline:
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:8000/api/login",
                data=json.dumps({"username": "admin", "password": "test-admin-password"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                assert json.load(response)["ok"] is True
            print("Packaged EXE smoke test passed")
            break
        except Exception as error:
            last_error = error
            time.sleep(1)
    else:
        raise RuntimeError(f"EXE did not start: {last_error}")
finally:
    process.terminate()
    process.wait(timeout=5)
