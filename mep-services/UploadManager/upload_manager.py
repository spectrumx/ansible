#!/usr/bin/env python3
"""Background SDS uploads with state stored inside each capture directory."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt


SERVICE_NAME = "uploadmanager"
DATA_ROOT = Path("/data/captures")
SDS_HOST = "sds.crc.nd.edu"
SDS_TOKEN_ENVIRONMENT = "SDS_SECRET_TOKEN"
STATUS_INTERVAL_S = 10.0
ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
STATUS_TOPIC = f"{SERVICE_NAME}/status"
EVENT_TOPIC = f"{SERVICE_NAME}/event"
STATUS_RELATIVE_PATH = Path("log_upload") / "upload_status.json"
ACTIVITY_RELATIVE_PATH = Path("log_upload") / "upload_activity.jsonl"
ACTIVITY_LIMIT = 500
ACTIVE_STATES = {
    "queued",
    "scanning",
    "authenticating",
    "uploading",
    "verifying",
    "stopping",
}
ALL_STATES = ACTIVE_STATES | {
    "waiting_for_credentials",
    "stopped",
    "failed",
    "complete",
}

COMMANDS = {
    "get_status": {"description": "Return upload state for local captures.", "arguments": {}},
    "get_uploads": {"description": "Return upload state for local captures.", "arguments": {}},
    "get_upload": {"description": "Return upload state for one capture.", "arguments": {"capture_name": {"type": "string", "required": True}}},
    "get_activity": {"description": "Return recent upload activity for one capture.", "arguments": {"capture_name": {"type": "string", "required": True}}},
    "check_sds": {"description": "Verify one capture against its SDS destination.", "arguments": {"capture_name": {"type": "string", "required": True}, "credentials": {"type": "object", "required": False}}},
    "start_upload": {"description": "Start an SDS upload.", "arguments": {"capture_name": {"type": "string", "required": True}, "credentials": {"type": "object", "required": False}, "dry_run": {"type": "boolean", "default": False}, "verbose": {"type": "boolean", "default": True}}},
    "stop_upload": {"description": "Stop an upload while retaining its SDS path.", "arguments": {"capture_name": {"type": "string", "required": True}}},
}


class UploadOutput:
    """Forward text written by the SDK as live upload activity."""

    def __init__(self, publish):
        self.publish = publish
        self.buffer = ""

    def write(self, text):
        self.buffer += str(text).replace("\r", "\n")
        lines = self.buffer.split("\n")
        self.buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.publish(line)
        return len(text)

    def flush(self):
        if self.buffer.strip():
            self.publish(self.buffer)
        self.buffer = ""


class UploadManager:
    """Own upload execution and each capture's upload_status.json."""

    def __init__(self):
        self.data_root = DATA_ROOT.resolve()
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._verification_queue = queue.Queue()
        self._stop_requested = set()
        self._active_capture = None
        self._verification_pending = set()
        self._running = True
        self._started_at = time.time()
        self._status_seq = 0
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{SERVICE_NAME}_{uuid.uuid4().hex[:8]}",
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._worker = threading.Thread(target=self._worker_loop, name="upload-manager-worker", daemon=True)
        self._worker.start()
        self._verifier = threading.Thread(target=self._verification_loop, name="upload-manager-verifier", daemon=True)
        self._verifier.start()

    def _capture_path(self, capture_name):
        if not isinstance(capture_name, str) or not capture_name or Path(capture_name).name != capture_name:
            raise ValueError("capture_name must be a non-empty basename")
        path = (self.data_root / capture_name).resolve()
        if path.parent != self.data_root or not path.is_dir() or path.is_symlink():
            raise ValueError(f"capture not found: {capture_name!r}")
        return path

    def _status_path(self, capture_name):
        return self._capture_path(capture_name) / STATUS_RELATIVE_PATH

    def _activity_path(self, capture_name):
        return self._capture_path(capture_name) / ACTIVITY_RELATIVE_PATH

    def _read_state(self, capture_name, required=True):
        path = self._status_path(capture_name)
        if not path.is_file():
            if required:
                raise ValueError(f"upload state is not initialized for {capture_name!r}")
            return None
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Cannot read log_upload/upload_status.json") from exc
        if not isinstance(state, dict):
            raise ValueError("Cannot read log_upload/upload_status.json")
        return state

    def _write_state(self, capture_name, state):
        path = self._status_path(capture_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _public_state(capture_name, state):
        result = dict(state)
        result.pop("credentials", None)
        result["capture_name"] = capture_name
        return result

    def _discover_uploads(self):
        uploads = []
        if not self.data_root.is_dir():
            return uploads
        for capture_path in sorted(self.data_root.iterdir(), key=lambda value: value.name):
            if not capture_path.is_dir() or capture_path.is_symlink() or capture_path.name.startswith("."):
                continue
            try:
                state = self._read_state(capture_path.name, required=False)
            except ValueError as exc:
                uploads.append({
                    "capture_name": capture_path.name,
                    "state": "error",
                    "error": str(exc),
                })
                continue
            if state is not None:
                uploads.append(self._public_state(capture_path.name, state))
        return uploads

    @staticmethod
    def _new_sds_path(capture_name):
        hostname = "".join(
            character
            for character in socket.gethostname().lower()
            if character.isalnum() or character in "_-"
        )
        if not hostname:
            raise RuntimeError("unable to derive hostname token for SDS path")
        return f"{capture_name}_{hostname}_{uuid.uuid4().hex[:6]}"

    def get_upload(self, capture_name):
        return self._public_state(capture_name, self._read_state(capture_name))

    def get_uploads(self):
        return self._discover_uploads()

    def get_activity(self, capture_name):
        path = self._activity_path(capture_name)
        if not path.is_file():
            return []
        records = deque(maxlen=ACTIVITY_LIMIT)
        with path.open(encoding="utf-8") as activity_file:
            for line in activity_file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
            return list(records)

    def queue_upload(self, arguments):
        capture_name = str(arguments.get("capture_name") or "")
        capture_path = self._capture_path(capture_name)
        if not (capture_path / "data").is_dir():
            raise ValueError("capture data directory is missing")
        dry_run = arguments.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        verbose = arguments.get("verbose", True)
        if not isinstance(verbose, bool):
            raise ValueError("verbose must be a boolean")
        credentials = arguments.get("credentials")
        if credentials is not None and not isinstance(credentials, dict):
            raise ValueError("credentials must be an object")
        existing = self._read_state(capture_name, required=False)
        if existing and existing.get("state") in ACTIVE_STATES:
            raise ValueError("capture already has an active upload")
        now = time.time()
        state = {
            "state": "queued",
            "remote_path": (existing.get("remote_path") if existing else None) or self._new_sds_path(capture_name),
            "requested_at": now,
            "started_at": None,
            "finished_at": None,
            "total_files": 0,
            "total_bytes": 0,
            "uploaded_files": 0,
            "uploaded_bytes": 0,
            "verification": {},
            "error": None,
            "credentials": credentials or (existing.get("credentials", {}) if existing else {}),
            "dry_run": dry_run,
            "verbose": verbose,
        }
        self._write_state(capture_name, state)
        self._queue.put(capture_name)
        public = self._public_state(capture_name, state)
        self._activity(capture_name, "queued", "Upload queued")
        self.publish_event("upload_queued", public)
        self.publish_status()
        return public

    def _transition(self, capture_name, state_name, error=None, **changes):
        if state_name not in ALL_STATES:
            raise ValueError(f"unsupported upload state: {state_name!r}")
        state = self._read_state(capture_name)
        state.update(changes)
        state["state"] = state_name
        state["error"] = error
        now = time.time()
        if state_name in {"scanning", "authenticating", "uploading", "verifying"}:
            state["started_at"] = state.get("started_at") or now
        if state_name in {"complete", "stopped", "failed", "waiting_for_credentials"}:
            state["finished_at"] = now
        self._write_state(capture_name, state)
        message = f"State changed to {state_name}"
        if error:
            message = f"{message}: {error}"
        self._activity(capture_name, "state_changed", message, "error" if error else "info")
        self.publish_status()
        return self._public_state(capture_name, state)

    def stop_upload(self, capture_name):
        state = self._read_state(capture_name)
        if state.get("state") not in ACTIVE_STATES:
            return self._public_state(capture_name, state)
        with self._lock:
            self._stop_requested.add(capture_name)
            running = self._active_capture == capture_name
        return self._transition(capture_name, "stopping" if running else "stopped")

    def _worker_loop(self):
        while self._running:
            capture_name = self._queue.get()
            if capture_name is None:
                return
            try:
                state = self._read_state(capture_name)
                if state.get("state") != "queued":
                    continue
                with self._lock:
                    self._active_capture = capture_name
                self._execute(capture_name)
            except Exception as exc:
                error = self._safe_error(capture_name, exc)
                logging.error("Upload for %s failed: %s", capture_name, error)
                try:
                    self._transition(capture_name, "failed", error)
                except Exception:
                    logging.error("Could not persist upload failure for %s", capture_name)
                self.publish_event("upload_failed", {
                    "capture_name": capture_name,
                    "error": error,
                })
            finally:
                with self._lock:
                    if self._active_capture == capture_name:
                        self._active_capture = None
                self._queue.task_done()

    def _execute(self, capture_name):
        capture_path = self._capture_path(capture_name)
        data_path = capture_path / "data"
        if not data_path.is_dir() or data_path.is_symlink():
            raise RuntimeError("capture data directory is missing")
        self._transition(capture_name, "scanning")
        files = [path for path in data_path.rglob("*") if path.is_file()]
        total_bytes = sum(path.stat().st_size for path in files)
        state = self._read_state(capture_name)
        state.update(
            total_files=len(files),
            total_bytes=total_bytes,
            uploaded_files=0,
            uploaded_bytes=0,
        )
        self._write_state(capture_name, state)
        if self._finish_requested(capture_name):
            return
        credentials = state.get("credentials") or {}
        token = credentials.get("token") or credentials.get("api_key") or os.environ.get(SDS_TOKEN_ENVIRONMENT)
        if not token:
            self._transition(capture_name, "waiting_for_credentials", "credentials required")
            return
        self._transition(capture_name, "authenticating")
        self._activity(capture_name, "authentication_started", f"Authenticating with {SDS_HOST}")
        import spectrumx
        sdk_output = UploadOutput(
            lambda message: self._activity(capture_name, "sdk_output", message)
        )
        try:
            with contextlib.redirect_stdout(sdk_output), contextlib.redirect_stderr(sdk_output):
                client = spectrumx.Client(
                    host=SDS_HOST,
                    env_config={"SDS_SECRET_TOKEN": token},
                )
                client.dry_run = bool(state.get("dry_run", False))
                client.authenticate()
                self._activity(capture_name, "authentication_completed", f"Authenticated with {SDS_HOST}")
                if self._finish_requested(capture_name):
                    return
                state = self._read_state(capture_name)
                self._transition(capture_name, "uploading")
                verbose = bool(state.get("verbose", True))
                self._activity(capture_name, "upload_started", f"Uploading {len(files)} files to {state['remote_path']}")
                upload_result = client.upload(
                    local_path=str(data_path),
                    sds_path=state["remote_path"],
                    verbose=verbose,
                    warn_skipped=True,
                )
        finally:
            sdk_output.flush()
        self._validate_upload_result(upload_result)
        self._activity(capture_name, "upload_completed", "SDS SDK upload returned successfully")
        if self._finish_requested(capture_name):
            return
        if client.dry_run:
            self._transition(capture_name, "complete", uploaded_files=0, uploaded_bytes=0)
            self.publish_event("upload_completed", self.get_upload(capture_name))
            return
        self._transition(
            capture_name,
            "verifying",
            uploaded_files=len(files),
            uploaded_bytes=total_bytes,
        )
        self._activity(capture_name, "verification_started", "Verifying SDS paths and sizes")
        self._activity(capture_name, "verification_local_inventory", "Reading local capture inventory")
        expected = {
            path.relative_to(data_path).as_posix(): path.stat().st_size
            for path in files
        }
        self._activity(capture_name, "verification_remote_inventory", "Reading SDS inventory")
        remote = self._remote_inventory(client, state["remote_path"])
        self._activity(capture_name, "verification_comparing", "Comparing local and SDS files")
        verification = self._verification_result(expected, remote)
        if verification["missing_files"] or verification["wrong_size_files"]:
            state = self._read_state(capture_name)
            state["verification"] = verification
            self._write_state(capture_name, state)
            raise RuntimeError(
                f"SDS verification mismatch: {verification['missing_files']} missing and "
                f"{verification['wrong_size_files']} wrong-size files"
            )
        self._transition(
            capture_name,
            "complete",
            uploaded_files=len(expected),
            uploaded_bytes=total_bytes,
            verification=verification,
        )
        self._activity(
            capture_name,
            "verification_completed",
            f"Verified {verification['verified_files']}/{verification['expected_files']} files",
        )
        self.publish_event("upload_completed", self.get_upload(capture_name))

    def _finish_requested(self, capture_name):
        with self._lock:
            stopping = capture_name in self._stop_requested
            if stopping:
                self._stop_requested.discard(capture_name)
        if stopping:
            self._transition(capture_name, "stopped")
            return True
        return False

    def check_sds(self, arguments):
        capture_name = str(arguments.get("capture_name") or "")
        if capture_name in self._verification_pending:
            raise ValueError("SDS check is already pending for this capture")
        state = self._read_state(capture_name)
        if not state.get("remote_path"):
            raise ValueError("capture has no assigned SDS path")
        credentials = arguments.get("credentials")
        if credentials is not None:
            if not isinstance(credentials, dict):
                raise ValueError("credentials must be an object")
            state["credentials"] = credentials
        state["verification"] = {
            "state": "checking",
            "checked_at": time.time(),
            "error": None,
        }
        self._write_state(capture_name, state)
        check_id = uuid.uuid4().hex
        self._verification_pending.add(capture_name)
        self._verification_queue.put((check_id, capture_name))
        self._activity(capture_name, "verification_requested", "SDS verification requested")
        self.publish_event("sds_check_started", {
            "check_id": check_id,
            "capture_name": capture_name,
            "remote_path": state["remote_path"],
        })
        self.publish_status()
        return {"accepted": True, "check_id": check_id, "capture_name": capture_name}

    def _verification_loop(self):
        while self._running:
            request = self._verification_queue.get()
            if request is None:
                return
            check_id, capture_name = request
            try:
                self._perform_sds_check(capture_name)
                upload = self.get_upload(capture_name)
                verification = upload.get("verification", {})
                self._activity(
                    capture_name,
                    "verification_completed",
                    f"Verification {verification.get('state') or 'completed'}: "
                    f"{verification.get('verified_files') or 0}/{verification.get('expected_files') or 0} files",
                )
                self.publish_event("sds_check_completed", {
                    "check_id": check_id,
                    "capture_name": capture_name,
                    "remote_path": upload.get("remote_path"),
                    **upload.get("verification", {}),
                })
            except Exception as exc:
                error = self._safe_error(capture_name, exc)
                logging.error("SDS check for %s failed: %s", capture_name, error)
                self._activity(capture_name, "verification_failed", f"SDS verification failed: {error}", "error")
                try:
                    state = self._read_state(capture_name)
                    state["verification"] = {
                        "state": "unavailable",
                        "checked_at": time.time(),
                        "error": error,
                    }
                    self._write_state(capture_name, state)
                except Exception:
                    pass
                self.publish_event("sds_check_completed", {
                    "check_id": check_id,
                    "capture_name": capture_name,
                    "state": "unavailable",
                    "error": error,
                })
            finally:
                self._verification_pending.discard(capture_name)
                self._verification_queue.task_done()
                self.publish_status()

    def _perform_sds_check(self, capture_name):
        state = self._read_state(capture_name)
        data_path = self._capture_path(capture_name) / "data"
        self._activity(capture_name, "verification_local_inventory", "Reading local capture inventory")
        expected = {
            path.relative_to(data_path).as_posix(): path.stat().st_size
            for path in data_path.rglob("*")
            if path.is_file()
        }
        credentials = state.get("credentials") or {}
        token = credentials.get("token") or credentials.get("api_key") or os.environ.get(SDS_TOKEN_ENVIRONMENT)
        if not token:
            raise RuntimeError("credentials required")
        self._activity(capture_name, "verification_authenticating", f"Authenticating with {SDS_HOST}")
        import spectrumx
        client = spectrumx.Client(
            host=SDS_HOST,
            env_config={"SDS_SECRET_TOKEN": token},
        )
        client.dry_run = False
        client.authenticate()
        self._activity(capture_name, "verification_remote_inventory", "Reading SDS inventory")
        remote = self._remote_inventory(client, state["remote_path"])
        self._activity(capture_name, "verification_comparing", "Comparing local and SDS files")
        state["verification"] = self._verification_result(expected, remote)
        self._write_state(capture_name, state)

    @staticmethod
    def _verification_result(expected, remote):
        missing = sorted(set(expected) - set(remote))
        wrong_size = sorted(
            path for path in set(expected) & set(remote)
            if expected[path] != remote[path]
        )
        verified_paths = set(expected) - set(missing) - set(wrong_size)
        return {
            "state": "verified" if not missing and not wrong_size else "incomplete",
            "checked_at": time.time(),
            "expected_files": len(expected),
            "verified_files": len(verified_paths),
            "expected_bytes": sum(expected.values()),
            "verified_bytes": sum(expected[path] for path in verified_paths),
            "missing_files": len(missing),
            "wrong_size_files": len(wrong_size),
            "missing_paths": missing,
            "wrong_size_paths": wrong_size,
            "error": None,
        }

    @staticmethod
    def _validate_upload_result(upload_result):
        if upload_result is None:
            raise RuntimeError("SDS SDK returned no upload result")
        try:
            results = list(upload_result)
        except TypeError as exc:
            raise RuntimeError("SDS SDK upload result was not iterable") from exc
        if any(not result for result in results):
            raise RuntimeError("SDS SDK reported failed file results")

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
            full_path = self._sdk_object_value(value, ("path", "sds_path", "file_path", "full_path", "name"))
            size = self._sdk_object_value(value, ("size", "file_size", "size_bytes", "bytes", "length"))
            if full_path is None or size is None:
                raise RuntimeError(f"Could not read SDS path and size from {value!r}")
            relative_path = self._sds_relative_path(full_path, sds_path)
            if relative_path in files:
                raise RuntimeError(f"Duplicate SDS relative path: {relative_path}")
            files[relative_path] = int(size)
        return files

    def _activity(self, capture_name, event, message, level="info"):
        activity = {
            "timestamp": time.time(),
            "level": level,
            "event": event,
            "message": message,
        }
        logging.log(
            getattr(logging, level.upper(), logging.INFO),
            "Upload %s [%s] %s",
            capture_name,
            event,
            message,
        )
        path = self._activity_path(capture_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as activity_file:
            activity_file.write(json.dumps(activity, separators=(",", ":")) + "\n")
        self.publish_event("upload_activity", {
            "capture_name": capture_name,
            "activity": activity,
        })

    def _safe_error(self, capture_name, error):
        message = f"{type(error).__name__}: {error}"
        try:
            credentials = self._read_state(capture_name).get("credentials") or {}
        except Exception:
            credentials = {}
        for value in credentials.values():
            if value:
                message = message.replace(str(value), "[credential redacted]")
        return message

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
            result = self.get_upload(args["capture_name"])
        elif task == "get_activity":
            result = {
                "capture_name": args["capture_name"],
                "activity": self.get_activity(args["capture_name"]),
            }
        elif task == "check_sds":
            result = self.check_sds(args)
        elif task == "start_upload":
            result = self.queue_upload(args)
        elif task == "stop_upload":
            result = self.stop_upload(args["capture_name"])
        else:
            raise ValueError(f"unsupported task_name: {task!r}")
        return {
            "success": True,
            "task_name": task,
            "session_id": request.get("session_id"),
            "status_data": result,
            "error": None,
        }

    def status_data(self):
        uploads = self.get_uploads()
        return {
            "uploads": uploads,
            "sds_checks": [
                {
                    "capture_name": upload["capture_name"],
                    "remote_path": upload.get("remote_path"),
                    **upload.get("verification", {}),
                }
                for upload in uploads
                if upload.get("remote_path")
            ],
            "data_root": str(self.data_root),
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
            response = self.handle_command(request)
        except Exception as exc:
            logging.exception("UploadManager command failed")
            response = {
                "success": False,
                "task_name": request.get("task_name") if isinstance(request, dict) else None,
                "session_id": request.get("session_id") if isinstance(request, dict) else None,
                "status_data": None,
                "error": str(exc),
            }
        self._publish(RESPONSE_TOPIC, response, retain=False)

    def announce(self):
        return {
            "title": "Upload Manager Service",
            "description": "Uploads capture data and stores state inside each capture.",
            "service": SERVICE_NAME,
            "type": "service",
            "version": "1.0",
            "time_started": self._started_at,
            "topics": {
                "announce": ANNOUNCE_TOPIC,
                "command": COMMAND_TOPIC,
                "response": RESPONSE_TOPIC,
                "status": STATUS_TOPIC,
                "event": EVENT_TOPIC,
            },
            "commands": COMMANDS,
        }

    def publish_status(self):
        self._status_seq += 1
        status = self.status_data()
        status.update({
            "service": SERVICE_NAME,
            "state": "online",
            "timestamp": time.time(),
            "seq": self._status_seq,
            "uptime_seconds": round(time.time() - self._started_at, 3),
        })
        self._publish(STATUS_TOPIC, status, retain=True)

    def publish_event(self, event_type, status_data):
        self._publish(EVENT_TOPIC, {
            "service": SERVICE_NAME,
            "event_type": event_type,
            "timestamp": time.time(),
            "status_data": status_data,
        }, retain=False)

    def _publish(self, topic, payload, retain):
        self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=retain)

    def run(self):
        self.client.connect("localhost", 1883, keepalive=60)
        self.client.loop_start()
        try:
            while self._running:
                self.publish_status()
                deadline = time.monotonic() + STATUS_INTERVAL_S
                while self._running and time.monotonic() < deadline:
                    time.sleep(min(1.0, deadline - time.monotonic()))
        finally:
            self._running = False
            self._queue.put(None)
            self._verification_queue.put(None)
            self._worker.join(timeout=5)
            self._verifier.join(timeout=5)
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    UploadManager().run()