"""Portable RX document resolution owned by CaptureOrchestrator.

No transport, device access, filesystem writes, or application state lives here.
The service supplies its existing Recorder preset resolver.
"""

from copy import deepcopy
import math

from scipy.signal import get_window


# ============================= #
#         CONFIGURATION         #
# ============================= #

CHANNEL_DEVICES = {"A": "rxa", "B": "rxb", "C": "rxc", "D": "rxd"}
TUNER_DEFAULT_IF_MHZ = 1090
TUNER_DEFAULT_INJECTION = "high"
MISC_DEFAULTS = {
    "TRIG_RX_SRC_SEL": 0, "EXT_RX_TRIG_ENABLE": 1, "EXT_BIAS_ENABLE": 0,
    "PPS_SOURCE_SEL": 1, "REF_SOURCE_SEL": 1, "GNSS_ANT_SEL": 0,
}
RX_DEFAULTS = {
    "CHAN_BIAS_EN": 0, "INT_RF_TRIG_SEL": 1, "FILTER_BYPASS_SEL": 1,
    "AMP_BYPASS_SEL": 1, "attenuation_db": 0,
}

# Public experiment choices mapped to existing Recorder configuration keys.
RECORDER_FIELDS = {
    "spectrum": {
        "enabled": "pipeline.spectrogram", "publish": "pipeline.spectrogram_mqtt",
        "window": "spectrogram.window", "segment_samples": "spectrogram.nperseg",
        "fft_samples": "spectrogram.nfft", "overlap_samples": "spectrogram.noverlap",
        "detrend": "spectrogram.detrend", "reduction": "spectrogram.reduce_op",
        "spectra_per_chunk": "spectrogram.num_spectra_per_chunk",
    },
    "images": {
        "enabled": "pipeline.spectrogram_output",
        "rows_per_image": "spectrogram_output.num_spectra_per_output",
        "dpi": "spectrogram_output.dpi", "columns": "spectrogram_output.col_wrap",
        "colour_map": "spectrogram_output.cmap",
        "snr_min_db": "spectrogram_output.snr_db_min", "snr_max_db": "spectrogram_output.snr_db_max",
    },
    "performance": {
        "batch_size": "packet.batch_size", "batch_capacity": "packet.batch_capacity",
        "max_packet_size": "packet.max_packet_size", "chunk_samples": "packet.num_samples",
        "buffer_size": "packet.buffer_size", "worker_threads": "scheduler.worker_thread_number",
    },
}
RESAMPLER_FIELDS = ("up", "down", "outrate_cutoff", "outrate_transition_width", "attenuation_db")
RECORDER_INTEGER_KEYS = {
    "packet.batch_size", "packet.batch_capacity", "packet.max_packet_size",
    "packet.num_samples", "packet.buffer_size", "scheduler.worker_thread_number",
    "spectrogram.nperseg", "spectrogram.nfft", "spectrogram.noverlap",
    "spectrogram.num_spectra_per_chunk", "spectrogram_output.num_spectra_per_output",
    "spectrogram_output.dpi", "spectrogram_output.col_wrap",
}


def mapping(value, allowed, path):
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"Unknown {path} fields: {', '.join(sorted(unknown))}")
    return value


def number(value, path, minimum=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{path} must be a finite number")
    if integer and value != int(value):
        raise ValueError(f"{path} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{path} must be at least {minimum}")
    return int(value) if integer else value


def config_value(config, key):
    value = config
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"Selected Recorder preset does not provide {key}")
        value = value[part]
    return deepcopy(value)


def validate_recorder_overrides(overrides, preset):
    """Validate values before the existing preset calculator converts integers."""
    for key, value in overrides.items():
        path = f"recorder.overrides.{key}"
        original = config_value(preset, key)
        if value is None and key in ("spectrogram.nfft", "spectrogram.noverlap"):
            continue
        if key == "spectrogram_output.figsize":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError(f"{path} must contain width and height")
            for item in value:
                number(item, path, minimum=0.001)
        elif key in RECORDER_INTEGER_KEYS or key.endswith((".up", ".down")):
            overrides[key] = number(value, path, minimum=0 if key.endswith(".noverlap") else 1, integer=True)
        elif isinstance(original, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{path} must be boolean")
        elif isinstance(original, str):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{path} must be a nonempty string")
        else:
            number(value, path)
            if key.startswith("resampler") and value <= 0:
                raise ValueError(f"{path} must be positive")


def legacy_document(document):
    """Convert the existing capture document to the public recipe, without record replay."""
    old = document.get("effective", document.get("input", {}))
    if not isinstance(old, dict):
        raise ValueError("Existing capture settings must be an object")
    acquisition = old.get("acquisition", {})
    receive = old.get("receive", {})
    sweep = acquisition.get("sweep", {})
    tuner = {"enabled": False, "adc_if_mhz": None, "injection": None}
    tuner.update({key: value for key, value in receive.get("external_tuner", {}).items() if key in tuner})
    if not tuner["enabled"]:
        tuner.update(adc_if_mhz=None, injection=None)
    settings = {
        "acquisition": {
            "mode": acquisition.get("mode", "single"),
            "frequency_hz": acquisition.get("rf_frequency_hz"),
            "duration_s": sweep.get("dwell_s") if acquisition.get("mode", "single") == "single" else None,
        },
        "receive": {
            "channel": receive.get("rfsoc_channel", "A"),
            "sample_rate_hz": int(receive.get("adc_sample_rate_mhz", 10)) * 1000000,
            "conjugate": receive.get("conjugate_policy", "auto"),
            "external_tuner": tuner,
        },
        "recorder": {"overrides": deepcopy(old.get("recorder", {}).get("overrides", {}))},
    }
    if acquisition.get("mode") == "sweep":
        settings["acquisition"]["sweep"] = {
            "end_frequency_hz": sweep.get("end_frequency_hz"),
            "step_hz": sweep.get("step_hz"), "dwell_s": sweep.get("dwell_s"),
        }
    return {"format_version": 1, "settings": settings}


def resolve_document(document, recorder_resolver, afe_defaults=None):
    """Return a complete recipe, existing RX arguments, and the Recorder model."""
    if not isinstance(document, dict):
        raise ValueError("Capture document must be an object")
    descriptor = document.get("document")
    if "settings" not in document and isinstance(descriptor, dict) and descriptor.get("type") == "mep_capture_settings":
        document = legacy_document(document)
    mapping(document, ("format_version", "name", "description", "settings", "record"), "document")
    version = document.get("format_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("format_version must be a positive integer")
    # Operational fields define validity; a version number alone is not a gate.
    supplied = mapping(document.get("settings", {}), ("acquisition", "receive", "recorder", "afe"), "settings")
    acquisition = {"mode": "single", "frequency_hz": None, "duration_s": None}
    acquisition.update(mapping(supplied.get("acquisition", {}), (*acquisition, "sweep"), "acquisition"))
    number(acquisition["frequency_hz"], "acquisition.frequency_hz", minimum=1)
    if acquisition["mode"] not in ("single", "sweep"):
        raise ValueError("acquisition.mode must be single or sweep")
    if acquisition["duration_s"] is not None:
        number(acquisition["duration_s"], "acquisition.duration_s", minimum=0.001)
    if acquisition["mode"] == "sweep":
        if acquisition["duration_s"] is not None:
            raise ValueError("Use sweep.dwell_s rather than duration_s for a sweep")
        sweep = mapping(acquisition.get("sweep", {}), ("end_frequency_hz", "step_hz", "dwell_s"), "acquisition.sweep")
        for key in ("end_frequency_hz", "step_hz", "dwell_s"):
            number(sweep.get(key), f"acquisition.sweep.{key}")
        if sweep["end_frequency_hz"] <= 0 or sweep["dwell_s"] <= 0 or sweep["step_hz"] == 0:
            raise ValueError("Sweep requires positive end/dwell and a nonzero step")
        if (sweep["end_frequency_hz"] - acquisition["frequency_hz"]) * sweep["step_hz"] < 0:
            raise ValueError("acquisition.sweep.step_hz must move toward the end frequency")
    else:
        if acquisition.get("sweep"):
            raise ValueError("acquisition.sweep requires mode: sweep")
        acquisition.pop("sweep", None)

    receive = {"channel": "A", "sample_rate_hz": 10000000, "conjugate": "auto", "external_tuner": {"enabled": False}}
    receive.update(mapping(supplied.get("receive", {}), receive, "receive"))
    if receive["channel"] not in CHANNEL_DEVICES:
        raise ValueError("receive.channel must be A, B, C, or D")
    rate = number(receive["sample_rate_hz"], "receive.sample_rate_hz", minimum=1, integer=True)
    if rate % 1000000:
        raise ValueError("receive.sample_rate_hz must select an available whole-MHz Recorder preset")
    if receive["conjugate"] not in ("auto", "force_on", "force_off"):
        raise ValueError("receive.conjugate must be auto, force_on, or force_off")
    tuner = {"enabled": False, "adc_if_mhz": None, "injection": None}
    tuner.update(mapping(receive["external_tuner"], tuner, "receive.external_tuner"))
    if not isinstance(tuner["enabled"], bool):
        raise ValueError("receive.external_tuner.enabled must be boolean")
    if tuner["enabled"]:
        if tuner["adc_if_mhz"] is None:
            tuner["adc_if_mhz"] = TUNER_DEFAULT_IF_MHZ
        if tuner["injection"] is None:
            tuner["injection"] = TUNER_DEFAULT_INJECTION
        number(tuner["adc_if_mhz"], "receive.external_tuner.adc_if_mhz", minimum=0.001)
        if tuner["injection"] not in ("high", "low"):
            raise ValueError("receive.external_tuner.injection must be high or low")
    else:
        tuner.update(adc_if_mhz=None, injection=None)
    receive["external_tuner"] = tuner

    device = CHANNEL_DEVICES[receive["channel"]]
    afe_input = mapping(supplied.get("afe", {}), ("misc", device), "afe")
    afe_defaults = mapping(afe_defaults if afe_defaults is not None else {}, ("misc", device), "AFE defaults")
    afe = {"misc": dict(MISC_DEFAULTS), device: dict(RX_DEFAULTS)}
    for group, defaults in afe.items():
        defaults.update(mapping(afe_defaults.get(group, {}), defaults, f"AFE defaults.{group}"))
        defaults.update(mapping(afe_input.get(group, {}), defaults, f"afe.{group}"))
        for key, value in defaults.items():
            maximum = 31 if key == "attenuation_db" else 1
            value = number(value, f"afe.{group}.{key}", minimum=0, integer=True)
            if value > maximum:
                raise ValueError(f"afe.{group}.{key} must be between 0 and {maximum}")
            defaults[key] = value

    recorder_input = mapping(supplied.get("recorder", {}), ("save_iq", "save_metadata", "spectrum", "images", "performance", "resampling", "overrides"), "recorder")
    model = recorder_resolver(rate // 1000000)
    if not model.get("available"):
        raise ValueError(model.get("error") or "Recorder preset unavailable")
    preset = model["config"]
    overrides = deepcopy(recorder_input.get("overrides", {}))
    if not isinstance(overrides, dict):
        raise ValueError("recorder.overrides must be an object")
    allowed = {key for fields in RECORDER_FIELDS.values() for key in fields.values()}
    allowed.update(("pipeline.digital_rf", "pipeline.metadata", "pipeline.int_converter", "spectrogram_output.figsize"))
    allowed.update(f"{stage}.{key}" for stage in ("resampler0", "resampler1", "resampler2") for key in RESAMPLER_FIELDS)
    allowed.update(f"pipeline.{stage}" for stage in ("resampler0", "resampler1", "resampler2"))
    unknown = set(overrides) - allowed
    if unknown:
        raise ValueError(f"Unsupported Recorder overrides: {', '.join(sorted(unknown))}")
    for key, target in (("save_iq", "pipeline.digital_rf"), ("save_metadata", "pipeline.metadata")):
        if key in recorder_input:
            if not isinstance(recorder_input[key], bool):
                raise ValueError(f"recorder.{key} must be boolean")
            overrides[target] = recorder_input[key]
    for group, fields in RECORDER_FIELDS.items():
        extra = ("width_inches", "height_inches") if group == "images" else ()
        values = mapping(recorder_input.get(group, {}), (*fields, *extra), f"recorder.{group}")
        for key, target in fields.items():
            if key not in values:
                continue
            value = values[key]
            old = config_value(preset, target)
            if isinstance(old, bool):
                if not isinstance(value, bool):
                    raise ValueError(f"recorder.{group}.{key} must be boolean")
            elif isinstance(old, str):
                if not isinstance(value, str) or not value:
                    raise ValueError(f"recorder.{group}.{key} must be a nonempty string")
            elif value is not None or target not in ("spectrogram.nfft", "spectrogram.noverlap"):
                number(value, f"recorder.{group}.{key}")
            overrides[target] = value
        if group == "images" and any(key in values for key in extra):
            size = list(preset["spectrogram_output"]["figsize"])
            for index, key in enumerate(extra):
                if key in values:
                    size[index] = number(values[key], f"recorder.images.{key}", minimum=0.001)
            overrides["spectrogram_output.figsize"] = size

    if "resampling" in recorder_input:
        stages = recorder_input["resampling"]
        if not isinstance(stages, list) or len(stages) > 3:
            raise ValueError("recorder.resampling must be a list of at most three stages")
        stage_names = ("resampler0", "resampler1", "resampler2")
        preferred = [s for s in stage_names if preset["pipeline"].get(s)]
        preferred += [s for s in stage_names if s not in preferred]
        used = set()
        for stage in stage_names:
            overrides[f"pipeline.{stage}"] = False
        for index, params in enumerate(stages):
            params = mapping(params, ("stage", *RESAMPLER_FIELDS), "recorder.resampling")
            stage = params.get("stage", preferred[index])
            if stage not in stage_names or stage in used or stage not in preset:
                raise ValueError("Resampling stages must be unique and present in the selected preset")
            used.add(stage)
            overrides[f"pipeline.{stage}"] = True
            for key in RESAMPLER_FIELDS:
                if key in params:
                    overrides[f"{stage}.{key}"] = number(params[key], f"recorder.{stage}.{key}", minimum=0.000001, integer=key in ("up", "down"))

    validate_recorder_overrides(overrides, preset)
    model = recorder_resolver(rate // 1000000, overrides)
    if not model.get("available"):
        raise ValueError(model.get("error") or "Invalid Recorder settings")
    config = model["config"]
    if not math.isclose(model["metrics"]["effective_sample_rate_hz"], rate, rel_tol=1e-9):
        raise ValueError("Recorder resampling does not produce receive.sample_rate_hz")
    get_window(config["spectrogram"].get("window", "hann"), 1)
    if config["spectrogram"].get("reduce_op", "max") not in ("max", "mean", "median"):
        raise ValueError("recorder.spectrum.reduction must be max, mean, or median")
    if config["spectrogram_output"]["snr_db_min"] >= config["spectrogram_output"]["snr_db_max"]:
        raise ValueError("recorder.images.snr_min_db must be less than snr_max_db")
    # Write concrete FFT choices rather than depending on future preset defaults.
    overrides["spectrogram.nfft"] = model["values"]["nfft"]
    overrides["spectrogram.noverlap"] = model["values"]["noverlap"]
    recorder = recorder_settings(model, overrides)
    result = {"format_version": 1}
    for key in ("name", "description"):
        if key in document:
            if not isinstance(document[key], str):
                raise ValueError(f"{key} must be a string")
            result[key] = document[key]
    result["settings"] = {"acquisition": acquisition, "receive": receive, "afe": afe, "recorder": recorder}
    return {"document": result, "recorder_model": model, "recorder_overrides": overrides}


def recorder_settings(model, overrides):
    """Describe all portable Recorder choices using an already resolved model."""
    config = model["config"]
    recorder = {"save_iq": bool(config["pipeline"].get("digital_rf")), "save_metadata": bool(config["pipeline"].get("metadata"))}
    for group, fields in RECORDER_FIELDS.items():
        recorder[group] = {key: config_value(config, target) for key, target in fields.items()}
    recorder["spectrum"].update(fft_samples=model["values"]["nfft"], overlap_samples=model["values"]["noverlap"])
    recorder["images"].update(zip(("width_inches", "height_inches"), config["spectrogram_output"]["figsize"]))
    recorder["resampling"] = [dict(stage=stage, **{key: config[stage][key] for key in RESAMPLER_FIELDS if key in config[stage]}) for stage in ("resampler0", "resampler1", "resampler2") if config["pipeline"].get(stage)]
    # This portable low-level choice has no dedicated GUI field. Other legacy
    # overrides, including paths, belong only in the detailed Recorder record.
    remaining = {key: value for key, value in overrides.items() if key == "pipeline.int_converter"}
    if remaining:
        recorder["overrides"] = remaining
    return recorder


def rx_arguments(document, recorder_overrides):
    """Map the public recipe to the existing RX request shape."""
    settings = document["settings"]
    acquisition, receive = settings["acquisition"], settings["receive"]
    sweep = acquisition.get("sweep", {})
    return {"settings": {
        "acquisition": {"mode": acquisition["mode"], "rf_frequency_hz": acquisition["frequency_hz"], "sweep": {
            "end_frequency_hz": sweep.get("end_frequency_hz"), "step_hz": sweep.get("step_hz"),
            "dwell_s": sweep.get("dwell_s", acquisition["duration_s"]),
        }},
        "receive": {"rfsoc_channel": receive["channel"], "adc_sample_rate_mhz": receive["sample_rate_hz"] // 1000000,
                    "conjugate_policy": receive["conjugate"], "external_tuner": receive["external_tuner"]},
        "recorder": {"overrides": recorder_overrides},
    }}


def afe_register_commands(afe):
    """Convert the selected RX attenuator dB choice to the existing bit command."""
    registers = deepcopy(afe)
    for device, values in registers.items():
        if "attenuation_db" in values:
            db = values.pop("attenuation_db")
            for bit, amount in enumerate((1, 2, 4, 8, 16)):
                values[f"ATTEN_C{amount}"] = (db >> bit) & 1
    return registers
