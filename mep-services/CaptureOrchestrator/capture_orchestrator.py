#!/usr/bin/env python3
"""MQTT service for coordinated RX and TX acquisition workflows."""

import csv
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
import yaml

SERVICE_NAME = "captureorchestrator"
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
STATUS_INTERVAL_S = 1.0
STATUS_WAIT_S = 3.0

ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
STATUS_TOPIC = f"{SERVICE_NAME}/status"
DATA_TOPIC = f"{SERVICE_NAME}/data"
EVENT_TOPIC = f"{SERVICE_NAME}/event"

RFSOC_COMMAND = "rfsoc/command"
RFSOC_STATUS = "rfsoc/status"
TUNER_COMMAND = "tunercontrol/command"
TUNER_STATUS = "tunercontrol/status"
RECORDER_COMMAND = "recorder/command"
RECORDER_STATUS = "recorder/status"
AFE_ANNOUNCE = "afecontrol/announce"
AFE_GNSS = "afecontrol/data/gps"
AFE_IMU = "afecontrol/data/imu"
AFE_MAG = "afecontrol/data/mag"
AFE_HK = "afecontrol/data/hk"
AFE_REGISTERS = "afecontrol/status/registers"

CHANNEL_PORTS = {"A": 60134, "B": 60133, "C": 60132, "D": 60131}
TX_CHANNELS = {"None", "A", "B", "A,B"}
TX_AMPLITUDE_BINS_MAX = 8191
TX_OFFSET_FREQ_MAX_MHZ = 32
TUNER_INJECTION = {"VALON": "high", "LMX2820": "high", "TEST": "high"}
CONJUGATE_POLICIES = {"auto", "force_on", "force_off"}
RECORDER_CONFIG_DIR = "/opt/radiohound/docker/recorder/configs"
CAPTURES_ROOT_DIR = Path("/data/captures")
PREVIEW_DATA_DIR = CAPTURES_ROOT_DIR / "preview" / "data"
CAPTURE_SETTINGS_FILENAME = "capture_settings.json"

COMMANDS = {
    "get_status": {"description": "Return current acquisition workflow state.", "arguments": {}},
    "get_config": {"description": "Return the staged workflow configuration.", "arguments": {}},
    "load_config": {"description": "Load a capture settings document from JSON or YAML.", "arguments": {"path": {"type": "string", "required": True}}},
    "save_config": {"description": "Save the staged capture settings document as JSON or YAML.", "arguments": {"path": {"type": "string", "required": True}}},
    "clear_config": {"description": "Reset staged configuration to service defaults.", "arguments": {}},
    "list_recorder_presets": {
        "description": "List recorder presets deployed for available sample rates.",
        "arguments": {},
    },
    "preview_recorder_settings": {
        "description": "Resolve and validate recorder draft settings against a deployed preset.",
        "arguments": {
            "sample_rate_mhz": {"type": "integer", "required": True},
            "draft": {"type": "object", "optional": True},
        },
    },
    "start_rx": {
        "description": "Start one RX capture or an RX frequency sweep.",
        "arguments": {
            "settings": {"type": "object", "optional": True},
            "freq_start_hz": {"type": "number", "optional": True},
            "freq_end_hz": {"type": "number", "optional": True},
            "step_hz": {"type": "number", "optional": True},
            "dwell_s": {"type": "number", "optional": True},
            "channel": {"type": "string", "optional": True, "values": ["A", "B", "C", "D"]},
            "sample_rate_mhz": {"type": "integer", "optional": True},
            "tuner": {"type": "string", "optional": True},
            "adc_if_mhz": {"type": "number", "optional": True},
            "injection": {"type": "string", "values": ["high", "low"]},
            "conjugate_policy": {"type": "string", "optional": True, "values": ["auto", "force_on", "force_off"]},
            "recorder_overrides": {"type": "object", "optional": True},
            "capture_name": {"type": "string", "optional": True},
        },
    },
    "stop_rx": {"description": "Stop the RX workflow, disable the recorder, and reset RFSoC.", "arguments": {}},
    "start_tx": {
        "description": "Start or update TX output.",
        "arguments": {
            "channel": {"type": "string", "optional": True, "values": ["None", "A", "B", "A,B"]},
            "center_freq_mhz": {"type": "number", "optional": True},
            "offset_freq_mhz": {"type": "number", "optional": True},
            "amplitude_bins": {"type": "integer", "optional": True},
            "external_tuner_enabled": {"type": "boolean", "optional": True},
            "adc_if_mhz": {"type": "number", "optional": True},
            "injection": {"type": "string", "values": ["high", "low"]},
        },
    },
    "stop_tx": {"description": "Stop TX output.", "arguments": {}},
    "abort": {"description": "Stop active RX and TX workflows.", "arguments": {}},
}

CONFIG_TYPE = "mep_capture_settings"
CONFIG_VERSION = 1
LEGACY_CONFIG_TYPE = "capture_orchestrator_config"


def discover_sample_rate_options(recorder_config_dir: str = RECORDER_CONFIG_DIR) -> list[str]:
    """Discover sample rates from deployed sr{N}MHz.yaml recorder presets."""
    pattern = re.compile(r"^sr(\d+)MHz\.yaml$")
    rates = set()
    try:
        names = os.listdir(recorder_config_dir)
    except OSError as exc:
        raise RuntimeError(
            f"Could not scan recorder config directory {recorder_config_dir!r}: {exc}"
        ) from exc
    for name in names:
        match = pattern.match(name)
        if match:
            rates.add(int(match.group(1)))
    return [str(rate) for rate in sorted(rates)]


def list_recorder_presets(recorder_config_dir: str = RECORDER_CONFIG_DIR) -> dict:
    sample_rates = discover_sample_rate_options(recorder_config_dir)
    return {
        "sample_rates": sample_rates,
        "presets": [
            {
                "name": f"sr{rate}MHz",
                "sample_rate_mhz": rate,
                "path": os.path.join(recorder_config_dir, f"sr{rate}MHz.yaml"),
            }
            for rate in sample_rates
        ],
    }


def _load_yaml_mapping(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as preset_file:
            data = yaml.safe_load(preset_file)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not load recorder preset {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Recorder preset must contain a YAML mapping: {path}")
    return data


def _set_dotted_value(mapping: dict, key: str, value):
    parts = key.split(".")
    target = mapping
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


def _normalize_recorder_pipeline(config: dict) -> None:
    pipeline = config.get("pipeline")
    if not isinstance(pipeline, dict):
        return
    if not bool(pipeline.get("digital_rf", False)):
        pipeline["int_converter"] = False
        pipeline["metadata"] = False


def recorder_preset_path(sample_rate_mhz: int, config_dir: str = None) -> tuple[str, str]:
    config_dir = config_dir or RECORDER_CONFIG_DIR
    filename = f"sr{int(sample_rate_mhz)}MHz.yaml"
    deployed_path = os.path.join(config_dir, filename)
    if os.path.isfile(deployed_path):
        return deployed_path, "deployed"
    return deployed_path, "unavailable"


def resolve_recorder_preset(
    sample_rate_mhz: int,
    overrides: dict[str, object] = None,
    config_dir: str = None,
) -> dict:
    """Resolve a recorder preset into editable values and calculated metrics."""
    preset_name = f"sr{int(sample_rate_mhz)}MHz"
    path, source = recorder_preset_path(sample_rate_mhz, config_dir)
    base = {
        "available": False,
        "preset_name": preset_name,
        "preset_path": path,
        "preset_source": source,
        "error": None,
        "values": {},
        "metrics": {},
        "enabled_resamplers": [],
    }
    if source == "unavailable":
        base["error"] = f"Recorder preset not found: {path}"
        return base

    try:
        if overrides is not None and not isinstance(overrides, dict):
            raise ValueError("Recorder overrides must be a mapping")
        config = deepcopy(_load_yaml_mapping(path))
        for key, value in (overrides or {}).items():
            _set_dotted_value(config, key, value)
        _normalize_recorder_pipeline(config)

        packet = config["packet"]
        pipeline = config["pipeline"]
        spectrogram = config["spectrogram"]
        output = config["spectrogram_output"]
        metadata = packet["header_metadata"]

        input_rate = Fraction(
            int(metadata["sample_rate_numerator"]),
            int(metadata.get("sample_rate_denominator", 1)),
        )
        chunk_size = int(packet["num_samples"])
        if input_rate <= 0 or chunk_size <= 0:
            raise ValueError("Input sample rate and packet.num_samples must be positive")

        effective_rate = input_rate
        enabled_resamplers = []
        for name in ("resampler0", "resampler1", "resampler2"):
            if not bool(pipeline.get(name, False)):
                continue
            params = config.get(name)
            if not isinstance(params, dict):
                raise ValueError(f"{name} is enabled but has no configuration")
            up = int(params["up"])
            down = int(params["down"])
            if up <= 0 or down <= 0:
                raise ValueError(f"{name}.up and {name}.down must be positive")
            scaled_chunk = chunk_size * up
            if scaled_chunk % down:
                raise ValueError(
                    f"{name} produces a non-integral chunk: {chunk_size} * {up} / {down}"
                )
            chunk_size = scaled_chunk // down
            effective_rate *= Fraction(up, down)
            enabled_resamplers.append({"name": name, "up": up, "down": down})

        nperseg = int(spectrogram.get("nperseg", 1024))
        noverlap_raw = spectrogram.get("noverlap")
        noverlap = nperseg // 2 if noverlap_raw is None else int(noverlap_raw)
        nfft_raw = spectrogram.get("nfft")
        nfft = nperseg if nfft_raw is None else int(nfft_raw)
        spectra_per_chunk = int(spectrogram.get("num_spectra_per_chunk", 1))
        spectra_per_output = int(output.get("num_spectra_per_output", 600))
        if nperseg <= 0 or nfft < nperseg:
            raise ValueError("nperseg must be positive and nfft must be >= nperseg")
        if noverlap < 0 or noverlap >= nperseg:
            raise ValueError("noverlap must satisfy 0 <= noverlap < nperseg")
        if spectra_per_chunk <= 0 or chunk_size % spectra_per_chunk:
            raise ValueError("num_spectra_per_chunk must evenly divide the effective chunk")
        if spectra_per_output <= 0:
            raise ValueError("num_spectra_per_output must be positive")

        samples_per_row = chunk_size // spectra_per_chunk
        if samples_per_row < nperseg:
            raise ValueError("nperseg does not fit in each spectrum input chunk")
        hop_samples = nperseg - noverlap
        segments_per_row = 1 + (samples_per_row - nperseg) // hop_samples
        scan_time = Fraction(samples_per_row, 1) / effective_rate
        fft_hop_time = Fraction(hop_samples, 1) / effective_rate
        frequency_resolution = effective_rate / nfft
        waterfall_duration = scan_time * spectra_per_output

        values = {
            "batch_size": int(packet.get("batch_size", 0)),
            "max_packet_size": int(packet.get("max_packet_size", 0)),
            "chunk_size": int(packet["num_samples"]),
            "batch_capacity": int(packet.get("batch_capacity", 4)),
            "buffer_size": int(packet.get("buffer_size", 4)),
            "worker_thread_number": int(config["scheduler"].get("worker_thread_number", 8)),
            "nperseg": nperseg,
            "nfft": nfft,
            "noverlap": noverlap,
            "window": str(spectrogram.get("window", "hann")),
            "reduce_op": str(spectrogram.get("reduce_op", "max")),
            "num_spectra_per_chunk": spectra_per_chunk,
            "num_spectra_per_output": spectra_per_output,
            "snr_db_min": float(output.get("snr_db_min", -5)),
            "snr_db_max": float(output.get("snr_db_max", 20)),
            "cmap": str(output.get("cmap", "viridis")),
            "dpi": int(output.get("dpi", 200)),
            "figsize": tuple(output.get("figsize", (6.4, 4.8))),
            "compute": bool(pipeline.get("spectrogram", True)),
            "mqtt": bool(pipeline.get("spectrogram_mqtt", True)),
            "output": bool(pipeline.get("spectrogram_output", True)),
            "digital_rf": bool(pipeline.get("digital_rf", True)),
            "metadata": bool(pipeline.get("metadata", True)),
        }
        metrics = {
            "input_sample_rate_hz": float(input_rate),
            "effective_sample_rate_hz": float(effective_rate),
            "input_chunk_size": int(packet["num_samples"]),
            "effective_chunk_size": chunk_size,
            "frequency_bins": nfft,
            "frequency_resolution_hz": float(frequency_resolution),
            "fft_hop_samples": hop_samples,
            "fft_hop_time_s": float(fft_hop_time),
            "segments_per_row": segments_per_row,
            "samples_per_row": samples_per_row,
            "scan_time_s": float(scan_time),
            "spectrum_rate_hz": float(1 / scan_time),
            "waterfall_rows": spectra_per_output,
            "waterfall_duration_s": float(waterfall_duration),
        }
        base.update(
            available=True,
            values=values,
            metrics=metrics,
            enabled_resamplers=enabled_resamplers,
            config=config,
        )
    except Exception as exc:
        base["error"] = f"Invalid recorder preset {path}: {exc}"
    return base


def recorder_draft_to_overrides(draft: dict[str, object]) -> dict[str, object]:
    """Validate GUI-neutral draft values and map them to recorder config keys."""
    figsize_value = draft["figsize"]
    if isinstance(figsize_value, str):
        figsize = tuple(float(part.strip()) for part in figsize_value.split(","))
    else:
        figsize = tuple(float(part) for part in figsize_value)
    if len(figsize) != 2 or any(value <= 0 for value in figsize):
        raise ValueError("Figure size must contain two positive values")

    batch_size = int(draft["batch_size"])
    max_packet_size = int(draft["max_packet_size"])
    chunk_size = int(draft["chunk_size"])
    batch_capacity = int(draft["batch_capacity"])
    buffer_size = int(draft["buffer_size"])
    worker_thread_number = int(draft["worker_thread_number"])
    if batch_size <= 0:
        raise ValueError("Batch size must be positive")
    if max_packet_size <= 0:
        raise ValueError("Max packet size must be positive")
    if chunk_size <= 0:
        raise ValueError("Chunk size must be positive")
    if batch_capacity <= 0:
        raise ValueError("Batch capacity must be positive")
    if buffer_size <= 0:
        raise ValueError("Buffer size must be positive")
    if worker_thread_number <= 0:
        raise ValueError("Worker threads must be positive")

    overrides = {
        "packet.batch_size": batch_size,
        "packet.max_packet_size": max_packet_size,
        "packet.num_samples": chunk_size,
        "packet.batch_capacity": batch_capacity,
        "packet.buffer_size": buffer_size,
        "scheduler.worker_thread_number": worker_thread_number,
        "spectrogram.nperseg": int(draft["nperseg"]),
        "spectrogram.nfft": int(draft["nfft"]),
        "spectrogram.noverlap": int(draft["noverlap"]),
        "spectrogram.window": str(draft["window"]),
        "spectrogram.reduce_op": str(draft["reduce_op"]),
        "spectrogram.num_spectra_per_chunk": int(draft["num_spectra_per_chunk"]),
        "spectrogram_output.num_spectra_per_output": int(draft["num_spectra_per_output"]),
        "spectrogram_output.snr_db_min": float(draft["snr_db_min"]),
        "spectrogram_output.snr_db_max": float(draft["snr_db_max"]),
        "spectrogram_output.cmap": str(draft["cmap"]),
        "spectrogram_output.dpi": int(draft["dpi"]),
        "spectrogram_output.figsize": figsize,
        "pipeline.spectrogram": bool(draft["compute"]),
        "pipeline.spectrogram_mqtt": bool(draft["mqtt"]),
        "pipeline.spectrogram_output": bool(draft["output"]),
        "pipeline.digital_rf": bool(draft["digital_rf"]),
        "pipeline.metadata": bool(draft["metadata"]),
    }
    if not bool(draft["digital_rf"]):
        overrides["pipeline.int_converter"] = False
    return overrides


def preview_recorder_settings(
    sample_rate_mhz: int,
    draft: dict[str, object],
    config_dir: str = None,
) -> dict:
    """Resolve draft recorder values without mutating recorder state."""
    preset_model = resolve_recorder_preset(sample_rate_mhz, config_dir=config_dir)
    if not preset_model.get("available"):
        preset_model["draft_valid"] = False
        preset_model["draft_error"] = preset_model.get("error", "Preset unavailable")
        return preset_model
    try:
        overrides = recorder_draft_to_overrides(draft)
    except (KeyError, TypeError, ValueError) as exc:
        preset_model["draft_valid"] = False
        preset_model["draft_error"] = str(exc)
        return preset_model
    model = resolve_recorder_preset(sample_rate_mhz, overrides, config_dir)
    if not model.get("available"):
        preset_model["draft_valid"] = False
        error = model.get("error", "Invalid REC settings")
        prefix = f"Invalid recorder preset {model.get('preset_path')}: "
        preset_model["draft_error"] = error.removeprefix(prefix)
        return preset_model
    model["overrides"] = overrides
    model["draft_valid"] = True
    model["draft_error"] = ""
    return model


class ConfigManager:
    """Manage reusable workflow configuration and per-request overrides."""

    def __init__(self):
        self._input = {}

    @staticmethod
    def _defaults():
        return {
            "capture": {"name": None},
            "acquisition": {
                "mode": "single",
                "rf_frequency_hz": None,
                "sweep": {"end_frequency_hz": None, "step_hz": None, "dwell_s": None},
            },
            "receive": {
                "rfsoc_channel": "A",
                "adc_sample_rate_mhz": 10,
                "conjugate_policy": "auto",
                "external_tuner": {
                    "enabled": False,
                    "adc_if_mhz": None,
                    "injection": None,
                },
            },
            "recorder": {"overrides": {}},
            "afe": {"overrides": {}},
            "transmit": {
                "channel": "None",
                "center_frequency_mhz": 0.0,
                "offset_frequency_mhz": 0.0,
                "amplitude_bins": 0,
            },
        }

    def get(self) -> dict:
        return {
            "document": {"type": CONFIG_TYPE, "version": CONFIG_VERSION},
            "input": deepcopy(self._input),
        }

    def clear(self) -> dict:
        self.__init__()
        return self.get()

    def load(self, path: str) -> dict:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("configuration must be a YAML mapping")
        document = loaded.get("document", {})
        if document.get("type") == LEGACY_CONFIG_TYPE:
            supplied_input, _ = self._legacy_rx_input(loaded.get("rx", {}))
            supplied_input["recorder"] = {
                "overrides": loaded.get("recorder", {}).get("overrides", {})
            }
            supplied_input["transmit"] = {
                "channel": loaded.get("tx", {}).get("channel", "None"),
                "center_frequency_mhz": loaded.get("tx", {}).get("center_freq_mhz", 0.0),
                "offset_frequency_mhz": loaded.get("tx", {}).get("offset_freq_mhz", 0.0),
                "amplitude_bins": loaded.get("tx", {}).get("amplitude_bins", 0),
            }
            self._resolve(supplied_input, require_frequency=False)
            self._input = supplied_input
            return self.get()
        if document.get("type") != CONFIG_TYPE:
            raise ValueError(f"configuration type must be {CONFIG_TYPE!r}")
        if int(document.get("version", 0)) != CONFIG_VERSION:
            raise ValueError(f"unsupported configuration version: {document.get('version')!r}")
        unknown = set(loaded) - {"document", "input", "effective", "provenance"}
        if unknown:
            raise ValueError(f"unknown configuration sections: {sorted(unknown)}")
        supplied_input = loaded.get("effective", loaded.get("input", {}))
        if not isinstance(supplied_input, dict):
            raise ValueError("configuration input or effective settings must be a mapping")
        self._resolve(supplied_input, require_frequency=False)
        self._input = deepcopy(supplied_input)
        return self.get()

    def save(self, path: str) -> dict:
        self._resolve(self._input, require_frequency=False)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix.lower() == ".json":
            target.write_text(json.dumps(self.get(), indent=2) + "\n", encoding="utf-8")
        else:
            target.write_text(yaml.safe_dump(self.get(), sort_keys=False), encoding="utf-8")
        return {"path": str(target), "saved": True}

    def resolve_rx(self, overrides: dict) -> dict:
        request_input, legacy_tuner = self._legacy_rx_input(overrides)
        if legacy_tuner is not None:
            raise ValueError("tuner selection is owned by TunerControl; use settings.receive.external_tuner.enabled")
        input_settings = self._merge(self._input, request_input)
        effective, provenance = self._resolve(input_settings, require_frequency=True)
        tuner = effective["receive"]["external_tuner"]
        return {
            "capture_name": effective["capture"]["name"],
            "freq_start_hz": effective["acquisition"]["rf_frequency_hz"],
            "freq_end_hz": effective["acquisition"]["sweep"]["end_frequency_hz"],
            "step_hz": effective["acquisition"]["sweep"]["step_hz"],
            "dwell_s": effective["acquisition"]["sweep"]["dwell_s"],
            "channel": effective["receive"]["rfsoc_channel"],
            "sample_rate_mhz": effective["receive"]["adc_sample_rate_mhz"],
            "conjugate_policy": effective["receive"]["conjugate_policy"],
            "external_tuner_enabled": tuner["enabled"],
            "adc_if_mhz": tuner["adc_if_mhz"],
            "injection": tuner["injection"],
            "recorder_overrides": effective["recorder"]["overrides"],
            "capture_settings": {
                "document": {"type": CONFIG_TYPE, "version": CONFIG_VERSION},
                "input": input_settings,
                "effective": effective,
                "provenance": provenance,
            },
        }

    def resolve_tx(self, overrides: dict) -> dict:
        input_settings = self._merge(self._input, {"transmit": {
            "channel": overrides.get("channel", self._defaults()["transmit"]["channel"]),
            "center_frequency_mhz": overrides.get("center_freq_mhz", self._defaults()["transmit"]["center_frequency_mhz"]),
            "offset_frequency_mhz": overrides.get("offset_freq_mhz", self._defaults()["transmit"]["offset_frequency_mhz"]),
            "amplitude_bins": overrides.get("amplitude_bins", self._defaults()["transmit"]["amplitude_bins"]),
        }})
        effective, _ = self._resolve(input_settings, require_frequency=False)
        transmit = effective["transmit"]
        if transmit["channel"] not in TX_CHANNELS:
            raise ValueError("transmit.channel is invalid")
        if abs(float(transmit["offset_frequency_mhz"])) >= TX_OFFSET_FREQ_MAX_MHZ:
            raise ValueError(f"transmit.offset_frequency_mhz magnitude must be less than {TX_OFFSET_FREQ_MAX_MHZ}")
        if not 0 <= int(transmit["amplitude_bins"]) <= TX_AMPLITUDE_BINS_MAX:
            raise ValueError(f"transmit.amplitude_bins must be between 0 and {TX_AMPLITUDE_BINS_MAX}")
        return {
            "channel": transmit["channel"],
            "center_freq_mhz": transmit["center_frequency_mhz"],
            "offset_freq_mhz": transmit["offset_frequency_mhz"],
            "amplitude_bins": transmit["amplitude_bins"],
            "external_tuner_enabled": bool(overrides.get("external_tuner_enabled", False)),
            "adc_if_mhz": overrides.get("adc_if_mhz"),
            "injection": overrides.get("injection"),
        }

    def _resolve(self, input_settings: dict, require_frequency: bool):
        effective = self._merge(self._defaults(), input_settings)
        receive = effective["receive"]
        receive.pop("apply_conjugate", None)
        tuner = receive["external_tuner"]
        acquisition = effective["acquisition"]
        if receive["rfsoc_channel"] not in CHANNEL_PORTS:
            raise ValueError("receive.rfsoc_channel must be A, B, C, or D")
        if int(receive["adc_sample_rate_mhz"]) <= 0:
            raise ValueError("receive.adc_sample_rate_mhz must be positive")
        if receive["conjugate_policy"] not in CONJUGATE_POLICIES:
            raise ValueError("receive.conjugate_policy must be auto, force_on, or force_off")
        if require_frequency and acquisition["rf_frequency_hz"] is None:
            raise ValueError("acquisition.rf_frequency_hz is required")
        if acquisition["mode"] not in {"single", "sweep"}:
            raise ValueError("acquisition.mode must be single or sweep")
        if acquisition["mode"] == "sweep":
            sweep = acquisition["sweep"]
            if any(sweep[key] is None for key in ("end_frequency_hz", "step_hz", "dwell_s")):
                raise ValueError("sweep requires end_frequency_hz, step_hz, and dwell_s")
            if float(sweep["step_hz"]) == 0:
                raise ValueError("sweep.step_hz must not be zero")
            if float(sweep["dwell_s"]) <= 0:
                raise ValueError("sweep.dwell_s must be positive")
            start = float(acquisition["rf_frequency_hz"])
            end = float(sweep["end_frequency_hz"])
            step = float(sweep["step_hz"])
            if (end - start) * step < 0:
                raise ValueError("sweep.step_hz must move toward end_frequency_hz")
        if not isinstance(tuner["enabled"], bool):
            raise ValueError("receive.external_tuner.enabled must be boolean")
        if tuner["enabled"]:
            if tuner["adc_if_mhz"] is None or tuner["injection"] not in {"high", "low"}:
                raise ValueError("external tuner requires adc_if_mhz and high or low injection")
        if not isinstance(effective["recorder"]["overrides"], dict):
            raise ValueError("recorder.overrides must be a mapping")
        if "packet.apply_conjugate" in effective["recorder"]["overrides"]:
            raise ValueError("recorder.overrides must not set orchestrator-owned packet.apply_conjugate")
        if effective["afe"]["overrides"]:
            raise ValueError("AFE overrides are not supported by this orchestrator yet")
        effective["recorder"]["preset"] = f"sr{int(receive['adc_sample_rate_mhz'])}MHz"
        provenance = self._provenance(effective, input_settings)
        provenance["recorder.preset"] = {"source": "derived", "rule": "sample-rate-preset"}
        return effective, provenance

    @staticmethod
    def _merge(base: dict, overrides: dict):
        result = deepcopy(base)
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = ConfigManager._merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result

    @staticmethod
    def _provenance(effective: dict, supplied: dict, prefix=""):
        result = {}
        for key, value in effective.items():
            path = f"{prefix}.{key}" if prefix else key
            provided = supplied.get(key) if isinstance(supplied, dict) else None
            if isinstance(value, dict):
                result.update(ConfigManager._provenance(value, provided or {}, path))
            else:
                result[path] = {"source": "override" if key in (supplied or {}) else "default"}
        return result

    @staticmethod
    def _legacy_rx_input(overrides: dict):
        direct = dict(overrides)
        supplied = direct.pop("settings", {})
        if supplied and not isinstance(supplied, dict):
            raise ValueError("settings must be an object")
        legacy_tuner = direct.pop("tuner", None)
        recorder_overrides = direct.pop("recorder_overrides", None)
        if legacy_tuner is not None:
            legacy_tuner = str(legacy_tuner).strip().upper()
            if legacy_tuner not in TUNER_INJECTION:
                raise ValueError(f"unsupported legacy tuner: {legacy_tuner}")
        mapping = {
            "capture_name": ("capture", "name"),
            "freq_start_hz": ("acquisition", "rf_frequency_hz"),
            "freq_end_hz": ("acquisition", "sweep", "end_frequency_hz"),
            "step_hz": ("acquisition", "sweep", "step_hz"),
            "dwell_s": ("acquisition", "sweep", "dwell_s"),
            "channel": ("receive", "rfsoc_channel"),
            "sample_rate_mhz": ("receive", "adc_sample_rate_mhz"),
            "conjugate_policy": ("receive", "conjugate_policy"),
            "adc_if_mhz": ("receive", "external_tuner", "adc_if_mhz"),
            "injection": ("receive", "external_tuner", "injection"),
        }
        translated = deepcopy(supplied)
        for key, path in mapping.items():
            if key not in direct:
                continue
            destination = translated
            for part in path[:-1]:
                destination = destination.setdefault(part, {})
            destination[path[-1]] = direct[key]
        if legacy_tuner is not None:
            translated.setdefault("receive", {}).setdefault("external_tuner", {})["enabled"] = True
        if recorder_overrides is not None:
            if not isinstance(recorder_overrides, dict):
                raise ValueError("recorder_overrides must be an object")
            translated.setdefault("recorder", {})["overrides"] = recorder_overrides
        if translated.get("acquisition", {}).get("sweep", {}).get("end_frequency_hz") is not None:
            translated.setdefault("acquisition", {})["mode"] = "sweep"
        return translated, legacy_tuner


class MqttClient:
    """Transport for direct commands to owning services."""

    def __init__(self, client: mqtt.Client):
        self.client = client

    def command(self, topic: str, task_name: str, arguments=None, session_id: str = None):
        payload = {"task_name": task_name, "arguments": arguments or {}}
        if session_id is not None:
            payload["session_id"] = session_id
        result = self.client.publish(topic, json.dumps(payload), qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed for {topic}: rc={result.rc}")

    def subscribe(self, topic: str):
        result, _ = self.client.subscribe(topic, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT subscribe failed for {topic}: rc={result}")

    def unsubscribe(self, topic: str):
        result, _ = self.client.unsubscribe(topic)
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT unsubscribe failed for {topic}: rc={result}")


class StatusTracker:
    """Cache service status for workflow context and observability."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = {}

    def update(self, topic: str, payload: dict):
        with self._lock:
            self._values[topic] = payload

    def latest(self, topic: str):
        with self._lock:
            return self._values.get(topic)

class CaptureTelemetryLogger:
    """Write capture-scoped AFE telemetry while an RX capture is active."""

    _STREAMS = ("gps", "mag", "imu", "hk")
    _TOPICS = (AFE_GNSS, AFE_IMU, AFE_MAG, AFE_HK)

    def __init__(self, mqtt_client: MqttClient, statuses: StatusTracker):
        self.mqtt = mqtt_client
        self.statuses = statuses
        self._lock = threading.RLock()
        self._fh = None
        self._writer = None
        self._schema = {}
        self._last_imu = {}
        self._last_mag = {}
        self._last_hk = {}
        self._active = False

    @staticmethod
    def schema_from_announce(announce: Optional[dict]) -> Optional[dict]:
        if not isinstance(announce, dict):
            return None
        schema = announce.get("schema")
        if not isinstance(schema, dict):
            return None
        if not all(
            isinstance(schema.get(stream), list) and schema[stream]
            for stream in CaptureTelemetryLogger._STREAMS
        ):
            return None
        return schema

    def start(self, capture_dir: Optional[str]):
        """Start logging without making telemetry a precondition for capture."""
        if not capture_dir:
            return
        with self._lock:
            if self._active:
                return
            schema = self.schema_from_announce(self.statuses.latest(AFE_ANNOUNCE))
            if schema is None:
                logging.error("Capture telemetry not logged: no schema in afecontrol/announce")
                return

            data_dir = Path(capture_dir) / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            path = data_dir / "capture_telemetry.csv"
            header = []
            for stream, prefix in (("gps", "gnss"), ("mag", "mag"), ("imu", "imu"), ("hk", "hk")):
                header.extend(f"{prefix}_{key}" for key in schema[stream])
            header.append("registers_json")

            existing = None
            if path.exists() and path.stat().st_size > 0:
                with path.open(newline="", encoding="utf-8") as existing_file:
                    existing = next(csv.reader(existing_file), None)
            if existing is not None and existing != header:
                logging.error(
                    "Capture telemetry not logged: %s was written with a different schema",
                    path,
                )
                return

            self._fh = path.open("a", newline="", encoding="utf-8")
            self._writer = csv.writer(self._fh)
            self._schema = schema
            self._last_imu = {}
            self._last_mag = {}
            self._last_hk = {}
            if existing is None:
                self._writer.writerow(header)
                self._sync_file()

            self._active = True
            try:
                for topic in self._TOPICS:
                    self.mqtt.subscribe(topic)
            except Exception:
                logging.exception("Capture telemetry subscriptions failed")
                self.stop()
                return
            logging.info("Capture telemetry log started: %s", path)

    def handle(self, topic: str, payload: dict):
        if not isinstance(payload, dict):
            return
        with self._lock:
            if not self._active:
                return
            if topic == AFE_IMU:
                self._last_imu.update(payload)
            elif topic == AFE_MAG:
                self._last_mag.update(payload)
            elif topic == AFE_HK:
                self._last_hk.update(payload)
            elif topic == AFE_GNSS:
                self._write_gps_row(payload)

    def _write_gps_row(self, gps: dict):
        if self._writer is None:
            return
        row = []
        for source, stream in (
            (gps, "gps"),
            (self._last_mag, "mag"),
            (self._last_imu, "imu"),
            (self._last_hk, "hk"),
        ):
            row.extend(source.get(key) for key in self._schema[stream])
        registers = self.statuses.latest(AFE_REGISTERS) or {}
        row.append(json.dumps(registers.get("registers", {}), sort_keys=True, separators=(",", ":")))
        try:
            self._writer.writerow(row)
            self._sync_file()
        except Exception:
            logging.exception("Capture telemetry write error")

    def _sync_file(self):
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def stop(self):
        """Stop logging and release subscriptions; safe when already stopped."""
        with self._lock:
            if not self._active and self._fh is None:
                return
            self._active = False
            for topic in self._TOPICS:
                try:
                    self.mqtt.unsubscribe(topic)
                except Exception:
                    logging.exception("Could not unsubscribe capture telemetry topic %s", topic)
            if self._fh is not None:
                try:
                    self._sync_file()
                    self._fh.close()
                except Exception:
                    logging.exception("Capture telemetry close error")
            self._fh = None
            self._writer = None
            logging.info("Capture telemetry log stopped")

    def active_topics(self):
        with self._lock:
            return self._TOPICS if self._active else ()


class WorkflowState:
    def __init__(self):
        self._lock = threading.Lock()
        self._value = {
            "state": "idle",
            "operation": None,
            "signal_path": None,
            "session_id": None,
            "frequency_hz": None,
            "step_index": None,
            "step_count": None,
            "capture_name": None,
            "error": None,
        }

    def set(self, **changes):
        with self._lock:
            self._value.update(changes)

    def get(self):
        with self._lock:
            return dict(self._value)


class Recorder:
    """Configure and control RecorderControl as one workflow component."""

    def __init__(self, mqtt_client: MqttClient):
        self.mqtt = mqtt_client

    def configure(self, settings, session_id=None):
        sample_rate_mhz = int(settings["sample_rate_mhz"])
        channel = settings["channel"]
        capture_name = settings.get("capture_name")
        capture_folder = capture_name or "preview"

        self.stop(session_id)
        self.mqtt.command(RECORDER_COMMAND, "config.load", {"name": f"sr{sample_rate_mhz}MHz"}, session_id)
        self.mqtt.command(RECORDER_COMMAND, "config.set", {"key": "basic_network.dst_port", "value": str(CHANNEL_PORTS[channel])}, session_id)

        if not capture_name:
            stale_dir = PREVIEW_DATA_DIR.with_name(
                f".preview_data_stale_{int(time.time() * 1000)}"
            )
            if PREVIEW_DATA_DIR.is_dir():
                logging.info("Starting preview capture: rotating %s", PREVIEW_DATA_DIR)
                PREVIEW_DATA_DIR.replace(stale_dir)
            PREVIEW_DATA_DIR.mkdir(parents=True, exist_ok=True)
            if stale_dir.is_dir():
                shutil.rmtree(stale_dir, ignore_errors=True)

        self.mqtt.command(RECORDER_COMMAND, "config.set", {"key": "drf_sink.channel_dir", "value": f"{capture_folder}/data/ch{channel}"}, session_id)
        self.mqtt.command(RECORDER_COMMAND, "config.set", {"key": "spectrogram_output.plot_subdir", "value": f"{capture_folder}/data/ch{channel}_spectrogram_images"}, session_id)
        for key, value in settings.get("recorder_overrides", {}).items():
            self.mqtt.command(RECORDER_COMMAND, "config.set", {"key": key, "value": value}, session_id)
        self.mqtt.command(
            RECORDER_COMMAND,
            "config.set",
            {"key": "packet.apply_conjugate", "value": settings["apply_conjugate"]},
            session_id,
        )

    def start(self, session_id=None):
        self.mqtt.command(RECORDER_COMMAND, "enable", session_id=session_id)
        self.mqtt.command(RECORDER_COMMAND, "status", session_id=session_id)

    def stop(self, session_id=None):
        self.mqtt.command(RECORDER_COMMAND, "disable", session_id=session_id)
        self.mqtt.command(RECORDER_COMMAND, "status", session_id=session_id)


class Rx:
    """Receive-path recipes."""

    def __init__(self, mqtt_client: MqttClient, statuses: StatusTracker, state: WorkflowState):
        self.mqtt = mqtt_client
        self.statuses = statuses
        self.state = state
        self._stop_requested = threading.Event()
        self._sweep_thread = None
        self._sweep_lock = threading.Lock()
        self.recorder = Recorder(mqtt_client)
        self.telemetry = CaptureTelemetryLogger(mqtt_client, statuses)

    def start(self, freq_start, freq_end=None, step=None, dwell=None, config=None, session_id=None):
        """Start one RX capture, or an RX sweep when freq_end is supplied."""
        config = dict(config or {})
        if freq_end is None:
            return self.start_single(freq_start, dwell=dwell, config=config, session_id=session_id)
        return self.start_sweep(freq_start, freq_end, step, dwell, config=config, session_id=session_id)

    def start_single(self, frequency_hz, dwell=None, config=None, session_id=None):
        config = dict(config or {})
        self._validate_config(config)
        self._stop_requested.clear()
        self._set_starting_state("start_rx", session_id, frequency_hz, settings=config)
        try:
            self.recorder.configure(config, session_id)
            self._configure_rx_frequency(frequency_hz, config, session_id)
            self.recorder.start(session_id)
            self._start_telemetry(config.get("capture_dir"))
            self.state.set(state="running")
            if dwell is not None:
                dwell = float(dwell)
                if dwell <= 0:
                    raise ValueError("dwell must be positive")
                self._dwell(dwell)
                self.stop(session_id)
            return {"state": self.state.get()["state"], "frequency_hz": frequency_hz, "capture_name": config.get("capture_name")}
        except Exception as exc:
            try:
                self.stop(session_id)
            except Exception:
                logging.exception("RX cleanup failed")
            self.state.set(state="failed", error=str(exc))
            raise

    def start_sweep(self, freq_start, freq_end, step, dwell, config=None, session_id=None):
        config = dict(config or {})
        self._validate_config(config)
        if step is None or float(step) == 0:
            raise ValueError("step is required for an RX sweep")
        if dwell is None or float(dwell) <= 0:
            raise ValueError("positive dwell is required for an RX sweep")
        frequencies = self._frequency_list(float(freq_start), float(freq_end), float(step))
        with self._sweep_lock:
            if self._sweep_thread is not None and self._sweep_thread.is_alive():
                raise RuntimeError("an RX sweep is already running")
            self._stop_requested.clear()
            self._sweep_thread = threading.Thread(
                target=self._run_sweep,
                args=(frequencies, float(dwell), config, session_id),
                daemon=True,
                name="rx-sweep",
            )
            self._sweep_thread.start()
        return {"state": "starting", "frequency_count": len(frequencies)}

    def _run_sweep(self, frequencies, dwell, settings, session_id):
        try:
            self._set_starting_state("start_rx", session_id, frequencies[0], len(frequencies), settings=settings)
            self.recorder.configure(settings, session_id)
            self.recorder.start(session_id)
            self._start_telemetry(settings.get("capture_dir"))
            self.state.set(state="running")
            for index, frequency_hz in enumerate(frequencies, start=1):
                if self._stop_requested.is_set():
                    break
                self.state.set(frequency_hz=frequency_hz, step_index=index)
                self._configure_rx_frequency(frequency_hz, settings, session_id)
                if self._stop_requested.wait(dwell):
                    break
            self.stop(session_id)
        except Exception as exc:
            logging.exception("RX sweep failed")
            try:
                self.stop(session_id)
            except Exception:
                logging.exception("RX sweep cleanup failed")
            self.state.set(state="failed", error=str(exc))

    def stop(self, session_id=None):
        self._stop_requested.set()
        self.telemetry.stop()
        try:
            self.recorder.stop(session_id)
        finally:
            try:
                self.mqtt.command(RFSOC_COMMAND, "reset", session_id=session_id)
            finally:
                self.state.set(state="idle", operation=None, signal_path=None, capture_name=None, error=None)
        return {"state": "idle"}

    def _configure_rx_frequency(self, frequency_hz, settings, session_id):
        use_external_tuner = settings.get("external_tuner_enabled", False)
        injection = settings.get("injection")
        # Reset RFSoC before changing channel or frequency.
        self.mqtt.command(RFSOC_COMMAND, "reset", session_id=session_id)

        # Select the channel before writing frequency fields.
        self.mqtt.command(RFSOC_COMMAND, "set", f"channel {settings['channel']}", session_id)

        if use_external_tuner:
            if settings.get("adc_if_mhz") is None or injection not in {"high", "low"}:
                raise ValueError("external tuner requires adc_if_mhz and injection")
            if_mhz = float(settings["adc_if_mhz"])
            # Set the RFSoC IF for the external-tuner path.
            self.mqtt.command(RFSOC_COMMAND, "set", f"freq_IF {if_mhz}", session_id)

            lo_mhz = frequency_hz / 1e6 + (if_mhz if injection == "high" else -if_mhz)

            # Set the external tuner LO for the requested RF frequency.
            self.mqtt.command(TUNER_COMMAND, "set_freq", {"freq_mhz": lo_mhz}, session_id)
        else:
            # Set the RFSoC NCO directly when no external tuner is selected.
            self.mqtt.command(RFSOC_COMMAND, "set", f"freq_IF {frequency_hz / 1e6}", session_id)

        # Write frequency metadata and arm on the next PPS.
        self.mqtt.command(RFSOC_COMMAND, "set", f"freq_metadata {frequency_hz}", session_id)

        # Arm the receive path on the next PPS edge.
        self.mqtt.command(RFSOC_COMMAND, "capture_next_pps", session_id=session_id)

    def _start_telemetry(self, capture_dir):
        try:
            self.telemetry.start(capture_dir)
        except Exception:
            logging.exception("Capture telemetry setup failed; RX capture will continue")
            self.telemetry.stop()

    def _dwell(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop_requested.is_set():
                return
            time.sleep(min(1.0, deadline - time.monotonic()))

    def _set_starting_state(self, operation, session_id, frequency_hz, step_count=None, settings=None):
        self.state.set(
            state="starting",
            operation=operation,
            signal_path="RX",
            session_id=session_id,
            frequency_hz=frequency_hz,
            step_index=0 if step_count else None,
            step_count=step_count,
            capture_name=(settings or {}).get("capture_name"),
            conjugate_policy=(settings or {}).get("conjugate_policy"),
            apply_conjugate=(settings or {}).get("apply_conjugate"),
            error=None,
        )

    @staticmethod
    def _frequency_list(start, end, step):
        if (end - start) * step < 0:
            raise ValueError("step must move from freq_start_hz toward freq_end_hz")
        frequencies = []
        current = start
        epsilon = abs(step) * 1e-9
        while current <= end + epsilon if step > 0 else current >= end - epsilon:
            frequencies.append(int(round(current)))
            current += step
        return frequencies

    @staticmethod
    def _validate_config(config):
        config["channel"] = str(config.get("channel", "A")).upper()
        if config["channel"] not in CHANNEL_PORTS:
            raise ValueError(f"channel must be one of {sorted(CHANNEL_PORTS)}")
        config["sample_rate_mhz"] = int(config["sample_rate_mhz"])
        tuner = str(config.get("tuner") or "").strip().upper() or None
        if tuner and tuner not in TUNER_INJECTION:
            raise ValueError(f"unsupported tuner: {tuner}")
        config["tuner"] = tuner
        config["injection"] = str(config.get("injection") or TUNER_INJECTION.get(tuner, "high")).lower()
        if config["injection"] not in ("high", "low"):
            raise ValueError("injection must be high or low")


class Tx:
    """Transmit-path recipes."""

    def __init__(self, mqtt_client: MqttClient, state: WorkflowState):
        self.mqtt = mqtt_client
        self.state = state

    def start(self, settings: dict, session_id=None):
        channel = settings["channel"]
        if channel not in TX_CHANNELS:
            raise ValueError(f"channel must be one of {sorted(TX_CHANNELS)}")
        center = float(settings["center_freq_mhz"])
        offset = float(settings["offset_freq_mhz"])
        amplitude = int(settings["amplitude_bins"])
        if abs(offset) >= TX_OFFSET_FREQ_MAX_MHZ:
            raise ValueError(f"offset_freq_mhz magnitude must be less than {TX_OFFSET_FREQ_MAX_MHZ}")
        if not 0 <= amplitude <= TX_AMPLITUDE_BINS_MAX:
            raise ValueError(f"amplitude_bins must be between 0 and {TX_AMPLITUDE_BINS_MAX}")
        if settings.get("external_tuner_enabled", False):
            injection = str(settings.get("injection") or "").lower()
            if settings.get("adc_if_mhz") is None or injection not in {"high", "low"}:
                raise ValueError("external tuner requires adc_if_mhz and injection")
            if_mhz = float(settings["adc_if_mhz"])

            lo_mhz = center + (if_mhz if injection == "high" else -if_mhz)

            # Set the external tuner LO for the requested TX center frequency.
            self.mqtt.command(TUNER_COMMAND, "set_freq", {"freq_mhz": lo_mhz}, session_id)

        # Set the TX center frequency.
        self.mqtt.command(RFSOC_COMMAND, "set", f"tx_center_freq {center}", session_id)

        # Set the TX baseband offset frequency.
        self.mqtt.command(RFSOC_COMMAND, "set", f"tx_offset_freq {offset}", session_id)

        # Set the TX waveform amplitude.
        self.mqtt.command(RFSOC_COMMAND, "set", f"tx_amplitude {amplitude}", session_id)

        # Select the TX channel.
        self.mqtt.command(RFSOC_COMMAND, "set", f"tx_channel {channel}", session_id)

        # Start RFSoC TX output.
        self.mqtt.command(RFSOC_COMMAND, "tx_start", session_id=session_id)
        self.state.set(state="running", operation="start_tx", signal_path="TX", session_id=session_id, error=None)
        return {"state": "running", "channel": channel, "center_freq_mhz": center}

    def stop(self, session_id=None):
        # Stop RFSoC TX output.
        self.mqtt.command(RFSOC_COMMAND, "tx_stop", session_id=session_id)
        self.state.set(state="idle", operation=None, signal_path=None, error=None)
        return {"state": "idle"}


class CaptureOrchestrator:
    """Selects and runs RX/TX recipes."""

    def __init__(self, mqtt_client: MqttClient, statuses: StatusTracker):
        self.config = ConfigManager()
        self.mqtt = mqtt_client
        self.statuses = statuses
        self.rx_state = WorkflowState()
        self.tx_state = WorkflowState()
        self.rx = Rx(mqtt_client, statuses, self.rx_state)
        self.tx = Tx(mqtt_client, self.tx_state)

    def _prepare_capture(self, resolved_rx: dict) -> dict:
        self._resolve_external_tuner(resolved_rx)
        self._resolve_conjugate(resolved_rx)
        capture_name = resolved_rx.get("capture_name")
        if not capture_name:
            return resolved_rx
        if Path(capture_name).name != capture_name or capture_name in {".", ".."}:
            raise ValueError("capture name must be a non-empty basename")
        capture_root = CAPTURES_ROOT_DIR / capture_name
        data_dir = capture_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (capture_root / "log_upload").mkdir(exist_ok=True)
        settings_path = data_dir / CAPTURE_SETTINGS_FILENAME
        capture_settings = resolved_rx.pop("capture_settings")
        if settings_path.exists():
            try:
                existing = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"invalid capture settings: {settings_path}") from exc
            if existing.get("effective") != capture_settings["effective"]:
                raise ValueError("capture settings differ; choose a new capture name")
        else:
            self._write_json(settings_path, capture_settings)
        resolved_rx["capture_dir"] = str(capture_root)
        return resolved_rx

    def _resolve_external_tuner(self, resolved_rx: dict):
        if not resolved_rx.get("external_tuner_enabled"):
            return
        status = self.statuses.latest(TUNER_STATUS)
        resolved_model = self._resolved_tuner_model(status)
        if resolved_model is None:
            return
        settings = resolved_rx["capture_settings"]
        tuner = settings["effective"]["receive"]["external_tuner"]
        tuner["resolved_model"] = resolved_model
        settings["provenance"]["receive.external_tuner.resolved_model"] = {
            "source": "service_resolved",
            "service": "tunercontrol",
        }

    @staticmethod
    def _resolve_conjugate(resolved_rx: dict):
        policy = resolved_rx["conjugate_policy"]
        if policy == "force_on":
            apply_conjugate = True
        elif policy == "force_off":
            apply_conjugate = False
        else:
            apply_conjugate = bool(
                resolved_rx.get("external_tuner_enabled")
                and resolved_rx.get("injection") == "high"
            )
        resolved_rx["apply_conjugate"] = apply_conjugate
        settings = resolved_rx["capture_settings"]
        settings["effective"]["receive"]["apply_conjugate"] = apply_conjugate
        settings["provenance"]["receive.apply_conjugate"] = {
            "source": "derived",
            "rule": "conjugate-policy",
        }

    @staticmethod
    def _resolved_tuner_model(status):
        if not isinstance(status, dict):
            return None
        tuner = status.get("tuner")
        if isinstance(tuner, dict):
            return str(tuner.get("name") or "") or None
        return None

    @staticmethod
    def _write_json(path: Path, payload: dict):
        temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(path)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def handle(self, request: dict):
        if not isinstance(request, dict):
            raise ValueError("command payload must be an object")
        task_name = request.get("task_name")
        arguments = request.get("arguments") or {}
        session_id = request.get("session_id")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        if task_name == "get_status":
            return {"rx": self.rx_state.get(), "tx": self.tx_state.get()}
        if task_name == "get_config":
            return self.config.get()
        if task_name == "load_config":
            return self.config.load(arguments["path"])
        if task_name == "save_config":
            return self.config.save(arguments["path"])
        if task_name == "clear_config":
            return self.config.clear()
        if task_name == "list_recorder_presets":
            return list_recorder_presets()
        if task_name == "preview_recorder_settings":
            if "draft" not in arguments:
                return resolve_recorder_preset(arguments["sample_rate_mhz"])
            return preview_recorder_settings(arguments["sample_rate_mhz"], arguments["draft"])
        if task_name == "start_rx":
            resolved = self.config.resolve_rx(arguments)
            recorder_model = resolve_recorder_preset(
                resolved["sample_rate_mhz"], resolved["recorder_overrides"]
            )
            if not recorder_model.get("available"):
                raise ValueError(recorder_model.get("error") or "Recorder preset is invalid")
            resolved = self._prepare_capture(resolved)
            return self.rx.start(
                freq_start=resolved.pop("freq_start_hz"),
                freq_end=resolved.pop("freq_end_hz", None),
                step=resolved.pop("step_hz", None),
                dwell=resolved.pop("dwell_s", None),
                config=resolved,
                session_id=session_id,
            )
        if task_name == "stop_rx":
            return self.rx.stop(session_id)
        if task_name == "start_tx":
            return self.tx.start(self.config.resolve_tx(arguments), session_id)
        if task_name == "stop_tx":
            return self.tx.stop(session_id)
        if task_name == "abort":
            return {"rx": self.rx.stop(session_id), "tx": self.tx.stop(session_id)}
        raise ValueError(f"unsupported task_name: {task_name!r}")

    def status(self, seq: int):
        return {
            "service": SERVICE_NAME,
            "state": "online",
            "timestamp": time.time(),
            "seq": seq,
            "rx": self.rx_state.get(),
            "tx": self.tx_state.get(),
        }


class CaptureOrchestratorService:
    def __init__(self, broker=MQTT_BROKER, port=MQTT_PORT):
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{SERVICE_NAME}_{uuid.uuid4().hex[:8]}",
        )
        self.mqtt = MqttClient(self.client)
        self.statuses = StatusTracker()
        self.orchestrator = CaptureOrchestrator(self.mqtt, self.statuses)
        self.broker = broker
        self.port = port
        self.started_at = time.time()
        self._status_seq = 0
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logging.error("MQTT connection failed: %s", reason_code)
            return
        topics = (
            COMMAND_TOPIC,
            RFSOC_STATUS,
            TUNER_STATUS,
            RECORDER_STATUS,
            AFE_ANNOUNCE,
            AFE_REGISTERS,
            *self.orchestrator.rx.telemetry.active_topics(),
        )
        for topic in topics:
            client.subscribe(topic, qos=1)
        self._publish(ANNOUNCE_TOPIC, self.announce(), retain=True)

    def _on_message(self, client, userdata, message):
        tracked_topics = {
            RFSOC_STATUS,
            TUNER_STATUS,
            RECORDER_STATUS,
            AFE_ANNOUNCE,
            AFE_REGISTERS,
            AFE_GNSS,
            AFE_IMU,
            AFE_MAG,
            AFE_HK,
        }
        if message.topic in tracked_topics:
            try:
                payload = json.loads(message.payload.decode("utf-8"))
                self.statuses.update(message.topic, payload)
                self.orchestrator.rx.telemetry.handle(message.topic, payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                logging.warning("Invalid JSON on %s", message.topic)
            return
        threading.Thread(
            target=self._handle_command,
            args=(message,),
            daemon=True,
            name="capture-orchestrator-command",
        ).start()

    def _handle_command(self, message):
        request = None
        try:
            request = json.loads(message.payload.decode("utf-8"))
            result = self.orchestrator.handle(request)
            response = {
                "task_name": request.get("task_name"),
                "session_id": request.get("session_id"),
                "success": True,
                "status_data": result,
                "error": None,
            }
        except Exception as exc:
            logging.exception("Capture workflow command failed")
            response = {
                "task_name": request.get("task_name") if isinstance(request, dict) else None,
                "session_id": request.get("session_id") if isinstance(request, dict) else None,
                "success": False,
                "status_data": None,
                "error": str(exc),
            }
            self._publish_event("workflow_failed", {"task_name": response["task_name"], "error": str(exc)})
        self._publish(RESPONSE_TOPIC, response, retain=False)

    def announce(self):
        return {
            "title": "Capture Orchestrator Service",
            "description": "Coordinates RX and TX acquisition recipes through MQTT commands to independent services.",
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

    def publish_status(self):
        self._status_seq += 1
        self._publish(STATUS_TOPIC, self.orchestrator.status(self._status_seq), retain=True)

    def _publish_event(self, event_type: str, status_data: dict):
        self._publish(EVENT_TOPIC, {
            "service": SERVICE_NAME,
            "event_type": event_type,
            "timestamp": time.time(),
            "status_data": status_data,
        }, retain=False)

    def _publish(self, topic: str, payload: dict, retain: bool):
        self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=retain)

    def run(self):
        self.client.connect(self.broker, self.port, keepalive=60)
        self.client.loop_start()
        try:
            while True:
                self.publish_status()
                time.sleep(STATUS_INTERVAL_S)
        finally:
            try:
                self.orchestrator.rx.stop()
            except Exception:
                logging.exception("RX cleanup failed during service shutdown")
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    CaptureOrchestratorService().run()
