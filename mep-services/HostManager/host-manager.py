#!/usr/bin/env python3
"""HostManager service exposed through MQTT."""

import json
import logging
import os
import platform
import shutil
import socket
import time
import uuid
import re
import subprocess
import threading
from typing import Any, Optional

import paho.mqtt.client as mqtt

SERVICE_NAME = "hostmanager"
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
STATUS_TOPIC = f"{SERVICE_NAME}/status"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
EVENT_TOPIC = f"{SERVICE_NAME}/event"
STATUS_INTERVAL_S = 1.0
NVP_MODEL_CONFIG_PATH = "/etc/nvpmodel.conf"
DEFAULT_FILESYSTEM_PATHS = ("/",)

DEFAULT_PATH = "/"
COMMAND_DESCRIPTIONS = {
    "get_status": {
        "description": "Return the current host platform state.",
        "arguments": {"disk_path": {"type": "string", "default": "/"}},
    },
    "get_disk": {
        "description": "Return filesystem usage for a path.",
        "arguments": {"path": {"type": "string", "default": "/"}},
    },
    "get_thermal": {
        "description": "Return all operating-system thermal zones.",
        "arguments": {},
    },
    "get_power_mode": {
        "description": "Return the current and configured power modes when available.",
        "arguments": {},
    },
    "set_power_mode": {
        "description": "Apply a configured Jetson power mode; the platform may reboot.",
        "arguments": {"mode_id": {"type": "string", "required": True}},
    },
    "set_status_interval": {
        "description": "Set the periodic status publication interval.",
        "arguments": {
            "interval_s": {"type": "float", "minimum": 0.1, "maximum": 60.0},
        },
    },
}


class HostManager:
    """Read host metrics without MQTT or application-specific behavior."""

    def __init__(self):
        self._cpu_previous = None

    def get_hostname(self) -> str:
        try:
            return socket.gethostname().split(".", 1)[0]
        except Exception:
            return "unknown-host"

    def get_cpu(self) -> dict:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as file_handle:
                values = [int(value) for value in file_handle.readline().split()[1:]]
            if len(values) < 4:
                return {"used_percent": None}
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            previous = self._cpu_previous
            self._cpu_previous = (total, idle)
            if previous is None or total <= previous[0]:
                return {"used_percent": None}
            total_delta = total - previous[0]
            idle_delta = idle - previous[1]
            return {"used_percent": round((total_delta - idle_delta) * 100.0 / total_delta, 2)}
        except (OSError, ValueError, IndexError):
            return {"used_percent": None}

    def get_cpu_cores(self) -> list[dict]:
        cores = []
        try:
            with open("/proc/stat", "r", encoding="utf-8") as file_handle:
                lines = file_handle.readlines()
        except OSError:
            return cores
        for line in lines:
            match = re.match(r"^cpu(\d+)\s+(.*)$", line)
            if not match:
                continue
            try:
                values = [int(value) for value in match.group(2).split()]
                total = sum(values)
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                core_id = int(match.group(1))
                freq_path = f"/sys/devices/system/cpu/cpu{core_id}/cpufreq/scaling_cur_freq"
                with open(freq_path, "r", encoding="utf-8") as freq_file:
                    frequency_mhz = int(freq_file.read().strip()) / 1000.0
                cores.append({"id": core_id, "frequency_mhz": round(frequency_mhz, 1), "total": total, "idle": idle})
            except (OSError, ValueError, IndexError):
                continue
        previous = getattr(self, "_core_cpu_previous", {})
        self._core_cpu_previous = {item["id"]: (item.pop("total"), item.pop("idle")) for item in cores}
        for item in cores:
            old = previous.get(item["id"])
            if old is None:
                item["used_percent"] = None
                continue
            total_delta = self._core_cpu_previous[item["id"]][0] - old[0]
            idle_delta = self._core_cpu_previous[item["id"]][1] - old[1]
            item["used_percent"] = round((total_delta - idle_delta) * 100.0 / total_delta, 2) if total_delta > 0 else None
        return cores

    def get_memory(self) -> dict:
        values = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as file_handle:
                for line in file_handle:
                    key, _, value = line.partition(":")
                    if key in ("MemTotal", "MemAvailable"):
                        values[key] = int(value.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            pass

        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        used = total - available if total is not None and available is not None else None
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "used_percent": round(used * 100.0 / total, 2) if total and used is not None else None,
        }

    def get_swap(self) -> dict:
        values = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as file_handle:
                for line in file_handle:
                    key, _, value = line.partition(":")
                    if key in ("SwapTotal", "SwapFree"):
                        values[key] = int(value.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        total = values.get("SwapTotal")
        free = values.get("SwapFree")
        used = total - free if total is not None and free is not None else None
        return {"total_bytes": total, "free_bytes": free, "used_bytes": used}

    def get_load(self) -> dict:
        try:
            with open("/proc/loadavg", "r", encoding="utf-8") as file_handle:
                values = file_handle.read().split()
            return {"1m": float(values[0]), "5m": float(values[1]), "15m": float(values[2])}
        except (OSError, ValueError, IndexError):
            return {"1m": None, "5m": None, "15m": None}

    def get_uptime(self) -> Optional[float]:
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as file_handle:
                return float(file_handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def get_disk(self, path: str = "/") -> dict:
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            return {"path": path, "error": str(exc)}
        used = usage.total - usage.free
        return {
            "path": path,
            "total_bytes": usage.total,
            "used_bytes": used,
            "free_bytes": usage.free,
            "used_percent": round(used * 100.0 / usage.total, 2) if usage.total else None,
        }

    def get_filesystems(self, paths=DEFAULT_FILESYSTEM_PATHS) -> list[dict]:
        filesystems = []
        seen = set()
        for path in paths:
            try:
                real_path = os.path.realpath(path)
                if real_path in seen:
                    continue
                seen.add(real_path)
                filesystems.append(self.get_disk(path))
            except OSError:
                continue
        return filesystems

    def get_network(self) -> dict:
        interfaces = []
        try:
            names = sorted(os.listdir("/sys/class/net"))
        except OSError:
            names = []
        for name in names:
            base = os.path.join("/sys/class/net", name)
            item = {"name": name}
            for key, filename in (("mac", "address"), ("state", "operstate"), ("mtu", "mtu"), ("speed_mbps", "speed")):
                try:
                    value = open(os.path.join(base, filename), "r", encoding="utf-8").read().strip()
                    item[key] = int(value) if key in ("mtu", "speed_mbps") else value
                except (OSError, ValueError):
                    item[key] = None
            stats = {}
            for direction in ("rx", "tx"):
                for counter in ("bytes", "packets", "errors", "dropped"):
                    try:
                        stats[f"{direction}_{counter}"] = int(open(
                            os.path.join(base, "statistics", f"{direction}_{counter}"),
                            "r", encoding="utf-8").read().strip())
                    except (OSError, ValueError):
                        stats[f"{direction}_{counter}"] = None
            item["statistics"] = stats
            interfaces.append(item)
        default_interface = None
        try:
            with open("/proc/net/route", "r", encoding="utf-8") as file_handle:
                next(file_handle, None)
                for line in file_handle:
                    fields = line.split()
                    if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                        default_interface = fields[0]
                        break
        except (OSError, ValueError):
            pass
        return {"default_interface": default_interface, "interfaces": interfaces}

    def get_gpu(self) -> dict:
        path = "/sys/devices/platform/gpu.0/load"
        try:
            with open(path, "r", encoding="utf-8") as file_handle:
                load = int(file_handle.read().strip())
            return {"utilization_percent": round(load / 10.0, 1), "source": path}
        except (OSError, ValueError):
            return {"utilization_percent": None, "source": path}

    def get_power_rails(self) -> dict:
        base = "/sys/class/hwmon"
        rails = []
        try:
            hwmon_names = os.listdir(base)
        except OSError:
            return {"rails": rails}
        for hwmon_name in hwmon_names:
            hwmon = os.path.join(base, hwmon_name)
            try:
                with open(os.path.join(hwmon, "name"), "r", encoding="utf-8") as file_handle:
                    if file_handle.read().strip() != "ina3221":
                        continue
            except OSError:
                continue
            for index in (1, 2, 3):
                try:
                    with open(os.path.join(hwmon, f"in{index}_label"), "r", encoding="utf-8") as file_handle:
                        name = file_handle.read().strip()
                    with open(os.path.join(hwmon, f"in{index}_input"), "r", encoding="utf-8") as file_handle:
                        voltage_mv = int(file_handle.read().strip())
                    with open(os.path.join(hwmon, f"curr{index}_input"), "r", encoding="utf-8") as file_handle:
                        current_ma = int(file_handle.read().strip())
                    rails.append({
                        "name": name,
                        "voltage_mv": voltage_mv,
                        "current_ma": current_ma,
                        "power_mw": round(voltage_mv * current_ma / 1000.0),
                    })
                except (OSError, ValueError):
                    continue
        return {"rails": rails}

    def get_power_mode(self) -> dict:
        result = {"current": None, "current_id": None, "available": [], "default_id": None, "error": None}
        try:
            with open(NVP_MODEL_CONFIG_PATH, "r", encoding="utf-8", errors="ignore") as file_handle:
                for line in file_handle:
                    mode_match = re.search(
                        r"<\s*POWER_MODEL\s+ID\s*=\s*(\d+)\s+NAME\s*=\s*([^>]+?)\s*>",
                        line,
                        re.IGNORECASE,
                    )
                    if mode_match:
                        result["available"].append({"id": mode_match.group(1), "name": mode_match.group(2).strip()})
                    default_match = re.search(
                        r"<\s*PM_CONFIG\s+DEFAULT\s*=\s*(\d+)\s*>",
                        line,
                        re.IGNORECASE,
                    )
                    if default_match:
                        result["default_id"] = default_match.group(1)
        except OSError:
            pass
        try:
            output = subprocess.check_output(
                ["nvpmodel", "-q"], stderr=subprocess.STDOUT, text=True, timeout=2
            )
            for line in output.splitlines():
                text = line.strip()
                match = re.search(r"(?:NV\s*)?Power\s*Mode\s*:\s*(.+)$", text, re.IGNORECASE)
                if match:
                    result["current"] = match.group(1).strip()
                elif text.isdigit():
                    result["current_id"] = text
        except (OSError, subprocess.SubprocessError) as exc:
            result["error"] = str(exc)
        return result

    def set_power_mode(self, mode_id) -> dict:
        mode_id = str(mode_id).strip()
        power_mode = self.get_power_mode()
        available_ids = {
            str(mode.get("id"))
            for mode in power_mode.get("available", [])
            if isinstance(mode, dict) and mode.get("id") is not None
        }
        if mode_id not in available_ids:
            raise ValueError(f"power mode {mode_id!r} is not present in {NVP_MODEL_CONFIG_PATH}")

        result = {"ok": False, "mode_id": mode_id, "error_code": None, "detail": None}
        try:
            command_prefix = []
            if os.geteuid() != 0:
                sudo_check = subprocess.run(
                    ["sudo", "-n", "true"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=2.0,
                )
                if sudo_check.returncode != 0:
                    result["error_code"] = "sudo_not_available"
                    result["detail"] = (sudo_check.stderr or sudo_check.stdout or "passwordless sudo unavailable").strip()
                    return result
                command_prefix = ["sudo", "-n"]
            applied = subprocess.run(
                [*command_prefix, "nvpmodel", "-m", mode_id],
                input="YES\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15.0,
            )
            if applied.returncode != 0:
                result["error_code"] = "apply_failed"
                result["detail"] = (applied.stderr or applied.stdout or "nvpmodel -m failed").strip()
                return result
            result["ok"] = True
            result["detail"] = ((applied.stdout or "") + " " + (applied.stderr or "")).strip() or "nvpmodel accepted mode"
        except subprocess.TimeoutExpired as exc:
            result["error_code"] = "timeout"
            result["detail"] = str(exc)
        except FileNotFoundError:
            result["error_code"] = "nvpmodel_not_found"
            result["detail"] = "nvpmodel command not found"
        except Exception as exc:
            result["error_code"] = "exception"
            result["detail"] = str(exc)
        return result

    def get_thermal(self) -> dict:
        zones = []
        thermal_root = "/sys/class/thermal"
        try:
            names = sorted(name for name in os.listdir(thermal_root) if name.startswith("thermal_zone"))
        except OSError:
            return {"zones": zones}
        for name in names:
            zone_path = os.path.join(thermal_root, name)
            zone = {"name": name, "temperature_c": None, "error": None}
            try:
                with open(os.path.join(zone_path, "type"), "r", encoding="utf-8") as file_handle:
                    zone["name"] = file_handle.read().strip() or name
                with open(os.path.join(zone_path, "temp"), "r", encoding="utf-8") as file_handle:
                    zone["temperature_c"] = int(file_handle.read().strip()) / 1000.0
                for trip_name in sorted(os.listdir(zone_path)):
                    if not trip_name.startswith("trip_point_") or not trip_name.endswith("_temp"):
                        continue
                    try:
                        with open(os.path.join(zone_path, trip_name), "r", encoding="utf-8") as file_handle:
                            zone[trip_name] = int(file_handle.read().strip()) / 1000.0
                    except (OSError, ValueError):
                        continue
            except (OSError, ValueError):
                zone["error"] = "thermal zone could not be read"
            zones.append(zone)
        return {"zones": zones}

    def get_identity(self) -> dict:
        return {
            "hostname": self.get_hostname(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        }

    def get_status(
        self,
        disk_path: str = DEFAULT_PATH,
        uptime_s: Optional[float] = None,
        seq: Optional[int] = None,
    ) -> dict:
        return {
            "service": SERVICE_NAME,
            "state": "online",
            "timestamp": time.time(),
            "seq": seq,
            "uptime_seconds": uptime_s,
            "identity": self.get_identity(),
            "cpu": self.get_cpu(),
            "cpu_cores": self.get_cpu_cores(),
            "memory": self.get_memory(),
            "swap": self.get_swap(),
            "load": self.get_load(),
            "host_uptime_seconds": self.get_uptime(),
                "disk": self.get_disk(disk_path),
            "filesystems": self.get_filesystems((disk_path,)),
            "network": self.get_network(),
            "thermal": self.get_thermal(),
            "power_mode": self.get_power_mode(),
            "platform": {
                "name": "nvidia_jetson_orin_nx",
                "gpu": self.get_gpu(),
                "power": self.get_power_rails(),
            },
        }


class HostManagerService:
    """MQTT adapter and polling loop for HostManager."""

    def __init__(self, broker: str = MQTT_BROKER, port: int = MQTT_PORT):
        self.platform = HostManager()
        self.broker = broker
        self.port = port
        self.started_at = time.time()
        self.status_interval_s = STATUS_INTERVAL_S
        self._status_seq = 0
        self._interval_changed = threading.Event()
        self._last_observed = {}
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{SERVICE_NAME}_{uuid.uuid4().hex[:8]}",
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logging.error("MQTT connection failed: rc=%s", reason_code)
            return
        client.subscribe(COMMAND_TOPIC)
        self._publish(ANNOUNCE_TOPIC, self.build_announce())
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
        task = request.get("task_name")
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("command arguments must be an object")

        if task == "get_status":
            result = self.build_status(arguments.get("disk_path", "/"))
        elif task == "get_disk":
            result = self.platform.get_disk(arguments.get("path", "/"))
        elif task == "get_thermal":
            result = self.platform.get_thermal()
        elif task == "get_power_mode":
            result = self.platform.get_power_mode()
        elif task == "set_power_mode":
            result = self.platform.set_power_mode(arguments.get("mode_id"))
            if result.get("ok"):
                logging.warning("Applied Jetson power mode %s; reboot may follow", result.get("mode_id"))
            else:
                logging.error("Could not apply Jetson power mode %s: %s", result.get("mode_id"), result.get("detail"))
        elif task == "set_status_interval":
            interval_s = float(arguments.get("interval_s"))
            if not 0.1 <= interval_s <= 60.0:
                raise ValueError("interval_s must be between 0.1 and 60 seconds")
            self.status_interval_s = interval_s
            self._interval_changed.set()
            result = {"status_interval_s": self.status_interval_s}
        else:
            raise ValueError(f"unsupported task: {task!r}")

        return {
            "success": True,
            "session_id": request.get("session_id"),
            "task_name": task,
            "status_data": result,
            "error": None,
        }

    def build_announce(self) -> dict:
        return {
            "service": SERVICE_NAME,
            "type": "service",
            "version": "0.1",
            "timestamp": time.time(),
            "identity": self.platform.get_identity(),
            "topics": {
                "announce": ANNOUNCE_TOPIC,
                "command": COMMAND_TOPIC,
                "status": STATUS_TOPIC,
                "response": RESPONSE_TOPIC,
            },
            "commands": COMMAND_DESCRIPTIONS,
            "schemas": {
                "status": {
                    "fields": [
                        "identity", "host_uptime_seconds", "uptime_seconds", "seq",
                        "cpu", "cpu_cores", "load", "memory", "swap", "disk",
                        "filesystems", "network", "thermal", "power_mode", "platform",
                    ],
                },
                "event": {
                    "fields": ["event_type", "timestamp", "seq", "source", "previous", "current"],
                },
            },
            "event_topic": EVENT_TOPIC,
        }

    def build_status(self, disk_path: str = DEFAULT_PATH) -> dict:
        self._status_seq += 1
        return self.platform.get_status(
            disk_path=disk_path,
            uptime_s=round(time.time() - self.started_at, 3),
            seq=self._status_seq,
        )

    def publish_status(self):
        status = self.build_status()
        self._publish(STATUS_TOPIC, status)
        self._publish_events(status)

    def _publish_events(self, status: dict):
        observations = {
            "disk": self._availability(status.get("disk")),
            "thermal": self._thermal_availability(status.get("thermal")),
            "network": self._network_state(status.get("network")),
            "gpu": self._availability(status.get("platform", {}).get("gpu")),
            "power": bool(status.get("platform", {}).get("power", {}).get("rails")),
        }
        for source, value in observations.items():
            previous = self._last_observed.get(source)
            if previous is not None and previous != value:
                self._publish(EVENT_TOPIC, {
                    "event_type": "state_changed",
                    "timestamp": time.time(),
                    "seq": status.get("seq"),
                    "source": source,
                    "previous": previous,
                    "current": value,
                }, retain=False)
            self._last_observed[source] = value

    @staticmethod
    def _availability(value):
        return isinstance(value, dict) and "error" not in value

    @staticmethod
    def _thermal_availability(value):
        return tuple(zone.get("name") for zone in value.get("zones", [])) if isinstance(value, dict) else None

    @staticmethod
    def _network_state(value):
        if not isinstance(value, dict):
            return None
        return tuple((item.get("name"), item.get("state")) for item in value.get("interfaces", []))

    def publish_error_event(self, source: str, error: str):
        self._publish(EVENT_TOPIC, {
            "event_type": "collection_error",
            "timestamp": time.time(),
            "source": source,
            "error": error,
        }, retain=False)

    def _publish(self, topic: str, payload: dict, retain: bool = True):
        self.client.publish(
            topic,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=retain,
        )

    def run(self):
        self.client.connect(self.broker, self.port, keepalive=60)
        self.client.loop_start()
        try:
            while True:
                self.publish_status()
                self._interval_changed.wait(self.status_interval_s)
                self._interval_changed.clear()
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    HostManagerService().run()
