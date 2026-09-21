"""Local Hands bridge.

Local HTTP tool-execution server for the ChatGPT web UI via the browser
extension. Python stdlib only. Binds strictly to the 127.0.0.1 loopback
interface (never a wildcard or LAN address).
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "bridge.log"
PID_PATH = BASE_DIR / "bridge.pid"
TESTS_DIR = BASE_DIR / "tests"

PROTOCOL_VERSION = "LOCAL_HANDS_V1"
CALL_OPEN = "[[" + PROTOCOL_VERSION + ":CALL]]"
CALL_CLOSE = "[[" + "/" + PROTOCOL_VERSION + ":CALL]]"
BATCH_OPEN = "[[" + PROTOCOL_VERSION + ":BATCH]]"
BATCH_CLOSE = "[[" + "/" + PROTOCOL_VERSION + ":BATCH]]"
RESULT_OPEN = "[[" + PROTOCOL_VERSION + ":RESULT]]"
RESULT_CLOSE = "[[" + "/" + PROTOCOL_VERSION + ":RESULT]]"
RESULTS_OPEN = "[[" + PROTOCOL_VERSION + ":RESULTS]]"
RESULTS_CLOSE = "[[" + "/" + PROTOCOL_VERSION + ":RESULTS]]"

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8787,
    "mode": "workspace_full_access",
    "allowed_roots": [str(Path.home())],
    "shell_enabled": True,
    "process_control": True,
    "max_inline_bytes": 32768,
    "command_timeout_seconds": 120,
}

MODES = ("safe", "workspace_full_access", "full_pc_access")

_write_lock = threading.Lock()
_id_lock = threading.Lock()
_seen_ids = OrderedDict()  # id -> result (in-memory LRU)
_SEEN_IDS_MAX = 512

_log_lock = threading.Lock()


def log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        with _log_lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if isinstance(user_cfg, dict):
            for key in cfg:
                if key in user_cfg:
                    cfg[key] = user_cfg[key]
    except (OSError, ValueError):
        pass
    if cfg.get("mode") not in MODES:
        cfg["mode"] = "workspace_full_access"
    try:
        cfg["port"] = int(cfg.get("port", 8787))
    except (TypeError, ValueError):
        cfg["port"] = 8787
    try:
        cfg["max_inline_bytes"] = int(cfg.get("max_inline_bytes", 32768))
    except (TypeError, ValueError):
        cfg["max_inline_bytes"] = 32768
    try:
        cfg["command_timeout_seconds"] = int(cfg.get("command_timeout_seconds", 120))
    except (TypeError, ValueError):
        cfg["command_timeout_seconds"] = 120
    if not isinstance(cfg.get("allowed_roots"), list):
        cfg["allowed_roots"] = list(DEFAULT_CONFIG["allowed_roots"])
    return cfg


def canonical(path_str):
    """Resolve to a canonical absolute path. Symlinks/junctions are resolved
    so a link cannot escape allowed_roots."""
    p = Path(path_str)
    if not p.is_absolute():
        p = Path.cwd() / p
    return Path(os.path.realpath(p))


class ToolError(Exception):
    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def check_fs_path(cfg, path_str, write=False):
    target = canonical(path_str)
    mode = cfg["mode"]
    if mode == "full_pc_access":
        if write:
            pass
        return target
    if mode == "safe":
        if write:
            raise ToolError("WRITE_DENIED_IN_SAFE_MODE",
                            "fs writes are disabled in safe mode")
    roots = [canonical(r) for r in cfg.get("allowed_roots", [])]
    ok = False
    for root in roots:
        if target == root or root in target.parents:
            ok = True
            break
    if not ok:
        raise ToolError("PATH_OUTSIDE_ALLOWED_ROOTS",
                        "path %s is outside allowed_roots for mode %s"
                        % (path_str, mode))
    return target


def shell_allowed(cfg):
    if not cfg.get("shell_enabled", False):
        raise ToolError("SHELL_DISABLED",
                        "shell tools are disabled in config")
    return True


def process_control_allowed(cfg):
    if not cfg.get("process_control", False):
        raise ToolError("PROCESS_CONTROL_DISABLED",
                        "process mutation tools are disabled in config")
    return True


def encode_text(data):
    return data.decode("utf-8", errors="replace")


def run_command(cfg, shell, command, extra_args=None):
    shell_allowed(cfg)
    timeout = cfg.get("command_timeout_seconds", 120)
    if shell == "powershell":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["cmd", "/c", command]
        if extra_args:
            argv = ["cmd", "/c"] + list(extra_args)
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError("TIMEOUT",
                        "command timed out after %d seconds" % timeout)
    except OSError as e:
        raise ToolError("SHELL_ERROR", "failed to start shell: %s" % e)
    result = {
        "shell": shell,
        "command": command,
        "exit_code": proc.returncode,
        "stdout": encode_text(proc.stdout),
        "stderr": encode_text(proc.stderr),
    }
    return result


def shrink_output(cfg, text, prefix):
    if len(text.encode("utf-8")) <= cfg["max_inline_bytes"]:
        return {"text": text}
    raw = text.encode("utf-8")
    total = len(raw)
    head_bytes = max(1, cfg["max_inline_bytes"] // 2)
    tail_bytes = max(1, cfg["max_inline_bytes"] // 2)
    digest = hashlib.sha256(raw).hexdigest()
    fname = "%s_%s_%d.txt" % (prefix, digest[:12], int(time.time() * 1000))
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        fpath = OUTPUT_DIR / fname
        with open(fpath, "wb") as f:
            f.write(raw)
        full_output_path = str(fpath)
    except OSError:
        full_output_path = None
    return {
        "truncated": True,
        "total_bytes": total,
        "max_inline_bytes": cfg["max_inline_bytes"],
        "head": raw[:head_bytes].decode("utf-8", errors="replace"),
        "tail": raw[-tail_bytes:].decode("utf-8", errors="replace"),
        "full_output_path": full_output_path,
    }


def tail_lines(cfg, path_str, max_bytes=None):
    target = check_fs_path(cfg, path_str, write=False)
    if not target.is_file():
        raise ToolError("NOT_A_FILE", "not a file: %s" % path_str)
    limit = cfg["max_inline_bytes"]
    if max_bytes is not None:
        try:
            limit = min(int(max_bytes), limit)
        except (TypeError, ValueError):
            pass
    try:
        size = target.stat().st_size
        read_size = min(size, max(1, limit))
        with open(target, "rb") as f:
            f.seek(max(0, size - read_size))
            data = f.read()
    except OSError as e:
        raise ToolError("READ_ERROR", "failed to read file: %s" % e)
    text = encode_text(data)
    return {"path": str(target), "bytes_returned": len(data),
            "text": text}


def tool_fs_read(cfg, args):
    target = check_fs_path(cfg, args.get("path"), write=False)
    if not target.is_file():
        raise ToolError("NOT_A_FILE", "not a file: %s" % args.get("path"))
    try:
        data = target.read_bytes()
    except OSError as e:
        raise ToolError("READ_ERROR", "failed to read file: %s" % e)
    out = shrink_output(cfg, encode_text(data), "fs.read")
    out["path"] = str(target)
    return out


def tool_fs_write(cfg, args):
    path = args.get("path")
    content = args.get("content")
    if path is None or content is None:
        raise ToolError("BAD_ARGS", "fs.write requires path and content")
    target = check_fs_path(cfg, path, write=True)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except OSError as e:
        raise ToolError("WRITE_ERROR", "failed to write file: %s" % e)
    return {"path": str(target), "bytes_written": len(content.encode("utf-8"))}


def tool_fs_list(cfg, args):
    path = args.get("path") or str(Path.cwd())
    target = check_fs_path(cfg, path, write=False)
    if not target.is_dir():
        raise ToolError("NOT_A_DIR", "not a directory: %s" % path)
    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                is_dir = None
                size = None
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_dir": is_dir,
                "size_bytes": size,
            })
    except OSError as e:
        raise ToolError("LIST_ERROR", "failed to list directory: %s" % e)
    return {"path": str(target), "entries": entries}


def tool_fs_stat(cfg, args):
    target = check_fs_path(cfg, args.get("path"), write=False)
    try:
        st = target.stat()
        lst = target.lstat()
        is_symlink = lst.st_ino != 0 and target.is_symlink()
    except OSError as e:
        raise ToolError("STAT_ERROR", "failed to stat path: %s" % e)
    return {
        "path": str(target),
        "exists": target.exists(),
        "is_file": target.is_file(),
        "is_dir": target.is_dir(),
        "is_symlink": is_symlink,
        "size_bytes": st.st_size,
        "modified_unix": st.st_mtime,
    }


def tool_fs_patch(cfg, args):
    path = args.get("path")
    find = args.get("find")
    replace = args.get("replace")
    replace_all = bool(args.get("replace_all", False))
    if path is None or find is None or replace is None:
        raise ToolError("BAD_ARGS",
                        "fs.patch requires path, find and replace")
    target = check_fs_path(cfg, path, write=True)
    if not target.is_file():
        raise ToolError("NOT_A_FILE", "not a file: %s" % path)
    try:
        original = target.read_text(encoding="utf-8")
    except OSError as e:
        raise ToolError("READ_ERROR", "failed to read file: %s" % e)
    count = original.count(find)
    if count == 0:
        raise ToolError("FIND_NOT_FOUND",
                        "find string not present in file; nothing replaced")
    if replace_all:
        updated = original.replace(find, replace)
    else:
        updated = original.replace(find, replace, 1)
    if count == 1 and not replace_all:
        replaced = 1
    else:
        replaced = count if replace_all else 1
    try:
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
    except OSError as e:
        raise ToolError("WRITE_ERROR", "failed to write file: %s" % e)
    return {"path": str(target), "replacements": replaced,
            "find_occurrences": count}


def tool_fs_mkdir(cfg, args):
    path = args.get("path")
    if path is None:
        raise ToolError("BAD_ARGS", "fs.mkdir requires path")
    target = check_fs_path(cfg, path, write=True)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ToolError("MKDIR_ERROR", "failed to create directory: %s" % e)
    return {"path": str(target)}


def _fs_move(cfg, src_str, dst_str):
    src = check_fs_path(cfg, src_str, write=True)
    dst = check_fs_path(cfg, dst_str, write=True)
    if not src.exists():
        raise ToolError("NOT_FOUND", "source does not exist: %s" % src_str)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as e:
        raise ToolError("MOVE_ERROR", "failed to move: %s" % e)
    return {"source": str(src), "destination": str(dst)}


def tool_fs_move(cfg, args):
    if args.get("source") is None or args.get("destination") is None:
        raise ToolError("BAD_ARGS", "fs.move requires source and destination")
    return _fs_move(cfg, args["source"], args["destination"])


def tool_fs_copy(cfg, args):
    if args.get("source") is None or args.get("destination") is None:
        raise ToolError("BAD_ARGS", "fs.copy requires source and destination")
    src = check_fs_path(cfg, args["source"], write=False)
    dst = check_fs_path(cfg, args["destination"], write=True)
    if not src.exists():
        raise ToolError("NOT_FOUND", "source does not exist: %s" % args["source"])
    try:
        if src.is_dir():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
    except OSError as e:
        raise ToolError("COPY_ERROR", "failed to copy: %s" % e)
    return {"source": str(src), "destination": str(dst)}


def tool_fs_delete(cfg, args):
    path = args.get("path")
    if path is None:
        raise ToolError("BAD_ARGS", "fs.delete requires path")
    target = check_fs_path(cfg, path, write=True)
    if not target.exists():
        raise ToolError("NOT_FOUND", "path does not exist: %s" % path)
    try:
        if target.is_dir():
            shutil.rmtree(str(target))
        else:
            target.unlink()
    except OSError as e:
        raise ToolError("DELETE_ERROR", "failed to delete: %s" % e)
    return {"path": str(target), "deleted": True}


def tool_shell_powershell(cfg, args):
    command = args.get("command")
    if not command:
        raise ToolError("BAD_ARGS", "shell.powershell requires command")
    return run_command(cfg, "powershell", command)


def tool_shell_cmd(cfg, args):
    command = args.get("command")
    if not command:
        raise ToolError("BAD_ARGS", "shell.cmd requires command")
    return run_command(cfg, "cmd", command)


def tool_process_list(cfg, args):
    limit = 100
    if args.get("limit") is not None:
        try:
            limit = int(args["limit"])
        except (TypeError, ValueError):
            pass
        limit = max(1, min(limit, 500))
    out = run_command(cfg, "powershell",
                      "Get-Process | Select-Object -First %d "
                      "Id,ProcessName,StartTime | ConvertTo-Json" % limit)
    try:
        out["processes"] = json.loads(out["stdout"] or "[]")
        if isinstance(out["processes"], dict):
            out["processes"] = [out["processes"]]
    except ValueError:
        out["processes"] = None
    return out


def tool_process_start(cfg, args):
    process_control_allowed(cfg)
    command = args.get("command")
    if not command:
        raise ToolError("BAD_ARGS", "process.start requires command")
    workdir = args.get("workdir")
    if workdir is not None:
        cwd = check_fs_path(cfg, workdir, write=False)
        if not cwd.is_dir():
            raise ToolError("NOT_A_DIR", "workdir is not a directory")
    else:
        cwd = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        raise ToolError("START_ERROR", "failed to start process: %s" % e)
    return {"pid": proc.pid, "command": command,
            "workdir": str(cwd) if cwd else None}


def tool_process_kill(cfg, args):
    process_control_allowed(cfg)
    pid = args.get("pid")
    if pid is None:
        raise ToolError("BAD_ARGS", "process.kill requires pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise ToolError("BAD_ARGS", "pid must be an integer")
    out = run_command(cfg, "powershell",
                      "Stop-Process -Id %d -Force -ErrorAction SilentlyContinue; "
                      "if (Get-Process -Id %d -ErrorAction SilentlyContinue) { 'STILL_RUNNING' } else { 'KILLED' }"
                      % (pid, pid))
    status = "KILLED" if "KILLED" in (out.get("stdout") or "") else "STILL_RUNNING"
    return {"pid": pid, "status": status}


TOOLS = {
    "fs.read": tool_fs_read,
    "fs.list": tool_fs_list,
    "fs.stat": tool_fs_stat,
    "fs.write": tool_fs_write,
    "fs.patch": tool_fs_patch,
    "fs.mkdir": tool_fs_mkdir,
    "fs.move": tool_fs_move,
    "fs.copy": tool_fs_copy,
    "fs.delete": tool_fs_delete,
    "shell.powershell": tool_shell_powershell,
    "shell.cmd": tool_shell_cmd,
    "process.list": tool_process_list,
    "process.start": tool_process_start,
    "process.kill": tool_process_kill,
    "file.tail": tail_lines,
}

TOOL_CAPABILITIES = [
    {"name": "fs.read", "args": {"path": "string"},
     "desc": "Read a file as UTF-8 text (truncated when over max_inline_bytes)."},
    {"name": "fs.list", "args": {"path": "string (optional)"},
     "desc": "List a directory."},
    {"name": "fs.stat", "args": {"path": "string"},
     "desc": "Stat a file or directory."},
    {"name": "fs.write", "args": {"path": "string", "content": "string"},
     "desc": "Write (create or overwrite) a UTF-8 file."},
    {"name": "fs.patch", "args": {"path": "string", "find": "string",
                                  "replace": "string",
                                  "replace_all": "boolean (optional)"},
     "desc": "Deterministic string replace; fails if find is absent."},
    {"name": "fs.mkdir", "args": {"path": "string"},
     "desc": "Create a directory (parents ok)."},
    {"name": "fs.move", "args": {"source": "string", "destination": "string"},
     "desc": "Move a file or directory."},
    {"name": "fs.copy", "args": {"source": "string", "destination": "string"},
     "desc": "Copy a file or directory."},
    {"name": "fs.delete", "args": {"path": "string"},
     "desc": "Delete a file or directory tree."},
    {"name": "shell.powershell", "args": {"command": "string"},
     "desc": "Run a PowerShell command, capture stdout/stderr."},
    {"name": "shell.cmd", "args": {"command": "string"},
     "desc": "Run a cmd command, capture stdout/stderr."},
    {"name": "process.list", "args": {"limit": "integer (optional)"},
     "desc": "List running processes."},
    {"name": "process.start", "args": {"command": "string",
                                       "workdir": "string (optional)"},
     "desc": "Start a detached process; returns pid."},
    {"name": "process.kill", "args": {"pid": "integer"},
     "desc": "Force-kill a process by pid."},
    {"name": "file.tail", "args": {"path": "string",
                                   "max_bytes": "integer (optional)"},
     "desc": "Return the tail of a file."},
]


def execute_tool(cfg, tool_name, args):
    if tool_name in ("fs.read", "fs.list", "fs.stat", "file.tail"):
        pass
    elif cfg["mode"] == "safe":
        if tool_name in ("fs.write", "fs.patch", "fs.mkdir", "fs.move",
                         "fs.copy", "fs.delete", "shell.powershell",
                         "shell.cmd", "process.start", "process.kill"):
            raise ToolError("WRITE_DENIED_IN_SAFE_MODE",
                            "tool %s is not allowed in safe mode" % tool_name)
    fn = TOOLS.get(tool_name)
    if fn is None:
        raise ToolError("UNKNOWN_TOOL", "unknown tool: %s" % tool_name)
    if not isinstance(args, dict):
        raise ToolError("BAD_ARGS", "args must be an object")
    return fn(cfg, args)


def make_result(request_id, tool, status, value=None, error=None):
    r = {"id": request_id, "tool": tool, "status": status}
    if error is not None:
        r["error"] = error
    else:
        r["result"] = value
    return r


def record_id(request_id, result):
    with _id_lock:
        if request_id in _seen_ids:
            return _seen_ids[request_id]
        _seen_ids[request_id] = result
        while len(_seen_ids) > _SEEN_IDS_MAX:
            _seen_ids.popitem(last=False)
        return result


def invoke_one(cfg, request_id, tool, args):
    if isinstance(request_id, str) and request_id:
        with _id_lock:
            cached = _seen_ids.get(request_id)
        if cached is not None:
            cached = dict(cached)
            cached["cached"] = True
            return cached
    start = time.monotonic()
    try:
        value = execute_tool(cfg, tool, args)
        result = make_result(request_id, tool, "ok", value=value)
    except ToolError as e:
        result = make_result(request_id, tool, "error",
                             error={"code": e.code, "message": e.message,
                                    **e.extra})
    except Exception as e:  # noqa: BLE001 - report, never crash the server
        result = make_result(request_id, tool, "error",
                             error={"code": "INTERNAL_ERROR",
                                    "message": "%s: %s" % (type(e).__name__, e)})
        log("internal error in %s: %s" % (tool, traceback.format_exc()))
    result["elapsed_ms"] = int((time.monotonic() - start) * 1000)
    if isinstance(request_id, str) and request_id:
        record_id(request_id, result)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalHandsBridge/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: N802
        log("http %s" % (fmt % args))

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:3080")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:3080")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"status": "ok",
                                  "protocol": PROTOCOL_VERSION,
                                  "version": 1,
                                  "pid": os.getpid()})
            return
        if self.path == "/capabilities":
            self._send_json(200, {
                "protocol": PROTOCOL_VERSION,
                "version": 1,
                "mode": self.server.cfg["mode"],
                "tools": TOOL_CAPABILITIES,
                "browser_tools": [
                    {"name": "browser.status",
                     "desc": "Extension status and current chat automation state."},
                    {"name": "browser.tabs",
                     "desc": "List open tabs (id, url, title, active)."},
                    {"name": "browser.open",
                     "desc": "Open a URL in a new tab."},
                    {"name": "browser.navigate",
                     "desc": "Navigate a tab to a URL."},
                    {"name": "browser.read_page",
                     "desc": "Return tab id, URL, title and body text (size-limited)."},
                ],
            })
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": "bad_json",
                                  "message": "body must be valid JSON"})
            return
        if self.path == "/invoke":
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "bad_payload",
                                      "message": "expected a JSON object"})
                return
            tool = payload.get("tool")
            args = payload.get("args", {})
            request_id = payload.get("id")
            if request_id is not None and not isinstance(request_id, str):
                request_id = str(request_id)
            result = invoke_one(self.server.cfg, request_id,
                                str(tool or ""), args if isinstance(args, dict) else {})
            code = 200 if result["status"] == "ok" else 400
            self._send_json(code, result)
            return
        if self.path == "/batch":
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "bad_payload",
                                      "message": "expected a JSON object"})
                return
            calls = payload.get("calls")
            if not isinstance(calls, list):
                self._send_json(400, {"error": "bad_payload",
                                      "message": "expected calls array"})
                return
            results = []
            for call in calls:
                if not isinstance(call, dict):
                    results.append(make_result(
                        call.get("id") if isinstance(call, dict) else None,
                        str(call.get("tool", "")) if isinstance(call, dict) else "",
                        "error",
                        error={"code": "BAD_CALL", "message": "call must be an object"}))
                    continue
                tool = str(call.get("tool", ""))
                args = call.get("args", {})
                request_id = call.get("id")
                if request_id is not None and not isinstance(request_id, str):
                    request_id = str(request_id)
                results.append(invoke_one(self.server.cfg, request_id,
                                          tool, args if isinstance(args, dict) else {}))
            self._send_json(200, {"results": results})
            return
        self._send_json(404, {"error": "not_found"})


def main():
    cfg = load_config()
    if cfg["host"] != "127.0.0.1":
        raise SystemExit("refusing to bind host %s: must be 127.0.0.1" % cfg["host"])
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), Handler)
    server.daemon_threads = True
    server.cfg = cfg

    pid = os.getpid()
    with open(PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(pid))

    log("bridge started pid=%d port=%d mode=%s" % (pid, cfg["port"], cfg["mode"]))
    print("Local Hands bridge listening on http://127.0.0.1:%d (pid %d)"
          % (cfg["port"], pid))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if os.path.exists(PID_PATH):
                with open(PID_PATH, "r", encoding="utf-8") as f:
                    if f.read().strip() == str(pid):
                        os.remove(PID_PATH)
        except OSError:
            pass
        server.server_close()
        log("bridge stopped pid=%d" % pid)


if __name__ == "__main__":
    main()
