#!/usr/bin/env python3
"""Systemd service lifecycle and journal access exposed through MQTT."""

import json
import logging
import os
import queue
import subprocess
import threading
import time
import uuid
from typing import Optional

import paho.mqtt.client as mqtt


SERVICE_NAME = "servicemanager"
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
STATUS_INTERVAL_S = 5.0
COMMAND_TIMEOUT_S = 30.0

ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
STATUS_TOPIC = f"{SERVICE_NAME}/status"
EVENT_TOPIC = f"{SERVICE_NAME}/event"
LOG_TOPIC = f"{SERVICE_NAME}/logs"

MANAGED_UNITS = (
    "afe-control.service",
    "archive-manager.service",
    "capture-orchestrator.service",
    "docker-manager.service",
    "host-manager.service",
    "ringbuffer.service",
    "tuner-control.service",
    "upload-manager.service",
)

SERVICE_ACTIONS = {
    "start_services": "start",
    "stop_services": "stop",
    "restart_services": "restart",
}

COMMAND_DESCRIPTIONS = {
    "get_status": {"description": "Refresh and return managed systemd unit state.", "arguments": {}},
    "start_services": {"description": "Start selected managed units.", "arguments": {"services": {"type": "array", "required": True}}},
    "stop_services": {"description": "Stop selected managed units.", "arguments": {"services": {"type": "array", "required": True}}},
    "restart_services": {"description": "Restart selected managed units.", "arguments": {"services": {"type": "array", "required": True}}},
    "get_logs": {"description": "Return a bounded journal snapshot.", "arguments": {"services": {"type": "array", "required": True}, "tail": {"type": "integer", "default": 100}}},
    "start_log_stream": {"description": "Start a non-retained journal stream.", "arguments": {"stream_id": {"type": "string", "required": True}, "services": {"type": "array", "required": True}, "tail": {"type": "integer", "default": 30}}},
    "stop_log_stream": {"description": "Stop a journal stream by ID.", "arguments": {"stream_id": {"type": "string", "required": True}}},
}


class CommandError(RuntimeError):
    """A command failed with details suitable for an MQTT response."""


class ServiceManager:
    """Own allowlisted systemctl and journalctl process execution."""

    def __init__(self):
        self._streams = {}
        self._stream_lock = threading.RLock()

    @staticmethod
    def _privileged(command: list[str]) -> list[str]:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return command
        return ["sudo", "-n", *command]

    @staticmethod
    def run(command: list[str], *, timeout: float = COMMAND_TIMEOUT_S) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"command timed out after {timeout:g} seconds") from exc
        except OSError as exc:
            raise CommandError(str(exc)) from exc

    @classmethod
    def run_checked(cls, command: list[str], *, timeout: float = COMMAND_TIMEOUT_S) -> str:
        result = cls.run(command, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
            raise CommandError(detail)
        return (result.stdout or "").strip()

    @staticmethod
    def validate_services(services) -> list[str]:
        if not isinstance(services, list) or not services:
            raise ValueError("services must be a non-empty array")
        selected = []
        for service in services:
            if not isinstance(service, str):
                raise ValueError("each service must be a string")
            unit = service.strip()
            if unit not in MANAGED_UNITS:
                raise ValueError(f"unmanaged service: {service!r}")
            if unit not in selected:
                selected.append(unit)
        return selected

    @staticmethod
    def validate_tail(value) -> int:
        try:
            tail = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("tail must be an integer") from exc
        if not 0 <= tail <= 5000:
            raise ValueError("tail must be between 0 and 5000")
        return tail

    def get_unit_status(self, unit: str) -> dict:
        result = self.run([
            "systemctl",
            "show",
            unit,
            "--no-pager",
            "--property=Id,Description,LoadState,ActiveState,SubState,UnitFileState,MainPID,ExecMainStatus,StateChangeTimestamp",
        ], timeout=10.0)
        fields = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                fields[key] = value
        return {
            "service": unit,
            "description": fields.get("Description") or unit,
            "load_state": fields.get("LoadState") or "unknown",
            "active_state": fields.get("ActiveState") or "unknown",
            "sub_state": fields.get("SubState") or "unknown",
            "unit_file_state": fields.get("UnitFileState") or "unknown",
            "main_pid": int(fields.get("MainPID") or 0),
            "exit_status": int(fields.get("ExecMainStatus") or 0),
            "changed_at": fields.get("StateChangeTimestamp") or None,
            "error": (result.stderr or "").strip() or None,
        }

    def get_status(self) -> dict:
        return {
            "service": SERVICE_NAME,
            "state": "online",
            "timestamp": time.time(),
            "services": [self.get_unit_status(unit) for unit in MANAGED_UNITS],
        }

    def run_service_action(self, action: str, services) -> dict:
        selected = self.validate_services(services)
        command = self._privileged(["systemctl", action, *selected])
        output = self.run_checked(command)
        return {"action": action, "services": selected, "output": output}

    def get_logs(self, services, tail=100) -> dict:
        selected = self.validate_services(services)
        line_count = self.validate_tail(tail)
        command = ["journalctl", "--no-pager", "-o", "cat", "-n", str(line_count)]
        for unit in selected:
            command.extend(["-u", unit])
        output = self.run_checked(command, timeout=30.0)
        return {"services": selected, "tail": line_count, "lines": output.splitlines()}

    def start_log_stream(self, stream_id: str, services, tail: int, publish_line) -> dict:
        if not isinstance(stream_id, str) or not stream_id.strip():
            raise ValueError("stream_id must be a non-empty string")
        stream_id = stream_id.strip()
        selected = self.validate_services(services)
        line_count = self.validate_tail(tail)
        self.stop_log_stream(stream_id)
        command = ["journalctl", "--follow", "--no-pager", "-o", "cat", "-n", str(line_count)]
        for unit in selected:
            command.extend(["-u", unit])
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise CommandError(str(exc)) from exc
        with self._stream_lock:
            self._streams[stream_id] = process

        def read_stream():
            try:
                if process.stdout is not None:
                    for line in process.stdout:
                        publish_line(stream_id, selected, line.rstrip("\r\n"))
            finally:
                return_code = process.wait()
                with self._stream_lock:
                    if self._streams.get(stream_id) is process:
                        self._streams.pop(stream_id, None)
                        publish_line(stream_id, selected, None, return_code=return_code)

        threading.Thread(target=read_stream, name=f"service-logs-{stream_id}", daemon=True).start()
        return {"stream_id": stream_id, "services": selected, "tail": line_count, "state": "streaming"}

    def stop_log_stream(self, stream_id: str) -> dict:
        if not isinstance(stream_id, str) or not stream_id.strip():
            raise ValueError("stream_id must be a non-empty string")
        stream_id = stream_id.strip()
        with self._stream_lock:
            process = self._streams.pop(stream_id, None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
        return {"stream_id": stream_id, "state": "stopped", "was_active": process is not None}

    def close(self):
        with self._stream_lock:
            stream_ids = list(self._streams)
        for stream_id in stream_ids:
            self.stop_log_stream(stream_id)


class ServiceManagerService:
    """MQTT adapter and command worker for ServiceManager."""

    def __init__(self, broker: str = MQTT_BROKER, port: int = MQTT_PORT):
        self.manager = ServiceManager()
        self.broker = broker
        self.port = port
        self._stop = threading.Event()
        self._status_changed = threading.Event()
        self._command_queue = queue.SimpleQueue()
        self._command_thread = threading.Thread(target=self._command_loop, name="service-manager-commands", daemon=True)
        self._status_thread = threading.Thread(target=self._status_loop, name="service-manager-status", daemon=True)
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{SERVICE_NAME}_{uuid.uuid4().hex[:8]}",
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.will_set(STATUS_TOPIC, json.dumps({"service": SERVICE_NAME, "state": "offline", "timestamp": time.time()}), retain=True)

    def _publish(self, topic: str, payload: dict, *, retain=False):
        self.client.publish(topic, json.dumps(payload), qos=0, retain=retain)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logging.error("MQTT connection failed: rc=%s", reason_code)
            return
        client.subscribe(COMMAND_TOPIC)
        self._publish(ANNOUNCE_TOPIC, self.build_announce(), retain=True)
        self._status_changed.set()

    def _on_message(self, client, userdata, message):
        self._command_queue.put(message.payload)

    @staticmethod
    def build_announce() -> dict:
        return {
            "service": SERVICE_NAME,
            "type": "service",
            "version": "1.0",
            "timestamp": time.time(),
            "topics": {
                "command": COMMAND_TOPIC,
                "response": RESPONSE_TOPIC,
                "status": STATUS_TOPIC,
                "event": EVENT_TOPIC,
                "logs": LOG_TOPIC,
            },
            "commands": COMMAND_DESCRIPTIONS,
            "managed_services": list(MANAGED_UNITS),
        }

    @staticmethod
    def response(request, *, success: bool, status_data=None, error=None) -> dict:
        return {
            "success": success,
            "session_id": request.get("session_id") if isinstance(request, dict) else None,
            "task_name": request.get("task_name") if isinstance(request, dict) else None,
            "status_data": status_data,
            "error": error,
        }

    def handle_command(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise ValueError("command payload must be an object")
        task = request.get("task_name")
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("command arguments must be an object")

        if task == "get_status":
            result = self.manager.get_status()
        elif task in SERVICE_ACTIONS:
            result = self.manager.run_service_action(SERVICE_ACTIONS[task], arguments.get("services"))
        elif task == "get_logs":
            result = self.manager.get_logs(arguments.get("services"), arguments.get("tail", 100))
        elif task == "start_log_stream":
            result = self.manager.start_log_stream(
                arguments.get("stream_id"),
                arguments.get("services"),
                arguments.get("tail", 30),
                self.publish_log,
            )
        elif task == "stop_log_stream":
            result = self.manager.stop_log_stream(arguments.get("stream_id"))
        else:
            raise ValueError(f"unsupported task: {task!r}")

        if task not in {"get_status", "get_logs", "start_log_stream", "stop_log_stream"}:
            self._publish(EVENT_TOPIC, {"event": "service_action", "task_name": task, "result": result, "timestamp": time.time()})
            self._status_changed.set()
        return result

    def publish_log(self, stream_id: str, services: list[str], line: Optional[str], *, return_code=None):
        self._publish(LOG_TOPIC, {
            "stream_id": stream_id,
            "services": services,
            "line": line,
            "state": "ended" if line is None else "streaming",
            "return_code": return_code,
            "timestamp": time.time(),
        })

    def publish_status(self):
        self._publish(STATUS_TOPIC, self.manager.get_status(), retain=True)

    def _command_loop(self):
        while not self._stop.is_set():
            payload = self._command_queue.get()
            if payload is None:
                return
            request = None
            try:
                request = json.loads(payload.decode("utf-8"))
                result = self.handle_command(request)
                response = self.response(request, success=True, status_data=result)
            except Exception as exc:
                logging.exception("ServiceManager command failed")
                response = self.response(request, success=False, error=str(exc))
            self._publish(RESPONSE_TOPIC, response)

    def _status_loop(self):
        while not self._stop.is_set():
            self._status_changed.wait(STATUS_INTERVAL_S)
            self._status_changed.clear()
            try:
                self.publish_status()
            except Exception:
                logging.exception("ServiceManager status publication failed")

    def run(self):
        self._command_thread.start()
        self._status_thread.start()
        try:
            self.client.connect(self.broker, self.port, keepalive=60)
            self.client.loop_forever()
        finally:
            self.close()

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._status_changed.set()
        self._command_queue.put(None)
        self.manager.close()
        self.client.disconnect()


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ServiceManagerService(
        broker=os.getenv("MQTT_BROKER", MQTT_BROKER),
        port=int(os.getenv("MQTT_PORT", str(MQTT_PORT))),
    ).run()


if __name__ == "__main__":
    main()
