# SPDX-FileCopyrightText: Copyright (c) 2026 Massachusetts Institute of Technology
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# afe_control.py - AFEControl service
#
# John Marino 2026-03-11, University of Colorado Boulder
# Based on Ben Welchman 08-01-2025, MIT Haystack Observatory
#
# Bridges the RP2040 analog front-end instrument (via gpsd) to MQTT.
# MQTT is the sole control and data surface.
#
# Prerequisites:
#   gpsd running with:
#     DEVICES="/dev/ttyGNSS1"
#     GPSD_OPTIONS="-n -r -s 460800 -D 3 -F /var/run/gpsd.sock"
#
# Usage:
#   python afe_service.py
#   python afe_service.py --mqtt_host=... --gpsd_host=... (see --help for all fields)

import csv
import dataclasses
import json
import logging
import os
import socket
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

import aiomqtt
import anyio
import exceptiongroup
import jsonargparse
import msgspec

# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger("afe_service")
logger.setLevel(os.environ.get("AFE_SERVICE_LOG_LEVEL", "INFO"))
logger.propagate = False
_console = logging.StreamHandler()
_console.setLevel(logging.DEBUG)
logger.addHandler(_console)

_LOG_MODES = {"normal": logging.INFO, "debug": logging.DEBUG}


def _log_mode() -> str:
    return "debug" if logger.getEffectiveLevel() <= logging.DEBUG else "normal"


# ============================================================================
# CONSTANTS
# ============================================================================

_GPSD_SOCK_PATH = "/var/run/gpsd.sock"
_GPSD_WATCH_CMD = b'?WATCH={"enable":true,"raw":1};\r\n'

# Service-owned workaround for Mode 13: drive telemetry by polling $TELEM?.
# Polling is owned by afecontrol/command/polling with one global interval.
_USE_SERVICE_TELEM_WORKAROUND = True
_POLL_LOOP_SLEEP_S = 0.2
_REGISTER_QUERY_GAP_S = 0.1
_REGISTER_WRITE_GAP_S = 0.3
_REGISTER_RESOLVE_TIMEOUT_S = 2.0
_GPSD_STATUS_HEARTBEAT_S = 5.0
_RAW_STREAM_DEFAULT_DURATION_S = 10.0
_RAW_STREAM_MAX_DURATION_S = 60.0

# ============================================================================
# SOURCE-OF-TRUTH TABLES
# ============================================================================
# !!!! WARNING !!!!!
# These tables are convenience copies for the MQTT API.
# The actual source of truth for register mappings, pin names, and defaults
# is controller.py on the RP2040 firmware. If the firmware changes, these
# tables MUST be updated to match.
# !!!! WARNING !!!!!

# ---- Pin tables ----

_MISC_PINS = [
    {"pin": 0, "name": "TRIG_TX_SRC_SEL",   "label": "TX Trigger Source Select",   "rp2040_default": 1, "service_default_override": 0, "0": "Internal",     "1": "External"},
    {"pin": 1, "name": "TRIG_RX_SRC_SEL",   "label": "RX Trigger Source Select",   "rp2040_default": 1, "service_default_override": 0, "0": "Internal",     "1": "External"},
    {"pin": 2, "name": "EXT_TX_TRIG_ENABLE", "label": "External TX Trigger Enable", "rp2040_default": 1, "service_default_override": 1, "0": "Enabled",      "1": "Disabled"},
    {"pin": 3, "name": "EXT_RX_TRIG_ENABLE", "label": "External RX Trigger Enable", "rp2040_default": 1, "service_default_override": 1, "0": "Enabled",      "1": "Disabled"},
    {"pin": 4, "name": "NOT_USED_4",         "label": "Not Used (4)",               "rp2040_default": 0, "service_default_override": 0, "0": "Reserved",     "1": "Reserved"},
    {"pin": 5, "name": "EXT_BIAS_ENABLE",    "label": "External Bias Enable",       "rp2040_default": 0, "service_default_override": 0, "0": "Disabled",     "1": "Enabled"},
    {"pin": 6, "name": "TEST_LED",           "label": "Test LED",                   "rp2040_default": 0, "service_default_override": 0, "0": "Off",          "1": "On"},
    {"pin": 7, "name": "PPS_SOURCE_SEL",     "label": "PPS Source Select",          "rp2040_default": 1, "service_default_override": 1, "0": "External",     "1": "Internal GNSS"},
    {"pin": 8, "name": "REF_SOURCE_SEL",     "label": "Reference Source Select",    "rp2040_default": 1, "service_default_override": 1, "0": "External",     "1": "Internal OCXO"},
    {"pin": 9, "name": "GNSS_ANT_SEL",       "label": "GNSS Antenna Select",        "rp2040_default": 0, "service_default_override": 0, "0": "External",     "1": "Internal"},
]

_TX_PINS = [
    {"pin": 0, "name": "NOT_USED_0",        "label": "Not Used (0)",           "rp2040_default": 0, "service_default_override": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 1, "name": "TX_BLANK_SEL",      "label": "TX Blanking Select",    "rp2040_default": 1, "service_default_override": 1, "0": "Blanked",    "1": "Not blanked"},
    {"pin": 2, "name": "FILTER_BYPASS_SEL", "label": "Filter Bypass Select",  "rp2040_default": 1, "service_default_override": 1, "0": "Filtered",   "1": "Bypassed"},
    {"pin": 3, "name": "NOT_USED_3",        "label": "Not Used (3)",           "rp2040_default": 0, "service_default_override": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 4, "name": "NOT_USED_4",        "label": "Not Used (4)",           "rp2040_default": 0, "service_default_override": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 5, "name": "NOT_USED_5",        "label": "Not Used (5)",           "rp2040_default": 0, "service_default_override": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 6, "name": "NOT_USED_6",        "label": "Not Used (6)",           "rp2040_default": 0, "service_default_override": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 7, "name": "NOT_USED_7",        "label": "Not Used (7)",           "rp2040_default": 0, "service_default_override": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 8, "name": "NOT_USED_8",        "label": "Not Used (8)",           "rp2040_default": 0, "service_default_override": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 9, "name": "TEST_LED",          "label": "Test LED",               "rp2040_default": 0, "service_default_override": 0, "0": "Off",        "1": "On"},
]

_RX_PINS = [
    {"pin": 0, "name": "CHAN_BIAS_EN",       "label": "Channel Bias Enable",       "rp2040_default": 0, "service_default_override": 0, "0": "Disabled",     "1": "Enabled"},
    {"pin": 1, "name": "INT_RF_TRIG_SEL",    "label": "Internal RF Trigger Select", "rp2040_default": 1, "service_default_override": 1, "0": "Not asserted", "1": "Asserted"},
    # P2 (pin3) on MAX chip controls CTRL on JSW2-63DR+: CTRL high selects RF1, CTRL low selects RF2; RF1 is no filter, RF2 is filtered. this is BACKWARDS from the stated comment in the RP2040's controller.py code as of 7/15/2026.
    {"pin": 2, "name": "FILTER_BYPASS_SEL",  "label": "Filter Bypass Select",       "rp2040_default": 1, "service_default_override": 1, "0": "Filtered",     "1": "Bypassed"},
    # CTL high = amplifier enabled, CTL low = amplifier bypassed; controlled directly through pin 9 CTL on the AM1065 via a 10k resistor and capacitor to ground.
    {"pin": 3, "name": "AMP_BYPASS_SEL",     "label": "Amplifier Bypass Select",    "rp2040_default": 1, "service_default_override": 1, "0": "Bypassed",     "1": "Enabled"},
    {"pin": 4, "name": "ATTEN_C1",           "label": "Attenuator +1 dB",           "rp2040_default": 0, "service_default_override": 0, "0": "Skip +1dB",    "1": "Add +1dB"},
    {"pin": 5, "name": "ATTEN_C2",           "label": "Attenuator +2 dB",           "rp2040_default": 0, "service_default_override": 0, "0": "Skip +2dB",    "1": "Add +2dB"},
    {"pin": 6, "name": "ATTEN_C4",           "label": "Attenuator +4 dB",           "rp2040_default": 0, "service_default_override": 0, "0": "Skip +4dB",    "1": "Add +4dB"},
    {"pin": 7, "name": "ATTEN_C8",           "label": "Attenuator +8 dB",           "rp2040_default": 0, "service_default_override": 0, "0": "Skip +8dB",    "1": "Add +8dB"},
    {"pin": 8, "name": "ATTEN_C16",          "label": "Attenuator +16 dB",          "rp2040_default": 0, "service_default_override": 0, "0": "Skip +16dB",   "1": "Add +16dB"},
    {"pin": 9, "name": "TEST_LED",           "label": "Test LED",                   "rp2040_default": 0, "service_default_override": 0, "0": "Off",          "1": "On"},
]

# ---- Devices ----

_DEVICES = {
    "misc": {"pins": _MISC_PINS, "prefix": "PMITMAX", "query": "$PMITMA?*",  "tlc": "MA?"},
    "tx1":  {"pins": _TX_PINS,   "prefix": "PMITXT1", "query": "$PMITXT1?*", "tlc": "XT1"},
    "tx2":  {"pins": _TX_PINS,   "prefix": "PMITXT2", "query": "$PMITXT2?*", "tlc": "XT2"},
    "rxa":  {"pins": _RX_PINS,   "prefix": "PMITXR4", "query": "$PMITXR4?*", "tlc": "XR4"},
    "rxb":  {"pins": _RX_PINS,   "prefix": "PMITXR3", "query": "$PMITXR3?*", "tlc": "XR3"},
    "rxc":  {"pins": _RX_PINS,   "prefix": "PMITXR2", "query": "$PMITXR2?*", "tlc": "XR2"},
    "rxd":  {"pins": _RX_PINS,   "prefix": "PMITXR1", "query": "$PMITXR1?*", "tlc": "XR1"},
}

# ---- IMU ODR table (names match controller.py odrDictList exactly) ----

_IMU_ODR_TABLE = [
    {"name": "ODR_OFF",         "label": "Off",                       "hz":    0,   "acc": True,  "gyr": True },
    {"name": "ACC_1_6_HZ_ULP",  "label": "1.6 Hz (Ultra-Low-Power)", "hz":    1.6, "acc": True,  "gyr": False},
    {"name": "GYR_6_5_HZ_LP",   "label": "6.5 Hz (Low-Power)",       "hz":    6.5, "acc": False, "gyr": True },
    {"name": "ODR_12_5_HZ_LP",  "label": "12.5 Hz (Low-Power)",      "hz":   12.5, "acc": True,  "gyr": True },
    {"name": "ODR_26_HZ_LP",    "label": "26 Hz (Low-Power)",        "hz":   26,   "acc": True,  "gyr": True },
    {"name": "ODR_52_HZ_NP",    "label": "52 Hz (Normal)",           "hz":   52,   "acc": True,  "gyr": True },
    {"name": "ODR_104_HZ_NP",   "label": "104 Hz (Normal)",          "hz":  104,   "acc": True,  "gyr": True },
    {"name": "ODR_208_HZ_HP",   "label": "208 Hz (High-Perf)",       "hz":  208,   "acc": True,  "gyr": True },
    {"name": "ODR_416_HZ_HP",   "label": "416 Hz (High-Perf)",       "hz":  416,   "acc": True,  "gyr": True },
    {"name": "ODR_833_HZ_HP",   "label": "833 Hz (High-Perf)",       "hz":  833,   "acc": True,  "gyr": True },
    {"name": "ODR_1_66_KHZ_HP", "label": "1.66 kHz (High-Perf)",     "hz": 1660,   "acc": True,  "gyr": True },
    {"name": "ODR_3_33_KHZ_HP", "label": "3.33 kHz (High-Perf)",     "hz": 3330,   "acc": True,  "gyr": True },
    {"name": "ODR_6_66_KHZ_HP", "label": "6.66 kHz (High-Perf)",     "hz": 6660,   "acc": True,  "gyr": True },
]

# ---- IMU mode settings ----

_IMU_MODE_SETTINGS = [
    {"key": "ahiperf", "label": "Accelerometer High-Performance Filter", "type": "bool", "default": 0, "0": "Off", "1": "On"},
    {"key": "aulp",    "label": "Accelerometer Ultra-Low-Power Mode",    "type": "bool", "default": 0, "0": "Off", "1": "On"},
    {"key": "glp",     "label": "Gyroscope Low-Power Mode",             "type": "bool", "default": 0, "0": "Off", "1": "On"},
]

# ---- Magnetometer parameters ----

_MAG_PARAMS = {
    "ccr":  {"label": "Cycle Count Register (CCR)",  "range": [50, 400],  "unit": "counts",   "default": 200,
             "description": "Controls measurement sensitivity. Higher count = more precision, slower rate."},
    "updr": {"label": "Update Rate Register (UPDR)", "range": [146, 159], "unit": "register", "default": 150,
             "description": "Continuous measurement data rate register value."},
}

# ---- Telemetry rate (shared by IMU, MAG, HK) ----

_RATE_PARAM = {
    "label": "Telemetry Reporting Rate", "range": [0, 60], "unit": "s", "default": 1,
    "description": "Interval between telemetry publishes (0 = off).",
}

# ---- Data field tables ----

_DATA_FIELDS_IMU = [
    {"key": "acc_x", "label": "Accelerometer X", "unit": "g",   "group": "accelerometer"},
    {"key": "acc_y", "label": "Accelerometer Y", "unit": "g",   "group": "accelerometer"},
    {"key": "acc_z", "label": "Accelerometer Z", "unit": "g",   "group": "accelerometer"},
    {"key": "gyr_x", "label": "Gyroscope X",     "unit": "°/s", "group": "gyroscope"},
    {"key": "gyr_y", "label": "Gyroscope Y",     "unit": "°/s", "group": "gyroscope"},
    {"key": "gyr_z", "label": "Gyroscope Z",     "unit": "°/s", "group": "gyroscope"},
]

_DATA_FIELDS_MAG = [
    {"key": "mag_x", "label": "Magnetometer X", "unit": "µT"},
    {"key": "mag_y", "label": "Magnetometer Y", "unit": "µT"},
    {"key": "mag_z", "label": "Magnetometer Z", "unit": "µT"},
]

_DATA_FIELDS_GPS = [
    {"key": "fix_valid",                  "label": "Fix Valid",                  "type": "bool"},
    {"key": "fix_mode",                   "label": "Fix Mode",                   "type": "int"},
    {"key": "fix_selection",              "label": "Fix Selection",              "type": "string"},
    {"key": "fix_quality",                "label": "Fix Quality",                "type": "int"},
    {"key": "faa_mode",                   "label": "FAA Mode",                   "type": "string"},
    {"key": "navigation_status",          "label": "Navigation Status",          "type": "string"},
    {"key": "lat",                        "label": "Latitude",                   "unit": "°"},
    {"key": "lon",                        "label": "Longitude",                  "unit": "°"},
    {"key": "altitude_msl_m",             "label": "Altitude MSL",              "unit": "m"},
    {"key": "altitude_hae_m",             "label": "Altitude HAE",              "unit": "m"},
    {"key": "geoid_separation_m",         "label": "Geoid Separation",          "unit": "m"},
    {"key": "speed_knots",                "label": "Speed",                     "unit": "knots"},
    {"key": "track_true_deg",             "label": "True Track",                "unit": "°"},
    {"key": "magnetic_variation_deg",     "label": "Magnetic Variation",        "unit": "°"},
    {"key": "differential_age_s",         "label": "Differential Age",           "unit": "s"},
    {"key": "differential_station_id",    "label": "Differential Station ID",    "type": "string"},
    {"key": "satellites_used",            "label": "Satellites Used",            "type": "int"},
    {"key": "satellites_visible",         "label": "Satellites Visible",         "type": "int"},
    {"key": "signals_visible",            "label": "Signals Visible",            "type": "int"},
    {"key": "gps_satellites_used",        "label": "GPS Satellites Used",        "type": "int"},
    {"key": "gps_satellites_visible",     "label": "GPS Satellites Visible",     "type": "int"},
    {"key": "glonass_satellites_used",    "label": "GLONASS Satellites Used",    "type": "int"},
    {"key": "glonass_satellites_visible", "label": "GLONASS Satellites Visible", "type": "int"},
    {"key": "galileo_satellites_used",    "label": "Galileo Satellites Used",    "type": "int"},
    {"key": "galileo_satellites_visible", "label": "Galileo Satellites Visible", "type": "int"},
    {"key": "beidou_satellites_used",     "label": "BeiDou Satellites Used",     "type": "int"},
    {"key": "beidou_satellites_visible",  "label": "BeiDou Satellites Visible",  "type": "int"},
    {"key": "pdop",                       "label": "PDOP",                       "type": "float"},
    {"key": "hdop",                       "label": "HDOP",                       "type": "float"},
    {"key": "vdop",                       "label": "VDOP",                       "type": "float"},
]

_DATA_FIELDS_HK = [
    {"key": "ocxo_locked",       "label": "OCXO Locked",              "type": "bool"},
    {"key": "spi_ok",            "label": "SPI OK",                   "type": "bool"},
    {"key": "mag_ok",            "label": "Magnetometer OK",          "type": "bool"},
    {"key": "imu_ok",            "label": "IMU OK",                   "type": "bool"},
    {"key": "sw_temp_c",         "label": "Switch Temperature",       "unit": "°C"},
    {"key": "mag_temp_c",        "label": "Magnetometer Temperature", "unit": "°C"},
    {"key": "imu_temp_c",        "label": "IMU Temperature",          "unit": "°C"},
    {"key": "imu_active",        "label": "IMU Active",               "type": "bool"},
    {"key": "imu_tilt",          "label": "IMU Tilt Detected",        "type": "bool"},
    {"key": "time_source_label", "label": "Time Source",              "type": "string"},
    {"key": "time_epoch_label",  "label": "Time Epoch",               "type": "string"},
]

# ---- Time enums ----

_TIME_SOURCE_LABELS = {0: "NOTSET", 1: "GNSS", 2: "EXTERNAL"}
_TIME_EPOCH_LABELS  = {0: "NOTSET", 1: "PPS",  2: "NMEA", 3: "IMMEDIATE"}

# ============================================================================
# DERIVED LOOKUPS (computed once from source tables — never duplicated)
# ============================================================================

_ALL_DEVICES     = list(_DEVICES)
_RX_DEVICES      = [d for d in _DEVICES if d.startswith("rx")]
_ATTEN_DB_RANGE  = [0, 31]
_LOG_MODE_OPTS   = list(_LOG_MODES)
_ACC_ODR_OPTIONS = [e["name"] for e in _IMU_ODR_TABLE if e["acc"]]
_GYR_ODR_OPTIONS = [e["name"] for e in _IMU_ODR_TABLE if e["gyr"]]
_VALID_ACC_ODR   = frozenset(_ACC_ODR_OPTIONS)
_VALID_GYR_ODR   = frozenset(_GYR_ODR_OPTIONS)

_PIN_IDX = {
    dev: {e["name"].upper(): e["pin"] for e in info["pins"]}
    for dev, info in _DEVICES.items()
}
_TLC_MAP = {info["tlc"]: dev for dev, info in _DEVICES.items()}

_REG_NAMES = {dev: [e["name"] for e in info["pins"]] for dev, info in _DEVICES.items()}
_REG_PINS  = {
    dev: [{"pin": e["pin"], "name": e["name"], "label": e["label"],
           "rp2040_default": e["rp2040_default"],
           "service_default_override": e["service_default_override"],
           "0": e["0"], "1": e["1"]} for e in info["pins"]]
    for dev, info in _DEVICES.items()
}

# ============================================================================
# STATE (derived from data-field tables — single source of truth)
# ============================================================================

def _make_buf(fields, extras=None, timestamps=("timestamp",)):
    """Buffer whose key order is the authoritative CSV/JSON column order.

    Parsers must .update() these, never rebind them: a replacement dict can reorder
    keys and silently misalign every row against the import-time _CSV_HEADER.
    """
    buf = {k: None for k in timestamps}
    for f in fields:
        buf[f["key"]] = None
    if extras:
        buf.update(extras)
    buf["service_timestamp"] = None
    return buf


def _decode_dev_regs(dev, bits):
    out = {}
    for e in _DEVICES[dev]["pins"]:
        p = e["pin"]
        v = None
        if p < len(bits):
            try:
                v = int(bits[p])
            except (TypeError, ValueError):
                pass
        out[e["name"]] = {
            "pin": p, "label": e["label"], "value": v,
            "rp2040_default": e["rp2040_default"],
            "service_default_override": e["service_default_override"],
            "meaning": e.get(str(v)) if v in (0, 1) else None,
        }
    return out


def _attenuation_db_from_bits(bits):
    try:
        values = [int(bits[idx]) for idx in range(4, 9)]
    except (TypeError, ValueError):
        return None
    if any(value not in (0, 1) for value in values):
        return None
    return sum(value << idx for idx, value in enumerate(values))


def _attenuation_db_status():
    return {
        dev: _attenuation_db_from_bits(_reg["registers"][_DEVICES[dev]["tlc"]])
        for dev in _RX_DEVICES
    }


def _service_default_register_commands(include_readback=True):
    cmds = []
    for dev, info in _DEVICES.items():
        slots = ["x"] * 10
        for e in info["pins"]:
            slots[e["pin"]] = str(int(e["service_default_override"]))
        cmds.append(_nmea_cksum(f"${info['prefix']},0,{','.join(slots)}*"))
    if include_readback:
        for info in _DEVICES.values():
            cmds.append(_nmea_cksum(info["query"]))
    return cmds


def _enum_label(m, v):
    return m.get(v, f"UNKNOWN({v})")


def _apply_register_fields(tlc, fields):
    source = "snapshot" if len(fields) == 10 else "write_ack"
    if len(fields) == 10:
        bits = [int(b) for b in fields]
    else:
        offset = int(fields[0])
        bits = list(_reg["registers"][tlc])
        for idx, field in enumerate(fields[1:], start=offset):
            if str(field).lower() == "x":
                continue
            bits[idx] = int(field)
    _reg["registers"][tlc] = bits
    _reg["registers_named"][_TLC_MAP[tlc]] = _decode_dev_regs(_TLC_MAP[tlc], bits)
    _reg["registers_timestamp"][tlc] = time.time()
    if source == "snapshot":
        _reg["snapshot_timestamp"][tlc] = _reg["registers_timestamp"][tlc]
    return source


# Module-level state — written by parsers, read by publishers and command handlers.
_buf_gps = _make_buf(_DATA_FIELDS_GPS)
_pending_gps = _make_buf(_DATA_FIELDS_GPS)
_pending_gsa_used = {}
_pending_gsv_visible = {}
_pending_gsv_signals = set()
_buf_imu = _make_buf(_DATA_FIELDS_IMU, timestamps=("acc_timestamp", "gyr_timestamp"))
_buf_mag = _make_buf(_DATA_FIELDS_MAG)
_buf_hk  = _make_buf(_DATA_FIELDS_HK, extras={"time_source": None, "time_epoch": None})

# Flat wide CSV header — one column per buffer field, prefixed by stream name.
# Computed once at import time from the live buffer key order.
_CSV_HEADER = (
    ["snapshot_utc"]
    + [f"gnss_{k}" for k in _buf_gps]
    + [f"mag_{k}"  for k in _buf_mag]
    + [f"imu_{k}"  for k in _buf_imu]
    + [f"hk_{k}"   for k in _buf_hk]
    + ["registers_json"]
)

_reg = {
    "registers":       {info["tlc"]: [None]*10 for info in _DEVICES.values()},
    "registers_named": {dev: _decode_dev_regs(dev, [None]*10) for dev in _DEVICES},
    "registers_timestamp": {info["tlc"]: None for info in _DEVICES.values()},
    "snapshot_timestamp": {info["tlc"]: None for info in _DEVICES.values()},
    "service_timestamp": None,
}

# Unified params — no duplicate standalone dicts.
_params = {
    "imu":   {s["key"]: None for s in _IMU_MODE_SETTINGS} | {"acc_odr": None, "gyr_odr": None},
    "mag":   {k: None for k in _MAG_PARAMS},
    "rates": {"poll_interval_s": _RATE_PARAM["default"]},
    "time":  {"time_source": None, "time_source_label": None,
              "time_epoch": None,  "time_epoch_label": None},
    "last_error": {"status": None, "tlc": None, "fields": []},
}

_gpsd_status = {
    "connected": False,
    "host": None,
    "port": None,
    "configured_device": None,
    "reported_device": None,
    "driver": None,
    "baud": None,
    "cycle_s": None,
    "version": None,
    "watch": None,
    "connected_timestamp": None,
    "disconnected_timestamp": None,
    "last_receive_timestamp": None,
    "last_error": None,
    "raw_stream_enabled": False,
    "raw_stream_mode": None,
    "raw_stream_expires_timestamp": None,
}

_raw_stream_deadline_monotonic = None

_startup_queries_sent = False
_log_rate_changed = None  # Event: set when log rate changes, wakes _emit_csv
_poll_interval_changed = None  # Event: set when poll interval changes, wakes _poll_telem

# GNRMC identifies the start of each receiver epoch. The next distinct RMC commits
# the completed prior epoch; this timeout only flushes the final epoch if input stalls.
_GPS_EPOCH_TIMEOUT_S = 1.5
_gps_epoch_key = None
_gps_epoch_started_monotonic = None
_gps_epoch_active = False
_gps_last_committed_epoch_key = None

# ============================================================================
# DESCRIBE SCHEMAS (built from source tables — zero hand-duplicated args)
# ============================================================================

def _imu_set_args():
    args = {
        "acc_odr": {"type": "string", "required": True, "options": _ACC_ODR_OPTIONS},
        "gyr_odr": {"type": "string", "required": True, "options": _GYR_ODR_OPTIONS},
    }
    for s in _IMU_MODE_SETTINGS:
        args[s["key"]] = {"type": "int", "options": [0, 1], "default": s["default"]}
    return args


def _mag_set_args():
    return {k: {"type": "int", "required": True, "range": p["range"], "description": p["label"]}
            for k, p in _MAG_PARAMS.items()}


_DESC_REGISTERS = {
    "subtopic": "registers",
    "reference": {
        "devices": _ALL_DEVICES, "rx_devices": _RX_DEVICES,
        "attenuation_db_range": _ATTEN_DB_RANGE,
        "registers_by_device": _REG_NAMES, "register_pins": _REG_PINS,
    },
    "commands": {
        "set_register": {
            "description": "Set a single named register to 0 or 1.",
            "arguments": {
                "device":   {"type": "string", "required": True, "options": _ALL_DEVICES},
                "register": {"type": "string", "required": True, "description": "See reference.registers_by_device."},
                "value":    {"type": "int", "required": True, "options": [0, 1]},
            },
        },
        "set_registers": {
            "description": "Set one or more named registers per device. Omitted registers or 'x' preserve current RP2040 state.",
            "arguments": {"<device>": {"type": "dict", "description": "Keys = register names, values = 0|1|'x'."}},
            "example": {"misc": {"TRIG_TX_SRC_SEL": 1, "TEST_LED": 0}, "rxd": {"CHAN_BIAS_EN": 1}},
        },
        "set_attenuation_db": {
            "description": "Set RX attenuator (0-31 dB, 5-bit binary).",
            "arguments": {
                "device": {"type": "string", "required": True, "options": _RX_DEVICES},
                "db":     {"type": "int", "required": True, "range": _ATTEN_DB_RANGE},
            },
        },
        "get_registers": {
            "description": "Query shadow register state from firmware.",
            "arguments": {"device": {"type": "string", "required": False, "default": "all",
                                     "options": _ALL_DEVICES + ["all"]}},
        },
        "get_attenuation_db": {
            "description": "Query RX attenuator state and publish derived dB values in status.",
            "arguments": {"device": {"type": "string", "required": False, "default": "all",
                                     "options": _RX_DEVICES + ["all"]}},
        },
        "reset_registers_to_service_default": {
            "description": "Apply service register defaults to all register devices and query readback.",
            "arguments": {},
        },
    },
}

_DESC_IMU = {
    "subtopic": "imu",
    "reference": {
        "odr_table": _IMU_ODR_TABLE, "mode_settings": _IMU_MODE_SETTINGS,
        "data_fields": _DATA_FIELDS_IMU,
    },
    "commands": {
        "set_imu":        {"description": "Set all IMU params at once (firmware requires all).",
                           "arguments": _imu_set_args()},
        "set_acc_odr":    {"description": "Set accelerometer ODR only (read-modify-write).",
                           "arguments": {"odr": {"type": "string", "required": True, "options": _ACC_ODR_OPTIONS}}},
        "set_gyr_odr":    {"description": "Set gyroscope ODR only (read-modify-write).",
                           "arguments": {"odr": {"type": "string", "required": True, "options": _GYR_ODR_OPTIONS}}},
        **{f"set_{s['key']}": {
            "description": f"Set {s['label'].lower()} (read-modify-write).",
            "arguments": {"value": {"type": "int", "options": [0, 1]}},
        } for s in _IMU_MODE_SETTINGS},
        "get_imu_params": {"description": "Query current IMU params from firmware.", "arguments": {}},
    },
    "note": "Firmware re-initializes the entire IMU on every set. Partial commands use cached state. set_rate/get_rate are disabled; use afecontrol/command/polling.",
}

_DESC_MAG = {
    "subtopic": "mag",
    "reference": {"params": _MAG_PARAMS, "data_fields": _DATA_FIELDS_MAG},
    "commands": {
        "set_mag":          {"description": "Set both mag params at once (firmware requires both).",
                             "arguments": _mag_set_args()},
        "set_cycle_count":  {"description": "Set cycle count only (read-modify-write).",
                             "arguments": {"ccr": {"type": "int", "range": _MAG_PARAMS["ccr"]["range"]}}},
        "set_update_rate":  {"description": "Set update rate only (read-modify-write).",
                             "arguments": {"updr": {"type": "int", "range": _MAG_PARAMS["updr"]["range"]}}},
        "get_mag_params":   {"description": "Query current mag params from firmware.", "arguments": {}},
    },
    "note": "Firmware re-initializes the magnetometer on every set. set_rate/get_rate are disabled; use afecontrol/command/polling.",
}

_DESC_HK = {
    "subtopic": "hk",
    "reference": {"data_fields": _DATA_FIELDS_HK},
    "commands": {},
    "note": "set_rate/get_rate are disabled; use afecontrol/command/polling.",
}

_DESC_GPS = {
    "subtopic": "gps",
    "reference": {"data_fields": _DATA_FIELDS_GPS},
    "commands": {
        "start_raw_stream": {
            "description": "Publish a non-retained diagnostic GPSD stream for a bounded lease.",
            "arguments": {
                "duration_s": {"type": "float", "minimum": 1,
                               "maximum": _RAW_STREAM_MAX_DURATION_S,
                               "default": _RAW_STREAM_DEFAULT_DURATION_S},
                "mode": {"type": "string", "options": ["gnss", "all"], "default": "gnss"},
            },
        },
        "stop_raw_stream": {
            "description": "End the active raw diagnostic stream lease.",
            "arguments": {},
        },
    },
}

_DESC_TIME = {
    "subtopic": "time",
    "reference": {"time_source_options": _TIME_SOURCE_LABELS, "time_epoch_options": _TIME_EPOCH_LABELS},
    "commands": {
        "configure":           {"description": "Apply time source and epoch as one ordered operation.",
                                "arguments": {
                                    "source": {"type": "string", "options": ["gnss", "external"]},
                                    "epoch": {"type": "string", "options": ["pps", "nmea", "immediate"]},
                                    "timestamp": {"type": "int", "optional": True},
                                }},
        "get_time_params":     {"description": "Query current time source/epoch from firmware.", "arguments": {}},
    },
}

_DESC_LOGGING = {
    "subtopic": "logging",
    "reference": {"log_mode_options": _LOG_MODE_OPTS},  # log_rate_current added dynamically in _send_announce
    "commands": {
        "configure":            {"description": "Validate and apply telemetry logging configuration.",
                                 "arguments": {
                                     "enabled": {"type": "boolean"},
                                     "path": {"type": "string"},
                                     "rate_s": {"type": "float", "minimum": 0},
                                 }},
        "get_log_status":       {"description": "Get current logging state.",     "arguments": {}},
        "set_service_log_mode": {"description": "Set script diagnostic verbosity.",
                                 "arguments": {"mode": {"type": "string", "options": _LOG_MODE_OPTS}}},
        "get_service_log_mode": {"description": "Get current script diagnostic verbosity.", "arguments": {}},
    },
}

_DESC_POLLING = {
    "subtopic": "polling",
    "reference": {"poll_interval_range": _RATE_PARAM["range"], "disabled_when_zero": True},
    "note": "Controls the single effective telemetry polling interval for IMU, MAG, and HK data (Mode 13 workaround). All telemetry streams are polled at the same interval.",
    "commands": {
        "set_interval": {"description": "Set telemetry polling interval (applies to IMU, MAG, and HK).",
                         "arguments": {"n": {"type": "int", "range": _RATE_PARAM["range"],
                                             "description": _RATE_PARAM["description"]}}},
        "get_interval": {"description": "Get current telemetry polling interval and rate configuration.", "arguments": {}},
    },
}

_DESC_SERVICE = {
    "subtopic": "(base)",
    "commands": {
        "status":   {"description": "Publish current service status.", "arguments": {}},
        "describe": {"description": "Return this schema.",             "arguments": {}},
        "refresh": {"description": "Refresh firmware parameters and telemetry.", "arguments": {}},
        "telem_dump": {"description": "Trigger one-shot telemetry dump from firmware.", "arguments": {}},
    },
}

_DESCRIBE = {
    "":          _DESC_SERVICE,
    "registers": _DESC_REGISTERS,
    "imu":       _DESC_IMU,
    "mag":       _DESC_MAG,
    "gps":       _DESC_GPS,
    "hk":        _DESC_HK,
    "time":      _DESC_TIME,
    "logging":   _DESC_LOGGING,
    "polling":   _DESC_POLLING,
}


def _command_topic_map(base):
    return {(base if s == "" else f"{base}/{s}"): list(d.get("commands", {}))
            for s, d in _DESCRIBE.items()}


# ============================================================================
# NMEA HELPERS
# ============================================================================

def _nmea_xor(data):
    r = 0
    for ch in data:
        r ^= ord(ch)
    return r


def _nmea_cksum(pkt):
    d, s = pkt.find("$"), pkt.rfind("*")
    if d == -1 or s == -1 or d >= s:
        raise RuntimeError(f"Malformed NMEA: {pkt!r}")
    return pkt + f"{_nmea_xor(pkt[d+1:s]):02X}"


def _nmea_verify(pkt):
    try:
        s = pkt.rfind("*")
        d = pkt.find("$")
        if s == -1 or d == -1 or d >= s or s + 3 > len(pkt):
            return False
        return _nmea_xor(pkt[d+1:s]) == int(pkt[s+1:s+3], 16)
    except (ValueError, IndexError):
        return False


def _nmea_to_epoch(t, d):
    """UTC epoch from NMEA time (hhmmss[.sss]) and date (ddmmyy), sub-second preserved."""
    whole = datetime(int(d[4:6])+2000, int(d[2:4]), int(d[0:2]),
                     int(t[0:2]), int(t[2:4]), int(t[4:6]),
                     tzinfo=timezone.utc).timestamp()
    frac = float(t[6:]) if len(t) > 6 and t[6] == "." else 0.0
    return whole + frac


def _ddmm_to_dec(ddmm, hemi):
    raw = float(ddmm)
    deg = int(raw / 100)
    dec = deg + (raw - deg * 100) / 60.0
    return round(-dec if hemi in ("S", "W") else dec, 6)


# ============================================================================
# COMMAND HANDLERS
# ============================================================================

def _cmd_registers(task_name, args):
    cmds = []
    if task_name == "set_register":
        dev = str(args.get("device", "")).lower().strip()
        reg = str(args.get("register", "")).upper().strip()
        val = int(args["value"])
        if dev not in _DEVICES:
            raise ValueError(f"Unknown device: {dev!r}. Valid: {_ALL_DEVICES}")
        idx = _PIN_IDX[dev].get(reg)
        if idx is None:
            raise ValueError(f"Unknown register {reg!r} on {dev!r}. Valid: {list(_PIN_IDX[dev])}")
        if val not in (0, 1):
            raise ValueError(f"value must be 0 or 1, got {val}")
        cmds.append(_nmea_cksum(f"${_DEVICES[dev]['prefix']},{idx},{val}*"))

    elif task_name == "set_attenuation_db":
        dev = str(args["device"]).lower().strip()
        db = int(args["db"])
        if dev not in _DEVICES or not dev.startswith("rx"):
            raise ValueError(f"{dev!r} is not an RX device. Valid: {_RX_DEVICES}")
        if not 0 <= db <= 31:
            raise ValueError(f"attenuation must be 0..31 dB, got {db}")
        cmds.append(_nmea_cksum(f"${_DEVICES[dev]['prefix']},4,{','.join(str((db>>i)&1) for i in range(5))}*"))

    elif task_name == "get_registers":
        dev = str(args.get("device", "all")).lower().strip()
        targets = _DEVICES.values() if dev in ("all", "") else [_DEVICES[dev]] if dev in _DEVICES else None
        if targets is None:
            raise ValueError(f"Unknown device: {dev!r}. Valid: {_ALL_DEVICES + ['all']}")
        for info in targets:
            cmds.append(_nmea_cksum(info["query"]))

    elif task_name == "get_attenuation_db":
        dev = str(args.get("device", "all")).lower().strip()
        targets = _RX_DEVICES if dev in ("all", "") else [dev] if dev in _RX_DEVICES else None
        if targets is None:
            raise ValueError(f"Unknown RX device: {dev!r}. Valid: {_RX_DEVICES + ['all']}")
        for target in targets:
            cmds.append(_nmea_cksum(_DEVICES[target]["query"]))

    elif task_name == "reset_registers_to_service_default":
        cmds.extend(_service_default_register_commands(include_readback=True))

    else:
        raise ValueError(f"Unknown registers command: {task_name!r}")
    return cmds


def _build_imu_nmea(p):
    mode_csv = ",".join(str(p[s["key"]]) for s in _IMU_MODE_SETTINGS)
    return _nmea_cksum(f"$PMITIMU,{p['acc_odr']},{p['gyr_odr']},{mode_csv}*")


def _require_known(params, label):
    if not any(v is not None for v in params.values()):
        raise ValueError(f"{label} params not yet known — send query first")


def _cmd_imu(task_name, args):
    cmds = []
    p = _params["imu"]

    if task_name == "get_imu_params":
        cmds.append(_nmea_cksum("$PMITIM?*"))

    elif task_name == "set_imu":
        acc = str(args["acc_odr"])
        gyr = str(args["gyr_odr"])
        if acc not in _VALID_ACC_ODR:
            raise ValueError(f"Invalid acc_odr {acc!r}. Valid: {_ACC_ODR_OPTIONS}")
        if gyr not in _VALID_GYR_ODR:
            raise ValueError(f"Invalid gyr_odr {gyr!r}. Valid: {_GYR_ODR_OPTIONS}")
        mode_vals = [int(args.get(s["key"], s["default"])) for s in _IMU_MODE_SETTINGS]
        cmds.append(_nmea_cksum(f"$PMITIMU,{acc},{gyr},{','.join(str(v) for v in mode_vals)}*"))

    elif task_name == "set_acc_odr":
        _require_known(p, "IMU")
        odr = str(args["odr"])
        if odr not in _VALID_ACC_ODR:
            raise ValueError(f"Invalid ODR {odr!r}. Valid: {_ACC_ODR_OPTIONS}")
        cmds.append(_build_imu_nmea({**p, "acc_odr": odr}))

    elif task_name == "set_gyr_odr":
        _require_known(p, "IMU")
        odr = str(args["odr"])
        if odr not in _VALID_GYR_ODR:
            raise ValueError(f"Invalid ODR {odr!r}. Valid: {_GYR_ODR_OPTIONS}")
        cmds.append(_build_imu_nmea({**p, "gyr_odr": odr}))

    elif task_name in {f"set_{s['key']}" for s in _IMU_MODE_SETTINGS}:
        _require_known(p, "IMU")
        cmds.append(_build_imu_nmea({**p, task_name[4:]: int(args["value"])}))

    else:
        raise ValueError(f"Unknown IMU command: {task_name!r}")
    return cmds


def _cmd_mag(task_name, args):
    cmds = []
    p = _params["mag"]

    if task_name == "get_mag_params":
        cmds.append(_nmea_cksum("$PMITMG?*"))

    elif task_name == "set_mag":
        cmds.append(_nmea_cksum(f"$PMITMGS,{int(args['ccr'])},{int(args['updr'])}*"))

    elif task_name == "set_cycle_count":
        _require_known(p, "Mag")
        cmds.append(_nmea_cksum(f"$PMITMGS,{int(args['ccr'])},{p['updr']}*"))

    elif task_name == "set_update_rate":
        _require_known(p, "Mag")
        cmds.append(_nmea_cksum(f"$PMITMGS,{p['ccr']},{int(args['updr'])}*"))

    else:
        raise ValueError(f"Unknown mag command: {task_name!r}")
    return cmds


def _cmd_hk(task_name, args):
    raise ValueError(f"Unknown hk command: {task_name!r}")


def _cmd_time(task_name, args):
    if task_name == "configure":
        source = str(args.get("source", "")).lower().strip()
        epoch = str(args.get("epoch", "")).lower().strip()
        if source not in {"gnss", "external"}:
            raise ValueError("source must be 'gnss' or 'external'")
        if epoch not in {"pps", "nmea", "immediate"}:
            raise ValueError("epoch must be 'pps', 'nmea', or 'immediate'")
        commands = [
            _nmea_cksum("$PMITTSG*" if source == "gnss" else "$PMITTSE*")
        ]
        if epoch == "nmea":
            commands.append(_nmea_cksum("$PMITTEN*"))
        else:
            try:
                timestamp = int(args["timestamp"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"timestamp is required for {epoch} epoch") from exc
            command = "$PMITTEP" if epoch == "pps" else "$PMITTEI"
            commands.append(_nmea_cksum(f"{command},{timestamp}*"))
        commands.append(_nmea_cksum("$PMITTP?*"))
        return commands
    _map = {
        "get_time_params":     lambda: _nmea_cksum("$PMITTP?*"),
    }
    if task_name not in _map:
        raise ValueError(f"Unknown time command: {task_name!r}")
    return [_map[task_name]()]


_HANDLERS = {
    "registers": _cmd_registers, "imu": _cmd_imu,
    "mag": _cmd_mag, "hk": _cmd_hk, "time": _cmd_time,
}

def _effective_poll_interval_s():
    v = _params["rates"].get("poll_interval_s")
    return int(v) if v is not None else 0


def _set_poll_interval_s(n):
    _params["rates"]["poll_interval_s"] = int(n)

# ============================================================================
# GPSD INTERFACE
# ============================================================================

class GPSDTransport:
    """Raw GPSD stream and serialized writes to the AFE UART."""

    def __init__(self, host, port, device, socket_path=_GPSD_SOCK_PATH):
        self.host = host
        self.port = port
        self.device = device
        self.socket_path = socket_path
        self._send_lock = anyio.Lock()

    async def open_raw_stream(self):
        stream = await anyio.connect_tcp(self.host, self.port)
        await stream.send(_GPSD_WATCH_CMD)
        return stream

    async def iter_lines(self, stream):
        buffer = b""
        while True:
            try:
                chunk = await stream.receive(4096)
            except anyio.EndOfStream:
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    yield line

    async def send_firmware_command(self, nmea):
        async with self._send_lock:
            await anyio.to_thread.run_sync(self._send_firmware_command_sync, nmea)

    def _send_firmware_command_sync(self, nmea):
        command = nmea if nmea.endswith("\r\n") else nmea + "\r\n"
        request = f"&{self.device}={command.encode('ascii').hex().upper()}\n".encode("ascii")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.socket_path)
            sock.sendall(request)
            reply = sock.recv(4096).decode("ascii").strip()
        finally:
            sock.close()
        if reply != "OK":
            raise RuntimeError(f"gpsd: unexpected reply {reply!r} for {nmea!r}")


# ============================================================================
# NMEA PARSERS
# ============================================================================

def _parse_gnrmc(line):
    try:
        parts = line.split("*")[0].split(",")
        fix = len(parts) > 2 and parts[2] == "A"
        # timestamp is the time *of this fix*; without a fix there isn't one, so it is
        # cleared rather than left carrying the last good value.
        u = {"fix_valid": fix, "timestamp": None, "lat": None, "lon": None,
             "service_timestamp": time.time()}
        if fix and len(parts) >= 10 and parts[1] and parts[9]:
            try: u["timestamp"] = _nmea_to_epoch(parts[1], parts[9])
            except ValueError: pass
        if fix and len(parts) > 4 and parts[3] and parts[4]:
            try: u["lat"] = _ddmm_to_dec(parts[3], parts[4])
            except ValueError: pass
        if fix and len(parts) > 6 and parts[5] and parts[6]:
            try: u["lon"] = _ddmm_to_dec(parts[5], parts[6])
            except ValueError: pass
        if len(parts) > 7 and parts[7]:
            try: u["speed_knots"] = float(parts[7])
            except ValueError: pass
        u["track_true_deg"] = None
        if len(parts) > 8 and parts[8]:
            try: u["track_true_deg"] = float(parts[8])
            except ValueError: pass
        u["magnetic_variation_deg"] = None
        if len(parts) > 11 and parts[10]:
            try:
                variation = float(parts[10])
                u["magnetic_variation_deg"] = -variation if parts[11] == "W" else variation
            except ValueError:
                pass
        u["faa_mode"] = parts[12] or None if len(parts) > 12 else None
        u["navigation_status"] = parts[13] or None if len(parts) > 13 else None
        _pending_gps.update(u)
    except Exception:
        logger.debug(f"$GNRMC parse error: {line!r}", exc_info=True)


def _parse_gngga(line):
    try:
        parts = line.split("*")[0].split(",")
        u = {"altitude_msl_m": None, "altitude_hae_m": None,
             "geoid_separation_m": None, "differential_age_s": None,
             "differential_station_id": None}
        for idx, key, cast in [(6, "fix_quality", int), (7, "satellites_used", int),
                                (8, "hdop", float), (9, "altitude_msl_m", float),
                                (11, "geoid_separation_m", float),
                                (13, "differential_age_s", float),
                                (14, "differential_station_id", str)]:
            if len(parts) > idx and parts[idx]:
                try: u[key] = cast(parts[idx])
                except ValueError: pass
        if u["altitude_msl_m"] is not None and u["geoid_separation_m"] is not None:
            u["altitude_hae_m"] = u["altitude_msl_m"] + u["geoid_separation_m"]
        _pending_gps.update(u)
    except Exception:
        logger.debug(f"$GNGGA parse error: {line!r}", exc_info=True)


def _parse_gngsa(line):
    try:
        parts = line.split("*")[0].split(",")
        u = {"fix_selection": parts[1] or None if len(parts) > 1 else None}
        for idx, key, cast in [(2, "fix_mode", int), (15, "pdop", float),
                               (16, "hdop", float), (17, "vdop", float)]:
            if len(parts) > idx and parts[idx]:
                try: u[key] = cast(parts[idx])
                except ValueError: pass
        system_id = int(parts[18]) if len(parts) > 18 and parts[18] else None
        constellation = {1: "gps", 2: "glonass", 3: "galileo", 4: "beidou"}.get(system_id)
        if constellation:
            _pending_gsa_used[constellation] = {sv for sv in parts[3:15] if sv}
            u[f"{constellation}_satellites_used"] = len(_pending_gsa_used[constellation])
        _pending_gps.update(u)
    except Exception:
        logger.debug(f"$GNGSA parse error: {line!r}", exc_info=True)


def _parse_gsv(line):
    try:
        parts = line.split("*")[0].split(",")
        talker = parts[0][1:3]
        constellation = {"GP": "gps", "GL": "glonass", "GA": "galileo",
                         "GB": "beidou", "BD": "beidou"}.get(talker)
        if not constellation:
            return
        observations = parts[4:]
        signal_id = observations[-1] if len(observations) % 4 == 1 else None
        if signal_id is not None:
            observations = observations[:-1]
        visible = _pending_gsv_visible.setdefault(constellation, set())
        for idx in range(0, len(observations) - 3, 4):
            satellite_id = observations[idx]
            if not satellite_id:
                continue
            visible.add(satellite_id)
            _pending_gsv_signals.add((constellation, satellite_id, signal_id))
        _pending_gps[f"{constellation}_satellites_visible"] = len(visible)
        _pending_gps["satellites_visible"] = sum(len(ids) for ids in _pending_gsv_visible.values())
        _pending_gps["signals_visible"] = len(_pending_gsv_signals)
    except Exception:
        logger.debug(f"GSV parse error: {line!r}", exc_info=True)


def _parse_pmitmag(line):
    p = line.split("*")[0].split(",")
    return {"timestamp": int(p[1]), "mag_x": float(p[2]), "mag_y": float(p[3]),
            "mag_z": float(p[4]), "service_timestamp": time.time()}


def _parse_pmitacc(line):
    p = line.split("*")[0].split(",")
    return {"acc_timestamp": int(p[1]), "acc_x": float(p[2]), "acc_y": float(p[3]),
            "acc_z": float(p[4]), "service_timestamp": time.time()}


def _parse_pmitgyr(line):
    p = line.split("*")[0].split(",")
    return {"gyr_timestamp": int(p[1]), "gyr_x": float(p[2]), "gyr_y": float(p[3]),
            "gyr_z": float(p[4]), "service_timestamp": time.time()}


def _parse_pmithk(line):
    p = line.split("*")[0].split(",")
    ts, te = int(p[11]), int(p[12])
    return {
        "timestamp": int(p[1]),
        "ocxo_locked": bool(int(p[2])),  "spi_ok": bool(int(p[3])),
        "mag_ok": bool(int(p[4])),       "imu_ok": bool(int(p[5])),
        "sw_temp_c": float(p[6]),        "mag_temp_c": float(p[7]),
        "imu_temp_c": float(p[8]),
        "imu_active": bool(int(p[9])),   "imu_tilt": bool(int(p[10])),
        "time_source": ts, "time_source_label": _enum_label(_TIME_SOURCE_LABELS, ts),
        "time_epoch": te,  "time_epoch_label":  _enum_label(_TIME_EPOCH_LABELS, te),
        "service_timestamp": time.time(),
    }


def _parse_pmitsr(line):
    try:
        parts = line.split("*")[0].split(",")
        if len(parts) < 3:
            return None
        status, tlc, fields = str(parts[1]), parts[2], parts[3:]

        if status != "0":
            logger.debug(f"$PMITSR error status={status!r} tlc={tlc!r}")
            _params["last_error"] = {"status": status, "tlc": tlc, "fields": fields}
            _reg["service_timestamp"] = time.time()
            return None

        if tlc in _TLC_MAP:
            _apply_register_fields(tlc, fields)
            parsed = "registers"

        elif tlc == "IM?" and len(fields) >= 5:
            _params["imu"].update(acc_odr=fields[0], gyr_odr=fields[1],
                                  ahiperf=int(fields[2]), aulp=int(fields[3]), glp=int(fields[4]))
            parsed = "imu"

        elif tlc == "MG?" and len(fields) >= 2:
            _params["mag"].update(ccr=int(fields[0]), updr=int(fields[1]))
            parsed = "mag"

        elif tlc == "R?" and len(fields) >= 3:
            # Firmware reports per-stream rates, but service polling uses one owned interval.
            _params["firmware_rates"] = {
                "telem_rate_s": int(fields[0]),
                "mag_rate_s": int(fields[1]),
                "imu_rate_s": int(fields[2]),
            }
            parsed = "rates"

        elif tlc == "TP?" and len(fields) >= 2:
            ts, te = int(fields[0]), int(fields[1])
            _params["time"] = {
                "time_source": ts, "time_source_label": _enum_label(_TIME_SOURCE_LABELS, ts),
                "time_epoch": te,  "time_epoch_label":  _enum_label(_TIME_EPOCH_LABELS, te),
            }
            parsed = "time"

        else:
            _params[tlc] = fields
            parsed = tlc

        _reg["service_timestamp"] = time.time()
        return parsed
    except Exception:
        logger.debug(f"$PMITSR parse error: {line!r}", exc_info=True)
        return None


# ============================================================================
# GPSD MONITOR + NMEA DISPATCH
# ============================================================================

async def _monitor_gpsd(client, service):
    global _startup_queries_sent
    logger.info(f"Connecting to gpsd at {service.gpsd_host}:{service.gpsd_port}")
    while True:
        try:
            async with await service.gpsd.open_raw_stream() as stream:
                now = time.time()
                _gpsd_status.update(
                    connected=True,
                    host=service.gpsd_host,
                    port=service.gpsd_port,
                    configured_device=service.str_device,
                    connected_timestamp=now,
                    last_error=None,
                )
                await _send_gpsd_status(client, service)
                await _pub_event(client, service, "connection",
                                 {"type": "gpsd_connected",
                                  "message": f"Connected to gpsd at {service.gpsd_host}:{service.gpsd_port}"})

                if not _startup_queries_sent:
                    ok = True
                    # Apply service-owned register overrides at startup before normal state queries.
                    startup_register_defaults = _service_default_register_commands(include_readback=True)
                    for idx, cmd in enumerate(startup_register_defaults):
                        try:
                            await service.gpsd.send_firmware_command(cmd)
                            logger.info(f"Startup register default command sent: {cmd}")
                            if idx < (len(startup_register_defaults) - 1):
                                await anyio.sleep(_REGISTER_QUERY_GAP_S)
                        except Exception as exc:
                            ok = False
                            logger.warning(f"Startup register default command failed for {cmd}: {exc}")

                    startup_queries = [
                        "$PMITIM?*",
                        "$PMITMG?*",
                        "$PMITR?*",
                        "$PMITTP?*",
                        *[i["query"] for i in _DEVICES.values()],
                    ]
                    for idx, q in enumerate(startup_queries):
                        try:
                            cmd = _nmea_cksum(q)
                            await service.gpsd.send_firmware_command(cmd)
                            logger.info(f"Startup query sent: {cmd}")
                            # Avoid bursting startup queries too quickly.
                            if idx < len(startup_queries) - 1:
                                await anyio.sleep(_REGISTER_QUERY_GAP_S)
                        except Exception as exc:
                            ok = False
                            logger.warning(f"Startup query failed for {q}: {exc}")
                    _startup_queries_sent = ok

                async for line in service.gpsd.iter_lines(stream):
                    _gpsd_status["last_receive_timestamp"] = time.time()
                    if _update_gpsd_metadata(line):
                        await _send_gpsd_status(client, service)
                    await _publish_raw_line(client, service, line)
                    await _dispatch_nmea(client, service, line)
                logger.warning("gpsd TCP stream closed; reconnecting in 5s")
                _clear_raw_stream_lease()
                await _commit_gps_epoch(client, service)
                _gpsd_status.update(
                    connected=False,
                    disconnected_timestamp=time.time(),
                    last_error="gpsd TCP stream closed",
                )
                await _send_gpsd_status(client, service)
                await _pub_event(client, service, "connection",
                                 {"type": "gpsd_disconnected", "message": "gpsd TCP stream closed"})
        except OSError as exc:
            logger.warning(f"gpsd connection error: {exc}; retrying in 5s")
            _clear_raw_stream_lease()
            _gpsd_status.update(
                connected=False,
                host=service.gpsd_host,
                port=service.gpsd_port,
                configured_device=service.str_device,
                disconnected_timestamp=time.time(),
                last_error=str(exc),
            )
            await _send_gpsd_status(client, service)
            await _pub_event(client, service, "connection", {"type": "gpsd_error", "message": str(exc)})
        await anyio.sleep(5)


def _update_gpsd_metadata(line):
    if not line.startswith("{"):
        return False
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(message, dict):
        return False

    message_class = str(message.get("class", "")).upper()
    updates = {}
    if message_class == "VERSION":
        release = message.get("release")
        revision = message.get("rev")
        if release:
            updates["version"] = f"{release} ({revision})" if revision else str(release)
    elif message_class == "DEVICE":
        updates = {
            "reported_device": message.get("path"),
            "driver": message.get("driver"),
            "baud": message.get("bps"),
            "cycle_s": message.get("cycle"),
        }
    elif message_class == "DEVICES":
        devices = message.get("devices")
        if isinstance(devices, list):
            device = next((item for item in devices if isinstance(item, dict)), None)
            if device is not None:
                updates = {
                    "reported_device": device.get("path"),
                    "driver": device.get("driver"),
                    "baud": device.get("bps"),
                    "cycle_s": device.get("cycle"),
                }
    elif message_class == "WATCH":
        updates["watch"] = {
            key: message.get(key)
            for key in ("enable", "json", "nmea", "raw", "scaled", "timing", "split24", "pps")
            if key in message
        }

    updates = {key: value for key, value in updates.items() if value is not None}
    changed = any(_gpsd_status.get(key) != value for key, value in updates.items())
    _gpsd_status.update(updates)
    return changed


def _clear_raw_stream_lease():
    global _raw_stream_deadline_monotonic
    _raw_stream_deadline_monotonic = None
    _gpsd_status.update(
        raw_stream_enabled=False,
        raw_stream_mode=None,
        raw_stream_expires_timestamp=None,
    )


def _raw_stream_active():
    if _raw_stream_deadline_monotonic is None:
        return False
    if time.monotonic() >= _raw_stream_deadline_monotonic:
        _clear_raw_stream_lease()
        return False
    return True


def _raw_line_allowed(line, mode):
    if mode == "all":
        return True
    return line.startswith("{") or (line.startswith("$") and not line.startswith("$PMIT"))


async def _publish_raw_line(client, service, line):
    if not _raw_stream_active():
        return
    mode = _gpsd_status["raw_stream_mode"]
    if not _raw_line_allowed(line, mode):
        return
    await client.publish(service.topic_data_raw, msgspec.json.encode({
        "timestamp": time.time(),
        "mode": mode,
        "line": line,
    }), retain=False)


async def _dispatch_nmea(client, service, line):
    global _buf_gps, _buf_mag, _buf_imu, _buf_hk

    if line.startswith("$PGPS") or line.startswith("$PGPN"):
        return
    if line.startswith("$") and not _nmea_verify(line):
        logger.debug(f"Bad checksum: {line!r}")
        return

    try:
        sentence = line[3:6] if len(line) >= 6 else ""
        if sentence == "RMC":
            epoch_key = _rmc_epoch_key(line)
            if _gps_epoch_active and epoch_key != _gps_epoch_key:
                await _commit_gps_epoch(client, service)
            if not _gps_epoch_active:
                if epoch_key == _gps_last_committed_epoch_key:
                    return
                _begin_gps_epoch(epoch_key)
            _parse_gnrmc(line)
        elif sentence == "GGA" and _gps_epoch_active:
            _parse_gngga(line)
        elif sentence == "GSA" and _gps_epoch_active:
            _parse_gngsa(line)
        elif sentence == "GSV" and _gps_epoch_active:
            _parse_gsv(line)
        elif line.startswith("$PMITMAG"):
            _buf_mag.update(_parse_pmitmag(line))
            await client.publish(service.topic_data_mag, msgspec.json.encode(_buf_mag))
        elif line.startswith("$PMITACC"):
            _buf_imu.update(_parse_pmitacc(line))
            await client.publish(service.topic_data_imu, msgspec.json.encode(_buf_imu))
        elif line.startswith("$PMITGYR"):
            _buf_imu.update(_parse_pmitgyr(line))
            await client.publish(service.topic_data_imu, msgspec.json.encode(_buf_imu))
        elif line.startswith("$PMITHK"):
            _buf_hk.update(_parse_pmithk(line))
            await client.publish(service.topic_data_hk, msgspec.json.encode(_buf_hk))
        elif line.startswith("$PMITSR"):
            await _handle_pmitsr(client, service, line)
    except Exception:
        logger.debug(f"Dispatch error: {line!r}", exc_info=True)


# ============================================================================
# MQTT PUBLISH
# ============================================================================

_seq = 0
_seq_reg = 0
_seq_imu = 0
_seq_mag = 0
_seq_time = 0
_seq_gpsd = 0


async def _pub_event(client, service, etype, payload):
    await client.publish(f"{service.name}/event/{etype}",
                         msgspec.json.encode({**payload, "timestamp": time.time()}))


async def _send_gpsd_status(client, service):
    global _seq_gpsd
    _seq_gpsd += 1
    await client.publish(service.topic_status_gpsd, msgspec.json.encode({
        "seq": _seq_gpsd,
        "timestamp": time.time(),
        **_gpsd_status,
    }), retain=True)


async def _handle_pmitsr(client, service, line):
    global _seq_reg, _seq_imu, _seq_mag, _seq_time
    parsed = _parse_pmitsr(line)

    parts = line.split("*")[0].split(",")
    if len(parts) < 3:
        return
    sc, tlc = str(parts[1]), parts[2]

    if sc != "0":
        await _pub_event(client, service, "error",
                         {"type": "firmware_error", "status": sc, "tlc": tlc, "fields": parts[3:]})
        return

    if tlc in _TLC_MAP:
        if parsed != "registers":
            return
        _seq_reg += 1
        await client.publish(service.topic_status_registers, msgspec.json.encode({
            "seq": _seq_reg, "timestamp": time.time(),
            "registers": _reg["registers"], "registers_named": _reg["registers_named"],
            "attenuation_db": _attenuation_db_status(),
        }), retain=True)

    elif tlc == "IM?":
        _seq_imu += 1
        await client.publish(service.topic_status_imu, msgspec.json.encode({
            "seq": _seq_imu, "timestamp": time.time(),
            **_params["imu"], "telem_poll_interval_s": _effective_poll_interval_s(),
        }), retain=True)

    elif tlc == "MG?":
        _seq_mag += 1
        await client.publish(service.topic_status_mag, msgspec.json.encode({
            "seq": _seq_mag, "timestamp": time.time(),
            **_params["mag"], "telem_poll_interval_s": _effective_poll_interval_s(),
        }), retain=True)

    elif tlc == "R?":
        _seq_imu += 1
        await client.publish(service.topic_status_imu, msgspec.json.encode({
            "seq": _seq_imu, "timestamp": time.time(),
            **_params["imu"], "telem_poll_interval_s": _effective_poll_interval_s(),
        }), retain=True)
        _seq_mag += 1
        await client.publish(service.topic_status_mag, msgspec.json.encode({
            "seq": _seq_mag, "timestamp": time.time(),
            **_params["mag"], "telem_poll_interval_s": _effective_poll_interval_s(),
        }), retain=True)

    elif tlc == "TP?":
        _seq_time += 1
        await client.publish(service.topic_status_time, msgspec.json.encode({
            "seq": _seq_time, "timestamp": time.time(), **_params["time"],
        }), retain=True)

    if tlc in {"IM?", "MG?", "R?", "TP?"}:
        await _send_status(client, service)


async def _send_announce(client, service):
    describe = dict(_DESCRIBE)
    logging_desc = dict(describe["logging"])
    logging_ref = dict(logging_desc["reference"])
    logging_ref["log_rate_current"] = service.int_telem_rate
    logging_ref["log_path_current"] = service.str_log_dir
    logging_desc["reference"] = logging_ref
    describe["logging"] = logging_desc

    polling_desc = dict(describe["polling"])
    polling_ref = dict(polling_desc["reference"])
    polling_ref["poll_interval_current"] = _effective_poll_interval_s()
    polling_desc["reference"] = polling_ref
    describe["polling"] = polling_desc
    
    await client.publish(service.topic_announce, msgspec.json.encode({
        "title": "AFE service",
        "description": "Control and monitor MEP analog front-end instrument (RP2040)",
        "author": "John Marino <john.marino@colorado.edu>",
        "version": "2.0", "type": "service", "time_started": time.time(),
        # Authoritative column order for every telemetry consumer. Derived from the
        # live buffers, so a consumer that reads this cannot drift from what is
        # published; data_fields alone is not sufficient (it omits the timestamps).
        "schema": {
            "gps": list(_buf_gps), "mag": list(_buf_mag),
            "imu": list(_buf_imu), "hk": list(_buf_hk),
            "registers_devices": [info["tlc"] for info in _DEVICES.values()],
        },
        "topics": {
            "command": service.topic_command, "response": f"{service.name}/response",
            "status": service.topic_status,
            "status_gpsd": service.topic_status_gpsd,
            "data": {"gps": service.topic_data_gps, "imu": service.topic_data_imu,
                     "mag": service.topic_data_mag, "hk": service.topic_data_hk,
                     "raw": service.topic_data_raw},
            "event": f"{service.name}/event",
        },
        "command_subtopics": _command_topic_map(service.topic_command),
        "describe": describe,
    }), retain=True)


async def _send_status(client, service):
    global _seq
    _seq += 1
    await client.publish(service.topic_status, msgspec.json.encode({
        "seq": _seq, "timestamp": time.time(), "state": "online",
        "node_id": service.node_id, "device": service.str_device,
        "telemetry_logging_enabled": service.bool_logging_enabled,
        "telemetry_log_dir": service.str_log_dir,
        "telemetry_log_rate_s": service.int_telem_rate,
        "service_log_mode": _log_mode(),
        "service_telem_workaround_enabled": _USE_SERVICE_TELEM_WORKAROUND,
        "service_telem_poll_interval_s": _effective_poll_interval_s(),
        "parameters": {
            "imu": dict(_params["imu"]),
            "mag": dict(_params["mag"]),
            "time": dict(_params["time"]),
        },
    }), retain=True)


async def _send_response(client, service, resp, cmd=None, subtopic=""):
    if cmd is None:
        cmd = {}
    topic = f"{service.name}/response/{subtopic}" if subtopic else f"{service.name}/response"
    envelope = dict(resp)
    envelope.update(session_id=cmd.get("session_id"), task_name=cmd.get("task_name"),
                    timestamp=time.time())
    await client.publish(topic, msgspec.json.encode(envelope))


# ============================================================================
# COMMAND PROCESSING
# ============================================================================

def _register_value_or_preserve(value):
    if isinstance(value, str) and value.lower().strip() == "x":
        return None
    iv = int(value)
    if iv not in (0, 1):
        raise ValueError(f"register values must be 0, 1, or 'x', got {value!r}")
    return iv


def _normalize_set_registers_args(args):
    if not isinstance(args, dict):
        raise ValueError(f"set_registers expects object arguments, got {type(args).__name__}")
    if not args:
        raise ValueError("set_registers requires at least one device payload")

    normalized = {}
    for dk, rd in args.items():
        dev = str(dk).lower().strip()
        if dev not in _DEVICES:
            raise ValueError(f"Unknown device: {dev!r}. Valid: {_ALL_DEVICES}")
        if not isinstance(rd, dict):
            raise ValueError(f"Expected dict for {dev!r}, got {type(rd).__name__}")

        slots = [None] * 10
        for rn, value in rd.items():
            rn_u = str(rn).upper().strip()
            idx = _PIN_IDX[dev].get(rn_u)
            if idx is None:
                raise ValueError(f"Unknown register {rn_u!r} on {dev!r}. Valid: {list(_PIN_IDX[dev])}")
            slots[idx] = _register_value_or_preserve(value)
        normalized[dev] = slots
    return normalized


def _full_register_write_cmd(dev, bits):
    return _nmea_cksum(f"${_DEVICES[dev]['prefix']},0,{','.join(str(int(b)) for b in bits)}*")


def _register_cache_bits(tlc):
    bits = _reg["registers"][tlc]
    if any(bit is None for bit in bits):
        raise ValueError(f"No complete cached register state for {_TLC_MAP[tlc]!r}; query readback unavailable")
    return list(bits)


async def _wait_for_register_snapshot(tlc, previous_ts):
    deadline = time.monotonic() + _REGISTER_RESOLVE_TIMEOUT_S
    while time.monotonic() < deadline:
        current_ts = _reg["snapshot_timestamp"].get(tlc)
        if current_ts is not None and (previous_ts is None or current_ts > previous_ts):
            return True
        await anyio.sleep(0.05)
    return False


async def _resolve_set_registers_commands(client, service, args):
    normalized = _normalize_set_registers_args(args)
    cmds = []
    for dev, requested in normalized.items():
        if all(value is not None for value in requested):
            cmds.append(_full_register_write_cmd(dev, requested))
            continue

        tlc = _DEVICES[dev]["tlc"]
        previous_ts = _reg["snapshot_timestamp"].get(tlc)
        query_cmd = _nmea_cksum(_DEVICES[dev]["query"])
        logger.info(f"  NMEA → {query_cmd!r}")
        await service.gpsd.send_firmware_command(query_cmd)

        if not await _wait_for_register_snapshot(tlc, previous_ts):
            logger.warning(f"Register resolve query timed out for {dev}; using cached service state")
            await _pub_event(client, service, "warning", {
                "type": "register_resolve_timeout",
                "device": dev,
                "message": "Timed out waiting for RP2040 register snapshot; using cached service state",
            })

        merged = _register_cache_bits(tlc)
        for idx, value in enumerate(requested):
            if value is not None:
                merged[idx] = value
        cmds.append(_full_register_write_cmd(dev, merged))
    return cmds


async def _dispatch_register_set_registers(client, service, args, payload):
    try:
        nmea_list = await _resolve_set_registers_commands(client, service, args)
    except (ValueError, KeyError) as exc:
        await _send_response(client, service, {"exception": str(exc)}, payload, "registers")
        return

    await _send_response(client, service,
                         {"state": "pending", "message": "'set_registers' sent to firmware"},
                         payload, "registers")
    failed = []
    for cmd in nmea_list:
        logger.info(f"  NMEA → {cmd!r}")
        try:
            await service.gpsd.send_firmware_command(cmd)
            if len(nmea_list) > 1:
                await anyio.sleep(_REGISTER_WRITE_GAP_S)
        except Exception as exc:
            logger.error(f"gpsd_send failed for {cmd!r}: {exc}")
            failed.append({"command": cmd, "error": str(exc)})

    if failed:
        await _send_response(client, service,
                             {"state": "error", "message": "Command write(s) failed", "failures": failed},
                             payload, "registers")
    else:
        await _send_response(client, service,
                             {"state": "ok", "message": "'set_registers' accepted", "commands_sent": len(nmea_list)},
                             payload, "registers")

async def _dispatch_nmea_cmd(client, service, handler, task_name, args, payload, subtopic=""):
    try:
        nmea_list = handler(task_name, args)
    except (ValueError, KeyError) as exc:
        await _send_response(client, service, {"exception": str(exc)}, payload, subtopic)
        return

    await _send_response(client, service,
                         {"state": "pending", "message": f"{task_name!r} sent to firmware"},
                         payload, subtopic)
    failed = []
    pace_register_queries = (
        subtopic == "registers"
        and (
            (
                task_name == "get_registers"
                and str(args.get("device", "all")).lower().strip() in ("", "all")
            )
            or (
                task_name == "get_attenuation_db"
                and str(args.get("device", "all")).lower().strip() in ("", "all")
            )
            or task_name == "reset_registers_to_service_default"
        )
        and len(nmea_list) > 1
    )
    for cmd in nmea_list:
        logger.info(f"  NMEA → {cmd!r}")
        try:
            await service.gpsd.send_firmware_command(cmd)
            if pace_register_queries:
                await anyio.sleep(_REGISTER_QUERY_GAP_S)
        except Exception as exc:
            logger.error(f"gpsd_send failed for {cmd!r}: {exc}")
            failed.append({"command": cmd, "error": str(exc)})

    if failed:
        await _send_response(client, service,
                             {"state": "error", "message": "Command write(s) failed", "failures": failed},
                             payload, subtopic)
    else:
        await _send_response(client, service,
                             {"state": "ok", "message": f"{task_name!r} accepted", "commands_sent": len(nmea_list)},
                             payload, subtopic)


async def _service_reject_deprecated_rate(client, service, sub, payload):
    await _send_response(client, service,
                         {
                             "state": "error",
                             "exception": "Deprecated command disabled. Use afecontrol/command/polling with set_interval/get_interval.",
                             "subtopic": sub,
                         },
                         payload, sub)


async def _service_set_interval(client, service, args, payload):
    global _poll_interval_changed
    try:
        n = int(args["n"])
    except (KeyError, TypeError, ValueError):
        await _send_response(client, service,
                             {"exception": "set_interval requires integer arguments.n"}, payload, "polling")
        return
    lo, hi = _RATE_PARAM["range"]
    if not (lo <= n <= hi):
        await _send_response(client, service,
                             {"exception": f"n must be in range [{lo}, {hi}], got {n}"}, payload, "polling")
        return

    _set_poll_interval_s(n)
    _poll_interval_changed.set()  # Wake _poll_telem immediately
    await _send_response(client, service,
                         {
                             "state": "ok",
                             "configured_interval_s": n,
                             "effective_telem_poll_interval_s": _effective_poll_interval_s(),
                         },
                         payload, "polling")
    await _send_status(client, service)


async def _service_get_interval(client, service, payload):
    await _send_response(client, service,
                         {
                             "state": "ok",
                             "configured_interval_s": _effective_poll_interval_s(),
                             "effective_telem_poll_interval_s": _effective_poll_interval_s(),
                         },
                         payload, "polling")


async def _service_telem_dump(client, service, payload):
    cmd = _nmea_cksum("$TELEM?*")
    await _send_response(client, service,
                         {"state": "pending", "message": "'telem_dump' sent to firmware"}, payload)
    try:
        await service.gpsd.send_firmware_command(cmd)
        await _send_response(client, service,
                             {"state": "ok", "message": "'telem_dump' accepted", "commands_sent": 1}, payload)
    except Exception as exc:
        await _send_response(client, service,
                             {"state": "error", "message": "Command write failed", "error": str(exc)}, payload)


async def _service_refresh(client, service, payload):
    commands = (
        _cmd_imu("get_imu_params", {})
        + _cmd_mag("get_mag_params", {})
        + _cmd_time("get_time_params", {})
        + [_nmea_cksum("$TELEM?*")]
    )
    failed = []
    for command in commands:
        try:
            await service.gpsd.send_firmware_command(command)
        except Exception as exc:
            failed.append({"command": command, "error": str(exc)})
    await _send_status(client, service)
    if failed:
        await _send_response(client, service, {
            "state": "error",
            "message": "One or more refresh commands failed",
            "failures": failed,
        }, payload)
    else:
        await _send_response(client, service, {
            "state": "ok",
            "message": "Refresh commands accepted",
            "commands_sent": len(commands),
        }, payload)


async def _handle_gps_commands(client, service, task_name, args, payload):
    global _raw_stream_deadline_monotonic
    subtopic = "gps"
    if task_name == "describe":
        await _send_response(client, service, _DESC_GPS, payload, subtopic)
        return
    if task_name == "stop_raw_stream":
        _clear_raw_stream_lease()
        await _send_gpsd_status(client, service)
        await _send_response(client, service, {"state": "ok", "raw_stream_enabled": False},
                             payload, subtopic)
        return
    if task_name != "start_raw_stream":
        await _send_response(client, service,
                             {"exception": f"Unknown gps command: {task_name!r}"}, payload, subtopic)
        return

    try:
        duration_s = float(args.get("duration_s", _RAW_STREAM_DEFAULT_DURATION_S))
    except (TypeError, ValueError):
        await _send_response(client, service, {"exception": "duration_s must be numeric"},
                             payload, subtopic)
        return
    mode = str(args.get("mode", "gnss")).lower().strip()
    if not 1 <= duration_s <= _RAW_STREAM_MAX_DURATION_S:
        await _send_response(client, service, {
            "exception": f"duration_s must be in range [1, {_RAW_STREAM_MAX_DURATION_S:g}]",
        }, payload, subtopic)
        return
    if mode not in {"gnss", "all"}:
        await _send_response(client, service,
                             {"exception": "mode must be 'gnss' or 'all'"}, payload, subtopic)
        return

    _raw_stream_deadline_monotonic = time.monotonic() + duration_s
    expires_timestamp = time.time() + duration_s
    _gpsd_status.update(
        raw_stream_enabled=True,
        raw_stream_mode=mode,
        raw_stream_expires_timestamp=expires_timestamp,
    )
    await _send_gpsd_status(client, service)
    await _send_response(client, service, {
        "state": "ok",
        "raw_stream_enabled": True,
        "mode": mode,
        "duration_s": duration_s,
        "expires_timestamp": expires_timestamp,
        "topic": service.topic_data_raw,
    }, payload, subtopic)


def _rmc_epoch_key(line):
    parts = line.split("*", 1)[0].split(",")
    utc_time = parts[1] if len(parts) > 1 else ""
    utc_date = parts[9] if len(parts) > 9 else ""
    return utc_date, utc_time


def _begin_gps_epoch(epoch_key):
    global _gps_epoch_key, _gps_epoch_started_monotonic, _gps_epoch_active
    _pending_gps.update(dict.fromkeys(_pending_gps))
    _pending_gsa_used.clear()
    _pending_gsv_visible.clear()
    _pending_gsv_signals.clear()
    _gps_epoch_key = epoch_key
    _gps_epoch_started_monotonic = time.monotonic()
    _gps_epoch_active = True


def _discard_gps_epoch():
    global _gps_epoch_key, _gps_epoch_started_monotonic, _gps_epoch_active
    _gps_epoch_key = None
    _gps_epoch_started_monotonic = None
    _gps_epoch_active = False
    _pending_gps.update(dict.fromkeys(_pending_gps))
    _pending_gsa_used.clear()
    _pending_gsv_visible.clear()
    _pending_gsv_signals.clear()


async def _commit_gps_epoch(client, service):
    global _gps_epoch_active, _gps_last_committed_epoch_key
    if not _gps_epoch_active:
        return
    _gps_epoch_active = False
    _gps_last_committed_epoch_key = _gps_epoch_key
    _buf_gps.update(_pending_gps)
    try:
        await client.publish(service.topic_data_gps, msgspec.json.encode(_buf_gps))
    except Exception as exc:
        await _pub_event(client, service, "error",
                         {"type": "gps_publish_error", "message": str(exc)})


async def _expire_gps_epochs(client, service):
    """Flush an open epoch if the next RMC never arrives."""
    while True:
        await anyio.sleep(0.1)
        if (
            _gps_epoch_active
            and _gps_epoch_started_monotonic is not None
            and time.monotonic() - _gps_epoch_started_monotonic >= _GPS_EPOCH_TIMEOUT_S
        ):
            await _commit_gps_epoch(client, service)


async def _expire_raw_stream_leases(client, service):
    while True:
        await anyio.sleep(0.1)
        if _gpsd_status["raw_stream_enabled"] and not _raw_stream_active():
            await _send_gpsd_status(client, service)


async def _publish_gpsd_status_heartbeat(client, service):
    while True:
        await anyio.sleep(_GPSD_STATUS_HEARTBEAT_S)
        await _send_gpsd_status(client, service)


async def _poll_telem(client, service):
    global _poll_interval_changed
    while True:
        try:
            interval = _effective_poll_interval_s()
            if _USE_SERVICE_TELEM_WORKAROUND and interval > 0:
                cmd = _nmea_cksum("$TELEM?*")
                await service.gpsd.send_firmware_command(cmd)
                # Sleep until interval expires OR poll_interval changes.
                # anyio.Event is one-shot (no .clear()) — re-arm with a fresh instance.
                _poll_interval_changed = anyio.Event()
                with anyio.move_on_after(interval):
                    await _poll_interval_changed.wait()
                continue
        except Exception as exc:
            await _pub_event(client, service, "error",
                             {"type": "telem_poll_error", "message": str(exc)})
        await anyio.sleep(_POLL_LOOP_SLEEP_S)


async def _process_commands(client, service):
    logger.info(f"Listening on {service.topic_command} and {service.topic_command}/#")
    async for message in client.messages:
        try:
            ft = str(message.topic)
            base = service.topic_command
            if ft == base:
                sub = ""
            elif ft.startswith(base + "/"):
                sub = ft[len(base)+1:]
            else:
                continue

            payload = msgspec.json.decode(message.payload)
            if not isinstance(payload, dict) or "task_name" not in payload:
                continue
            tn = payload["task_name"]
            args = payload.get("arguments", {})
            logger.debug(f"Command [{sub or 'service'}] {tn}: {args}")

            if sub == "":
                if tn == "status":
                    await _send_status(client, service)
                    await _send_gpsd_status(client, service)
                elif tn == "describe":
                    await _send_response(client, service, _DESCRIBE, payload)
                elif tn == "telem_dump":
                    await _service_telem_dump(client, service, payload)
                elif tn == "refresh":
                    await _service_refresh(client, service, payload)
                else:
                    await _send_response(client, service,
                                         {"exception": f"Unknown service command: {tn!r}"}, payload)

            elif sub in _HANDLERS:
                if tn == "describe":
                    await _send_response(client, service, _DESCRIBE.get(sub, {}), payload, sub)
                elif sub == "registers" and tn == "set_registers":
                    await _dispatch_register_set_registers(client, service, args, payload)
                elif _USE_SERVICE_TELEM_WORKAROUND and sub in ("imu", "mag", "hk") and tn in ("set_rate", "get_rate"):
                    await _service_reject_deprecated_rate(client, service, sub, payload)
                else:
                    await _dispatch_nmea_cmd(client, service, _HANDLERS[sub], tn, args, payload, sub)

            elif sub == "gps":
                await _handle_gps_commands(client, service, tn, args, payload)

            elif sub == "logging":
                await _handle_logging(client, service, tn, args, payload)

            elif sub == "polling":
                if tn == "describe":
                    await _send_response(client, service, _DESCRIBE.get("polling", {}), payload, "polling")
                elif tn == "set_interval":
                    await _service_set_interval(client, service, args, payload)
                elif tn == "get_interval":
                    await _service_get_interval(client, service, payload)
                else:
                    await _send_response(client, service,
                                         {"exception": f"Unknown polling command: {tn!r}"}, payload, "polling")

            else:
                await _send_response(client, service,
                                     {"exception": f"Unknown subtopic: {sub!r}. Valid: {[k for k in _DESCRIBE if k]}"},
                                     payload)

        except Exception:
            logger.exception(f"Error processing {message.topic}")
            try: cp = msgspec.json.decode(message.payload) if message.payload else {}
            except Exception: cp = {}
            se = ""
            try:
                ft2 = str(message.topic)
                if ft2.startswith(service.topic_command + "/"):
                    se = ft2[len(service.topic_command)+1:]
            except Exception: pass
            await _send_response(client, service, {"exception": traceback.format_exc()}, cp, se)


async def _handle_logging(client, service, tn, args, payload):
    s = "logging"
    if tn == "describe":
        await _send_response(client, service, _DESC_LOGGING, payload, s)
    elif tn == "configure":
        enabled = args.get("enabled")
        path = str(args.get("path", "")).strip()
        try:
            rate_s = float(args["rate_s"])
        except (KeyError, TypeError, ValueError):
            await _send_response(client, service, {"exception": "rate_s must be a positive number"}, payload, s)
            return
        if not isinstance(enabled, bool):
            await _send_response(client, service, {"exception": "enabled must be boolean"}, payload, s)
            return
        if not path:
            await _send_response(client, service, {"exception": "path must be a non-empty string"}, payload, s)
            return
        if rate_s <= 0:
            await _send_response(client, service, {"exception": "rate_s must be greater than zero"}, payload, s)
            return
        service.bool_logging_enabled = enabled
        service.str_log_dir = path
        service.int_telem_rate = rate_s
        _log_rate_changed.set()
        await _send_status(client, service)
        await _send_response(client, service, {
            "state": "ok",
            "telemetry_logging_enabled": enabled,
            "telemetry_log_dir": path,
            "telemetry_log_rate_s": rate_s,
        }, payload, s)
        await _pub_event(client, service, "logging", {
            "type": "logging_configured",
            "enabled": enabled,
            "log_dir": path,
            "rate_s": rate_s,
        })
    elif tn == "get_log_status":
        await _send_response(client, service, {
            "telemetry_logging_enabled": service.bool_logging_enabled,
            "telemetry_log_dir": service.str_log_dir,
            "telemetry_log_rate_s": service.int_telem_rate,
            "service_log_mode": _log_mode(),
        }, payload, s)
    elif tn == "set_service_log_mode":
        mode = str(args.get("mode", "")).lower().strip()
        if mode not in _LOG_MODES:
            await _send_response(client, service,
                                 {"exception": f"mode must be one of {_LOG_MODE_OPTS}, got {mode!r}"}, payload, s)
        else:
            prev = _log_mode()
            logger.setLevel(_LOG_MODES[mode])
            await _send_response(client, service,
                                 {"state": "ok", "previous_service_log_mode": prev, "service_log_mode": mode}, payload, s)
            await _send_status(client, service)
    elif tn == "get_service_log_mode":
        await _send_response(client, service, {"service_log_mode": _log_mode()}, payload, s)
    else:
        await _send_response(client, service, {"exception": f"Unknown logging command: {tn!r}"}, payload, s)


# ============================================================================
# SERVICE CLASS
# ============================================================================

@dataclasses.dataclass(kw_only=True)
class AFEControlService:
    name: str = "afecontrol"
    node_id: Optional[str] = None
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    gpsd_host: str = "localhost"
    gpsd_port: int = 2947
    str_device: str = "/dev/ttyGNSS1"
    int_telem_rate: float = 10
    str_log_dir: str = "/data/log_telemetry"
    bool_logging_enabled: bool = True
    gpsd: Optional[GPSDTransport] = dataclasses.field(init=False, default=None, repr=False)

    topic_announce: str = dataclasses.field(init=False)
    topic_command: str = dataclasses.field(init=False)
    topic_status: str = dataclasses.field(init=False)
    topic_status_registers: str = dataclasses.field(init=False)
    topic_status_imu: str = dataclasses.field(init=False)
    topic_status_mag: str = dataclasses.field(init=False)
    topic_status_time: str = dataclasses.field(init=False)
    topic_status_gpsd: str = dataclasses.field(init=False)
    topic_data_gps: str = dataclasses.field(init=False)
    topic_data_imu: str = dataclasses.field(init=False)
    topic_data_mag: str = dataclasses.field(init=False)
    topic_data_hk: str = dataclasses.field(init=False)
    topic_data_raw: str = dataclasses.field(init=False)

    def __post_init__(self):
        if self.node_id is None:
            self.node_id = os.getenv("NODE_ID", socket.gethostname())
        n = self.name
        self.topic_announce         = f"{n}/announce"
        self.topic_command          = f"{n}/command"
        self.topic_status           = f"{n}/status"
        self.topic_status_registers = f"{n}/status/registers"
        self.topic_status_imu       = f"{n}/status/imu"
        self.topic_status_mag       = f"{n}/status/mag"
        self.topic_status_time      = f"{n}/status/time"
        self.topic_status_gpsd      = f"{n}/status/gpsd"
        self.topic_data_gps         = f"{n}/data/gps"
        self.topic_data_imu         = f"{n}/data/imu"
        self.topic_data_mag         = f"{n}/data/mag"
        self.topic_data_hk          = f"{n}/data/hk"
        self.topic_data_raw         = f"{n}/data/raw"


# ============================================================================
# CSV LOGGING
# ============================================================================

# Persistent handle for the always-on telemetry log. Reopened only when the
# log directory or calendar day changes; every row is fsynced individually,
# so durability never depends on this handle surviving to be closed cleanly.
_csv_fh = None
_csv_writer = None
_csv_key = None  # (log_dir, date_str) the open handle corresponds to


def _csv_close():
    global _csv_fh, _csv_writer, _csv_key
    if _csv_fh is not None:
        try:
            _csv_fh.flush()
            os.fsync(_csv_fh.fileno())
            _csv_fh.close()
        except Exception:
            logger.exception("CSV close error")
    _csv_fh, _csv_writer, _csv_key = None, None, None


def _csv_ensure_open(service, now):
    """(Re)open the log file if the log dir or day changed. Always append, never truncate."""
    global _csv_fh, _csv_writer, _csv_key
    key = (service.str_log_dir, now.strftime("%Y%m%d"))
    if key == _csv_key and _csv_fh is not None:
        return
    _csv_close()
    log_dir, date_str = key
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"telemetry_{date_str}.csv")
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    if not write_header:
        with open(path, "r", newline="", encoding="utf-8") as existing:
            existing_header = next(csv.reader(existing), [])
        if existing_header != _CSV_HEADER:
            suffix = 2
            while True:
                candidate = os.path.join(log_dir, f"telemetry_{date_str}_{suffix}.csv")
                if not os.path.exists(candidate) or os.path.getsize(candidate) == 0:
                    path = candidate
                    write_header = True
                    break
                with open(candidate, "r", newline="", encoding="utf-8") as existing:
                    if next(csv.reader(existing), []) == _CSV_HEADER:
                        path = candidate
                        break
                suffix += 1
            logger.warning(
                "Telemetry schema mismatch in primary daily file; continuing in %s",
                path,
            )
    fh = open(path, "a", newline="", encoding="utf-8")
    writer = csv.writer(fh)
    if write_header:
        writer.writerow(_CSV_HEADER)
        fh.flush()
        os.fsync(fh.fileno())
    _csv_fh, _csv_writer, _csv_key = fh, writer, key


async def _emit_csv(service):
    global _log_rate_changed
    try:
        while True:
            rate = float(service.int_telem_rate)
            if service.bool_logging_enabled:
                try:
                    now = datetime.now(timezone.utc)
                    _csv_ensure_open(service, now)
                    row = [now.isoformat()]
                    for buf in (_buf_gps, _buf_mag, _buf_imu, _buf_hk):
                        row.extend(buf.values())
                    row.append(json.dumps(_reg["registers"], sort_keys=True, separators=(",", ":")))
                    _csv_writer.writerow(row)
                    _csv_fh.flush()
                    os.fsync(_csv_fh.fileno())  # durable now, not "whenever the OS gets to it"
                    logger.debug(f"Telemetry logged: {_csv_key}")
                except Exception:
                    logger.exception("CSV write error")

            # Sleep until rate expires OR log_rate changes.
            # anyio.Event is one-shot (no .clear()) — re-arm with a fresh instance.
            _log_rate_changed = anyio.Event()
            with anyio.move_on_after(rate):
                await _log_rate_changed.wait()
    finally:
        _csv_close()  # best-effort tidy-up only; durability is already guaranteed per-row above


# ============================================================================
# MAIN
# ============================================================================

async def main(service):
    global _log_rate_changed, _poll_interval_changed
    service.gpsd = GPSDTransport(service.gpsd_host, service.gpsd_port, service.str_device)
    _gpsd_status.update(
        host=service.gpsd_host,
        port=service.gpsd_port,
        configured_device=service.str_device,
    )
    _log_rate_changed = anyio.Event()
    _poll_interval_changed = anyio.Event()
    will = aiomqtt.Will(
        service.topic_status,
        payload=msgspec.json.encode({"state": "offline", "seq": -1, "timestamp": time.time()}),
        qos=0, retain=True,
    )
    client = aiomqtt.Client(service.mqtt_host, service.mqtt_port, keepalive=60, will=will)
    while True:
        try:
            async with client:
                _clear_raw_stream_lease()
                _discard_gps_epoch()
                _gpsd_status.update(
                    connected=False,
                    disconnected_timestamp=time.time(),
                )
                await client.subscribe(service.topic_command)
                await client.subscribe(service.topic_command + "/#")
                await _send_announce(client, service)
                await _send_status(client, service)
                await _send_gpsd_status(client, service)
                with exceptiongroup.catch({Exception: lambda e: logger.error("Task exception", exc_info=e)}):
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(_monitor_gpsd, client, service)
                        tg.start_soon(_process_commands, client, service)
                        tg.start_soon(_expire_gps_epochs, client, service)
                        tg.start_soon(_expire_raw_stream_leases, client, service)
                        tg.start_soon(_publish_gpsd_status_heartbeat, client, service)
                        tg.start_soon(_poll_telem, client, service)
                        tg.start_soon(_emit_csv, service)
        except aiomqtt.MqttError:
            logger.warning("MQTT connection lost; reconnecting in 5s ...")
            await anyio.sleep(5)


if __name__ == "__main__":
    logger.info("Starting afe_service")
    service = jsonargparse.auto_cli(AFEControlService)
    anyio.run(main, service)
