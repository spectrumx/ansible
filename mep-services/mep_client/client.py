"""Request/response clients for MEP-managed services."""

import base64
import json
import logging
import math
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Optional

from . import bus
import numpy as np


class ComponentClient:
    """Correlate requests and expose status for one MEP component."""

    def __init__(self, message_bus, name: str, status_topic: str, command_topic: str, response_topic: str):
        self.bus = message_bus
        self.name = name
        self.status_topic = status_topic
        self.command_topic = command_topic
        self.response_topic = response_topic
        self._pending = {}
        message_bus.on_status(status_topic, self._on_status)
        message_bus.on_status(response_topic, self._on_response)

    def _on_status(self, payload: dict):
        pass

    def _on_response(self, payload: dict):
        if not isinstance(payload, dict):
            return
        callback = self._pending.pop(payload.get("session_id"), None)
        if callback is not None:
            try:
                callback(payload)
            except Exception:
                logging.exception("Response callback failed for %s", self.name)

    def get_status(self) -> dict:
        status = self.bus.get_cached_status(self.status_topic)
        return status if isinstance(status, dict) else {}

    def on_status(self, callback):
        self.bus.on_status(self.status_topic, callback)

    def is_available(self) -> bool:
        status = self.get_status()
        return bool(status) and status.get("state") not in {"offline", "error", "failed"}

    def request(self, task_name: str, arguments: Optional[dict] = None, *, callback=None):
        session_id = f"{self.name}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        payload = {"task_name": task_name, "arguments": arguments or {}, "session_id": session_id}
        if callback is not None:
            self._pending[session_id] = callback
        sent = self.bus.publish_command(self.command_topic, payload, sleep_s=0.05)
        if not sent:
            self._pending.pop(session_id, None)
        return {"task_name": task_name, "session_id": session_id, "status_topic": self.status_topic, "sent": sent}


class CaptureClient(ComponentClient):
    def __init__(self, message_bus):
        super().__init__(message_bus, "captureorchestrator", bus.CAPTURE_ORCHESTRATOR_STATUS_TOPIC, bus.CAPTURE_ORCHESTRATOR_COMMAND_TOPIC, bus.CAPTURE_ORCHESTRATOR_RESPONSE_TOPIC)

    def start_rx(self, callback=None, **arguments):
        return self.request("start_rx", arguments, callback=callback)

    def stop_rx(self, callback=None):
        return self.request("stop_rx", callback=callback)

    def start_tx(self, callback=None, **arguments):
        return self.request("start_tx", arguments, callback=callback)

    def stop_tx(self, callback=None):
        return self.request("stop_tx", callback=callback)

    def abort(self, callback=None):
        return self.request("abort", callback=callback)

    def list_recorder_presets(self, callback=None):
        return self.request("list_recorder_presets", callback=callback)

    def preview_recorder_settings(self, sample_rate_mhz: int, draft: Optional[dict] = None, callback=None):
        arguments = {"sample_rate_mhz": int(sample_rate_mhz)}
        if draft is not None:
            arguments["draft"] = dict(draft)
        return self.request("preview_recorder_settings", arguments, callback=callback)


class ArchiveClient(ComponentClient):
    def __init__(self, message_bus):
        self._captures = []
        super().__init__(message_bus, "archivemanager", bus.ARCHIVE_MANAGER_STATUS_TOPIC, bus.ARCHIVE_MANAGER_COMMAND_TOPIC, bus.ARCHIVE_MANAGER_RESPONSE_TOPIC)

    @staticmethod
    def extract_list(payload, key):
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get(key), list):
            return payload[key]
        status = payload.get("status_data")
        return status.get(key) if isinstance(status, dict) and isinstance(status.get(key), list) else None

    def _on_status(self, payload: dict):
        captures = self.extract_list(payload, "captures")
        if captures is not None:
            self._captures = captures

    def list_captures(self):
        return [dict(capture) for capture in self._captures if isinstance(capture, dict)]

    def refresh(self, callback=None):
        def complete(response):
            captures = self.extract_list(response, "captures")
            if captures is not None:
                self._captures = captures
            if callback is not None:
                callback(response)
        return self.request("get_captures", callback=complete)

    def get_capture(self, capture_id: str, callback=None):
        return self.request("get_capture", {"capture_id": capture_id}, callback=callback)

    def delete_capture(self, capture_id: str, callback=None):
        return self.request("delete_capture", {"capture_id": capture_id}, callback=callback)

    def delete_preview(self, callback=None):
        return self.request("delete_preview", callback=callback)

    def rename_capture(self, capture_id: str, new_name: str, callback=None):
        return self.request("rename_capture", {"capture_id": capture_id, "new_name": new_name}, callback=callback)


class UploadClient(ComponentClient):
    def __init__(self, message_bus):
        self._uploads = []
        self._sds_checks = []
        super().__init__(message_bus, "uploadmanager", bus.UPLOAD_MANAGER_STATUS_TOPIC, bus.UPLOAD_MANAGER_COMMAND_TOPIC, bus.UPLOAD_MANAGER_RESPONSE_TOPIC)
        message_bus.on_status(bus.UPLOAD_MANAGER_EVENT_TOPIC, self._on_event)

    def _on_status(self, payload: dict):
        uploads = ArchiveClient.extract_list(payload, "uploads")
        checks = ArchiveClient.extract_list(payload, "sds_checks")
        if uploads is not None:
            self._uploads = uploads
        if checks is not None:
            self._sds_checks = checks

    def _on_event(self, payload: dict):
        if not isinstance(payload, dict) or payload.get("event_type") != "upload_activity":
            return
        status = payload.get("status_data")
        activity = status.get("activity") if isinstance(status, dict) else None
        job_id = str(status.get("job_id") or "") if isinstance(status, dict) else ""
        revision = activity.get("activity_id") if isinstance(activity, dict) else None
        for upload in self._uploads:
            if isinstance(upload, dict) and str(upload.get("job_id") or "") == job_id:
                upload["activity_revision"] = revision
                break

    def get_uploads(self):
        return [dict(upload) for upload in self._uploads if isinstance(upload, dict)]

    def on_activity(self, callback):
        self.bus.on_status(bus.UPLOAD_MANAGER_EVENT_TOPIC, callback)

    def get_sds_check(self, capture_id: str):
        return next((dict(check) for check in self._sds_checks if isinstance(check, dict) and str(check.get("capture_id") or "") == str(capture_id)), {})

    def refresh(self, callback=None):
        def complete(response):
            uploads = ArchiveClient.extract_list(response, "uploads")
            if uploads is not None:
                self._uploads = uploads
            if callback is not None:
                callback(response)
        return self.request("get_uploads", callback=complete)

    def get_upload_activity(self, job_id: str, *, limit: int = 100, callback=None):
        return self.request("get_upload_activity", {"job_id": job_id, "limit": int(limit)}, callback=callback)

    def start_upload(self, capture_id: str, *, destination="sds", credentials=None, dry_run=False, callback=None):
        arguments = {"capture_id": capture_id, "destination": destination, "dry_run": bool(dry_run)}
        if isinstance(credentials, dict):
            arguments["credentials"] = credentials
        return self.request("start_upload", arguments, callback=callback)

    def check_sds(self, capture_id: str, *, credentials=None, callback=None):
        arguments = {"capture_id": capture_id}
        if isinstance(credentials, dict):
            arguments["credentials"] = credentials
        return self.request("check_sds", arguments, callback=callback)

    def pause_upload(self, job_id: str, callback=None):
        return self.request("pause_upload", {"job_id": job_id}, callback=callback)

    def resume_upload(self, job_id: str, *, credentials=None, callback=None):
        arguments = {"job_id": job_id}
        if isinstance(credentials, dict):
            arguments["credentials"] = credentials
        return self.request("resume_upload", arguments, callback=callback)

    def retry_upload(self, job_id: str, *, credentials=None, dry_run=None, callback=None):
        arguments = {"job_id": job_id}
        if isinstance(credentials, dict):
            arguments["credentials"] = credentials
        if dry_run is not None:
            arguments["dry_run"] = bool(dry_run)
        return self.request("retry_upload", arguments, callback=callback)

    def cancel_upload(self, job_id: str, callback=None):
        return self.request("stop_upload", {"job_id": job_id}, callback=callback)

    def delete_upload(self, job_id: str, callback=None):
        return self.request("delete_upload", {"job_id": job_id}, callback=callback)


class HostClient(ComponentClient):
    def __init__(self, message_bus):
        self._announce = {}
        super().__init__(message_bus, "hostmanager", bus.HOST_MANAGER_STATUS_TOPIC, bus.HOST_MANAGER_COMMAND_TOPIC, bus.HOST_MANAGER_RESPONSE_TOPIC)
        message_bus.on_status(bus.HOST_MANAGER_ANNOUNCE_TOPIC, self._on_announce)

    def _on_announce(self, payload):
        if isinstance(payload, dict):
            self._announce = payload

    def get_announce(self):
        return dict(self._announce)

    def on_announce(self, callback):
        self.bus.on_status(bus.HOST_MANAGER_ANNOUNCE_TOPIC, callback)

    def refresh(self, callback=None):
        return self.request("get_status", callback=callback)

    def set_status_interval(self, interval_s: float, callback=None):
        return self.request("set_status_interval", {"interval_s": float(interval_s)}, callback=callback)

    def set_power_mode(self, mode_id: str, callback=None):
        return self.request("set_power_mode", {"mode_id": str(mode_id)}, callback=callback)


class LogBuffer:
    def __init__(self):
        self.log_busy = False
        self.log_paused = False
        self.log_scope = None
        self._stream_id = f"client_{uuid.uuid4().hex}"
        self._messages = deque(maxlen=2000)
        self._lock = threading.Lock()
        self._rendered_count = 0
        self._line_callback = None
        self._exit_callback = None

    def receive(self, payload):
        if not isinstance(payload, dict) or payload.get("stream_id") != self._stream_id:
            return
        line = payload.get("line")
        if isinstance(line, str):
            with self._lock:
                self._messages.append((datetime.now().strftime("%H:%M:%S"), line))
            if self._line_callback:
                self._line_callback(line)
        if payload.get("state") == "ended":
            self.log_busy = False
            callback = self._exit_callback
            self._line_callback = None
            self._exit_callback = None
            if callback:
                callback(payload.get("return_code"))

    def get_new_entries(self):
        with self._lock:
            entries = list(self._messages)
            start = min(self._rendered_count, len(entries))
            result = entries[start:]
            self._rendered_count = len(entries)
        return result

    def clear(self):
        with self._lock:
            self._messages.clear()
            self._rendered_count = 0


class DockerClient(ComponentClient):
    def __init__(self, message_bus):
        self.services = {}
        self.service_names = []
        self.compose_dir = ""
        self.engine_status = "Unavailable"
        self.status_error = None
        self.action_busy = False
        self.refresh_busy = False
        self.logs = LogBuffer()
        super().__init__(message_bus, "dockermanager", bus.DOCKER_MANAGER_STATUS_TOPIC, bus.DOCKER_MANAGER_COMMAND_TOPIC, bus.DOCKER_MANAGER_RESPONSE_TOPIC)
        message_bus.on_status(bus.DOCKER_MANAGER_LOG_TOPIC, self.logs.receive)

    def _on_status(self, payload):
        if not isinstance(payload, dict):
            return
        engine = payload.get("engine") if isinstance(payload.get("engine"), dict) else {}
        compose = payload.get("compose") if isinstance(payload.get("compose"), dict) else {}
        self.engine_status = str(engine.get("state") or "Unavailable").capitalize()
        self.compose_dir = str(compose.get("directory") or "")
        self.status_error = compose.get("error") or engine.get("error")
        rows = compose.get("services") if isinstance(compose.get("services"), list) else []
        self.services = {str(row["service"]): row for row in rows if isinstance(row, dict) and row.get("service")}
        self.service_names = sorted(self.services)

    @property
    def log_busy(self): return self.logs.log_busy
    @property
    def log_paused(self): return self.logs.log_paused
    @log_paused.setter
    def log_paused(self, value): self.logs.log_paused = value
    @property
    def log_scope(self): return self.logs.log_scope

    def refresh(self, callback=None):
        self.refresh_busy = True
        def complete(response):
            self.refresh_busy = False
            if callback: callback(response)
        return self.request("get_status", callback=complete)

    def run_action(self, task_name, *, services=None, force_recreate=False, callback=None):
        arguments = {}
        if services: arguments["services"] = list(services)
        if task_name in {"up_services", "up_project"}: arguments["force_recreate"] = bool(force_recreate)
        self.action_busy = True
        def complete(response):
            self.action_busy = False
            if callback: callback(response)
        return self.request(task_name, arguments, callback=complete)

    @staticmethod
    def preview_action(task_name, *, services=None, force_recreate=False):
        arguments = {}
        if services: arguments["services"] = list(services)
        if task_name in {"up_services", "up_project"} and force_recreate: arguments["force_recreate"] = True
        return f"{task_name} {json.dumps(arguments, separators=(',', ':'))}"

    def stream_start(self, *, services=None, tail=30, on_line=None, on_exit=None):
        self.logs._line_callback, self.logs._exit_callback = on_line, on_exit
        self.logs.log_busy, self.logs.log_paused = True, False
        self.logs.log_scope = ",".join(services) if services else "all"
        return self.request("start_log_stream", {"stream_id": self.logs._stream_id, "services": list(services or []), "tail": int(tail)})

    def stream_stop(self, callback=None):
        self.logs.log_busy, self.logs.log_paused, self.logs.log_scope = False, True, None
        return self.request("stop_log_stream", {"stream_id": self.logs._stream_id}, callback=callback)

    def get_new_log_entries(self): return self.logs.get_new_entries()
    def clear_log(self): self.logs.clear()


class SystemdClient(ComponentClient):
    def __init__(self, message_bus):
        self.services = {}
        self.service_names = []
        self.action_busy = False
        self.refresh_busy = False
        self._announced_services = []
        self.logs = LogBuffer()
        super().__init__(message_bus, "servicemanager", bus.SERVICE_MANAGER_STATUS_TOPIC, bus.SERVICE_MANAGER_COMMAND_TOPIC, bus.SERVICE_MANAGER_RESPONSE_TOPIC)
        message_bus.on_status(bus.SERVICE_MANAGER_ANNOUNCE_TOPIC, self._on_announce)
        message_bus.on_status(bus.SERVICE_MANAGER_LOG_TOPIC, self.logs.receive)

    def _on_announce(self, payload):
        services = payload.get("managed_services") if isinstance(payload, dict) else None
        if isinstance(services, list):
            self._announced_services = [str(service) for service in services]
            self._refresh_names()

    def _on_status(self, payload):
        rows = payload.get("services") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            self.services = {str(row["service"]): row for row in rows if isinstance(row, dict) and row.get("service")}
            self._refresh_names()

    def _refresh_names(self):
        self.service_names = sorted(set(self._announced_services) | set(self.services))

    @property
    def log_busy(self): return self.logs.log_busy
    @property
    def log_paused(self): return self.logs.log_paused
    @log_paused.setter
    def log_paused(self, value): self.logs.log_paused = value
    @property
    def log_scope(self): return self.logs.log_scope

    def refresh(self, callback=None):
        self.refresh_busy = True
        def complete(response):
            self.refresh_busy = False
            status = response.get("status_data") if isinstance(response, dict) else None
            if isinstance(status, dict): self._on_status(status)
            if callback: callback(response)
        return self.request("get_status", callback=complete)

    def run_action(self, action: str, services: list[str], callback=None):
        self.action_busy = True
        def complete(response):
            self.action_busy = False
            if callback: callback(response)
        return self.request(f"{action}_services", {"services": list(services)}, callback=complete)

    def stream_start(self, services: list[str], *, tail=30, on_line=None, on_exit=None):
        self.logs._line_callback, self.logs._exit_callback = on_line, on_exit
        self.logs.log_busy, self.logs.log_paused = True, False
        self.logs.log_scope = tuple(services)
        return self.request("start_log_stream", {"stream_id": self.logs._stream_id, "services": list(services), "tail": int(tail)})

    def stream_stop(self, callback=None):
        self.logs.log_busy, self.logs.log_paused, self.logs.log_scope = False, True, None
        return self.request("stop_log_stream", {"stream_id": self.logs._stream_id}, callback=callback)

    def get_new_log_entries(self): return self.logs.get_new_entries()
    def clear_log(self): self.logs.clear()


class RFSoCClient:
    def __init__(self, message_bus):
        self.bus = message_bus

    def _command(self, task_name: str, arguments=None):
        payload = {"task_name": task_name}
        if arguments is not None:
            payload["arguments"] = arguments
        return self.bus.publish_command(bus.RFSOC_COMMAND_TOPIC, payload)

    def get_status(self) -> dict:
        status = self.bus.get_cached_status(bus.RFSOC_STATUS_TOPIC)
        return status if isinstance(status, dict) else {}

    def on_status(self, callback):
        self.bus.on_status(bus.RFSOC_STATUS_TOPIC, callback)

    def on_pll_config(self, callback):
        self.bus.on_status(bus.RFSOC_PLL_CONFIG_TOPIC, callback)

    def reset(self):
        return self._command("reset")

    def status(self):
        return self._command("status", {})

    def capture_next_pps(self):
        return self._command("capture_next_pps")

    def capture_now(self):
        return self._command("capture")

    def set_channel(self, channels: str):
        return self._command("set", f"channel {channels}")

    def set_frequency_metadata(self, frequency_hz: float):
        return self._command("set", f"freq_metadata {frequency_hz}")

    def set_if(self, if_mhz: float):
        return self._command("set", f"freq_IF {if_mhz}")

    def set_pps_publish_interval(self, interval_s: int):
        return self._command("set_pps_publish_interval", int(interval_s))

    def get_pll_config(self, converter: str, tile: int):
        converter = str(converter).strip().lower()
        if converter not in {"adc", "dac"}:
            raise ValueError("converter must be 'adc' or 'dac'")
        return self.bus.publish_command(
            bus.RFSOC_COMMAND_TOPIC,
            {"task_name": "get", "arguments": f"pll_config {converter} {int(tile)}"},
            sleep_s=0,
        )

    def set_tx_center_frequency(self, frequency_mhz: float):
        return self._command("set", f"tx_center_freq {frequency_mhz}")

    def set_tx_offset_frequency(self, frequency_mhz: float):
        if abs(frequency_mhz) >= bus.TX_OFFSET_FREQ_MAX_MHZ:
            raise ValueError(
                f"TX offset frequency magnitude must be < {bus.TX_OFFSET_FREQ_MAX_MHZ} MHz"
            )
        return self._command("set", f"tx_offset_freq {frequency_mhz}")

    def set_tx_amplitude(self, amplitude_bins: int):
        if not 0 <= amplitude_bins <= bus.TX_AMPLITUDE_BINS_MAX:
            raise ValueError(f"TX amplitude must be 0..{bus.TX_AMPLITUDE_BINS_MAX} bins")
        return self._command("set", f"tx_amplitude {amplitude_bins}")

    def set_tx_channel(self, channels: str):
        if channels not in bus.TX_CHANNEL_OPTIONS:
            raise ValueError(f"TX channel must be one of {list(bus.TX_CHANNEL_OPTIONS)}")
        return self._command("set", f"tx_channel {channels}")

    def tx_start(self):
        return self._command("tx_start")

    def tx_stop(self):
        return self._command("tx_stop")


class TunerClient:
    def __init__(self, message_bus):
        self.bus = message_bus

    def _command(self, task_name: str, arguments=None):
        return self.bus.publish_command(
            bus.TUNER_COMMAND_TOPIC,
            {"task_name": task_name, "arguments": arguments or {}},
        )

    def initialize(self, force_tuner: Optional[str] = None):
        arguments = {"force_tuner": force_tuner} if force_tuner else {}
        return self._command("init_tuner", arguments)

    def set_frequency(self, frequency_mhz: float):
        return self._command("set_freq", {"freq_mhz": frequency_mhz})

    def get_frequency(self):
        return self._command("get_freq")

    def set_power(self, power_dbm: float):
        return self._command("set_power", {"pwr_dbm": power_dbm})

    def get_power(self):
        return self._command("get_power")

    def check_lock(self):
        return self._command("get_lock_status")

    def restart(self):
        return self._command("restart_tuner")

    def status(self):
        return self._command("status")

    def get_status(self) -> dict:
        status = self.bus.get_cached_status(bus.TUNER_STATUS_TOPIC)
        return status if isinstance(status, dict) else {}

    def on_status(self, callback):
        self.bus.on_status(bus.TUNER_STATUS_TOPIC, callback)

    def on_response(self, callback):
        self.bus.on_status(bus.TUNER_RESPONSE_TOPIC, callback)


class AFEClient:
    def __init__(self, message_bus):
        self.bus = message_bus

    def _command(self, suffix: str, task_name: str, arguments=None, session_id=None):
        payload = {"task_name": task_name, "arguments": arguments or {}}
        if session_id:
            payload["session_id"] = session_id
        topic = bus.AFE_COMMAND_TOPIC if not suffix else f"{bus.AFE_COMMAND_TOPIC}/{suffix}"
        return self.bus.publish_command(topic, payload)

    def set_register(self, device: str, register: str, value: int):
        return self._command("registers", "set_register", {
            "device": device, "register": register, "value": value,
        })

    def set_registers(self, device: str, registers: dict):
        return self._command("registers", "set_registers", {device: registers})

    def set_attenuation(self, device: str, db: int, session_id: Optional[str] = None):
        if not 0 <= db <= 31:
            raise ValueError("attenuation must be 0..31 dB")
        return self._command(
            "registers", "set_attenuation_db", {"device": device, "db": db}, session_id
        )

    def get_registers(self, device: str = "all"):
        return self._command("registers", "get_registers", {"device": device})

    def status(self):
        return self._command("", "status")

    def describe(self):
        return self._command("", "describe")

    def telemetry_dump(self):
        return self._command("", "telem_dump")

    def start_raw_stream(self, duration_s: float = 10.0, mode: str = "gnss"):
        return self._command("gps", "start_raw_stream", {
            "duration_s": float(duration_s), "mode": str(mode),
        })

    def stop_raw_stream(self):
        return self._command("gps", "stop_raw_stream")

    def set_imu_config(
        self,
        acc_odr: Optional[str] = None,
        gyr_odr: Optional[str] = None,
        ahiperf: Optional[int] = None,
        aulp: Optional[int] = None,
        glp: Optional[int] = None,
    ):
        arguments = {
            key: value
            for key, value in {
                "acc_odr": acc_odr,
                "gyr_odr": gyr_odr,
                "ahiperf": ahiperf,
                "aulp": aulp,
                "glp": glp,
            }.items()
            if value is not None
        }
        return self._command("imu", "set_imu", arguments)

    def get_imu_params(self):
        return self._command("imu", "get_imu_params")

    def set_mag_config(self, ccr: Optional[int] = None, updr: Optional[int] = None):
        arguments = {
            key: value
            for key, value in {"ccr": ccr, "updr": updr}.items()
            if value is not None
        }
        return self._command("mag", "set_mag", arguments)

    def get_mag_params(self):
        return self._command("mag", "get_mag_params")

    def get_hk_rate(self):
        return self._command("hk", "get_rate")

    def set_polling_interval(self, interval: int):
        return self._command("polling", "set_interval", {"n": interval})

    def get_polling_interval(self):
        return self._command("polling", "get_interval")

    def set_hk_rate(self, interval: int):
        return self._command("hk", "set_rate", {"n": interval})

    def set_mag_rate(self, interval: int):
        return self._command("mag", "set_rate", {"n": interval})

    def set_imu_rate(self, interval: int):
        return self._command("imu", "set_rate", {"n": interval})

    def configure_time(self, source: str, epoch: str, timestamp: Optional[int] = None):
        arguments = {"source": source, "epoch": epoch}
        if timestamp is not None:
            arguments["timestamp"] = int(timestamp)
        return self._command("time", "configure", arguments)

    def get_time_params(self):
        return self._command("time", "get_time_params")

    def configure_logging(self, enabled: bool, path: str, rate_s: float):
        return self._command("logging", "configure", {
            "enabled": bool(enabled), "path": str(path), "rate_s": float(rate_s),
        })

    def get_log_status(self):
        return self._command("logging", "get_log_status")

    def refresh(self):
        return self._command("", "refresh")

    def set_service_log_mode(self, mode: str):
        if mode not in {"normal", "debug"}:
            raise ValueError("mode must be 'normal' or 'debug'")
        return self._command("logging", "set_service_log_mode", {"mode": mode})

    def get_service_log_mode(self):
        return self._command("logging", "get_service_log_mode")

    def get_status(self) -> dict:
        return self._cached(bus.AFE_STATUS_TOPIC)

    def get_announce(self) -> dict:
        return self._cached(bus.AFE_ANNOUNCE_TOPIC)

    def get_register_status(self) -> dict:
        return self._cached(bus.AFE_REGISTERS_TOPIC)

    def _cached(self, topic) -> dict:
        status = self.bus.get_cached_status(topic)
        return status if isinstance(status, dict) else {}

    def on_status(self, callback):
        self.bus.on_status(bus.AFE_STATUS_TOPIC, callback)

    def on_announce(self, callback):
        self.bus.on_status(bus.AFE_ANNOUNCE_TOPIC, callback)

    def on_registers(self, callback):
        self.bus.on_status(bus.AFE_REGISTERS_TOPIC, callback)

    def on_gnss(self, callback):
        self.bus.on_status(bus.AFE_GNSS_TOPIC, callback)

    def on_imu(self, callback):
        self.bus.on_status(bus.AFE_IMU_TOPIC, callback)

    def on_magnetometer(self, callback):
        self.bus.on_status(bus.AFE_MAG_TOPIC, callback)

    def on_housekeeping(self, callback):
        self.bus.on_status(bus.AFE_HK_TOPIC, callback)

    def on_gpsd_status(self, callback):
        self.bus.on_status(bus.AFE_GPSD_STATUS_TOPIC, callback)

    def on_raw_data(self, callback):
        self.bus.on_status(bus.AFE_RAW_TOPIC, callback)

    def on_polling_response(self, callback):
        self.bus.on_status(f"{bus.AFE_RESPONSE_TOPIC}/polling", callback)

    def on_logging_response(self, callback):
        self.bus.on_status(f"{bus.AFE_RESPONSE_TOPIC}/logging", callback)


class RecorderClient:
    def __init__(self, message_bus):
        self.bus = message_bus

    def get_status(self) -> dict:
        status = self.bus.get_cached_status(bus.RECORDER_STATUS_TOPIC)
        return status if isinstance(status, dict) else {}

    def on_status(self, callback):
        self.bus.on_status(bus.RECORDER_STATUS_TOPIC, callback)


class SpectrumClient:
    def __init__(self, message_bus):
        self.bus = message_bus
        self._topic = bus.SPECTRUM_TOPIC_PATTERN
        self._callbacks = []
        self._active = False
        self.bus.on_status_pattern(self._topic, self._on_payload, subscribe=False)

    @property
    def topic(self) -> str:
        return self._topic

    def on_frame(self, callback):
        self._callbacks.append(callback)

    def set_topic(self, topic: str):
        topic = str(topic).strip()
        if not topic:
            raise ValueError("spectrum topic cannot be empty")
        if topic == self._topic:
            return
        was_active = self._active
        if was_active:
            self.bus.unsubscribe(self._topic)
        self.bus.remove_status_pattern(self._topic, self._on_payload)
        self._topic = topic
        self.bus.on_status_pattern(self._topic, self._on_payload, subscribe=False)
        if was_active:
            self.bus.subscribe(self._topic)

    def start(self):
        if not self._active:
            self._active = True
            self.bus.subscribe(self._topic)

    def stop(self):
        if self._active:
            self.bus.unsubscribe(self._topic)
            self._active = False

    def _on_payload(self, topic, payload):
        frame = self._decode(payload)
        if frame is None:
            return
        for callback in tuple(self._callbacks):
            callback(frame)

    @staticmethod
    def _decode(payload):
        encoded = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(encoded, str) or not encoded:
            return None
        try:
            raw = base64.b64decode(encoded)
        except (TypeError, ValueError):
            return None
        sample_count = len(raw) // 4
        if sample_count <= 0:
            return None
        row = np.frombuffer(raw, dtype="<f4", count=sample_count).copy()
        np.maximum(row, np.float32(1e-12), out=row)
        np.log10(row, out=row)
        row *= np.float32(10.0)
        row_min = float(np.min(row))
        row_max = float(np.max(row))
        if not (math.isfinite(row_min) and math.isfinite(row_max)):
            return None
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return {
            "row": row,
            "row_min": row_min,
            "row_max": row_max,
            "ts": payload.get("timestamp"),
            "center_frequency": payload.get("center_frequency"),
            "sample_rate": payload.get("sample_rate"),
            "n": int(row.size),
            "fmin": metadata.get("fmin"),
            "fmax": metadata.get("fmax"),
            "scan_time": metadata.get("scan_time"),
            "units": "dBFS",
        }


class DiagnosticsClient:
    def __init__(self, message_bus):
        self.bus = message_bus

    def on_message(self, callback):
        self.bus.on_message(callback)

    def publish(self, address: str, payload: str) -> bool:
        return self.bus.publish(address, payload)


class MEPClient:
    """MEP component clients sharing one bus connection."""

    DEFAULT_HOST = bus.BUS_HOST
    DEFAULT_PORT = bus.BUS_PORT
    CHANNEL_OPTIONS = bus.CHANNEL_OPTIONS
    RECORDER_CHANNEL_PORTS = bus.RECORDER_CHANNEL_PORTS
    TUNER_OPTIONS = bus.TUNER_OPTIONS
    CONJUGATE_POLICY_DEFAULT = bus.CONJUGATE_POLICY_DEFAULT
    CONJUGATE_POLICY_OPTIONS = bus.CONJUGATE_POLICY_OPTIONS
    TX_CHANNEL_OPTIONS = bus.TX_CHANNEL_OPTIONS
    TX_OFFSET_FREQ_MAX_MHZ = bus.TX_OFFSET_FREQ_MAX_MHZ
    TX_AMPLITUDE_BINS_MAX = bus.TX_AMPLITUDE_BINS_MAX

    def __init__(self, host: str = bus.BUS_HOST, port: int = bus.BUS_PORT):
        self.bus = bus.Bus(host, port)
        self.capture = CaptureClient(self.bus)
        self.archive = ArchiveClient(self.bus)
        self.upload = UploadClient(self.bus)
        self.host = HostClient(self.bus)
        self.docker = DockerClient(self.bus)
        self.systemd = SystemdClient(self.bus)
        self.rfsoc = RFSoCClient(self.bus)
        self.tuner = TunerClient(self.bus)
        self.afe = AFEClient(self.bus)
        self.recorder = RecorderClient(self.bus)
        self.spectrum = SpectrumClient(self.bus)
        self.diagnostics = DiagnosticsClient(self.bus)

    def on_connection_state(self, callback):
        self.bus.on_connection_state(callback)

    def get_connection_status(self) -> dict:
        return self.bus.get_connection_status()

    def is_connected(self) -> bool:
        return self.bus.is_connected()

    def disconnect(self):
        self.bus.disconnect()
