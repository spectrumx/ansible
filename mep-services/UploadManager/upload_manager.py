#!/usr/bin/env python3
"""Background upload service with SDS as its first destination."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import queue
import re
import sqlite3
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

SERVICE_NAME = "uploadmanager"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("UPLOAD_MANAGER_CONFIG", BASE_DIR / "upload_manager.yaml"))
ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
STATUS_TOPIC = f"{SERVICE_NAME}/status"
DATA_TOPIC = f"{SERVICE_NAME}/data"
EVENT_TOPIC = f"{SERVICE_NAME}/event"
LOCK_NAME = ".upload_manager.lock"
CAPTURE_IDENTITY_FILENAME = "capture_identity.json"
ACTIVE_STATES = {"queued", "scanning", "authenticating", "uploading", "verifying", "verification_pending", "pause_requested", "cancelling", "waiting_for_retry", "waiting_for_credentials"}
TERMINAL_STATES = {"complete", "paused", "cancelled", "failed"}
INTENT_STATES = ACTIVE_STATES | {"paused"}
ALL_STATES = ACTIVE_STATES | TERMINAL_STATES
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

COMMANDS = {
    "get_status": {"description": "Return upload-manager state and jobs.", "arguments": {}},
    "get_uploads": {"description": "Return all upload jobs.", "arguments": {}},
    "get_upload": {"description": "Return one upload job.", "arguments": {"job_id": {"type": "string", "required": True}}},
    "get_upload_activity": {"description": "Return the durable activity timeline for one upload job.", "arguments": {"job_id": {"type": "string", "required": True}, "limit": {"type": "integer", "default": 100}}},
    "check_sds": {"description": "Check whether a capture's current data exists on SDS with matching sizes.", "arguments": {"capture_id": {"type": "string", "required": True}, "credentials": {"type": "object", "required": False}}},
    "start_upload": {"description": "Queue a capture upload.", "arguments": {"capture_id": {"type": "string", "required": True}, "destination": {"type": "string", "required": True}, "credentials": {"type": "object", "required": False}, "dry_run": {"type": "boolean", "default": False}}},
    "pause_upload": {"description": "Pause automatic processing of an upload job.", "arguments": {"job_id": {"type": "string", "required": True}}},
    "resume_upload": {"description": "Resume a paused upload job.", "arguments": {"job_id": {"type": "string", "required": True}, "credentials": {"type": "object", "required": False}}},
    "stop_upload": {"description": "Cancel an upload job.", "arguments": {"job_id": {"type": "string", "required": True}}},
    "retry_upload": {"description": "Retry a failed or cancelled upload.", "arguments": {"job_id": {"type": "string", "required": True}, "dry_run": {"type": "boolean", "required": False}}},
    "delete_upload": {"description": "Delete an inactive upload record.", "arguments": {"job_id": {"type": "string", "required": True}}},
}


class VerificationUnavailable(RuntimeError):
    """Remote verification could not complete because SDS was unavailable."""


class _ActivityCapture(io.TextIOBase):
    """Mirror SDK output while separating redraws from durable lines."""

    def __init__(self, emit, emit_progress, mirror=None):
        self._emit = emit
        self._emit_progress = emit_progress
        self._mirror = mirror
        self._buffer = ""
        self._last_message = None
        self._last_progress = None
        self._lock = threading.Lock()

    def writable(self):
        return True

    def isatty(self):
        return bool(self._mirror is not None and self._mirror.isatty())

    def write(self, value):
        if not value:
            return 0
        if self._mirror is not None:
            self._mirror.write(value)
            self._mirror.flush()
        with self._lock:
            self._buffer += ANSI_ESCAPE.sub("", str(value))
            while True:
                separators = [position for position in (self._buffer.find("\r"), self._buffer.find("\n")) if position >= 0]
                if not separators:
                    break
                position = min(separators)
                message = self._buffer[:position]
                separator = self._buffer[position]
                self._buffer = self._buffer[position + 1:]
                self._offer(message, progress=separator == "\r")
        return len(value)

    def flush(self):
        with self._lock:
            if self._buffer:
                self._offer(self._buffer, progress=True)
                self._buffer = ""

    def close(self):
        with self._lock:
            if self._buffer:
                self._offer(self._buffer, progress=False)
                self._buffer = ""
        super().close()

    def _offer(self, value, progress):
        message = " ".join(str(value).strip().split())
        if not message:
            return
        if progress:
            if message != self._last_progress:
                self._emit_progress(message)
                self._last_progress = message
            return
        lowered = message.lower()
        urgent = any(word in lowered for word in ("warning", "error", "failed", "timeout"))
        if message != self._last_message or urgent:
            self._emit(message, "warning" if urgent else "info")
            self._last_message = message


class _ActivityLogHandler(logging.Handler):
    def __init__(self, emit):
        super().__init__()
        self._emit = emit
        self.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))

    def emit(self, record):
        try:
            self._emit(self.format(record), record.levelname.lower())
        except Exception:
            self.handleError(record)


class UploadManager:
    """Own upload jobs, persistence, credentials, and destination execution."""

    def __init__(self, config_path: Path = CONFIG_PATH):
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        self.data_root = Path(config.get("data_root", "/data/captures")).resolve()
        self.database_path = Path(config.get("database_path", "/data/upload_manager/state.sqlite3"))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.activity_limit_per_job = max(50, int(config.get("activity_limit_per_job", 500)))
        sds = config.get("sds", {})
        self.sds_host = str(sds.get("host", "sds.crc.nd.edu"))
        self.sds_api_key_environment = str(sds.get("api_key_environment", "SDS_SECRET_TOKEN"))
        self.status_interval_s = max(0.1, float(config.get("status_interval_s", 10)))
        self._db_lock = threading.RLock()
        self._queue = queue.Queue()
        self._cancelled = set()
        self._paused = set()
        self._active_job_id = None
        self._verification_queue = queue.Queue()
        self._verification_pending = set()
        self._worker_running = True
        self._started_at = time.time()
        self._status_seq = 0
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"{SERVICE_NAME}_{uuid.uuid4().hex[:8]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._initialize_database()
        self._recover_jobs()
        self._worker = threading.Thread(target=self._worker_loop, name="upload-manager-worker", daemon=True)
        self._worker.start()
        self._verifier = threading.Thread(target=self._verification_loop, name="upload-manager-verifier", daemon=True)
        self._verifier.start()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_database(self):
        with self._connect() as connection:
            connection.executescript((BASE_DIR / "schema.sql").read_text(encoding="utf-8"))
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(upload_jobs)")}
            if "credentials_json" not in columns:
                connection.execute("ALTER TABLE upload_jobs ADD COLUMN credentials_json TEXT")
            if "dry_run" not in columns:
                connection.execute("ALTER TABLE upload_jobs ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0")
            remote_columns = {row["name"] for row in connection.execute("PRAGMA table_info(remote_captures)")}
            for name, declaration in (
                ("verification_status", "TEXT NOT NULL DEFAULT 'never_checked'"),
                ("last_checked_at", "REAL"),
                ("expected_files", "INTEGER"),
                ("verified_files", "INTEGER"),
                ("missing_files", "INTEGER"),
                ("wrong_size_files", "INTEGER"),
                ("verification_error", "TEXT"),
            ):
                if name not in remote_columns:
                    connection.execute(f"ALTER TABLE remote_captures ADD COLUMN {name} {declaration}")
            connection.execute(
                "UPDATE remote_captures SET verification_status='unavailable',verification_error='service restarted during SDS check' WHERE verification_status='checking'"
            )
            connection.commit()

    def _recover_jobs(self):
        with self._db_lock, self._connect() as connection:
            requeued = [row["job_id"] for row in connection.execute("SELECT job_id FROM upload_jobs WHERE state IN ('scanning','authenticating','uploading','verifying','verification_pending','waiting_for_retry')").fetchall()]
            cancelled = [row["job_id"] for row in connection.execute("SELECT job_id FROM upload_jobs WHERE state='cancelling'").fetchall()]
            paused = [row["job_id"] for row in connection.execute("SELECT job_id FROM upload_jobs WHERE state='pause_requested'").fetchall()]
            connection.execute("UPDATE upload_jobs SET state='queued', last_error='service restarted; job requeued' WHERE state IN ('scanning','authenticating','uploading','verifying','verification_pending','waiting_for_retry')")
            connection.execute("UPDATE upload_jobs SET state='cancelled', finished_at=?, cancelled_at=?, current_file=NULL WHERE state='cancelling'", (time.time(), time.time()))
            connection.execute("UPDATE upload_jobs SET state='paused', finished_at=?, current_file=NULL WHERE state='pause_requested'", (time.time(),))
            connection.commit()
            rows = connection.execute("SELECT job_id FROM upload_jobs WHERE state='queued'").fetchall()
        for job_id in requeued:
            self._record_activity(job_id, "service_recovery", "Service restarted while job was active; job requeued", "warning")
        for job_id in cancelled:
            self._record_activity(job_id, "service_recovery", "Service restarted while Stop was pending; job marked cancelled", "warning")
        for job_id in paused:
            self._record_activity(job_id, "service_recovery", "Service restarted while Pause was pending; job marked paused", "warning")
        for row in rows:
            self._queue.put(row["job_id"])
        for lock_path in self.data_root.glob(f"*/{LOCK_NAME}"):
            try:
                lock_path.unlink()
            except OSError:
                pass
        lease_dir = self.database_path.parent / "leases"
        if lease_dir.is_dir():
            for lease_path in lease_dir.glob("*.json"):
                try:
                    lease_path.unlink()
                except OSError:
                    pass

    def queue_upload(self, arguments):
        capture_id = str(arguments["capture_id"])
        destination = str(arguments.get("destination") or "sds").lower()
        if destination != "sds":
            raise ValueError(f"unsupported upload destination: {destination!r}")
        dry_run = arguments.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        credentials_json = self._encode_credentials(arguments.get("credentials"))
        path = self._capture_path_for_id(capture_id)
        name = path.name
        with self._db_lock, self._connect() as connection:
            active = connection.execute("SELECT 1 FROM upload_jobs WHERE capture_id=? AND state IN ({}) LIMIT 1".format(",".join("?" for _ in INTENT_STATES)), (capture_id, *sorted(INTENT_STATES))).fetchone()
            if active:
                raise ValueError("capture already has an active upload")
            remote = connection.execute("SELECT remote_path FROM remote_captures WHERE capture_id=? AND destination=?", (capture_id, destination)).fetchone()
            if remote is not None and remote["remote_path"]:
                sds_path = remote["remote_path"]
            else:
                sds_path = self._new_sds_path(name)
                if remote is None:
                    connection.execute("INSERT INTO remote_captures(capture_id,destination,remote_path) VALUES(?, ?, ?)", (capture_id, destination, sds_path))
                else:
                    connection.execute("UPDATE remote_captures SET remote_path=? WHERE capture_id=? AND destination=?", (sds_path, capture_id, destination))
            job_id = uuid.uuid4().hex
            connection.execute(
                "DELETE FROM upload_jobs WHERE capture_id=? AND destination=? AND state IN ('complete','paused','cancelled','failed')",
                (capture_id, destination),
            )
            connection.execute("INSERT INTO upload_jobs(job_id,capture_id,name,path,destination,sds_path,state,requested_at,credentials_json,dry_run) VALUES(?,?,?,?,?,?,'queued',?,?,?)", (job_id, capture_id, name, str(path), destination, sds_path, time.time(), credentials_json, int(dry_run)))
            connection.commit()
        job = self.get_upload(job_id)
        self._record_activity(job_id, "queued", f"Upload queued for {destination}; dry_run={dry_run}")
        self._queue.put(job_id)
        self.publish_event("upload_queued", job)
        self.publish_status()
        return job

    def _capture_path_for_id(self, capture_id):
        for path in self.data_root.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            metadata_path = path / CAPTURE_IDENTITY_FILENAME
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(metadata, dict) and metadata.get("capture_id") == capture_id:
                return path.resolve()
        raise ValueError(f"capture not found: {capture_id!r}")

    @staticmethod
    def _new_sds_path(capture_name):
        hostname = "".join(character for character in socket.gethostname().lower() if character.isalnum() or character in "_-")
        if not hostname:
            raise RuntimeError("unable to derive hostname token for SDS path")
        return f"{capture_name}_{hostname}_{uuid.uuid4().hex[:6]}"

    def get_upload(self, job_id):
        record = self._get_upload_record(job_id)
        record["credentials_stored"] = bool(record.get("credentials_json"))
        record.pop("credentials_json", None)
        return record

    def _get_upload_record(self, job_id):
        with self._db_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT upload_jobs.*, (SELECT MAX(activity_id) FROM upload_job_activity WHERE upload_job_activity.job_id=upload_jobs.job_id) AS activity_revision FROM upload_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"upload job not found: {job_id!r}")
        return dict(row)

    def get_uploads(self):
        with self._db_lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT upload_jobs.*, (SELECT MAX(activity_id) FROM upload_job_activity WHERE upload_job_activity.job_id=upload_jobs.job_id) AS activity_revision FROM upload_jobs ORDER BY requested_at DESC"
            ).fetchall()
        jobs = [dict(row) for row in rows]
        for job in jobs:
            job["credentials_stored"] = bool(job.get("credentials_json"))
            job.pop("credentials_json", None)
        return jobs

    def get_upload_activity(self, job_id, limit=100):
        self._get_upload_record(job_id)
        limit = max(1, min(500, int(limit)))
        with self._db_lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT activity_id,timestamp,level,event,message FROM upload_job_activity WHERE job_id=? ORDER BY activity_id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_sds_checks(self):
        with self._db_lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT capture_id,destination,remote_path,verification_status,last_checked_at,expected_files,verified_files,missing_files,wrong_size_files,verification_error FROM remote_captures ORDER BY capture_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def check_sds(self, arguments):
        capture_id = str(arguments.get("capture_id") or "")
        if not capture_id:
            raise ValueError("capture_id is required")
        credentials = arguments.get("credentials")
        if credentials is not None and not isinstance(credentials, dict):
            raise ValueError("credentials must be an object")
        with self._db_lock:
            if self._active_job_id is not None:
                raise ValueError("SDS check is unavailable while an upload is running")
            if capture_id in self._verification_pending:
                raise ValueError("SDS check is already pending for this capture")
        if self._has_active_upload():
            raise ValueError("SDS check is unavailable while an upload is active")
        path = self._capture_path_for_id(capture_id)
        if not (path / "data").is_dir():
            raise ValueError("capture data directory is missing")
        with self._db_lock, self._connect() as connection:
            remote = connection.execute(
                "SELECT remote_path FROM remote_captures WHERE capture_id=? AND destination='sds'",
                (capture_id,),
            ).fetchone()
            if remote is None or not remote["remote_path"]:
                raise ValueError("capture has no assigned SDS path")
            connection.execute(
                "UPDATE remote_captures SET verification_status='checking',verification_error=NULL WHERE capture_id=? AND destination='sds'",
                (capture_id,),
            )
            connection.commit()
        with self._db_lock:
            self._verification_pending.add(capture_id)
        check_id = uuid.uuid4().hex
        self._verification_queue.put({
            "check_id": check_id,
            "capture_id": capture_id,
            "credentials": credentials,
            "source": "manual",
        })
        self.publish_event("sds_check_started", {"check_id": check_id, "capture_id": capture_id})
        self.publish_status()
        return {"accepted": True, "check_id": check_id, "capture_id": capture_id}

    def _verification_loop(self):
        while self._worker_running:
            request = self._verification_queue.get()
            if request is None:
                return
            capture_id = request["capture_id"]
            try:
                self._perform_sds_check(request)
            except Exception as exc:
                logging.warning("SDS check for capture %s failed: %s: %s", capture_id, type(exc).__name__, exc)
                status = self._save_sds_check(capture_id, "unavailable", error=f"{type(exc).__name__}: {exc}")
                self.publish_event("sds_check_completed", {
                    "check_id": request["check_id"],
                    **status,
                    "result": "unavailable",
                    "error": f"{type(exc).__name__}: {exc}",
                })
            finally:
                with self._db_lock:
                    self._verification_pending.discard(capture_id)
                self._verification_queue.task_done()
                self.publish_status()

    def _perform_sds_check(self, request):
        capture_id = request["capture_id"]
        path = self._capture_path_for_id(capture_id) / "data"
        files = []
        for file_path in path.rglob("*"):
            if file_path.is_file() and file_path.name != LOCK_NAME:
                stat = file_path.stat()
                files.append((file_path.relative_to(path).as_posix(), stat.st_size))
        expected = dict(files)
        with self._db_lock, self._connect() as connection:
            remote = connection.execute(
                "SELECT remote_path FROM remote_captures WHERE capture_id=? AND destination='sds'",
                (capture_id,),
            ).fetchone()
            credentials_row = connection.execute(
                "SELECT credentials_json FROM upload_jobs WHERE capture_id=? AND credentials_json IS NOT NULL ORDER BY requested_at DESC LIMIT 1",
                (capture_id,),
            ).fetchone()
        if remote is None or not remote["remote_path"]:
            raise RuntimeError("capture has no assigned SDS path")
        credentials = request.get("credentials") or self._decode_credentials(
            credentials_row["credentials_json"] if credentials_row else None
        )
        token = credentials.get("token") or credentials.get("api_key") or os.environ.get(self.sds_api_key_environment)
        if not token:
            self._save_sds_check(capture_id, "credentials_required", expected_files=len(expected), error="credentials required")
            self.publish_event("sds_check_completed", {
                "check_id": request["check_id"],
                "capture_id": capture_id,
                "sds_path": remote["remote_path"],
                "result": "credentials_required",
                "error": "credentials required",
            })
            return
        import spectrumx
        client = spectrumx.Client(host=self.sds_host, env_config={"SDS_SECRET_TOKEN": token})
        client.dry_run = False
        client.authenticate()
        remote_files = self._remote_inventory(client, remote["remote_path"])
        missing = sorted(set(expected) - set(remote_files))
        wrong_size = sorted(
            relative_path
            for relative_path in set(expected) & set(remote_files)
            if expected[relative_path] != remote_files[relative_path]
        )
        verified_files = len(expected) - len(missing) - len(wrong_size)
        result = "verified" if not missing and not wrong_size else "incomplete"
        status = self._save_sds_check(
            capture_id,
            result,
            expected_files=len(expected),
            verified_files=verified_files,
            missing_files=len(missing),
            wrong_size_files=len(wrong_size),
        )
        self.publish_event("sds_check_completed", {
            "check_id": request["check_id"],
            **status,
            "missing_paths": missing,
            "wrong_size_paths": wrong_size,
        })

    def _save_sds_check(self, capture_id, status, expected_files=None, verified_files=None, missing_files=None, wrong_size_files=None, error=None):
        checked_at = time.time()
        with self._db_lock, self._connect() as connection:
            connection.execute(
                "UPDATE remote_captures SET verification_status=?,last_checked_at=?,expected_files=?,verified_files=?,missing_files=?,wrong_size_files=?,verification_error=? WHERE capture_id=? AND destination='sds'",
                (status, checked_at, expected_files, verified_files, missing_files, wrong_size_files, error, capture_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT capture_id,remote_path,verification_status,last_checked_at,expected_files,verified_files,missing_files,wrong_size_files,verification_error FROM remote_captures WHERE capture_id=? AND destination='sds'",
                (capture_id,),
            ).fetchone()
        return dict(row) if row is not None else {"capture_id": capture_id, "verification_status": status}

    def _has_active_upload(self):
        with self._db_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM upload_jobs WHERE state IN ({}) LIMIT 1".format(
                    ",".join("?" for _ in ACTIVE_STATES)
                ),
                tuple(sorted(ACTIVE_STATES)),
            ).fetchone()
        return row is not None

    def stop_upload(self, job_id):
        job = self.get_upload(job_id)
        with self._db_lock:
            self._cancelled.add(job_id)
            self._paused.discard(job_id)
            is_running = self._active_job_id == job_id
        if job["state"] == "paused":
            self._record_activity(job_id, "stop_requested", "Paused upload intent cancelled", "warning")
            job = self._transition(job_id, "cancelled")
            self.publish_event("upload_cancelled", job)
            self.publish_status()
            return job
        if job["state"] not in ACTIVE_STATES:
            self._record_activity(job_id, "stop_ignored", f"Stop ignored because job is already {job['state']}", "warning")
            return job
        if is_running:
            self._record_activity(job_id, "stop_requested", "Stop requested; waiting for the active SDS SDK call to return", "warning")
            self._set_current_activity(job_id, "Stop requested; waiting for SDS SDK")
            job = self._transition(job_id, "cancelling")
        else:
            self._record_activity(job_id, "stop_requested", "Stop requested before SDK processing began", "warning")
            job = self._transition(job_id, "cancelled")
            self.publish_event("upload_cancelled", job)
        self.publish_status()
        return job

    def pause_upload(self, job_id):
        job = self.get_upload(job_id)
        if job["state"] not in ACTIVE_STATES - {"cancelling", "pause_requested"}:
            raise ValueError("upload job is not active")
        with self._db_lock:
            self._paused.add(job_id)
            is_running = self._active_job_id == job_id
        if is_running:
            self._record_activity(job_id, "pause_requested", "Pause requested; waiting for the active SDS SDK call to return", "warning")
            self._set_current_activity(job_id, "Pause requested; waiting for SDS SDK")
            job = self._transition(job_id, "pause_requested")
        else:
            self._record_activity(job_id, "paused", "Automatic upload processing paused", "warning")
            job = self._transition(job_id, "paused")
            self.publish_event("upload_paused", job)
        self.publish_status()
        return job

    def resume_upload(self, job_id, credentials=None):
        job = self.get_upload(job_id)
        if job["state"] != "paused":
            raise ValueError("upload job is not paused")
        credentials_json = self._encode_credentials(credentials) if credentials is not None else None
        with self._db_lock:
            self._paused.discard(job_id)
            self._cancelled.discard(job_id)
        with self._db_lock, self._connect() as connection:
            fields = ["state='queued'", "finished_at=NULL", "last_error=NULL", "current_file=NULL"]
            values = []
            if credentials_json is not None:
                fields.append("credentials_json=?")
                values.append(credentials_json)
            values.append(job_id)
            connection.execute(f"UPDATE upload_jobs SET {', '.join(fields)} WHERE job_id=?", values)
            connection.commit()
        job = self.get_upload(job_id)
        self._record_activity(job_id, "resumed", "Upload processing resumed")
        self._queue.put(job_id)
        self.publish_event("upload_queued", job)
        self.publish_status()
        return job

    def retry_upload(self, job_id, credentials=None, dry_run=None):
        job = self.get_upload(job_id)
        if job["state"] not in {"complete", "cancelled", "failed", "verification_pending", "waiting_for_retry", "waiting_for_credentials"}:
            raise ValueError("upload job is not retryable")
        if dry_run is not None and not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        credentials_json = self._encode_credentials(credentials) if credentials is not None else None
        with self._db_lock:
            self._cancelled.discard(job_id)
            self._paused.discard(job_id)
        with self._db_lock, self._connect() as connection:
            fields = ["state='queued'", "retry_count=retry_count+1", "finished_at=NULL", "cancelled_at=NULL", "last_error=NULL", "current_file=NULL"]
            values = []
            if credentials_json is not None:
                fields.append("credentials_json=?")
                values.append(credentials_json)
            if dry_run is not None:
                fields.append("dry_run=?")
                values.append(int(bool(dry_run)))
            values.append(job_id)
            connection.execute(f"UPDATE upload_jobs SET {', '.join(fields)} WHERE job_id=?", values)
            connection.commit()
        job = self.get_upload(job_id)
        self._record_activity(job_id, "retry_queued", f"Retry requested; attempt {job['retry_count']}")
        self._queue.put(job_id)
        self.publish_event("upload_queued", job)
        self.publish_status()
        return job

    def delete_upload(self, job_id):
        job = self.get_upload(job_id)
        if job["state"] in INTENT_STATES:
            raise ValueError("cannot delete an active or paused upload intent; cancel it first")
        with self._db_lock, self._connect() as connection:
            connection.execute("DELETE FROM upload_jobs WHERE job_id=?", (job_id,))
            connection.commit()
        return {"job_id": job_id, "deleted": True}

    def _worker_loop(self):
        while self._worker_running:
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                if self.get_upload(job_id)["state"] != "queued":
                    continue
                with self._db_lock:
                    self._active_job_id = job_id
                self._record_activity(job_id, "worker_started", "Background worker started processing the job")
                self._execute(job_id)
            except Exception as exc:
                if self._is_cancelled(job_id):
                    logging.warning(
                        "Upload job %s SDK call ended after Stop was requested: %s: %s",
                        job_id,
                        type(exc).__name__,
                        exc,
                    )
                elif self._is_paused(job_id):
                    logging.warning(
                        "Upload job %s SDK call ended after Pause was requested: %s: %s",
                        job_id,
                        type(exc).__name__,
                        exc,
                    )
                elif isinstance(exc, VerificationUnavailable):
                    logging.warning("Upload job %s verification unavailable: %s", job_id, exc)
                else:
                    logging.exception("Upload job %s failed", job_id)
                self._handle_failure(job_id, exc)
            finally:
                with self._db_lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = None
                self._queue.task_done()

    def _execute(self, job_id):
        record = self._get_upload_record(job_id)
        job = dict(record)
        job.pop("credentials_json", None)
        if self._finish_cancellation(job_id, "Worker skipped the cancelled job"):
            return
        capture_path = Path(job["path"]).resolve()
        if capture_path.parent != self.data_root or not capture_path.is_dir():
            raise RuntimeError("capture path is missing or outside data_root")
        path = capture_path / "data"
        if not path.is_dir() or path.is_symlink():
            raise RuntimeError(f"capture data directory is missing: {path}")
        self._transition(job_id, "scanning")
        self._record_activity(job_id, "scan_started", f"Scanning capture data {path}")
        files = []
        for file_path in path.rglob("*"):
            if self._finish_cancellation(job_id, "Stop completed during local capture scanning"):
                return
            if not file_path.is_file() or file_path.name == LOCK_NAME:
                continue
            stat = file_path.stat()
            files.append((file_path.relative_to(path).as_posix(), stat.st_size, stat.st_mtime_ns))
        self._set_manifest(job_id, files)
        total_bytes = sum(item[1] for item in files)
        self._record_activity(job_id, "scan_completed", f"Manifest contains {len(files)} files and {total_bytes} bytes")
        lease_path = self._write_lease(capture_path, job_id)
        self._record_activity(job_id, "lease_created", f"Upload lease created at {lease_path}")
        try:
            self._transition(job_id, "authenticating")
            if self._finish_cancellation(job_id, "Stop completed before authentication began"):
                return
            credentials = self._decode_credentials(record.get("credentials_json"))
            token = credentials.get("token") or credentials.get("api_key") or os.environ.get(self.sds_api_key_environment)
            if not token:
                if self._finish_cancellation(job_id, "Stop completed before credential lookup finished"):
                    return
                self._record_activity(job_id, "credentials_required", "No SDS credential was available", "warning")
                self._transition(job_id, "waiting_for_credentials", "credentials required")
                return
            import spectrumx
            client = spectrumx.Client(host=self.sds_host, env_config={"SDS_SECRET_TOKEN": token})
            client.dry_run = bool(record.get("dry_run", False))
            logging.info("Upload job %s SDS dry_run=%s", job_id, client.dry_run)
            self._progress(job_id, 0, 0, f"Authenticating with {self.sds_host}")
            self._record_activity(job_id, "authentication_started", f"Authenticating with {self.sds_host}; dry_run={client.dry_run}")
            logging.info("Upload job %s authenticating with %s", job_id, self.sds_host)
            client.authenticate()
            self._record_activity(job_id, "authentication_completed", f"Authenticated with {self.sds_host}")
            logging.info("Upload job %s authenticated with %s", job_id, self.sds_host)
            if self._finish_cancellation(job_id, "Stop completed after authentication returned"):
                return
            self._transition(job_id, "uploading")
            if self._finish_cancellation(job_id, "Stop completed before upload began"):
                return
            self._progress(job_id, 0, 0, "Uploading capture through SDS SDK")
            self._record_activity(job_id, "upload_started", f"SDS SDK upload started for {job['sds_path']}")
            logging.info(
                "Upload job %s uploading %s files (%s bytes) to %s",
                job_id,
                len(files),
                total_bytes,
                job["sds_path"],
            )
            def capture_sdk_activity(message, level="info"):
                sanitized = str(message).replace(str(token), "[credential redacted]")
                self._record_activity(job_id, "sdk_output", sanitized, level, emit_log=False)

            def publish_sdk_progress(message):
                sanitized = str(message).replace(str(token), "[credential redacted]")
                self.publish_event(
                    "upload_sdk_progress",
                    {"job_id": job_id, "message": sanitized},
                )

            capture = _ActivityCapture(
                capture_sdk_activity,
                publish_sdk_progress,
                mirror=sys.__stderr__,
            )
            spectrumx_logger = logging.getLogger("spectrumx")
            spectrumx_handler = _ActivityLogHandler(capture_sdk_activity)
            spectrumx_logger.addHandler(spectrumx_handler)
            try:
                with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
                    upload_result = client.upload(
                        local_path=str(path),
                        sds_path=job["sds_path"],
                        verbose=True,
                        warn_skipped=True,
                    )
            finally:
                spectrumx_logger.removeHandler(spectrumx_handler)
                capture.close()
            if upload_result is None:
                raise RuntimeError("SDS SDK returned no upload result")
            self._validate_upload_result(upload_result)
            self._record_activity(job_id, "upload_returned", "SDS SDK upload returned successfully")
            logging.info("Upload job %s SDS upload returned successfully", job_id)
            if self._finish_cancellation(job_id, "Stop completed after the SDS upload call returned"):
                return
            if client.dry_run:
                self._record_activity(job_id, "dry_run_completed", f"SDS SDK dry-run completed for {len(files)} files")
                self._transition(job_id, "complete")
                self.publish_event("upload_completed", self.get_upload(job_id))
                return
            self._transition(job_id, "verifying")
            self._progress(job_id, 0, 0, "Verifying SDS paths and sizes")
            self._record_activity(job_id, "verification_started", "Verifying remote paths and sizes")
            try:
                remote_files = self._remote_inventory(client, job["sds_path"])
            except Exception as exc:
                self._save_sds_check(
                    job["capture_id"],
                    "unavailable",
                    expected_files=len(files),
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise VerificationUnavailable(
                    f"SDS upload returned, but remote verification was unavailable: {type(exc).__name__}: {exc}"
                ) from exc
            if self._finish_cancellation(job_id, "Stop completed after remote verification returned"):
                return
            expected_files = {relative_path: size for relative_path, size, _ in files}
            missing = sorted(set(expected_files) - set(remote_files))
            wrong_size = sorted(
                relative_path
                for relative_path in set(expected_files) & set(remote_files)
                if expected_files[relative_path] != remote_files[relative_path]
            )
            self._apply_verification(job_id, expected_files, remote_files)
            verified_files = len(expected_files) - len(missing) - len(wrong_size)
            self._save_sds_check(
                job["capture_id"],
                "verified" if not missing and not wrong_size else "incomplete",
                expected_files=len(expected_files),
                verified_files=verified_files,
                missing_files=len(missing),
                wrong_size_files=len(wrong_size),
            )
            if missing or wrong_size:
                raise RuntimeError(
                    "SDS verification mismatch: "
                    f"{len(missing)} missing and {len(wrong_size)} wrong-size files"
                )
            self._record_activity(job_id, "verification_completed", f"Verified {len(files)} files and {total_bytes} bytes")
            self._transition(job_id, "complete")
            self._record_activity(job_id, "completed", f"Upload completed: {len(files)} files and {total_bytes} bytes")
            self.publish_event("upload_completed", self.get_upload(job_id))
        finally:
            try:
                lease_path.unlink()
            except OSError:
                pass
            self._record_activity(job_id, "lease_released", "Upload lease released")

    def _handle_failure(self, job_id, error):
        try:
            job = self.get_upload(job_id)
            if self._is_cancelled(job_id):
                self._record_activity(
                    job_id,
                    "cancelled_after_sdk_error",
                    f"Stop completed after SDK call ended: {type(error).__name__}: {error}",
                    "warning",
                )
                self._transition(job_id, "cancelled", str(error))
                self.publish_event("upload_cancelled", self.get_upload(job_id))
            elif self._is_paused(job_id):
                self._record_activity(
                    job_id,
                    "paused_after_sdk_error",
                    f"Pause completed after SDK call ended: {type(error).__name__}: {error}",
                    "warning",
                )
                self._transition(job_id, "paused", str(error))
                self.publish_event("upload_paused", self.get_upload(job_id))
            elif isinstance(error, VerificationUnavailable):
                delay = min(7200, 30 * (4 ** min(job["retry_count"], 4)))
                self._record_activity(job_id, "verification_pending", str(error), "warning")
                self._record_activity(job_id, "retry_scheduled", f"Verification retry scheduled in {delay} seconds", "warning")
                self._transition(job_id, "verification_pending", str(error))
                self.publish_event("upload_verification_pending", {**self.get_upload(job_id), "retry_delay_s": delay})
                threading.Timer(delay, self._scheduled_retry, args=(job_id,)).start()
            else:
                self._record_activity(job_id, "error", f"{type(error).__name__}: {error}", "error")
                delay = min(7200, 30 * (4 ** min(job["retry_count"], 4)))
                self._record_activity(job_id, "retry_scheduled", f"Retry scheduled in {delay} seconds", "warning")
                self._transition(job_id, "waiting_for_retry", str(error))
                self.publish_event("upload_retry_scheduled", {**self.get_upload(job_id), "retry_delay_s": delay})
                threading.Timer(delay, self._scheduled_retry, args=(job_id,)).start()
        except Exception:
            logging.exception("Could not persist upload failure")

    def _scheduled_retry(self, job_id):
        if self._worker_running and not self._is_cancelled(job_id) and not self._is_paused(job_id):
            try:
                job = self.get_upload(job_id)
                if job["state"] not in {"waiting_for_retry", "verification_pending"}:
                    return
                self._record_activity(job_id, "retry_timer_elapsed", "Retry delay elapsed; requeueing job")
                self.retry_upload(job_id)
            except Exception:
                logging.exception("Could not requeue upload %s", job_id)

    @staticmethod
    def _validate_upload_result(upload_result):
        try:
            results = list(upload_result)
        except TypeError as exc:
            raise RuntimeError("SDS SDK upload result was not iterable") from exc
        failures = [result for result in results if not result]
        if not failures:
            return
        details = []
        for result in failures:
            if callable(result):
                try:
                    result()
                except Exception as exc:
                    details.append(f"{type(exc).__name__}: {exc}")
                    continue
            details.append(repr(result))
        raise RuntimeError(
            f"SDS SDK reported {len(failures)} failed file result(s): " + "; ".join(details)
        )

    @staticmethod
    def _sdk_object_value(value, names):
        for name in names:
            if hasattr(value, name):
                result = getattr(value, name)
                if result is not None:
                    return result
        if isinstance(value, dict):
            for name in names:
                result = value.get(name)
                if result is not None:
                    return result
        for method_name in ("model_dump", "dict"):
            method = getattr(value, method_name, None)
            if not callable(method):
                continue
            try:
                data = method()
            except Exception:
                continue
            if isinstance(data, dict):
                for name in names:
                    result = data.get(name)
                    if result is not None:
                        return result
        return None

    @staticmethod
    def _sds_relative_path(full_path, sds_path):
        full_path = str(full_path).replace("\\", "/")
        root = str(sds_path).strip("/")
        marker = f"/{root}/"
        if marker in full_path:
            return full_path.split(marker, 1)[1]
        stripped = full_path.strip("/")
        prefix = f"{root}/"
        return stripped[len(prefix):] if stripped.startswith(prefix) else stripped

    def _remote_inventory(self, client, sds_path):
        objects = client.list_files(sds_path=sds_path)
        if objects is None:
            raise RuntimeError("SDS SDK returned no remote inventory")
        files = {}
        for value in objects:
            full_path = self._sdk_object_value(
                value,
                ("path", "sds_path", "file_path", "full_path", "name"),
            )
            size = self._sdk_object_value(
                value,
                ("size", "file_size", "size_bytes", "bytes", "length"),
            )
            if full_path is None or size is None:
                raise RuntimeError(f"Could not read SDS path and size from {value!r}")
            relative_path = self._sds_relative_path(full_path, sds_path)
            if relative_path in files:
                raise RuntimeError(f"Duplicate SDS relative path: {relative_path}")
            files[relative_path] = int(size)
        return files

    def _apply_verification(self, job_id, expected_files, remote_files):
        verified = {
            path
            for path, size in expected_files.items()
            if remote_files.get(path) == size
        }
        uploaded_bytes = sum(expected_files[path] for path in verified)
        with self._db_lock, self._connect() as connection:
            connection.execute("UPDATE upload_job_files SET state='pending' WHERE job_id=?", (job_id,))
            connection.executemany(
                "UPDATE upload_job_files SET state='complete',last_error=NULL WHERE job_id=? AND relative_path=?",
                [(job_id, path) for path in verified],
            )
            connection.execute(
                "UPDATE upload_jobs SET uploaded_files=?,uploaded_bytes=?,current_file=? WHERE job_id=?",
                (len(verified), uploaded_bytes, f"Verified {len(verified)} / {len(expected_files)} files", job_id),
            )
            connection.commit()
        self.publish_event("upload_progress", self.get_upload(job_id))

    def _set_manifest(self, job_id, files):
        with self._db_lock, self._connect() as connection:
            connection.execute("DELETE FROM upload_job_files WHERE job_id=?", (job_id,))
            connection.executemany("INSERT INTO upload_job_files(job_id,relative_path,size_bytes,modified_ns,state) VALUES(?,?,?,?, 'pending')", [(job_id, path, size, modified) for path, size, modified in files])
            connection.execute("UPDATE upload_jobs SET total_files=?,total_bytes=?,uploaded_files=0,uploaded_bytes=0 WHERE job_id=?", (len(files), sum(item[1] for item in files), job_id))
            connection.commit()

    def _progress(self, job_id, uploaded_files, uploaded_bytes, current_file):
        with self._db_lock, self._connect() as connection:
            connection.execute("UPDATE upload_jobs SET uploaded_files=?,uploaded_bytes=?,current_file=? WHERE job_id=?", (uploaded_files, uploaded_bytes, current_file, job_id))
            connection.commit()
        self.publish_event("upload_progress", self.get_upload(job_id))

    def _set_current_activity(self, job_id, message):
        with self._db_lock, self._connect() as connection:
            connection.execute("UPDATE upload_jobs SET current_file=? WHERE job_id=?", (message, job_id))
            connection.commit()

    def _transition(self, job_id, state, error=None):
        if state not in ALL_STATES:
            raise ValueError(f"unsupported upload state: {state!r}")
        if state not in TERMINAL_STATES | {"cancelling"} and self._is_cancelled(job_id):
            current = self.get_upload(job_id)
            if current.get("state") == "cancelling":
                self._record_activity(job_id, "transition_suppressed", f"Suppressed transition to {state} because Stop is pending", "warning")
                return current
        now = time.time()
        fields = ["state=?", "last_error=?"]
        values = [state, error]
        if state in {"scanning", "authenticating", "uploading", "verifying"}:
            fields.append("started_at=COALESCE(started_at,?)")
            values.append(now)
        if state in TERMINAL_STATES:
            fields.append("finished_at=?")
            values.append(now)
        if state == "cancelled":
            fields.append("cancelled_at=?")
            values.append(now)
        if state == "complete":
            fields.append("current_file=NULL")
        if state in {"complete", "cancelled"}:
            fields.append("credentials_json=NULL")
        values.append(job_id)
        with self._db_lock, self._connect() as connection:
            connection.execute(f"UPDATE upload_jobs SET {', '.join(fields)} WHERE job_id=?", values)
            if connection.total_changes == 0:
                raise ValueError(f"upload job not found: {job_id!r}")
            connection.commit()
        job = self.get_upload(job_id)
        self._record_activity(job_id, "state_changed", f"State changed to {state}", "error" if state == "failed" else "info")
        self.publish_status()
        return job

    def _record_activity(self, job_id, event, message, level="info", emit_log=True):
        timestamp = time.time()
        activity_id = None
        try:
            with self._db_lock, self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO upload_job_activity(job_id,timestamp,level,event,message) VALUES(?,?,?,?,?)",
                    (job_id, timestamp, level, event, str(message)),
                )
                activity_id = cursor.lastrowid
                connection.execute(
                    "DELETE FROM upload_job_activity WHERE job_id=? AND activity_id NOT IN (SELECT activity_id FROM upload_job_activity WHERE job_id=? ORDER BY activity_id DESC LIMIT ?)",
                    (job_id, job_id, self.activity_limit_per_job),
                )
                connection.commit()
        except Exception:
            logging.exception("Could not persist activity for upload job %s", job_id)
        if emit_log:
            logging.log(getattr(logging, str(level).upper(), logging.INFO), "Upload job %s [%s] %s", job_id, event, message)
        activity = {
            "activity_id": activity_id,
            "timestamp": timestamp,
            "level": level,
            "event": event,
            "message": str(message),
        }
        self.publish_event("upload_activity", {"job_id": job_id, "activity": activity})
        return activity

    def _write_lease(self, path, job_id):
        lease_dir = self.database_path.parent / "leases"
        lease_dir.mkdir(parents=True, exist_ok=True)
        lease_path = lease_dir / f"{job_id}.json"
        lease_path.write_text(
            json.dumps({
                "job_id": job_id,
                "capture_path": str(path),
                "pid": os.getpid(),
                "timestamp": time.time(),
            }),
            encoding="utf-8",
        )
        return lease_path

    def _is_cancelled(self, job_id):
        with self._db_lock:
            return job_id in self._cancelled

    def _is_paused(self, job_id):
        with self._db_lock:
            return job_id in self._paused

    def _finish_cancellation(self, job_id, message):
        if not self._is_cancelled(job_id):
            if not self._is_paused(job_id):
                return False
            self._record_activity(job_id, "paused", "Pause completed at an upload checkpoint", "warning")
            job = self._transition(job_id, "paused")
            self.publish_event("upload_paused", job)
            return True
        self._record_activity(job_id, "cancelled", message, "warning")
        job = self._transition(job_id, "cancelled")
        self.publish_event("upload_cancelled", job)
        return True

    def handle_command(self, request):
        if not isinstance(request, dict):
            raise ValueError("command payload must be an object")
        task = request.get("task_name")
        args = request.get("arguments") or {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        if task == "get_status":
            result = self.status_data()
        elif task == "get_uploads":
            result = {"uploads": self.get_uploads()}
        elif task == "get_upload":
            result = self.get_upload(args["job_id"])
        elif task == "get_upload_activity":
            result = {"job_id": args["job_id"], "activity": self.get_upload_activity(args["job_id"], args.get("limit", 100))}
        elif task == "check_sds":
            result = self.check_sds(args)
        elif task == "start_upload":
            result = self.queue_upload(args)
        elif task == "pause_upload":
            result = self.pause_upload(args["job_id"])
        elif task == "resume_upload":
            result = self.resume_upload(args["job_id"], args.get("credentials"))
        elif task == "stop_upload":
            result = self.stop_upload(args["job_id"])
        elif task == "retry_upload":
            result = self.retry_upload(args["job_id"], args.get("credentials"), args.get("dry_run"))
        elif task == "delete_upload":
            result = self.delete_upload(args["job_id"])
        else:
            raise ValueError(f"unsupported task_name: {task!r}")
        return {"success": True, "task_name": task, "session_id": request.get("session_id"), "status_data": result, "error": None}

    def status_data(self):
        return {
            "uploads": self.get_uploads(),
            "sds_checks": self.get_sds_checks(),
            "data_root": str(self.data_root),
            "environment_credentials_available": bool(
                os.environ.get(self.sds_api_key_environment)
            ),
        }

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logging.error("MQTT connection failed: %s", reason_code)
            return
        client.subscribe(COMMAND_TOPIC, qos=1)
        self._publish(ANNOUNCE_TOPIC, self.announce(), retain=True)
        self.publish_status()

    def _on_message(self, client, userdata, message):
        request = None
        try:
            request = json.loads(message.payload.decode("utf-8"))
            logging.info("Received command %s session=%s", request.get("task_name"), request.get("session_id"))
            response = self.handle_command(request)
            logging.info("Completed command %s session=%s", request.get("task_name"), request.get("session_id"))
        except Exception as exc:
            logging.exception("Command failed: %s", request.get("task_name") if isinstance(request, dict) else "invalid request")
            response = {"success": False, "task_name": request.get("task_name") if isinstance(request, dict) else None, "session_id": request.get("session_id") if isinstance(request, dict) else None, "status_data": None, "error": str(exc)}
        self._publish(RESPONSE_TOPIC, response, retain=False)

    def announce(self):
        return {"title": "Upload Manager Service", "description": "Destination-neutral upload lifecycle service; SDS is the first backend.", "service": SERVICE_NAME, "type": "service", "version": "1.0", "time_started": self._started_at, "topics": {"announce": ANNOUNCE_TOPIC, "command": COMMAND_TOPIC, "response": RESPONSE_TOPIC, "status": STATUS_TOPIC, "data": DATA_TOPIC, "event": EVENT_TOPIC}, "commands": COMMANDS}

    def publish_status(self):
        self._status_seq += 1
        status = self.status_data()
        status.update({"service": SERVICE_NAME, "state": "online", "timestamp": time.time(), "seq": self._status_seq, "uptime_seconds": round(time.time() - self._started_at, 3)})
        self._publish(STATUS_TOPIC, status, retain=True)

    def publish_event(self, event_type, status_data):
        self._publish(EVENT_TOPIC, {"service": SERVICE_NAME, "event_type": event_type, "timestamp": time.time(), "status_data": status_data}, retain=False)

    @staticmethod
    def _encode_credentials(credentials):
        if credentials is None:
            return None
        if not isinstance(credentials, dict):
            raise ValueError("credentials must be an object")
        return json.dumps(credentials, separators=(",", ":"))

    @staticmethod
    def _decode_credentials(credentials_json):
        if not credentials_json:
            return {}
        try:
            credentials = json.loads(credentials_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        return credentials if isinstance(credentials, dict) else {}

    def _publish(self, topic, payload, retain):
        self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=retain)

    def run(self):
        self.client.connect("localhost", 1883, keepalive=60)
        self.client.loop_start()
        try:
            while self._worker_running:
                self.publish_status()
                deadline = time.monotonic() + self.status_interval_s
                while self._worker_running and time.monotonic() < deadline:
                    time.sleep(min(1.0, deadline - time.monotonic()))
        finally:
            self._worker_running = False
            self._queue.put(None)
            self._verification_queue.put(None)
            self._worker.join(timeout=5)
            self._verifier.join(timeout=5)
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    UploadManager().run()
