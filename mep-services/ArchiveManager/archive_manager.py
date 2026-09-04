#!/usr/bin/env python3
"""ArchiveManager local capture inventory and deletion service."""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

import inotify.adapters
import paho.mqtt.client as mqtt
import yaml


SERVICE_NAME = "archivemanager"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ARCHIVE_MANAGER_CONFIG", BASE_DIR / "archive_manager.yaml"))
ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
STATUS_TOPIC = f"{SERVICE_NAME}/status"
DATA_TOPIC = f"{SERVICE_NAME}/data"
EVENT_TOPIC = f"{SERVICE_NAME}/event"
CAPTURE_ORCHESTRATOR_STATUS_TOPIC = "captureorchestrator/status"
UPLOAD_MANAGER_STATUS_TOPIC = "uploadmanager/status"
CAPTURE_IDENTITY_FILENAME = "capture_identity.json"
CAPTURE_SETTINGS_FILENAME = "capture_settings.json"
ACTIVE_UPLOAD_STATES = {
    "queued",
    "scanning",
    "authenticating",
    "uploading",
    "verifying",
    "verification_pending",
    "pause_requested",
    "paused",
    "waiting_for_retry",
    "waiting_for_credentials",
}
COMMANDS = {
    "get_status": {
        "description": "Return the current local capture inventory summary.",
        "arguments": {},
    },
    "get_captures": {
        "description": "Return the current local capture inventory summary.",
        "arguments": {},
    },
    "get_capture": {
        "description": "Return one managed local capture by stable ID.",
        "arguments": {"capture_id": {"type": "string", "required": True}},
    },
    "delete_capture": {
        "description": "Delete one local capture directory.",
        "arguments": {"capture_id": {"type": "string", "required": True}},
    },
    "delete_preview": {
        "description": "Delete the reserved preview directory.",
        "arguments": {},
    },
    "rename_capture": {
        "description": "Rename a local capture directory while preserving its stable identity.",
        "arguments": {
            "capture_id": {"type": "string", "required": True},
            "new_name": {"type": "string", "required": True},
        },
    },
}

OBSERVED_EVENTS = {
    "IN_CLOSE_WRITE",
    "IN_CREATE",
    "IN_DELETE",
    "IN_DELETE_SELF",
    "IN_MOVED_FROM",
    "IN_MOVED_TO",
    "IN_MOVE_SELF",
}


class ArchiveManagerService:
    """Maintain the local capture inventory and expose it over MQTT."""

    def __init__(self, config_path: Path = CONFIG_PATH):
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        self.config_path = config_path
        self.data_root = Path(config.get("data_root", "/data/captures")).resolve()
        self.reconcile_interval_s = max(
            300.0, float(config.get("reconcile_interval_s", 3600))
        )
        self.debounce_interval_s = max(
            0.0, float(config.get("debounce_interval_s", 1.0))
        )

        self.started_at = time.time()
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        self._publication_lock = threading.Lock()
        self._inventory = {}
        self._inventory_seq = 0
        self._status_seq = 0
        self._last_inventory_change = None
        self._watcher_state = "starting"
        self._watcher = None
        self._threads = []
        self._command_queue = queue.SimpleQueue()
        self._initial_publication_done = False
        self._workers_started = False
        self._active_capture_path = None
        self._active_capture_name = None
        self._active_capture_id = None
        self._active_upload_ids = set()
        self._cancelling_upload_ids = set()
        self._capture_status_received = False
        self._upload_status_received = False
        self._debounce_timer = None

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{SERVICE_NAME}_{uuid.uuid4().hex[:8]}",
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def run(self):
        self._prepare_inventory()
        try:
            self.client.connect("localhost", 1883, keepalive=60)
            self.client.loop_forever()
        finally:
            self.close()

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._command_queue.put(None)
        for thread in self._threads:
            thread.join(timeout=2.0)
        self.client.disconnect()

    def _prepare_inventory(self):
        if not self.data_root.is_dir():
            raise FileNotFoundError(f"data root does not exist: {self.data_root}")

        with self._state_lock:
            self._inventory = self._scan_inventory()

        self._watcher = inotify.adapters.InotifyTree(str(self.data_root))
        with self._state_lock:
            self._inventory = self._scan_inventory()
            self._watcher_state = "watching"

        logging.info(
            "Capture inventory initialized: %d captures", len(self._inventory)
        )

    def _start_workers(self):
        with self._state_lock:
            if self._workers_started:
                return
            self._workers_started = True
            self._threads = [
                threading.Thread(
                    target=self._watch_filesystem,
                    name="data-manager-inotify",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._reconcile_loop,
                    name="data-manager-reconcile",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._command_loop,
                    name="data-manager-command",
                    daemon=True,
                ),
            ]
        for thread in self._threads:
            thread.start()

    def _watch_filesystem(self):
        try:
            for event in self._watcher.event_gen(yield_nones=True, timeout_s=1):
                if self._stop.is_set():
                    return
                if event is None:
                    continue
                _, type_names, path, filename = event
                self._handle_filesystem_event(set(type_names), Path(path) / filename)
        except Exception as exc:
            logging.exception("ArchiveManager filesystem watcher failed")
            self._publish_watcher_failure(str(exc))

    def _reconcile_loop(self):
        while not self._stop.wait(self.reconcile_interval_s):
            try:
                self._reconcile()
            except Exception:
                logging.exception("ArchiveManager inventory reconciliation failed")

    def _command_loop(self):
        while not self._stop.is_set():
            payload = self._command_queue.get()
            if payload is None:
                return
            request = None
            try:
                request = json.loads(payload.decode("utf-8"))
                response = self.handle_command(request)
            except Exception as exc:
                logging.exception("ArchiveManager command failed")
                response = self._error_response(request, exc)
            self._publish(RESPONSE_TOPIC, response, retain=False)

    def _handle_filesystem_event(self, type_names, path: Path):
        if "IN_Q_OVERFLOW" in type_names:
            self._reconcile()
            return
        if not type_names.intersection(OBSERVED_EVENTS):
            return

        try:
            relative = path.resolve().relative_to(self.data_root)
        except ValueError:
            return
        if not relative.parts:
            return

        capture_name = relative.parts[0]
        if capture_name.startswith("."):
            return
        is_capture_root_event = len(relative.parts) == 1
        is_capture_metadata = relative.name in {
            CAPTURE_IDENTITY_FILENAME,
            CAPTURE_SETTINGS_FILENAME,
        }
        if (
            "IN_CREATE" in type_names
            and "IN_ISDIR" not in type_names
            and not is_capture_metadata
        ):
            return

        if (
            not is_capture_root_event
            and not is_capture_metadata
            and self._should_suppress_scan(path)
        ):
            self._schedule_debounced_scan()
            return

        self._refresh_capture(capture_name)

    def _should_suppress_scan(self, path: Path):
        if not self._active_capture_path:
            return False
        try:
            resolved = path.resolve()
            active_capture = Path(self._active_capture_path).resolve()
            resolved.relative_to(active_capture)
            return True
        except OSError:
            return False
        except ValueError:
            return False

    def _schedule_debounced_scan(self):
        if self.debounce_interval_s <= 0:
            self._reconcile()
            return
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
        self._debounce_timer = threading.Timer(
            self.debounce_interval_s,
            self._reconcile,
        )
        self._debounce_timer.daemon = True
        self._debounce_timer.start()

    def _scan_inventory(self):
        return {
            capture_path.name: self._scan_capture(capture_path)
            for capture_path in sorted(self.data_root.iterdir(), key=lambda item: item.name)
            if capture_path.is_dir() and not capture_path.name.startswith(".") and not capture_path.is_symlink()
        }

    def _scan_capture(self, capture_path: Path):
        settings = self._read_capture_settings(capture_path)
        identity = self._read_capture_identity(capture_path)
        issues = []
        if identity.get("error"):
            issues.append(identity.pop("error"))
        if settings.get("error"):
            issues.append(settings.pop("error"))
        capture_id = identity.get("capture_id")
        file_count = 0
        size_bytes = 0
        first_seen = None
        last_modified = None
        for file_path in capture_path.rglob("*"):
            if not file_path.is_file():
                continue
            try:
                stat = file_path.stat()
            except FileNotFoundError:
                continue
            file_count += 1
            size_bytes += stat.st_size
            first_seen = stat.st_ctime if first_seen is None else min(first_seen, stat.st_ctime)
            last_modified = stat.st_mtime if last_modified is None else max(last_modified, stat.st_mtime)

        try:
            directory_stat = capture_path.stat()
            first_seen = directory_stat.st_ctime if first_seen is None else min(first_seen, directory_stat.st_ctime)
            last_modified = directory_stat.st_mtime if last_modified is None else max(last_modified, directory_stat.st_mtime)
        except FileNotFoundError:
            pass

        return {
            "capture_id": capture_id,
            "name": capture_path.name,
            "path": str(capture_path),
            "state": "managed" if capture_id else "legacy",
            "identity": identity,
            "settings": settings,
            "issues": issues,
            "file_count": file_count,
            "size_bytes": size_bytes,
            "first_seen": first_seen,
            "last_modified": last_modified,
        }

    def _reconcile(self):
        with self._publication_lock:
            with self._state_lock:
                inventory = self._scan_inventory()
                if inventory == self._inventory:
                    return
                self._inventory = inventory
                publications = self._record_inventory_change_locked(
                    "inventory_changed",
                    {"reason": "reconciliation", "capture_count": len(inventory)},
                )
            self._publish_inventory_publications(publications)
        logging.info("Capture inventory reconciled: %d captures", len(inventory))

    def _refresh_capture(self, capture_name: str):
        capture_path = self.data_root / capture_name
        with self._publication_lock:
            with self._state_lock:
                previous = self._inventory.get(capture_name)
                current = (
                    self._scan_capture(capture_path)
                    if capture_path.is_dir()
                    else None
                )
                if current == previous:
                    return

                if current is None:
                    if previous is None:
                        return
                    del self._inventory[capture_name]
                    event_type = "capture_removed"
                    event_data = {
                        "capture_id": previous["capture_id"],
                        "name": capture_name,
                    }
                else:
                    self._inventory[capture_name] = current
                    event_type = (
                        "capture_added" if previous is None else "capture_changed"
                    )
                    event_data = self._copy(current)

                publications = self._record_inventory_change_locked(
                    event_type, event_data
                )
            self._publish_inventory_publications(publications)

    def _record_inventory_change_locked(self, event_type, event_data):
        self._inventory_seq += 1
        self._last_inventory_change = time.time()
        return (
            self._event_payload(event_type, event_data),
            {
                "service": SERVICE_NAME,
                "inventory_seq": self._inventory_seq,
                "timestamp": self._last_inventory_change,
                "captures": self._snapshot_locked(),
            },
            self._status_payload_locked(),
        )

    def _publish_inventory_publications(self, publications):
        event, data, status = publications
        self._publish(EVENT_TOPIC, event, retain=False)
        self._publish(DATA_TOPIC, data, retain=False)
        self._publish(STATUS_TOPIC, status, retain=True)

    def _publish_watcher_failure(self, error):
        with self._publication_lock:
            with self._state_lock:
                if self._watcher_state == "error":
                    return
                self._watcher_state = "error"
                event = self._event_payload("watcher_failed", {"error": error})
                status = self._status_payload_locked()
            self._publish(EVENT_TOPIC, event, retain=False)
            self._publish(STATUS_TOPIC, status, retain=True)

    def delete_capture(self, capture_id):
        with self._publication_lock:
            with self._state_lock:
                name, capture = self._capture_by_id_locked(capture_id)
                if capture.get("state") != "managed" or not capture.get("capture_id"):
                    raise ValueError("legacy captures cannot be modified")
                if capture["capture_id"] == self._active_capture_id:
                    raise ValueError("cannot delete a capture while it is recording")

                path = (self.data_root / name).resolve()
                if path.parent != self.data_root or not path.is_dir() or path.is_symlink():
                    raise ValueError("refusing to delete a path outside the capture root")

                shutil.rmtree(path)
                del self._inventory[name]
                result = {
                    "name": name,
                    "capture_id": capture["capture_id"],
                    "deleted": True,
                }
                publications = self._record_inventory_change_locked(
                    "capture_deleted", result
                )
            self._publish_inventory_publications(publications)
        return result

    def delete_preview(self):
        with self._publication_lock:
            with self._state_lock:
                capture = self._inventory.get("preview")
                if capture is None:
                    raise ValueError("preview capture not found")
                if self._active_capture_name == "preview":
                    raise ValueError("cannot delete preview while it is recording")

                path = (self.data_root / "preview").resolve()
                if path.parent != self.data_root or not path.is_dir() or path.is_symlink():
                    raise ValueError("refusing to delete a path outside the capture root")

                shutil.rmtree(path)
                del self._inventory["preview"]
                result = {"name": "preview", "capture_id": None, "deleted": True}
                publications = self._record_inventory_change_locked(
                    "capture_deleted", result
                )
            self._publish_inventory_publications(publications)
        return result

    def rename_capture(self, capture_id, new_name):
        new_name = self._validate_capture_name(new_name)

        with self._publication_lock:
            with self._state_lock:
                old_name, capture = self._capture_by_id_locked(capture_id)
                self._require_inactive_capture(capture)
                if old_name == new_name:
                    return self._copy(capture)
                if (self.data_root / new_name).exists():
                    raise ValueError(f"destination already exists: {new_name!r}")

                source = (self.data_root / old_name).resolve()
                target = (self.data_root / new_name).resolve()
                if source.parent != self.data_root or source.is_symlink():
                    raise ValueError("refusing to rename a path outside the capture root")

                shutil.move(str(source), str(target))
                renamed = self._scan_capture(target)
                if renamed.get("capture_id") != capture["capture_id"]:
                    shutil.move(str(target), str(source))
                    raise RuntimeError("capture identity changed during rename")
                del self._inventory[old_name]
                self._inventory[new_name] = renamed
                result = {
                    "old_name": old_name,
                    "new_name": new_name,
                    "capture_id": capture["capture_id"],
                    "renamed": True,
                }
                publications = self._record_inventory_change_locked(
                    "capture_renamed", result
                )
            self._publish_inventory_publications(publications)
        return result

    def handle_command(self, request):
        if not isinstance(request, dict):
            raise ValueError("command payload must be an object")

        task_name = request.get("task_name")
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")

        if task_name == "get_status":
            result = self.status_data()
        elif task_name == "get_captures":
            self._reconcile()
            result = {"captures": self._snapshot()}
        elif task_name == "get_capture":
            result = self._capture_by_id(arguments.get("capture_id"))
        elif task_name == "delete_capture":
            result = self.delete_capture(arguments.get("capture_id"))
        elif task_name == "delete_preview":
            result = self.delete_preview()
        elif task_name == "rename_capture":
            result = self.rename_capture(arguments.get("capture_id"), arguments.get("new_name"))
        else:
            raise ValueError(f"unsupported task_name: {task_name!r}")

        return {
            "task_name": task_name,
            "session_id": request.get("session_id"),
            "success": True,
            "status_data": result,
            "error": None,
        }

    def status_data(self):
        with self._state_lock:
            return self._status_data_locked()

    def announce(self):
        return {
            "title": "Data Manager Service",
            "description": "Local capture inventory using inotify directory watching.",
            "service": SERVICE_NAME,
            "type": "service",
            "version": "1.0",
            "time_started": self.started_at,
            "topics": {
                "announce": ANNOUNCE_TOPIC,
                "command": COMMAND_TOPIC,
                "response": RESPONSE_TOPIC,
                "status": STATUS_TOPIC,
                "data": DATA_TOPIC,
                "event": EVENT_TOPIC,
            },
            "commands": COMMANDS,
        }

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logging.error("MQTT connection failed: %s", reason_code)
            return

        for topic in (
            COMMAND_TOPIC,
            CAPTURE_ORCHESTRATOR_STATUS_TOPIC,
            UPLOAD_MANAGER_STATUS_TOPIC,
        ):
            result, _ = client.subscribe(topic, qos=1)
            if result != mqtt.MQTT_ERR_SUCCESS:
                logging.error("MQTT subscription failed for %s: rc=%s", topic, result)
                return

        if not self._initial_publication_done:
            with self._publication_lock:
                self._publish(ANNOUNCE_TOPIC, self.announce(), retain=True)
                with self._state_lock:
                    status = self._status_payload_locked()
                self._publish(STATUS_TOPIC, status, retain=True)
                self._initial_publication_done = True
        self._start_workers()
        logging.info("ArchiveManager subscribed to %s", COMMAND_TOPIC)

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ):
        if reason_code != 0 and not self._stop.is_set():
            logging.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, message):
        if message.topic == COMMAND_TOPIC:
            self._command_queue.put(message.payload)
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logging.warning("Invalid JSON on %s", message.topic)
            return
        if message.topic == CAPTURE_ORCHESTRATOR_STATUS_TOPIC:
            self._update_capture_activity(payload)
        elif message.topic == UPLOAD_MANAGER_STATUS_TOPIC:
            self._update_upload_activity(payload)

    def _update_capture_activity(self, payload):
        rx = payload.get("rx") if isinstance(payload, dict) else None
        is_active = isinstance(rx, dict) and rx.get("state") in {"starting", "running"}
        capture_id = rx.get("capture_id") if is_active else None
        capture_name = rx.get("capture_name") if is_active else None
        if is_active and not capture_name:
            capture_name = "preview"
        with self._state_lock:
            previous_capture_name = self._active_capture_name
            previous_capture_id = self._active_capture_id
            self._active_capture_id = capture_id
            self._active_capture_name = capture_name
            self._capture_status_received = True
            self._active_capture_path = (
                str(self.data_root / capture_name)
                if isinstance(capture_name, str)
                else None
            )
        refresh_names = set()
        if isinstance(previous_capture_name, str) and previous_capture_name:
            if previous_capture_name != capture_name or previous_capture_id != capture_id:
                refresh_names.add(previous_capture_name)
        if isinstance(capture_name, str) and capture_name:
            if capture_name != previous_capture_name or capture_id != previous_capture_id:
                refresh_names.add(capture_name)
        for name in refresh_names:
            self._refresh_capture(name)

    def _update_upload_activity(self, payload):
        uploads = payload.get("uploads") if isinstance(payload, dict) else None
        if not isinstance(uploads, list):
            return
        active_ids = {
            str(upload["capture_id"])
            for upload in uploads
            if isinstance(upload, dict)
            and upload.get("state") in ACTIVE_UPLOAD_STATES
            and upload.get("capture_id")
        }
        cancelling_ids = {
            str(upload["capture_id"])
            for upload in uploads
            if isinstance(upload, dict)
            and upload.get("state") == "cancelling"
            and upload.get("capture_id")
        }
        with self._state_lock:
            self._active_upload_ids = active_ids
            self._cancelling_upload_ids = cancelling_ids
            self._upload_status_received = True

    def _publish(self, topic, payload, retain):
        result = self.client.publish(
            topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=retain,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logging.error("MQTT publish failed for %s: rc=%s", topic, result.rc)

    def _status_payload_locked(self):
        self._status_seq += 1
        now = time.time()
        return {
            "service": SERVICE_NAME,
            "state": "online",
            "timestamp": now,
            "seq": self._status_seq,
            "uptime_seconds": round(now - self.started_at, 3),
            **self._status_data_locked(),
        }

    def _status_data_locked(self):
        captures = tuple(self._inventory.values())
        snapshot = self._snapshot_locked()
        return {
            "inventory_seq": self._inventory_seq,
            "capture_count": len(captures),
            "file_count": sum(capture["file_count"] for capture in captures),
            "capture_bytes": sum(capture["size_bytes"] for capture in captures),
            "last_inventory_change": self._last_inventory_change,
            "watcher": self._watcher_state,
            "active_capture_path": self._active_capture_path,
            "active_capture_name": self._active_capture_name,
            "captures": snapshot,
        }

    def _snapshot(self):
        with self._state_lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        return self._copy(
            [self._inventory[name] for name in sorted(self._inventory)]
        )

    def _capture_by_name(self, name):
        name = self._validate_capture_name(name)
        with self._state_lock:
            capture = self._inventory.get(name)
            if capture is None:
                raise ValueError(f"capture not found: {name!r}")
            return self._copy(capture)

    def _capture_by_id(self, capture_id):
        with self._state_lock:
            _, capture = self._capture_by_id_locked(capture_id)
            return self._copy(capture)

    def _capture_by_id_locked(self, capture_id):
        capture_id = str(capture_id or "")
        if not capture_id:
            raise ValueError("capture_id is required")
        for name, capture in self._inventory.items():
            if capture.get("capture_id") == capture_id:
                return name, capture
        raise ValueError(f"capture not found: {capture_id!r}")

    def _require_inactive_capture(self, capture, allow_cancelling_upload=False):
        if capture.get("state") != "managed" or not capture.get("capture_id"):
            raise ValueError("legacy captures cannot be modified")
        if not self._capture_status_received or not self._upload_status_received:
            raise RuntimeError("capture activity status is not available")
        capture_id = capture["capture_id"]
        if capture_id == self._active_capture_id:
            raise ValueError("cannot modify a capture while it is recording")
        if capture_id in self._active_upload_ids or (
            capture_id in self._cancelling_upload_ids
            and not allow_cancelling_upload
        ):
            raise ValueError("cannot modify a capture while it has an active upload")

    @staticmethod
    def _validate_capture_name(name):
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ValueError("capture name must be a non-empty basename")
        return name

    @staticmethod
    def _read_capture_settings(path):
        settings_path = path / CAPTURE_SETTINGS_FILENAME
        if not settings_path.is_file():
            return {}
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"error": str(exc)}
        if not isinstance(settings, dict):
            return {"error": "settings must be an object"}
        return settings

    @staticmethod
    def _read_capture_identity(path):
        metadata_path = path / CAPTURE_IDENTITY_FILENAME
        if not metadata_path.is_file():
            return {"error": "capture identity is missing"}
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"error": str(exc)}
        if not isinstance(metadata, dict) or not metadata.get("capture_id"):
            return {"error": "capture identity must include capture_id"}
        return metadata

    @staticmethod
    def _event_payload(event_type, status_data):
        return {
            "service": SERVICE_NAME,
            "event_type": event_type,
            "timestamp": time.time(),
            "status_data": status_data,
        }

    @staticmethod
    def _error_response(request, error):
        return {
            "task_name": request.get("task_name") if isinstance(request, dict) else None,
            "session_id": request.get("session_id") if isinstance(request, dict) else None,
            "success": False,
            "status_data": None,
            "error": str(error),
        }

    @staticmethod
    def _copy(value):
        return json.loads(json.dumps(value))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    ArchiveManagerService().run()
