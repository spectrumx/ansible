#!/usr/bin/env python3
"""Docker and Docker Compose management exposed through MQTT."""

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from typing import Optional

import paho.mqtt.client as mqtt


SERVICE_NAME = "dockermanager"
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
COMPOSE_DIRECTORY = "/opt/radiohound/docker"
STATUS_INTERVAL_S = 5.0
COMMAND_TIMEOUT_S = 120.0

ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
STATUS_TOPIC = f"{SERVICE_NAME}/status"
EVENT_TOPIC = f"{SERVICE_NAME}/event"
LOG_TOPIC = f"{SERVICE_NAME}/logs"

SERVICE_ACTIONS = {
    "start_services": "start",
    "stop_services": "stop",
    "restart_services": "restart",
}
PROJECT_ACTIONS = {
    "down_project": "down",
    "pull_project": "pull",
}
SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

COMMAND_DESCRIPTIONS = {
    "get_status": {"description": "Refresh and return Docker and Compose state.", "arguments": {}},
    "start_services": {"description": "Start selected Compose services.", "arguments": {"services": {"type": "array", "required": True}}},
    "stop_services": {"description": "Stop selected Compose services without removing the project.", "arguments": {"services": {"type": "array", "required": True}}},
    "restart_services": {"description": "Restart selected Compose services.", "arguments": {"services": {"type": "array", "required": True}}},
    "up_services": {"description": "Create and start selected Compose services.", "arguments": {"services": {"type": "array", "required": True}, "force_recreate": {"type": "boolean", "default": False}}},
    "up_project": {"description": "Create and start the entire Compose project.", "arguments": {"force_recreate": {"type": "boolean", "default": False}}},
    "down_project": {"description": "Stop and remove the entire Compose project.", "arguments": {}},
    "pull_project": {"description": "Pull images for the entire Compose project.", "arguments": {}},
    "get_logs": {"description": "Return a bounded Compose log snapshot.", "arguments": {"services": {"type": "array", "default": []}, "tail": {"type": "integer", "default": 100}}},
    "start_log_stream": {"description": "Start a non-retained Compose log stream on dockermanager/logs.", "arguments": {"stream_id": {"type": "string", "required": True}, "services": {"type": "array", "default": []}, "tail": {"type": "integer", "default": 30}}},
    "stop_log_stream": {"description": "Stop a Compose log stream by ID.", "arguments": {"stream_id": {"type": "string", "required": True}}},
}


class CommandError(RuntimeError):
    """A command failed with details suitable for an MQTT response."""


class DockerManager:
    """Own Docker CLI and Compose process execution."""

    def __init__(self, compose_directory: str = COMPOSE_DIRECTORY):
        self.compose_directory = compose_directory
        self._streams = {}
        self._stream_lock = threading.RLock()

    @staticmethod
    def run(command: list[str], *, cwd: Optional[str] = None, timeout: float = COMMAND_TIMEOUT_S) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                command,
                cwd=cwd,
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

    def run_checked(self, command: list[str], *, cwd: Optional[str] = None, timeout: float = COMMAND_TIMEOUT_S) -> str:
        result = self.run(command, cwd=cwd, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
            raise CommandError(detail)
        return (result.stdout or "").strip()

    def compose(self, arguments: list[str], *, timeout: float = COMMAND_TIMEOUT_S) -> str:
        return self.run_checked(
            ["docker", "compose", *arguments],
            cwd=self.compose_directory,
            timeout=timeout,
        )

    @staticmethod
    def parse_json_rows(text: str) -> list[dict]:
        if not text.strip():
            return []
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return [value]
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        except json.JSONDecodeError:
            pass
        rows = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def configured_services(self) -> list[str]:
        output = self.compose(["config", "--services"], timeout=15.0)
        return sorted({line.strip() for line in output.splitlines() if line.strip()})

    def validate_services(self, services) -> list[str]:
        if not isinstance(services, list) or not services:
            raise ValueError("services must be a non-empty array")
        normalized = []
        for service in services:
            if not isinstance(service, str) or not SERVICE_NAME_PATTERN.fullmatch(service):
                raise ValueError(f"invalid Compose service name: {service!r}")
            if service not in normalized:
                normalized.append(service)
        unknown = sorted(set(normalized) - set(self.configured_services()))
        if unknown:
            raise ValueError(f"unknown Compose services: {', '.join(unknown)}")
        return normalized

    @staticmethod
    def validate_tail(value) -> int:
        try:
            tail = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("tail must be an integer") from exc
        if not 0 <= tail <= 5000:
            raise ValueError("tail must be between 0 and 5000")
        return tail

    @staticmethod
    def ports_from_row(row: dict) -> str:
        publishers = row.get("Publishers")
        if not isinstance(publishers, list):
            return str(row.get("Ports") or "")
        ports = []
        for publisher in publishers:
            if not isinstance(publisher, dict):
                continue
            target = publisher.get("TargetPort")
            published = publisher.get("PublishedPort")
            protocol = publisher.get("Protocol") or "tcp"
            host = publisher.get("URL") or publisher.get("HostIP") or ""
            if published is not None and target is not None:
                source = f"{host}:{published}" if host else str(published)
                ports.append(f"{source}->{target}/{protocol}")
            elif target is not None:
                ports.append(f"{target}/{protocol}")
        return ", ".join(ports)

    def get_engine_status(self) -> dict:
        try:
            self.run_checked(["docker", "info"], timeout=10.0)
            output = self.run_checked(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], timeout=10.0)
        except CommandError as exc:
            return {"state": "unavailable", "error": str(exc), "containers": []}
        containers = []
        for row in self.parse_json_rows(output):
            containers.append({
                "id": row.get("ID"),
                "name": row.get("Names"),
                "image": row.get("Image"),
                "state": row.get("State"),
                "status": row.get("Status"),
                "ports": row.get("Ports"),
            })
        return {"state": "reachable", "error": None, "containers": containers}

    def get_compose_status(self) -> dict:
        try:
            configured = self.configured_services()
            output = self.compose(["ps", "-a", "--no-trunc", "--format", "json"], timeout=15.0)
        except (CommandError, ValueError) as exc:
            return {
                "state": "unavailable",
                "directory": self.compose_directory,
                "error": str(exc),
                "services": [],
            }
        rows_by_service = {}
        for row in self.parse_json_rows(output):
            service = str(row.get("Service") or "").strip()
            if service:
                rows_by_service[service] = row
        services = []
        for service in configured:
            row = rows_by_service.get(service, {})
            services.append({
                "service": service,
                "container": row.get("Name"),
                "state": row.get("State") or "not_created",
                "status": row.get("Status"),
                "command": row.get("Command"),
                "ports": self.ports_from_row(row),
                "exit_code": row.get("ExitCode"),
                "health": row.get("Health"),
            })
        return {
            "state": "available",
            "directory": self.compose_directory,
            "error": None,
            "services": services,
        }

    def get_status(self) -> dict:
        return {
            "service": SERVICE_NAME,
            "state": "online",
            "timestamp": time.time(),
            "engine": self.get_engine_status(),
            "compose": self.get_compose_status(),
        }

    def run_service_action(self, action: str, services) -> dict:
        selected = self.validate_services(services)
        output = self.compose([action, *selected])
        return {"action": action, "scope": "services", "services": selected, "output": output}

    def up_services(self, services, force_recreate=False) -> dict:
        selected = self.validate_services(services)
        arguments = ["up", "-d"]
        if bool(force_recreate):
            arguments.append("--force-recreate")
        arguments.extend(selected)
        output = self.compose(arguments)
        return {"action": "up", "scope": "services", "services": selected, "output": output}

    def run_project_action(self, action: str, *, force_recreate=False) -> dict:
        arguments = [action]
        if action == "up":
            arguments.append("-d")
            if bool(force_recreate):
                arguments.append("--force-recreate")
        output = self.compose(arguments)
        return {"action": action, "scope": "project", "output": output}

    def get_logs(self, services=None, tail=100) -> dict:
        selected = self.validate_services(services) if services else []
        line_count = self.validate_tail(tail)
        output = self.compose(["logs", "--no-color", "--tail", str(line_count), *selected], timeout=30.0)
        return {"services": selected, "tail": line_count, "lines": output.splitlines()}

    def start_log_stream(self, stream_id: str, services, tail: int, publish_line) -> dict:
        if not isinstance(stream_id, str) or not stream_id.strip():
            raise ValueError("stream_id must be a non-empty string")
        stream_id = stream_id.strip()
        selected = self.validate_services(services) if services else []
        line_count = self.validate_tail(tail)
        self.stop_log_stream(stream_id)
        command = ["docker", "compose", "logs", "--follow", "--no-color", "--tail", str(line_count), *selected]
        try:
            process = subprocess.Popen(
                command,
                cwd=self.compose_directory,
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

        threading.Thread(target=read_stream, name=f"docker-logs-{stream_id}", daemon=True).start()
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


class DockerManagerService:
    """MQTT adapter and command worker for DockerManager."""

    def __init__(self, broker: str = MQTT_BROKER, port: int = MQTT_PORT, compose_directory: str = COMPOSE_DIRECTORY):
        self.manager = DockerManager(compose_directory)
        self.broker = broker
        self.port = port
        self.started_at = time.time()
        self._stop = threading.Event()
        self._status_changed = threading.Event()
        self._command_queue = queue.SimpleQueue()
        self._command_thread = threading.Thread(target=self._command_loop, name="docker-manager-commands", daemon=True)
        self._status_thread = threading.Thread(target=self._status_loop, name="docker-manager-status", daemon=True)
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

    def build_announce(self) -> dict:
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
        elif task == "up_services":
            result = self.manager.up_services(arguments.get("services"), arguments.get("force_recreate", False))
        elif task == "up_project":
            result = self.manager.run_project_action("up", force_recreate=arguments.get("force_recreate", False))
        elif task in PROJECT_ACTIONS:
            result = self.manager.run_project_action(PROJECT_ACTIONS[task])
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
            self._publish(EVENT_TOPIC, {"event": "compose_action", "task_name": task, "result": result, "timestamp": time.time()})
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
                logging.exception("DockerManager command failed")
                response = self.response(request, success=False, error=str(exc))
            self._publish(RESPONSE_TOPIC, response)

    def _status_loop(self):
        while not self._stop.is_set():
            self._status_changed.wait(STATUS_INTERVAL_S)
            self._status_changed.clear()
            try:
                self.publish_status()
            except Exception:
                logging.exception("DockerManager status publication failed")

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
    DockerManagerService(
        broker=os.getenv("MQTT_BROKER", MQTT_BROKER),
        port=int(os.getenv("MQTT_PORT", str(MQTT_PORT))),
        compose_directory=os.getenv("DOCKER_COMPOSE_DIR", COMPOSE_DIRECTORY),
    ).run()


if __name__ == "__main__":
    main()