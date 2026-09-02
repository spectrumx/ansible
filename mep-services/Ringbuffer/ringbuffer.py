#!/usr/bin/env python3
"""DigitalRF ringbuffer service exposed through MQTT."""

import json
import logging
import os
import socket
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from digital_rf.ringbuffer import DigitalRFRingbuffer

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required: pip install PyYAML") from exc

SERVICE_NAME = "ringbuffer"
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
CONFIG_PATH = os.environ.get(
    "RINGBUFFER_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ringbuffer.yaml"),
)
STATUS_INTERVAL_S = 10.0

ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
STATUS_TOPIC = f"{SERVICE_NAME}/status"
DATA_TOPIC = f"{SERVICE_NAME}/data"
EVENT_TOPIC = f"{SERVICE_NAME}/event"

COMMANDS = {
    "get_status": {
        "description": "Return the current ringbuffer state.",
        "arguments": {},
    },
    "start": {
        "description": "Start the DigitalRF ringbuffer.",
        "arguments": {"ringbuffer_name": {"type": "string", "required": True}},
    },
    "stop": {
        "description": "Stop the DigitalRF ringbuffer.",
        "arguments": {"ringbuffer_name": {"type": "string", "required": True}},
    },
    "restart": {
        "description": "Stop and start the DigitalRF ringbuffer.",
        "arguments": {"ringbuffer_name": {"type": "string", "required": True}},
    },
}


class Ringbuffer:
    """Small lifecycle wrapper around DigitalRFRingbuffer."""

    def __init__(self, path: str, **options):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self._ring = DigitalRFRingbuffer(path, **options)
        self._running = False
        self._last_error = None
        self.config = dict(options)

    def start(self):
        if self._running:
            return
        self._ring.start()
        self._running = True

    def stop(self):
        if not self._running:
            return
        self._ring.stop()
        self._running = False

    def status(self) -> dict:
        return {
            "running": self._running,
            "path": self.path,
            "error": self._last_error,
        }


class RingbufferService:
    def __init__(self, broker: str = MQTT_BROKER, port: int = MQTT_PORT, config_path: str = CONFIG_PATH):
        self.broker = broker
        self.port = port
        self.started_at = time.time()
        self._status_seq = 0
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self.ringbuffers = {}
        self._build_ringbuffers()
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{SERVICE_NAME}_{uuid.uuid4().hex[:8]}",
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _load_config(self, path: str) -> dict:
        with open(path, "r", encoding="utf-8") as file_handle:
            config = yaml.safe_load(file_handle) or {}
        definitions = config.get("ringbuffers")
        if not isinstance(definitions, dict) or not definitions:
            raise ValueError("ringbuffer config must contain a non-empty ringbuffers mapping")
        return definitions

    def _build_ringbuffers(self):
        for name, definition in self.config.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("ringbuffer names must be non-empty strings")
            if not isinstance(definition, dict):
                raise ValueError(f"configuration for {name!r} must be a mapping")
            path = definition.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError(f"ringbuffer {name!r} requires a path")
            limits = {
                key: definition.get(key)
                for key in ("size", "count", "duration", "verbose", "dryrun", "starttime", "endtime", "include_drf", "include_dmd", "force_polling")
                if key in definition
            }
            if "duration_ms" in definition:
                limits["duration"] = definition["duration_ms"]
            if not any(limits.get(key) is not None for key in ("size", "count", "duration")):
                raise ValueError(f"ringbuffer {name!r} requires size, count, or duration_ms")
            enabled = bool(definition.get("enabled", True))
            instance = Ringbuffer(path, **limits)
            instance.name = name
            instance.enabled = enabled
            instance.config = dict(definition)
            self.ringbuffers[name] = instance

    def _get_ringbuffer(self, arguments: dict) -> Ringbuffer:
        name = arguments.get("ringbuffer_name")
        if not isinstance(name, str) or name not in self.ringbuffers:
            raise ValueError(f"unknown ringbuffer_name: {name!r}")
        return self.ringbuffers[name]

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logging.error("MQTT connection failed: rc=%s", reason_code)
            return
        client.subscribe(COMMAND_TOPIC, qos=1)
        self._publish(ANNOUNCE_TOPIC, self.build_announce(), retain=True)
        self.publish_status()

    def _on_message(self, client, userdata, message):
        request = None
        try:
            request = json.loads(message.payload.decode("utf-8"))
            response = self.handle_command(request)
        except Exception as exc:
            response = {
                "success": False,
                "session_id": request.get("session_id") if isinstance(request, dict) else None,
                "task_name": request.get("task_name") if isinstance(request, dict) else None,
                "status_data": None,
                "error": str(exc),
            }
        self._publish(RESPONSE_TOPIC, response, retain=False)

    def handle_command(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise ValueError("command payload must be an object")
        task_name = request.get("task_name")
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        if task_name == "get_status":
            status_data = self.build_status(arguments.get("ringbuffer_name"))
        elif task_name == "start":
            ringbuffer = self._get_ringbuffer(arguments)
            ringbuffer.start()
            status_data = ringbuffer.status()
        elif task_name == "stop":
            ringbuffer = self._get_ringbuffer(arguments)
            ringbuffer.stop()
            status_data = ringbuffer.status()
        elif task_name == "restart":
            ringbuffer = self._get_ringbuffer(arguments)
            ringbuffer.stop()
            ringbuffer.start()
            status_data = ringbuffer.status()
        else:
            raise ValueError(f"unsupported task_name: {task_name!r}")
        return {
            "success": True,
            "session_id": request.get("session_id"),
            "task_name": task_name,
            "status_data": status_data,
            "error": None,
        }

    def build_announce(self) -> dict:
        return {
            "title": "DigitalRF Ringbuffer Service",
            "description": "Owns the lifecycle of configured DigitalRF ringbuffers.",
            "service": SERVICE_NAME,
            "type": "service",
            "version": "0.1",
            "time_started": self.started_at,
            "config_path": self.config_path,
            "topics": {
                "announce": ANNOUNCE_TOPIC,
                "command": COMMAND_TOPIC,
                "response": RESPONSE_TOPIC,
                "status": STATUS_TOPIC,
                "data": DATA_TOPIC,
                "event": EVENT_TOPIC,
            },
            "commands": COMMANDS,
            "ringbuffers": {
                name: dict(instance.config, path=instance.path, enabled=instance.enabled)
                for name, instance in self.ringbuffers.items()
            },
        }

    def build_status(self, ringbuffer_name: str = None) -> dict:
        self._status_seq += 1
        instances = self.ringbuffers
        if ringbuffer_name is not None:
            if ringbuffer_name not in instances:
                raise ValueError(f"unknown ringbuffer_name: {ringbuffer_name!r}")
            instances = {ringbuffer_name: instances[ringbuffer_name]}
        return {
            "service": SERVICE_NAME,
            "state": "online",
            "timestamp": time.time(),
            "seq": self._status_seq,
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "ringbuffers": {name: ringbuffer.status() for name, ringbuffer in instances.items()},
        }

    def publish_status(self):
        self._publish(STATUS_TOPIC, self.build_status(), retain=True)

    def _publish(self, topic: str, payload: dict, retain: bool):
        self.client.publish(
            topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=retain,
        )

    def run(self):
        for ringbuffer in self.ringbuffers.values():
            if ringbuffer.enabled:
                ringbuffer.start()
        self.client.connect(self.broker, self.port, keepalive=60)
        self.client.loop_start()
        try:
            while True:
                self.publish_status()
                time.sleep(STATUS_INTERVAL_S)
        finally:
            self.client.loop_stop()
            self.client.disconnect()
            for ringbuffer in self.ringbuffers.values():
                ringbuffer.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    RingbufferService().run()
