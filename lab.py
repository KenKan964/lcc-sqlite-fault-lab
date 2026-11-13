"""Neutral stdlib process/SQLite laboratory shared by both comparison arms.

Contains no diagnosis rules or scenario sequencing. Not a production supervisor.
"""
from __future__ import annotations
import json
import queue
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from adapter import connect, initialize, snapshot

TIMEOUT = 10


class Child:
    def __init__(self, lab, name, **options):
        self.lab, self.name, self.events = lab, name, []
        self.q = queue.Queue()
        # Worker arguments are a closed protocol. In particular, a scenario plan
        # must never override --sandbox or --token, even via a duplicate flag.
        allowed = {"pause", "mutant", "amount", "op"}
        if set(options) - allowed:
            raise ValueError("unsupported worker option")
        if "pause" in options:
            points = options["pause"]
            if not isinstance(points, str) or not set(points.split(",")).issubset(
                    {"", "before_begin", "before_commit", "after_commit", "commit_busy"}):
                raise ValueError("invalid checkpoint")
        if "mutant" in options and options["mutant"] not in {"none", "duplicate", "false_complete"}:
            raise ValueError("invalid test mutant")
        if "amount" in options and (type(options["amount"]) is not int or
                                    not 0 <= options["amount"] <= 1000000000):
            raise ValueError("invalid synthetic amount")
        if "op" in options and (not isinstance(options["op"], str) or
                                not 1 <= len(options["op"]) <= 80 or
                                not all(ch.isalnum() or ch in "-_" for ch in options["op"])):
            raise ValueError("invalid synthetic operation ID")
        cmd = [sys.executable, "-u", str(Path(__file__).with_name("worker.py")),
               "--sandbox", str(lab.directory), "--token", lab.token]
        for key, val in options.items():
            cmd.extend(["--" + key, str(val)])
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                  shell=False)
        self.stderr = ""

        def read_stdout():
            try:
                for line in self.p.stdout:
                    self.q.put(json.loads(line))
            except Exception as exc:
                self.q.put({"event": "protocol_error", "detail": str(exc)})
            finally:
                self.q.put(None)

        def read_stderr():
            self.stderr = self.p.stderr.read()

        self.reader = threading.Thread(target=read_stdout, daemon=True)
        self.err_reader = threading.Thread(target=read_stderr, daemon=True)
        self.reader.start()
        self.err_reader.start()
        lab.record("spawn", child=name, pid=self.p.pid, options=options)

    def receive(self):
        event = self.q.get(timeout=TIMEOUT)
        if event is None:
            raise RuntimeError(f"unexpected EOF: {self.name}: {self.stderr}")
        self.events.append(event)
        self.lab.record("child_event", child=self.name, data=event)
        return event

    def wait_point(self, point):
        while True:
            e = self.receive()
            if e["event"] == "checkpoint" and e["point"] == point:
                return e

    def go(self):
        self.p.stdin.write("continue\n")
        self.p.stdin.flush()
        self.lab.record("continue", child=self.name)

    def finish(self, *, killed=False):
        self.p.wait(timeout=TIMEOUT)
        self.reader.join(TIMEOUT)
        self.err_reader.join(TIMEOUT)
        while True:
            try:
                e = self.q.get_nowait()
            except queue.Empty:
                break
            if e is not None:
                self.events.append(e)
                self.lab.record("child_event", child=self.name, data=e)
        self.lab.record("exit", child=self.name, pid=self.p.pid,
                        returncode=self.p.returncode, killed=killed, stderr=self.stderr)
        for stream in (self.p.stdin, self.p.stdout, self.p.stderr):
            stream.close()
        if not killed and self.p.returncode != 0:
            raise RuntimeError(f"worker failed: {self.stderr}")

    def kill(self):
        # Only this live Popen handle created here; no arbitrary PID API.
        if self.p.poll() is not None:
            raise RuntimeError("child exited before requested injection")
        self.lab.record("kill_requested", child=self.name, pid=self.p.pid,
                        last_checkpoint=next((e.get("point") for e in reversed(self.events)
                                              if e["event"] == "checkpoint"), None))
        self.p.kill()
        self.finish(killed=True)


class Lab:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.token = secrets.token_hex(24)
        self.path = initialize(self.directory, self.token)
        self.children = {}
        self.lock_connection = None
        self.trace = []
        self.start = time.perf_counter_ns()
        self.recovery_state = "UNOBSERVED"

    def record(self, event, **data):
        entry = {"seq": len(self.trace), "elapsed_ns": time.perf_counter_ns() - self.start,
                 "event": event, **data}
        self.trace.append(entry)
        # Parent-owned append log survives a test-child kill.
        with (self.directory / "trace.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def spawn(self, name, **options):
        if name in self.children:
            raise ValueError("unique child name required")
        child = Child(self, name, **options)
        self.children[name] = child
        return child

    def lock(self, kind):
        if self.lock_connection:
            raise ValueError("already locked")
        c = connect(self.path)
        try:
            c.execute({"reader": "BEGIN", "writer": "BEGIN IMMEDIATE",
                       "exclusive": "BEGIN EXCLUSIVE"}[kind])
            c.execute("SELECT * FROM receipts").fetchall()
        except Exception:
            c.close()
            raise
        self.lock_connection = c
        self.record("lock_acquired", kind=kind)

    def unlock(self):
        if not self.lock_connection:
            raise ValueError("no lock")
        self.lock_connection.execute("ROLLBACK")
        self.lock_connection.close()
        self.lock_connection = None
        self.record("lock_released")

    def observe(self):
        try:
            result = {"readable": True, **snapshot(self.path)}
        except sqlite3.Error as exc:
            result = {"readable": False, "code": getattr(exc, "sqlite_errorcode", None),
                      "name": getattr(exc, "sqlite_errorname", None)}
        self.record("snapshot", data=result)
        return result

    def cleanup(self):
        for c in self.children.values():
            if c.p.poll() is None:
                c.kill()
        if self.lock_connection:
            self.unlock()

    def archive(self):
        return {"trace": self.trace, "children": {
            name: {"events": c.events, "returncode": c.p.returncode, "stderr": c.stderr}
            for name, c in self.children.items()}}
