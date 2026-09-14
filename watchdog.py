from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

VERSION = "0.5.0"
POLL_SECONDS = 15
EARLY_RATE_LIMIT_POLL_SECONDS = 60
EARLY_READY_CONFIRM_SECONDS = 5 * 60
RESET_GRACE_SECONDS = 45
API_RETRY_SECONDS = 60
LOOKBACK_SECONDS = 2 * 24 * 3600
MAX_HANDLED_AGE = 14 * 24 * 3600

RESUME_PROMPT = (
    "\u7ee7\u7eed\u521a\u624d\u56e0 Codex \u4e94\u5c0f\u65f6\u989d\u5ea6\u9650\u5236\u800c\u4e2d\u65ad\u7684\u4efb\u52a1\u3002"
    "\u5148\u68c0\u67e5\u5f53\u524d\u5de5\u4f5c\u533a\u3001\u5df2\u5b8c\u6210\u7684\u64cd\u4f5c\u548c\u4f1a\u8bdd\u4e0a\u4e0b\u6587\uff0c"
    "\u4e0d\u8981\u91cd\u590d\u5df2\u7ecf\u5b8c\u6210\u7684\u5de5\u4f5c\uff1b\u7136\u540e\u4ece\u4e2d\u65ad\u5904\u7ee7\u7eed\u539f\u4efb\u52a1\u3002"
)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
LOG_DIR = APP_DIR / "logs"
RUN_DIR = LOG_DIR / "runs"
STATE_PATH = APP_DIR / "state.json"
LOG_PATH = LOG_DIR / "watchdog.log"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RUN_DIR.mkdir(parents=True, exist_ok=True)

CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
HISTORY_DB = CODEX_HOME / "thread_history_1.sqlite"
STATE_DB = CODEX_HOME / "state_5.sqlite"
QUEUE_DB = CODEX_HOME / "queue_1.sqlite"


def find_codex_exe() -> Path:
    candidates = [
        Path(r"C:\tools\CodexMultiProviderApp\resources\codex.exe"),
        Path(r"C:\Program Files\ChatGPT\resources\codex.exe"),
    ]
    for p in candidates:
        if p.exists():
            return p
    import shutil

    found = shutil.which("codex")
    if found:
        return Path(found)
    raise FileNotFoundError("codex.exe not found")


CODEX_EXE = find_codex_exe()


def local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def fmt_ts(ts: float | int | None) -> str:
    if not ts:
        return "unknown"
    return dt.datetime.fromtimestamp(float(ts), tz=local_now().tzinfo).strftime("%Y-%m-%d %H:%M:%S %z")


def log(msg: str) -> None:
    line = f"[{local_now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": 1, "handled": {}, "pending": {}, "running": None}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        data.setdefault("version", 1)
        data.setdefault("handled", {})
        data.setdefault("pending", {})
        data.setdefault("running", None)
        return data
    except Exception as exc:
        log(f"State file unreadable, starting fresh: {exc}")
        return {"version": 1, "handled": {}, "pending": {}, "running": None}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def ro_connect(path: Path) -> sqlite3.Connection:
    uri = "file:" + path.as_posix() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2)


def get_thread_meta(thread_id: str) -> dict[str, Any] | None:
    try:
        with ro_connect(STATE_DB) as c:
            row = c.execute(
                "select title,thread_source,archived,cwd,model,reasoning_effort from threads where id=?",
                (thread_id,),
            ).fetchone()
            parent = c.execute(
                "select parent_thread_id from thread_spawn_edges where child_thread_id=? limit 1",
                (thread_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {
        "title": row[0] or "",
        "thread_source": row[1],
        "archived": bool(row[2]),
        "cwd": row[3] or "",
        "model": row[4],
        "reasoning_effort": row[5],
        "parent_thread_id": parent[0] if parent else None,
    }


def is_root_user_thread(thread_id: str) -> tuple[bool, dict[str, Any] | None]:
    meta = get_thread_meta(thread_id)
    if not meta:
        return False, None
    if meta["archived"]:
        return False, meta
    if meta["parent_thread_id"]:
        return False, meta
    if meta["thread_source"] == "subagent":
        return False, meta
    return True, meta


def parse_error(error_json: str | None) -> dict[str, Any] | None:
    if not error_json:
        return None
    try:
        value = json.loads(error_json)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def parse_reset_from_message(message: str, completed_at: int) -> int:
    # Current form: "try again at Sep 10th, 2026 3:24 AM."
    m = re.search(
        r"try again at\s+(?:(?P<mon>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,\s+(?P<year>\d{4})\s+)?(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)",
        message,
        re.IGNORECASE,
    )
    tz = local_now().tzinfo
    if not m:
        return int(completed_at + 5 * 3600)

    hour = int(m.group("hour")) % 12
    if m.group("ampm").upper() == "PM":
        hour += 12
    minute = int(m.group("minute"))
    failure_dt = dt.datetime.fromtimestamp(completed_at, tz=tz)

    if m.group("mon"):
        mon = dt.datetime.strptime(m.group("mon")[:3], "%b").month
        candidate = dt.datetime(
            int(m.group("year")), mon, int(m.group("day")), hour, minute, tzinfo=tz
        )
    else:
        candidate = failure_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate < failure_dt - dt.timedelta(minutes=1):
            candidate += dt.timedelta(days=1)
    return int(candidate.timestamp())


def latest_turn(thread_id: str) -> dict[str, Any] | None:
    try:
        with ro_connect(HISTORY_DB) as c:
            row = c.execute(
                "select turn_id,rollout_ordinal,status,error_json,started_at,completed_at "
                "from thread_turns where thread_id=? order by rollout_ordinal desc limit 1",
                (thread_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {
        "turn_id": row[0],
        "ordinal": row[1],
        "status": row[2],
        "error_json": row[3],
        "started_at": row[4],
        "completed_at": row[5],
    }


def queued_count(thread_id: str) -> int:
    try:
        with ro_connect(QUEUE_DB) as c:
            row = c.execute(
                "select count(*) from queued_items where thread_id=?", (thread_id,)
            ).fetchone()
            return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def scan_limit_failures() -> list[dict[str, Any]]:
    cutoff = int(time.time()) - LOOKBACK_SECONDS
    try:
        with ro_connect(HISTORY_DB) as c:
            rows = c.execute(
                """
                select t.thread_id,t.turn_id,t.rollout_ordinal,t.status,t.error_json,t.completed_at
                from thread_turns t
                join (
                    select thread_id,max(rollout_ordinal) as max_ord
                    from thread_turns group by thread_id
                ) x on x.thread_id=t.thread_id and x.max_ord=t.rollout_ordinal
                where t.status='failed' and coalesce(t.completed_at,0)>=?
                """,
                (cutoff,),
            ).fetchall()
    except sqlite3.Error as exc:
        log(f"History database read failed: {exc}")
        return []

    out: list[dict[str, Any]] = []
    for thread_id, turn_id, ordinal, status, error_json, completed_at in rows:
        err = parse_error(error_json)
        if not err or err.get("codexErrorInfo") != "usageLimitExceeded":
            continue
        ok, meta = is_root_user_thread(thread_id)
        if not ok or not meta:
            continue
        message = str(err.get("message") or "")
        reset_at = parse_reset_from_message(message, int(completed_at or time.time()))
        out.append(
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "ordinal": ordinal,
                "completed_at": int(completed_at or time.time()),
                # Keep the reset parsed from the actual usage-limit failure as the
                # authoritative scheduled reset. API snapshots are stored separately
                # because they can briefly report a bogus new five-hour window.
                "message_reset_at": reset_at,
                "due_at": reset_at + RESET_GRACE_SECONDS,
                "api_reset_at": None,
                "next_probe_at": int(time.time()) + EARLY_RATE_LIMIT_POLL_SECONDS,
                "early_ready_since": None,
                "early_ready_reset_at": None,
                "early_ready_samples": 0,
                "official_due_checked": False,
                "message": message,
                "title": meta["title"],
                "cwd": meta["cwd"],
                "model": meta["model"],
            }
        )
    out.sort(key=lambda x: x["completed_at"])
    return out


class AppServerProbe:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self.next_id = 1

    def __enter__(self) -> "AppServerProbe":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [str(CODEX_EXE), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )
        assert self.proc.stdout is not None
        threading.Thread(target=self._reader, daemon=True).start()
        init = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-continue",
                    "title": "Codex Continue",
                    "version": VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=12,
        )
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init['error']}")
        return self

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\r\n"))

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: int = 12) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdin is not None
        request_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == request_id:
                return obj
        raise TimeoutError(f"App Server request timed out: {method}")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def read_rate_limits() -> dict[str, Any]:
    with AppServerProbe() as app:
        response = app.request("account/rateLimits/read", timeout=15)
    if "error" in response:
        raise RuntimeError(str(response["error"]))
    return response.get("result", {})


def rate_limit_ready(snapshot: dict[str, Any]) -> tuple[bool, int | None, str]:
    limits = snapshot.get("rateLimits") or {}
    primary = limits.get("primary") or {}
    used = primary.get("usedPercent")
    resets_at = primary.get("resetsAt")
    reached_type = limits.get("rateLimitReachedType")
    ready = (used is not None and float(used) < 100.0 and not reached_type)
    detail = f"usedPercent={used}, reachedType={reached_type}, resetsAt={resets_at}"
    return ready, int(resets_at) if resets_at else None, detail


def resume_process(
    thread_id: str, failed_turn_id: str, thread_cwd: str | None
) -> subprocess.Popen[Any]:
    stamp = local_now().strftime("%Y%m%d-%H%M%S")
    run_log = RUN_DIR / f"{stamp}-{thread_id}.log"
    f = run_log.open("ab", buffering=0)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = [
        str(CODEX_EXE),
        "queue",
        "--thread",
        thread_id,
        "--message",
        RESUME_PROMPT,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=f,
        stderr=subprocess.STDOUT,
        cwd=str(APP_DIR),
        creationflags=flags,
    )
    f.close()
    log(
        f"Resume queued through existing app-server: thread={thread_id} "
        f"failed_turn={failed_turn_id} pid={proc.pid} log={run_log}"
    )
    return proc

def update_early_ready_confirmation(
    item: dict[str, Any], now: int, api_reset: int | None
) -> tuple[bool, str]:
    """Require a stable early-ready window before trusting an early quota reset."""
    if api_reset is None:
        item["early_ready_since"] = None
        item["early_ready_reset_at"] = None
        item["early_ready_samples"] = 0
        return False, "resetAt missing; early reset cannot be confirmed"

    previous_reset = item.get("early_ready_reset_at")
    since = item.get("early_ready_since")

    # A changed resetAt means the backend changed its story. Restart the clock.
    if since is None or previous_reset != api_reset:
        item["early_ready_since"] = now
        item["early_ready_reset_at"] = api_reset
        item["early_ready_samples"] = 1
        return False, "early-ready confirmation started"

    item["early_ready_samples"] = int(item.get("early_ready_samples", 0)) + 1
    elapsed = now - int(since)
    if elapsed >= EARLY_READY_CONFIRM_SECONDS:
        return True, f"early-ready stable for {elapsed}s"
    return False, f"early-ready stable for {elapsed}s/{EARLY_READY_CONFIRM_SECONDS}s"


def clear_early_ready_confirmation(item: dict[str, Any]) -> None:
    item["early_ready_since"] = None
    item["early_ready_reset_at"] = None
    item["early_ready_samples"] = 0


def candidate_still_valid(item: dict[str, Any]) -> tuple[bool, str]:
    cur = latest_turn(item["thread_id"])
    if not cur:
        return False, "latest turn unavailable"
    if cur["turn_id"] != item["turn_id"]:
        return False, f"a newer turn exists ({cur['turn_id']})"
    if cur["status"] != "failed":
        return False, f"latest turn status is {cur['status']}"
    err = parse_error(cur["error_json"])
    if not err or err.get("codexErrorInfo") != "usageLimitExceeded":
        return False, "latest failure is no longer usageLimitExceeded"
    if queued_count(item["thread_id"]) > 0:
        return False, "thread already has queued user input"
    return True, "ok"


def prune_state(state: dict[str, Any]) -> None:
    cutoff = int(time.time()) - MAX_HANDLED_AGE
    state["handled"] = {
        k: v for k, v in state.get("handled", {}).items() if int(v.get("at", 0)) >= cutoff
    }


def acquire_mutex() -> Any:
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\CodexContinueWatchdog-v1")
    if not handle:
        raise OSError("CreateMutexW failed")
    if kernel32.GetLastError() == 183:
        print("CodexContinue is already running.")
        sys.exit(0)
    return handle


def main() -> int:
    acquire_mutex()
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW("CodexContinue - Codex quota auto resume")
        except Exception:
            pass

    state = load_state()
    # Migrate pending plans written by v0.2.0 without trusting their last API reset.
    for item in state.get("pending", {}).values():
        if "message_reset_at" not in item:
            legacy_due = int(item.get("due_at", 0) or 0)
            legacy_reset = int(item.get("reset_at", 0) or max(0, legacy_due - RESET_GRACE_SECONDS))
            item["message_reset_at"] = legacy_reset
            item["due_at"] = legacy_reset + RESET_GRACE_SECONDS
        item.setdefault("api_reset_at", None)
        item.setdefault("next_probe_at", int(time.time()) + EARLY_RATE_LIMIT_POLL_SECONDS)
        item.setdefault("early_ready_since", None)
        item.setdefault("early_ready_reset_at", None)
        item.setdefault("early_ready_samples", 0)
        item.setdefault("official_due_checked", False)

    if state.get("running"):
        log(f"Discarding stale watcher process marker from a previous run: {state['running']}")
        state["running"] = None
    running_proc: subprocess.Popen[Any] | None = None
    running_thread: str | None = None
    log(f"CodexContinue v{VERSION} started. codex={CODEX_EXE}")
    log(f"Watching {HISTORY_DB}")

    while True:
        try:
            prune_state(state)

            if running_proc is not None:
                rc = running_proc.poll()
                if rc is None:
                    save_state(state)
                    time.sleep(POLL_SECONDS)
                    continue
                running_info = state.get("running") or {}
                failed_turn_id = running_info.get("failed_turn_id")
                log(f"Resume process finished: thread={running_thread} exit={rc}")
                state["running"] = None
                if rc != 0 and failed_turn_id:
                    # The resume command itself failed before a new turn was created.
                    # Re-arm the failed quota turn so the watcher can retry after a short backoff.
                    state["handled"].pop(failed_turn_id, None)
                    log(
                        f"Resume command failed; re-arming recovery: "
                        f"thread={running_thread} failed_turn={failed_turn_id}"
                    )
                    time.sleep(API_RETRY_SECONDS)
                running_proc = None
                running_thread = None

            failures = scan_limit_failures()
            visible_ids = {x["turn_id"] for x in failures}

            # Drop stale pending plans automatically.
            for turn_id in list(state["pending"].keys()):
                if turn_id not in visible_ids:
                    old = state["pending"].pop(turn_id)
                    log(f"Pending recovery cancelled as stale: thread={old.get('thread_id')} turn={turn_id}")

            # Register newly observed quota failures.
            for item in failures:
                turn_id = item["turn_id"]
                if turn_id in state["handled"] or turn_id in state["pending"]:
                    continue
                state["pending"][turn_id] = item
                log(
                    f"Quota failure detected: thread={item['thread_id']} title={item['title']!r} "
                    f"turn={turn_id} reset={fmt_ts(item['message_reset_at'])} planned={fmt_ts(item['due_at'])}"
                )

            # Resume one root user thread at a time.
            now = int(time.time())

            def next_action_at(x: dict[str, Any]) -> int:
                times = [int(x.get("next_probe_at", now) or now)]
                if not bool(x.get("official_due_checked", False)):
                    times.append(int(x.get("due_at", now) or now))
                return min(times)

            due = sorted(
                (x for x in state["pending"].values() if next_action_at(x) <= now),
                key=lambda x: (next_action_at(x), x.get("completed_at", 0)),
            )
            if due:
                item = due[0]
                turn_id = item["turn_id"]
                valid, reason = candidate_still_valid(item)
                if not valid:
                    log(f"Recovery cancelled: thread={item['thread_id']} turn={turn_id}: {reason}")
                    state["pending"].pop(turn_id, None)
                    state["handled"][turn_id] = {"at": now, "result": "cancelled", "reason": reason}
                else:
                    official_due_reached = now >= int(item.get("due_at", 0) or 0)
                    try:
                        snapshot = read_rate_limits()
                        ready, api_reset, detail = rate_limit_ready(snapshot)
                    except Exception as exc:
                        # If the scheduled reset point has already passed, do not hammer
                        # the API every 15 seconds; retry on the normal API cadence.
                        if official_due_reached:
                            item["official_due_checked"] = True
                        item["next_probe_at"] = now + API_RETRY_SECONDS
                        log(f"Rate-limit check failed; retrying later: {exc}")
                    else:
                        item["api_reset_at"] = api_reset
                        if official_due_reached:
                            item["official_due_checked"] = True

                        should_resume = False
                        resume_reason = ""

                        if not ready:
                            if item.get("early_ready_since") is not None:
                                log(
                                    f"Early-ready confirmation reset: thread={item['thread_id']} "
                                    f"quota became unavailable again ({detail})"
                                )
                            clear_early_ready_confirmation(item)
                            item["next_probe_at"] = now + EARLY_RATE_LIMIT_POLL_SECONDS
                            log(
                                f"Quota still unavailable ({detail}); "
                                f"next check={fmt_ts(item['next_probe_at'])}"
                            )
                        elif official_due_reached:
                            # After the reset time explicitly returned by the actual
                            # usageLimitExceeded failure, a single fresh ready snapshot is enough.
                            clear_early_ready_confirmation(item)
                            should_resume = True
                            resume_reason = "scheduled reset passed and quota is available"
                        else:
                            # Before the scheduled reset, quota can briefly report a bogus
                            # new window. Require five minutes of stable readiness and the
                            # same resetAt value before trusting an early reset.
                            confirmed, confirm_detail = update_early_ready_confirmation(
                                item, now, api_reset
                            )
                            item["next_probe_at"] = now + EARLY_RATE_LIMIT_POLL_SECONDS
                            if confirmed:
                                should_resume = True
                                resume_reason = confirm_detail
                            else:
                                log(
                                    f"Quota appears available early ({detail}); {confirm_detail}; "
                                    f"next confirmation={fmt_ts(item['next_probe_at'])}"
                                )

                        if should_resume:
                            # Last-moment safety check after quota confirmation.
                            valid, reason = candidate_still_valid(item)
                            if not valid:
                                log(
                                    f"Recovery cancelled at final check: "
                                    f"thread={item['thread_id']}: {reason}"
                                )
                                state["pending"].pop(turn_id, None)
                                state["handled"][turn_id] = {
                                    "at": now,
                                    "result": "cancelled",
                                    "reason": reason,
                                }
                            else:
                                log(
                                    f"Quota confirmed for recovery: thread={item['thread_id']} "
                                    f"reason={resume_reason}"
                                )
                                running_proc = resume_process(item["thread_id"], turn_id, item.get("cwd"))
                                running_thread = item["thread_id"]
                                state["pending"].pop(turn_id, None)
                                state["handled"][turn_id] = {
                                    "at": now,
                                    "result": "resume_launched",
                                    "thread_id": item["thread_id"],
                                    "pid": running_proc.pid,
                                }
                                state["running"] = {
                                    "thread_id": item["thread_id"],
                                    "failed_turn_id": turn_id,
                                    "pid": running_proc.pid,
                                    "started_at": now,
                                }

            save_state(state)
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log("Stopped by user.")
            save_state(state)
            return 0
        except Exception as exc:
            log(f"Watchdog loop error: {type(exc).__name__}: {exc}")
            try:
                save_state(state)
            except Exception:
                pass
            time.sleep(API_RETRY_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())


