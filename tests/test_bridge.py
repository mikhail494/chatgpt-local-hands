"""Auto-tests A-K for the Local Hands bridge.

Run: python tests\\test_bridge.py
Exits 0 when all tests pass, non-zero otherwise.
"""

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BRIDGE_PY = BASE_DIR / "bridge.py"
CONFIG_PATH = BASE_DIR / "config.json"
PID_PATH = BASE_DIR / "bridge.pid"
TEST_TMP = BASE_DIR / "tests" / "tmp"

HOST = "127.0.0.1"
PORT = 8787
BASE_URL = "http://%s:%d" % (HOST, PORT)

PASS = []
FAIL = []


def check(name, ok, detail=""):
    if ok:
        PASS.append(name)
        print("PASS %s" % name)
    else:
        FAIL.append(name)
        print("FAIL %s %s" % (name, detail))


def http(method, path, token=None, body=None, raw_body=None, headers=None):
    url = BASE_URL + path
    data = None
    if raw_body is not None:
        data = raw_body
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Local-Hands-Token", token)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        return e.code, payload
    except Exception as e:
        return None, {"_exc": "%s: %s" % (type(e).__name__, e)}


def wait_health(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, payload = http("GET", "/health")
        if code == 200:
            return True
        time.sleep(0.3)
    return False


def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def stop_existing_bridge():
    """If a bridge already listens (leftover), stop it by its pid file."""
    if not port_in_use():
        return
    try:
        with open(PID_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline and port_in_use():
            time.sleep(0.2)
    except (OSError, ValueError):
        pass


def start_bridge():
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE_PY)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(BASE_DIR),
    )
    if not wait_health():
        try:
            out = proc.stdout.read().decode("utf-8", errors="replace")
        except Exception:
            out = ""
        proc.kill()
        raise RuntimeError("bridge did not become healthy; output:\n%s" % out)
    return proc


def stop_bridge(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def load_bridge_module():
    spec = importlib.util.spec_from_file_location("lh_bridge", BRIDGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    if TEST_TMP.exists():
        shutil.rmtree(TEST_TMP, ignore_errors=True)
    TEST_TMP.mkdir(parents=True, exist_ok=True)

    stop_existing_bridge()
    proc = start_bridge()

    try:
        # A: bound exactly to 127.0.0.1
        code, payload = http("GET", "/health")
        src = BRIDGE_PY.read_text(encoding="utf-8")
        mod = load_bridge_module()
        cfg = mod.load_config()
        bind_ok = (code == 200 and payload.get("status") == "ok"
                   and cfg.get("host") == "127.0.0.1"
                   and 'ThreadingHTTPServer(("127.0.0.1"' in src
                   and '0.0.0.0' not in src)
        check("A bind_127_0_0_1", bind_ok, "code=%s host=%s" % (code, cfg.get("host")))

        # B: no token is required anywhere (intentional localhost-only model)
        code1, _ = http("GET", "/capabilities", token="ignored-token-header")
        code2, _ = http("POST", "/invoke", token="ignored-token-header",
                        body={"tool": "fs.stat", "args": {"path": "."}})
        code3, _ = http("GET", "/capabilities")  # no token at all
        check("B no_token_required",
              code1 == 200 and code2 == 200 and code3 == 200,
              "codes=%s" % [code1, code2, code3])

        # C: fs.read
        probe = TEST_TMP / "probe_c.txt"
        probe.write_text("hello local hands", encoding="utf-8")
        code, res = http("POST", "/invoke", body={"tool": "fs.read",
                               "args": {"path": str(probe)}})
        ok = (code == 200 and res.get("status") == "ok"
              and res.get("result", {}).get("text") == "hello local hands")
        check("C fs_read", ok, json.dumps(res)[:200])

        # D: fs.write in test dir
        target = TEST_TMP / "written_d.txt"
        code, res = http("POST", "/invoke", body={"tool": "fs.write",
                               "args": {"path": str(target),
                                        "content": "line1\nline2\n"}})
        ok = (code == 200 and res.get("status") == "ok"
              and target.exists()
              and target.read_text(encoding="utf-8") == "line1\nline2\n")
        check("D fs_write", ok, json.dumps(res)[:200])

        # E: powershell command
        marker = "lh_test_%d" % int(time.time())
        code, res = http("POST", "/invoke", body={"tool": "shell.powershell",
                               "args": {"command": "Write-Output %s" % marker}})
        r = res.get("result", {})
        ok = (code == 200 and res.get("status") == "ok"
              and r.get("exit_code") == 0 and marker in (r.get("stdout") or ""))
        check("E shell_powershell", ok, json.dumps(res)[:200])

        # F: process.list
        code, res = http("POST", "/invoke", body={"tool": "process.list", "args": {"limit": 5}})
        r = res.get("result", {})
        procs = r.get("processes")
        ok = (code == 200 and res.get("status") == "ok"
              and isinstance(procs, list) and len(procs) > 0
              and all("ProcessName" in p or "processname" in p for p in procs))
        check("F process_list", ok, json.dumps(res)[:200])

        # G: batch executes multiple calls
        b1 = TEST_TMP / "batch_g1.txt"
        b2 = TEST_TMP / "batch_g2.txt"
        code, res = http("POST", "/batch", body={
            "calls": [
                {"id": "g1", "tool": "fs.write",
                 "args": {"path": str(b1), "content": "one"}},
                {"id": "g2", "tool": "fs.write",
                 "args": {"path": str(b2), "content": "two"}},
                {"id": "g3", "tool": "fs.stat",
                 "args": {"path": str(b1)}},
            ],
        })
        results = res.get("results", [])
        ok = (code == 200 and len(results) == 3
              and all(x.get("status") == "ok" for x in results)
              and b1.exists() and b2.exists()
              and results[2].get("result", {}).get("is_file") is True)
        check("G batch", ok, json.dumps(res)[:300])

        # H: same id never re-executed
        hfile = TEST_TMP / "once_h.txt"
        body_h = {"id": "h-fixed-id", "tool": "fs.write",
                  "args": {"path": str(hfile), "content": "FIRST"}}
        code1, res1 = http("POST", "/invoke", body=body_h)
        code2, res2 = http("POST", "/invoke", body={
            "id": "h-fixed-id", "tool": "fs.write",
            "args": {"path": str(hfile), "content": "SECOND"}})
        content = hfile.read_text(encoding="utf-8") if hfile.exists() else ""
        ok = (code1 == 200 and code2 == 200
              and res1.get("status") == "ok"
              and res2.get("status") == "ok"
              and res2.get("cached") is True
              and content == "FIRST")
        check("H idempotency_same_id", ok,
              "cached=%s content=%s" % (res2.get("cached"), content))

        # I: malformed JSON rejected, nothing executed
        before = sorted(p.name for p in TEST_TMP.iterdir())
        code, res = http("POST", "/invoke",
                         raw_body=b"{this is not json")
        after = sorted(p.name for p in TEST_TMP.iterdir())
        ok = code == 400 and res.get("error") == "bad_json" and before == after
        check("I malformed_json_rejected", ok, "code=%s" % code)

        # J: path traversal outside allowed_roots blocked (workspace mode)
        outside = Path("C:\\Windows\\Temp\\lh_traversal_probe.txt")
        code, res = http("POST", "/invoke", body={"tool": "fs.write",
                               "args": {"path": str(outside), "content": "x"}})
        r = res.get("result", {})
        err = res.get("error", {})
        ok = (code == 400 and res.get("status") == "error"
              and err.get("code") == "PATH_OUTSIDE_ALLOWED_ROOTS"
              and not outside.exists())
        # also: dotdot escape from a subdir of the root still blocked when
        # the target lands outside all roots
        up = str(TEST_TMP / ".." / ".." / ".." / ".." / "lh_escape_probe.txt")
        code2, res2 = http("POST", "/invoke",
                           body={"tool": "fs.write",
                                 "args": {"path": up, "content": "x"}})
        err2 = res2.get("error", {})
        escape_target = (TEST_TMP / ".." / ".." / ".." / ".." / "lh_escape_probe.txt")
        ok = ok and (code2 == 400 and err2.get("code") == "PATH_OUTSIDE_ALLOWED_ROOTS"
                     and not escape_target.resolve().exists())
        check("J path_traversal_blocked", ok,
              "c1=%s c2=%s" % (res.get("error"), res2.get("error")))

    finally:
        stop_bridge(proc)

    # K: after restart the bridge still serves without any token
    proc2 = start_bridge()
    try:
        code, res = http("GET", "/capabilities")
        code2, _ = http("POST", "/invoke",
                        body={"tool": "fs.stat", "args": {"path": "."}})
        ok = (code == 200 and code2 == 200
              and isinstance(res.get("tools"), list)
              and len(res["tools"]) >= 15)
        check("K no_token_after_restart", ok, "cap=%s invoke=%s" % (code, code2))
    finally:
        stop_bridge(proc2)

    if TEST_TMP.exists():
        shutil.rmtree(TEST_TMP, ignore_errors=True)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED: %s" % ", ".join(FAIL))
        return 1
    print("ALL TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
