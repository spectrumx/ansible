#!/usr/bin/env python3
"""Compatibility adapter between RadioHound and the public MEP client API."""

# ============================= #
#            IMPORTS            #
# ============================= #

import json
import logging
import math
from pathlib import Path
import re
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mep_client import MEPClient


# ============================= #
#         CONFIGURATION         #
# ============================= #

# Defaults preserve the legacy MAC-selection and direct-broker conventions.
# Set overrides only when the site uses a different identity or endpoint.
NODE_ID = ""
# Default display name: MEP-<last four ID digits>.
NODE_NAME = ""
LOCAL_HOST = "localhost"
LOCAL_PORT = 1883
REMOTE_HOST = ""
# Optional read-only host path to an existing system.json.
SYSTEM_CONFIG_FILE = None
DEFAULT_REMOTE_HOST = "radiohound.ee.nd.edu"
REMOTE_PORT = 1883
ANNOUNCE_INTERVAL_S = 10
LOCAL_RETRY_S = 10

logger = logging.getLogger("radiohoundgateway")

# Explicit status projections: do not export arbitrary service configuration.
COMPONENT_FIELDS = {
    "rfsoc": ("state", "state_RX", "state_TX", "f_c_hz", "f_if_hz", "f_s", "pps_count", "channels", "error", "timestamp"),
    "recorder": ("state", "error", "timestamp"),
    "tuner_control": ("state", "ready", "backend", "name", "detected_tuners", "lock_supported", "lock_status", "error", "timestamp"),
    "captureorchestrator": ("state", "error", "timestamp"),
    "gnss": (
        "fix_valid", "fix_mode", "fix_quality", "lat", "lon", "altitude_msl_m",
        "altitude_hae_m", "satellites_used", "satellites_visible", "hdop",
        "timestamp", "service_timestamp",
    ),
}
WORKFLOW_FIELDS = ("state", "operation", "signal_path", "frequency_hz", "capture_name", "step_index", "step_count", "error")

# ============================= #
#       STATUS TRANSLATION      #
# ============================= #

def status_value(value):
    """Limit projected status to finite JSON values of bounded size."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, (tuple, list)):
        return [
            status_value(item) for item in value[:16]
            if not isinstance(item, (dict, list, tuple))
        ]
    return None


def select_fields(payload, fields):
    return {key: status_value(payload[key]) for key in fields if key in payload}


def system_config():
    """Read only an explicitly selected existing config; never create one."""
    if SYSTEM_CONFIG_FILE:
        with Path(SYSTEM_CONFIG_FILE).open(encoding="utf-8") as config_file:
            config = json.load(config_file)
        if not isinstance(config, dict):
            raise ValueError("Existing system.json must contain a JSON object")
        return config
    return {}


def broker_host(config):
    """Preserve explicit override, system broker_name, and legacy fallback."""
    name = REMOTE_HOST or config.get("broker_name") or DEFAULT_REMOTE_HOST
    if not isinstance(name, str) or not name.strip():
        raise ValueError("broker_name must be a non-empty hostname")
    return name.strip()


def host_fields(payload):
    """Translate existing HostManager values to legacy announcement fields."""
    fields = {}
    for section, key, target in (
        ("identity", "hostname", "hostname"),
        ("identity", "release", "kernelVersion"),
        ("cpu", "used_percent", "cpu_percent_used"),
        ("memory", "used_percent", "memory_percent_used"),
        ("platform", "name", "hardware_type"),
    ):
        values = payload.get(section)
        if isinstance(values, dict) and key in values:
            fields[target] = status_value(values[key])

    # Icarus displayed disk usage in GiB, rounded to one decimal.
    disk = payload.get("disk")
    if isinstance(disk, dict):
        for key, target in (("free_bytes", "disk_free"), ("used_bytes", "disk_used")):
            value = disk.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                fields[target] = round(value / 1024 ** 3, 1)

    return fields


def node_id_from_host(payload):
    """Select the legacy interface identity using public HostManager status."""
    network = payload.get("network", {})
    interfaces = network.get("interfaces", []) if isinstance(network, dict) else []
    by_name = {
        item.get("name"): item.get("mac") for item in interfaces
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    preferred = ("en0", "eth0", "eth1", "wlan0")
    names = list(preferred) + [name for name in by_name if name.startswith("e") and name not in preferred]

    for name in names:
        mac = by_name.get(name)
        if not isinstance(mac, str):
            continue

        node_id = mac.replace(":", "").lower()
        if (
            re.fullmatch(r"[0-9a-f]{12}", node_id)
            and node_id not in {"000000000000", "ffffffffffff", "3608fcc6ef4b"}
        ):
            return node_id
    return None

# ============================= #
#         LATEST STATUS         #
# ============================= #

class StatusSnapshot:
    """Fixed-size latest-state cache shared by local and remote workers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._connected = False
        self._components = {}
        self._node_id = None
        self._host_fields = {}
        self._host_current = False
        self._spectrum = None

    def host_status(self, payload):
        if not isinstance(payload, dict):
            return

        node_id = node_id_from_host(payload)
        fields = host_fields(payload)
        with self._lock:
            if self._node_id is None and node_id is not None:
                self._node_id = node_id
            self._host_fields = fields
            self._host_current = self._connected

    def node_id(self):
        with self._lock:
            return self._node_id

    def connection(self, status):
        connected = bool(status.get("connected", False))
        with self._lock:
            self._connected = connected
            if not connected:
                self._host_current = False
                self._spectrum = None
                for entry in self._components.values():
                    entry["current"] = False

    def source_message(self, topic, payload):
        """Keep one latest periodogram with its original RadioHound route."""
        if not isinstance(topic, str) or not re.fullmatch(
            r"radiohound/clients/data/[0-9a-fA-F]{12}(?:/[^/+#\x00]+)*", topic
        ):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), str):
            return
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("data_type") != "periodogram":
            return

        # The public callback supplies a read-only parsed message. Do not decode,
        # mutate, or send it over the network from the local callback thread.
        with self._lock:
            if self._connected:
                self._spectrum = (topic, payload)

    def take_spectrum(self):
        with self._lock:
            message = self._spectrum
            self._spectrum = None
            return message

    def update(self, name, payload):
        if not isinstance(payload, dict) or not payload:
            return
        selected = select_fields(payload, COMPONENT_FIELDS[name])
        if name == "captureorchestrator":
            for path in ("rx", "tx"):
                if isinstance(payload.get(path), dict):
                    selected[path] = select_fields(payload[path], WORKFLOW_FIELDS)
        if not selected:
            return

        with self._lock:
            self._components[name] = {
                "status": selected,
                "observed_at": time.time(),
                "current": self._connected,
            }

    def announcement(self, node_id, node_name, host):
        with self._lock:
            capabilities = {"serial": node_id}
            for name in COMPONENT_FIELDS:
                entry = self._components.get(name)
                available = bool(self._connected and entry and entry["current"])
                if available:
                    component = dict(entry["status"])
                    component.setdefault("state", "unknown")
                else:
                    component = {"state": "unavailable"}
                    if entry:
                        component["last_known"] = dict(entry["status"])
                component["available"] = available
                component["observed_at"] = entry["observed_at"] if entry else None
                capabilities[name] = component

            capabilities["gateway"] = {
                "read_only": True,
                "local_connection": {
                    "connected": self._connected,
                    "host": LOCAL_HOST,
                    "port": LOCAL_PORT,
                },
            }
            node_fields = dict(self._host_fields) if self._connected and self._host_current else {
                key: None for key in self._host_fields
            }

            # RadioHound stores location at the announcement's payload root.
            # Keep fix validity and source timestamps in the GNSS capability too.
            gnss = capabilities["gnss"]
            latitude = gnss.get("lat")
            longitude = gnss.get("lon")
            valid_location = (
                gnss["available"] and gnss.get("fix_valid") is True
                and isinstance(latitude, (int, float)) and not isinstance(latitude, bool)
                and isinstance(longitude, (int, float)) and not isinstance(longitude, bool)
                and -90 <= latitude <= 90 and -180 <= longitude <= 180
            )
            altitude = gnss.get("altitude_msl_m")
            valid_altitude = (
                valid_location and isinstance(altitude, (int, float))
                and not isinstance(altitude, bool)
            )
            node_fields.update({
                "latitude": latitude if valid_location else None,
                "longitude": longitude if valid_location else None,
                "altitude": altitude if valid_altitude else None,
            })

        # Keep the legacy announcement envelope consumed by RadioHound App.
        node_fields.update({
            "mac_address": node_id,
            "short_name": node_name or f"MEP-{node_id[-4:]}",
            "broker_name": host,
            "sensors": [{"type": "sensors.mep", "capabilities": capabilities}],
        })
        return {"message": "ANNOUNCE", "payload": node_fields}

# ============================= #
#        LOCAL OBSERVATION      #
# ============================= #

def observe_local(snapshot, stopping, client_factory=MEPClient):
    """Observe status and spectrum; recreate our client if connection stays down.

    MEPClient has no public reconnect method. Recreating our own instance also
    recovers an initial connection failure without touching its private bus.
    """
    while not stopping.is_set():
        client = None
        try:
            # Observe only public status callbacks; never request device actions.
            client = client_factory(host=LOCAL_HOST, port=LOCAL_PORT)
            client.on_connection_state(snapshot.connection)
            client.host.on_status(snapshot.host_status)
            client.afe.on_gnss(lambda payload: snapshot.update("gnss", payload))
            client.spectrum.on_source_message(snapshot.source_message)
            # This only subscribes to existing data; it does not start acquisition.
            client.spectrum.start()
            for name, component in (
                ("rfsoc", client.rfsoc),
                ("recorder", client.recorder),
                ("tuner_control", client.tuner),
                ("captureorchestrator", client.capture),
            ):
                # Public subscriptions deliver any already-cached status too.
                component.on_status(lambda payload, name=name: snapshot.update(name, payload))

            # Recreate this gateway's client only after a sustained disconnection.
            disconnected_at = None
            while not stopping.wait(1):
                if client.is_connected():
                    disconnected_at = None
                elif disconnected_at is None:
                    disconnected_at = time.monotonic()
                elif time.monotonic() - disconnected_at >= LOCAL_RETRY_S:
                    break
        except Exception:
            snapshot.connection({"connected": False})
            logger.exception("Local status observer failed; retrying")
            stopping.wait(LOCAL_RETRY_S)
        finally:
            snapshot.connection({"connected": False})
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    logger.warning("Could not close local status client")

# ============================= #
#        REMOTE PUBLICATION     #
# ============================= #

def remote_client(node_id, connected_event):
    """Create the publish-only remote client; never subscribe to commands."""
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"radiohoundgateway_{node_id}",
    )
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.max_queued_messages_set(1)

    last_state = [None]

    def report(state):
        if state != last_state[0]:
            logger.info("RadioHound connection: %s", state)
            last_state[0] = state

    def on_connect(client, userdata, flags, reason_code, properties):
        report("connected" if reason_code == 0 else "connection rejected")
        if reason_code == 0:
            connected_event.set()

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        report("disconnected")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_connect_fail = lambda client, userdata: report("unavailable; reconnecting")
    return client

# ============================= #
#         SERVICE LIFECYCLE     #
# ============================= #

def run():
    stopping = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda signum, frame: stopping.set())

    # Validate overrides before starting the local observer.
    node_id = NODE_ID.lower()
    if node_id and not re.fullmatch(r"[0-9a-f]{12}", node_id):
        raise ValueError("NODE_ID must contain exactly 12 hexadecimal digits")
    if ANNOUNCE_INTERVAL_S <= 0 or LOCAL_RETRY_S <= 0:
        raise ValueError("Announcement and retry intervals must be positive")
    if not 1 <= LOCAL_PORT <= 65535 or not 1 <= REMOTE_PORT <= 65535:
        raise ValueError("MQTT ports must be between 1 and 65535")

    config = system_config()
    host = broker_host(config)
    node_name = NODE_NAME or config.get("short_name", "")
    if not isinstance(node_name, str):
        raise ValueError("short_name must be a string")

    # Start local observation independently of the external connection.
    snapshot = StatusSnapshot()
    observer = threading.Thread(
        target=observe_local,
        args=(snapshot, stopping),
        name="gateway-local-status",
        daemon=True,
    )
    observer.start()
    remote = None
    pending = None
    next_announcement = 0
    invalid_spectrum_reported = False
    try:
        if not node_id:
            logger.info("Waiting for node identity from HostManager status")
            while not stopping.is_set():
                node_id = snapshot.node_id()
                if node_id:
                    break
                stopping.wait(1)
            if stopping.is_set():
                return

        # Connect directly to the selected broker; never create tunnels or bridges.
        logger.info("Announcing node %s to %s:%s", node_id, host, REMOTE_PORT)
        connected_event = threading.Event()
        remote = remote_client(node_id, connected_event)
        topic = f"radiohound/clients/announce/{node_id}"
        # connect_async keeps DNS/connect/retry work off the local observer thread.
        remote.connect_async(host, REMOTE_PORT, keepalive=30)
        remote.loop_start()

        # Announcements take priority. Spectrum uses the gaps between them;
        # both share a single outstanding publication and a latest-only cache.
        while not stopping.is_set():
            if connected_event.is_set():
                connected_event.clear()
                pending = None
                next_announcement = 0
                snapshot.take_spectrum()
            if not remote.is_connected():
                pending = None
                snapshot.take_spectrum()
            elif pending is None or pending.rc != mqtt.MQTT_ERR_SUCCESS or pending.is_published():
                if time.monotonic() >= next_announcement:
                    payload = snapshot.announcement(node_id, node_name, host)
                    pending = remote.publish(topic, json.dumps(payload, allow_nan=False), qos=0, retain=False)
                    next_announcement = time.monotonic() + ANNOUNCE_INTERVAL_S
                else:
                    message = snapshot.take_spectrum()
                    if message is not None:
                        source_topic, payload = message
                        try:
                            encoded = json.dumps(payload, allow_nan=False)
                        except (TypeError, ValueError):
                            if not invalid_spectrum_reported:
                                logger.warning("Dropping spectrum that cannot be serialized as JSON")
                                invalid_spectrum_reported = True
                        else:
                            invalid_spectrum_reported = False
                            pending = remote.publish(source_topic, encoded, qos=0, retain=False)

            # Short bounded waits let spectrum flow independently of heartbeat rate.
            stopping.wait(0.05)
    finally:
        stopping.set()
        if remote is not None:
            remote.disconnect()
            remote.loop_stop()
        observer.join(timeout=3)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    run()
