"""MEP message bus topics and connection mechanics."""

import json
import logging
import threading
import time
import uuid
from typing import Callable, Optional

import paho.mqtt.client as mqtt


BUS_HOST = "localhost"
BUS_PORT = 1883

RFSOC_COMMAND_TOPIC = "rfsoc/command"
RFSOC_STATUS_TOPIC = "rfsoc/status"
RFSOC_PLL_CONFIG_TOPIC = "rfsoc/pll_config"
RECORDER_STATUS_TOPIC = "recorder/status"

TUNER_COMMAND_TOPIC = "tunercontrol/command"
TUNER_STATUS_TOPIC = "tunercontrol/status"
TUNER_RESPONSE_TOPIC = "tunercontrol/response"

AFE_COMMAND_TOPIC = "afecontrol/command"
AFE_RESPONSE_TOPIC = "afecontrol/response"
AFE_STATUS_TOPIC = "afecontrol/status"
AFE_ANNOUNCE_TOPIC = "afecontrol/announce"
AFE_GNSS_TOPIC = "afecontrol/data/gps"
AFE_IMU_TOPIC = "afecontrol/data/imu"
AFE_MAG_TOPIC = "afecontrol/data/mag"
AFE_HK_TOPIC = "afecontrol/data/hk"
AFE_REGISTERS_TOPIC = "afecontrol/status/registers"
AFE_GPSD_STATUS_TOPIC = "afecontrol/status/gpsd"
AFE_RAW_TOPIC = "afecontrol/data/raw"

CAPTURE_ORCHESTRATOR_COMMAND_TOPIC = "captureorchestrator/command"
CAPTURE_ORCHESTRATOR_RESPONSE_TOPIC = "captureorchestrator/response"
CAPTURE_ORCHESTRATOR_STATUS_TOPIC = "captureorchestrator/status"

ARCHIVE_MANAGER_COMMAND_TOPIC = "archivemanager/command"
ARCHIVE_MANAGER_RESPONSE_TOPIC = "archivemanager/response"
ARCHIVE_MANAGER_STATUS_TOPIC = "archivemanager/status"

UPLOAD_MANAGER_COMMAND_TOPIC = "uploadmanager/command"
UPLOAD_MANAGER_RESPONSE_TOPIC = "uploadmanager/response"
UPLOAD_MANAGER_STATUS_TOPIC = "uploadmanager/status"
UPLOAD_MANAGER_EVENT_TOPIC = "uploadmanager/event"

HOST_MANAGER_COMMAND_TOPIC = "hostmanager/command"
HOST_MANAGER_RESPONSE_TOPIC = "hostmanager/response"
HOST_MANAGER_STATUS_TOPIC = "hostmanager/status"
HOST_MANAGER_ANNOUNCE_TOPIC = "hostmanager/announce"

DOCKER_MANAGER_COMMAND_TOPIC = "dockermanager/command"
DOCKER_MANAGER_RESPONSE_TOPIC = "dockermanager/response"
DOCKER_MANAGER_STATUS_TOPIC = "dockermanager/status"
DOCKER_MANAGER_LOG_TOPIC = "dockermanager/logs"

SERVICE_MANAGER_COMMAND_TOPIC = "servicemanager/command"
SERVICE_MANAGER_RESPONSE_TOPIC = "servicemanager/response"
SERVICE_MANAGER_STATUS_TOPIC = "servicemanager/status"
SERVICE_MANAGER_ANNOUNCE_TOPIC = "servicemanager/announce"
SERVICE_MANAGER_LOG_TOPIC = "servicemanager/logs"

SPECTRUM_TOPIC_PATTERN = "radiohound/clients/data/#"

RECORDER_CHANNEL_PORTS = {"A": 60134, "B": 60133, "C": 60132, "D": 60131}
CHANNEL_OPTIONS = sorted(RECORDER_CHANNEL_PORTS)

TUNERS = {
    "VALON": {"backend": "valon", "injection_side": "high"},
    "LMX2820": {"backend": "lmx2820", "injection_side": "high"},
    "TEST": {"backend": "dummy", "injection_side": "high"},
}
TUNER_OPTIONS = ["None", *TUNERS, "auto"]

CONJUGATE_POLICY_DEFAULT = "auto"
CONJUGATE_POLICY_OPTIONS = ("auto", "force_on", "force_off")

TX_AMPLITUDE_BINS_MAX = 8191
TX_OFFSET_FREQ_MAX_MHZ = 32
TX_CHANNEL_OPTIONS = ("None", "A", "B", "A,B")


class Bus:
    """One bus connection with subscriptions, dispatch, and retained-state cache."""

    def __init__(self, host: str = BUS_HOST, port: int = BUS_PORT):
        self._host = host
        self._port = port
        self._listeners: dict[str, list[Callable]] = {}
        self._global_listeners: list[Callable] = []
        self._pattern_listeners: list[tuple[str, Callable]] = []
        self._connection_listeners: list[Callable[[dict], None]] = []
        self._subscriptions: set[str] = set()
        self._status_cache: dict[str, dict] = {}
        self._registry_lock = threading.RLock()
        self._subscription_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._connected = False
        self._last_error: Optional[str] = None
        self._loop_started = False
        self.spec_topic = SPECTRUM_TOPIC_PATTERN

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=f"mep_client_{uuid.uuid4().hex[:12]}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        logging.info("Connecting to bus at %s:%s", host, port)
        try:
            self._client.connect(host, port, keepalive=60)
            self._client.loop_start()
            self._loop_started = True
            time.sleep(0.5)
        except OSError as exc:
            self._last_error = str(exc)
            logging.warning("Bus offline: could not connect to %s:%s (%s)", host, port, exc)

    def on_status(self, topic: str, callback: Callable[[dict], None]):
        with self._registry_lock:
            self._listeners.setdefault(topic, []).append(callback)
        self.subscribe(topic)
        cached = self.get_cached_status(topic)
        if isinstance(cached, dict):
            try:
                callback(cached)
            except Exception:
                logging.exception("Cached bus listener failed for %s", topic)

    def on_message(self, callback: Callable[[str, bytes], None]):
        with self._registry_lock:
            self._global_listeners.append(callback)

    def on_status_pattern(
        self,
        pattern: str,
        callback: Callable[[str, dict], None],
        subscribe: bool = True,
    ):
        with self._registry_lock:
            self._pattern_listeners.append((pattern, callback))
        if subscribe:
            self.subscribe(pattern)

    def remove_status_pattern(self, pattern: str, callback: Callable[[str, dict], None]):
        with self._registry_lock:
            listener = (pattern, callback)
            if listener in self._pattern_listeners:
                self._pattern_listeners.remove(listener)

    def subscribe(self, topic: str):
        with self._subscription_lock:
            self._subscriptions.add(topic)
        if self._connected:
            self._client.subscribe(topic)

    def unsubscribe(self, topic: str):
        with self._subscription_lock:
            self._subscriptions.discard(topic)
        if self._connected:
            self._client.unsubscribe(topic)

    def on_connection_state(
        self, callback: Callable[[dict], None], emit_initial: bool = True
    ):
        self._connection_listeners.append(callback)
        if emit_initial:
            callback(self.get_connection_status())

    def remove_connection_listener(self, callback: Callable[[dict], None]):
        if callback in self._connection_listeners:
            self._connection_listeners.remove(callback)

    def remove_listener(self, topic: str, callback: Callable):
        with self._registry_lock:
            listeners = self._listeners.get(topic, [])
            if callback in listeners:
                listeners.remove(callback)

    def get_cached_status(self, topic: str) -> Optional[dict]:
        with self._cache_lock:
            return self._status_cache.get(topic)

    def is_connected(self) -> bool:
        return self._connected

    def get_connection_status(self) -> dict:
        return {
            "connected": self._connected,
            "host": self._host,
            "port": self._port,
            "last_error": self._last_error,
        }

    def reconnect(self) -> bool:
        try:
            self._client.reconnect()
            if not self._loop_started:
                self._client.loop_start()
                self._loop_started = True
            return True
        except OSError as exc:
            self._last_error = str(exc)
            self._connected = False
            logging.warning("Bus reconnect failed for %s:%s (%s)", self._host, self._port, exc)
            self._emit_connection_state()
            return False

    def publish_command(self, topic: str, payload: dict, sleep_s: float = 0.1) -> bool:
        if not self._connected:
            logging.warning("Bus offline: command not sent to %s payload=%s", topic, payload)
            return False
        result = self._client.publish(topic, json.dumps(payload))
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self._last_error = f"publish rc={result.rc}"
            logging.warning("Bus publish failed: topic=%s rc=%s", topic, result.rc)
            return False
        if sleep_s:
            time.sleep(sleep_s)
        return True

    def publish(self, topic: str, payload: str = "", retain: bool = False) -> bool:
        if not self._connected:
            logging.warning("Bus offline: publish not sent to %s", topic)
            return False
        result = self._client.publish(topic, payload, retain=retain)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self._last_error = f"publish rc={result.rc}"
            logging.warning("Bus publish failed: topic=%s rc=%s", topic, result.rc)
            return False
        return True

    def clear_retained(self, topic: str) -> bool:
        return self.publish(topic, retain=True)

    def disconnect(self):
        if self._loop_started:
            self._client.loop_stop()
            self._loop_started = False
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, return_code):
        self._connected = return_code == 0
        self._last_error = None if self._connected else f"rc={return_code}"
        if self._connected:
            with self._subscription_lock:
                subscriptions = tuple(self._subscriptions)
            for topic in subscriptions:
                client.subscribe(topic)
        self._emit_connection_state()

    def _on_disconnect(self, client, userdata, return_code):
        self._connected = False
        if return_code != 0:
            self._last_error = f"disconnect rc={return_code}"
        self._emit_connection_state()

    def _on_message(self, client, userdata, message):
        with self._registry_lock:
            global_callbacks = tuple(self._global_listeners)
            exact_callbacks = tuple(self._listeners.get(message.topic, ()))
            pattern_callbacks = tuple(
                callback
                for pattern, callback in self._pattern_listeners
                if self.topic_matches(message.topic, pattern)
            )

        for callback in global_callbacks:
            try:
                callback(message.topic, message.payload)
            except Exception:
                logging.exception("Raw bus listener failed for %s", message.topic)

        if not exact_callbacks and not pattern_callbacks:
            return
        try:
            payload = json.loads(message.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        if exact_callbacks and isinstance(payload, dict):
            with self._cache_lock:
                self._status_cache[message.topic] = payload
        for callback in exact_callbacks:
            try:
                callback(payload)
            except Exception:
                logging.exception("Bus listener failed for %s", message.topic)
        if isinstance(payload, dict):
            for callback in pattern_callbacks:
                try:
                    callback(message.topic, payload)
                except Exception:
                    logging.exception("Pattern bus listener failed for %s", message.topic)

    def _emit_connection_state(self):
        status = self.get_connection_status()
        for callback in tuple(self._connection_listeners):
            try:
                callback(status)
            except Exception:
                logging.exception("Bus connection listener failed")

    @staticmethod
    def topic_matches(topic: str, pattern: str) -> bool:
        topic_parts = topic.split("/")
        pattern_parts = pattern.split("/")
        if len(pattern_parts) > len(topic_parts) and pattern_parts[-1] != "#":
            return False
        for index, pattern_part in enumerate(pattern_parts):
            if pattern_part == "#":
                return True
            if index >= len(topic_parts):
                return False
            if pattern_part != "+" and pattern_part != topic_parts[index]:
                return False
        return len(topic_parts) == len(pattern_parts)