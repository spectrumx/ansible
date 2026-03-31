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
# afe_service.py — clean rewrite
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
#   Env prefix: AFE_  (AFE_MQTT_HOST, AFE_GPSD_HOST, ...)

import csv
import dataclasses
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
# Polling is owned by afe/command/polling with one global interval.
_USE_SERVICE_TELEM_WORKAROUND = True
_POLL_LOOP_SLEEP_S = 0.2

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
    {"pin": 0, "name": "TRIG_TX_SRC_SEL",   "label": "TX Trigger Source Select",   "default": 1, "0": "Internal",     "1": "External"},
    {"pin": 1, "name": "TRIG_RX_SRC_SEL",   "label": "RX Trigger Source Select",   "default": 1, "0": "Internal",     "1": "External"},
    {"pin": 2, "name": "EXT_TX_TRIG_ENABLE", "label": "External TX Trigger Enable", "default": 1, "0": "Enabled",      "1": "Disabled"},
    {"pin": 3, "name": "EXT_RX_TRIG_ENABLE", "label": "External RX Trigger Enable", "default": 1, "0": "Enabled",      "1": "Disabled"},
    {"pin": 4, "name": "NOT_USED_4",         "label": "Not Used (4)",               "default": 0, "0": "Reserved",     "1": "Reserved"},
    {"pin": 5, "name": "EXT_BIAS_ENABLE",    "label": "External Bias Enable",       "default": 0, "0": "Disabled",     "1": "Enabled"},
    {"pin": 6, "name": "TEST_LED",           "label": "Test LED",                   "default": 0, "0": "Off",          "1": "On"},
    {"pin": 7, "name": "PPS_SOURCE_SEL",     "label": "PPS Source Select",          "default": 1, "0": "External",     "1": "Internal GNSS"},
    {"pin": 8, "name": "REF_SOURCE_SEL",     "label": "Reference Source Select",    "default": 1, "0": "External",     "1": "Internal OCXO"},
    {"pin": 9, "name": "GNSS_ANT_SEL",       "label": "GNSS Antenna Select",        "default": 0, "0": "External",     "1": "Internal"},
]

_TX_PINS = [
    {"pin": 0, "name": "NOT_USED_0",        "label": "Not Used (0)",           "default": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 1, "name": "TX_BLANK_SEL",      "label": "TX Blanking Select",    "default": 1, "0": "Blanked",    "1": "Not blanked"},
    {"pin": 2, "name": "FILTER_BYPASS_SEL", "label": "Filter Bypass Select",  "default": 1, "0": "Filtered",   "1": "Bypassed"},
    {"pin": 3, "name": "NOT_USED_3",        "label": "Not Used (3)",           "default": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 4, "name": "NOT_USED_4",        "label": "Not Used (4)",           "default": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 5, "name": "NOT_USED_5",        "label": "Not Used (5)",           "default": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 6, "name": "NOT_USED_6",        "label": "Not Used (6)",           "default": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 7, "name": "NOT_USED_7",        "label": "Not Used (7)",           "default": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 8, "name": "NOT_USED_8",        "label": "Not Used (8)",           "default": 0, "0": "Reserved",   "1": "Reserved"},
    {"pin": 9, "name": "TEST_LED",          "label": "Test LED",              "default": 0, "0": "Off",        "1": "On"},
]

_RX_PINS = [
    {"pin": 0, "name": "CHAN_BIAS_EN",       "label": "Channel Bias Enable",       "default": 0, "0": "Disabled",     "1": "Enabled"},
    {"pin": 1, "name": "INT_RF_TRIG_SEL",   "label": "Internal RF Trigger Select", "default": 1, "0": "Not asserted", "1": "Asserted"},
    {"pin": 2, "name": "FILTER_BYPASS_SEL", "label": "Filter Bypass Select",       "default": 1, "0": "Bypassed",     "1": "Filtered"},
    {"pin": 3, "name": "AMP_BYPASS_SEL",    "label": "Amplifier Bypass Select",    "default": 1, "0": "Bypassed",     "1": "Enabled"},
    {"pin": 4, "name": "ATTEN_C1",          "label": "Attenuator +1 dB",           "default": 0, "0": "Skip +1dB",    "1": "Add +1dB"},
    {"pin": 5, "name": "ATTEN_C2",          "label": "Attenuator +2 dB",           "default": 0, "0": "Skip +2dB",    "1": "Add +2dB"},
    {"pin": 6, "name": "ATTEN_C4",          "label": "Attenuator +4 dB",           "default": 0, "0": "Skip +4dB",    "1": "Add +4dB"},
    {"pin": 7, "name": "ATTEN_C8",          "label": "Attenuator +8 dB",           "default": 0, "0": "Skip +8dB",    "1": "Add +8dB"},
    {"pin": 8, "name": "ATTEN_C16",         "label": "Attenuator +16 dB",          "default": 0, "0": "Skip +16dB",   "1": "Add +16dB"},
    {"pin": 9, "name": "TEST_LED",          "label": "Test LED",                   "default": 0, "0": "Off",          "1": "On"},
]

# ---- Devices ----

_DEVICES = {
    "misc": {"pins": _MISC_PINS, "prefix": "PMITMAX", "query": "$PMITMA?*",  "tlc": "MA?"},
    "tx1":  {"pins": _TX_PINS,   "prefix": "PMITXT1", "query": "$PMITXT1?*", "tlc": "XT1"},
    "tx2":  {"pins": _TX_PINS,   "prefix": "PMITXT2", "query": "$PMITXT2?*", "tlc": "XT2"},
    "rx1":  {"pins": _RX_PINS,   "prefix": "PMITXR1", "query": "$PMITXR1?*", "tlc": "XR1"},
    "rx2":  {"pins": _RX_PINS,   "prefix": "PMITXR2", "query": "$PMITXR2?*", "tlc": "XR2"},
    "rx3":  {"pins": _RX_PINS,   "prefix": "PMITXR3", "query": "$PMITXR3?*", "tlc": "XR3"},
    "rx4":  {"pins": _RX_PINS,   "prefix": "PMITXR4", "query": "$PMITXR4?*", "tlc": "XR4"},
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
    {"key": "fix_valid",   "label": "Fix Valid",   "type": "bool"},
    {"key": "lat",         "label": "Latitude",    "unit": "°"},
    {"key": "lon",         "label": "Longitude",   "unit": "°"},
    {"key": "altitude_m",  "label": "Altitude",    "unit": "m"},
    {"key": "speed_knots", "label": "Speed",       "unit": "knots"},
    {"key": "track_deg",   "label": "Track",       "unit": "°"},
    {"key": "fix_quality", "label": "Fix Quality", "type": "int"},
    {"key": "satellites",  "label": "Satellites",  "type": "int"},
    {"key": "hdop",        "label": "HDOP",        "type": "float"},
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
_LOG_RATE_RANGE  = [1, 3600]
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
           "default": e["default"], "0": e["0"], "1": e["1"]} for e in info["pins"]]
    for dev, info in _DEVICES.items()
}

# ============================================================================
# STATE (derived from data-field tables — single source of truth)
# ============================================================================

def _make_buf(fields, extras=None):
    buf = {"timestamp": None}
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
            "default": e["default"], "meaning": e.get(str(v)) if v in (0, 1) else None,
        }
    return out


def _enum_label(m, v):
    return m.get(v, f"UNKNOWN({v})")


# Module-level state — written by parsers, read by publishers and command handlers.
_buf_gps = _make_buf(_DATA_FIELDS_GPS)
_buf_imu = _make_buf(_DATA_FIELDS_IMU)
_buf_mag = _make_buf(_DATA_FIELDS_MAG)
_buf_hk  = _make_buf(_DATA_FIELDS_HK, extras={"time_source": None, "time_epoch": None})

_reg = {
    "registers":       {info["tlc"]: [None]*10 for info in _DEVICES.values()},
    "registers_named": {dev: _decode_dev_regs(dev, [None]*10) for dev in _DEVICES},
    "service_timestamp": None,
}

# Unified params — no duplicate standalone dicts.
_params = {
    "imu":   {s["key"]: None for s in _IMU_MODE_SETTINGS} | {"acc_odr": None, "gyr_odr": None},
    "mag":   {k: None for k in _MAG_PARAMS},
    "rates": {"poll_interval_s": 5},
    "time":  {"time_source": None, "time_source_label": None,
              "time_epoch": None,  "time_epoch_label": None},
    "last_error": {"status": None, "tlc": None, "fields": []},
}

_startup_queries_sent = False
_gpsd_send_lock = None

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
            "description": "Bulk-set registers. One NMEA per device ('x' for untouched pins).",
            "arguments": {"<device>": {"type": "dict", "description": "Keys = register names, values = 0|1."}},
            "example": {"misc": {"TRIG_TX_SRC_SEL": 1, "TEST_LED": 0}, "rx1": {"CHAN_BIAS_EN": 1}},
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
    "note": "Firmware re-initializes the entire IMU on every set. Partial commands use cached state. set_rate/get_rate are disabled; use afe/command/polling.",
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
    "note": "Firmware re-initializes the magnetometer on every set. set_rate/get_rate are disabled; use afe/command/polling.",
}

_DESC_HK = {
    "subtopic": "hk",
    "reference": {"data_fields": _DATA_FIELDS_HK},
    "commands": {},
    "note": "set_rate/get_rate are disabled; use afe/command/polling.",
}

_DESC_GPS = {
    "subtopic": "gps",
    "reference": {"data_fields": _DATA_FIELDS_GPS},
}

_DESC_TIME = {
    "subtopic": "time",
    "reference": {"time_source_options": _TIME_SOURCE_LABELS, "time_epoch_options": _TIME_EPOCH_LABELS},
    "commands": {
        "set_source_gnss":     {"description": "Set time source to GNSS, epoch to NMEA.",       "arguments": {}},
        "set_source_external": {"description": "Set time source to External (no epoch set).",    "arguments": {}},
        "set_epoch_pps":       {"description": "Set epoch to PPS with given timestamp.",
                                "arguments": {"ts": {"type": "int", "description": "Unix epoch"}}},
        "set_epoch_nmea":      {"description": "Set epoch to NMEA.",                             "arguments": {}},
        "set_epoch_immediate": {"description": "Set epoch immediately with given timestamp.",
                                "arguments": {"ts": {"type": "int", "description": "Unix epoch"}}},
        "get_time_params":     {"description": "Query current time source/epoch from firmware.", "arguments": {}},
    },
}

_DESC_LOGGING = {
    "subtopic": "logging",
    "reference": {"log_rate_range": _LOG_RATE_RANGE, "log_mode_options": _LOG_MODE_OPTS},
    "commands": {
        "enable_logging":       {"description": "Enable CSV telemetry logging.",  "arguments": {}},
        "disable_logging":      {"description": "Disable CSV telemetry logging.", "arguments": {}},
        "get_log_status":       {"description": "Get current logging state.",     "arguments": {}},
        "set_log_path":         {"description": "Change log output directory.",
                                 "arguments": {"path": {"type": "string"}}},
        "set_log_rate_sec":     {"description": "Change CSV flush interval.",
                                 "arguments": {"n": {"type": "int", "range": _LOG_RATE_RANGE}}},
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
    return int(datetime(int(d[4:6])+2000, int(d[2:4]), int(d[0:2]),
                        int(t[0:2]), int(t[2:4]), int(t[4:6]),
                        tzinfo=timezone.utc).timestamp())


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

    elif task_name == "set_registers":
        for dk, rd in args.items():
            dev = str(dk).lower().strip()
            if dev not in _DEVICES:
                raise ValueError(f"Unknown device: {dev!r}. Valid: {_ALL_DEVICES}")
            if not isinstance(rd, dict):
                raise ValueError(f"Expected dict for {dev!r}, got {type(rd).__name__}")
            slots = ["x"] * 10
            for rn, v in rd.items():
                rn_u = str(rn).upper().strip()
                idx = _PIN_IDX[dev].get(rn_u)
                if idx is None:
                    raise ValueError(f"Unknown register {rn_u!r} on {dev!r}. Valid: {list(_PIN_IDX[dev])}")
                iv = int(v)
                if iv not in (0, 1):
                    raise ValueError(f"value for {rn_u!r} must be 0 or 1, got {iv}")
                slots[idx] = str(iv)
            cmds.append(_nmea_cksum(f"${_DEVICES[dev]['prefix']},0,{','.join(slots)}*"))

    elif task_name == "set_attenuation_db":
        dev = str(args["device"]).lower().strip()
        db = int(args["db"])
        if dev not in _DEVICES or not dev.startswith("rx"):
            raise ValueError(f"{dev!r} is not an RX device (rx1..rx4)")
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
    _map = {
        "set_source_gnss":     lambda: _nmea_cksum("$PMITTSG*"),
        "set_source_external": lambda: _nmea_cksum("$PMITTSE*"),
        "set_epoch_pps":       lambda: _nmea_cksum(f"$PMITTEP,{int(args['ts'])}*"),
        "set_epoch_nmea":      lambda: _nmea_cksum("$PMITTEN*"),
        "set_epoch_immediate": lambda: _nmea_cksum(f"$PMITTEI,{int(args['ts'])}*"),
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

def _gpsd_send(nmea, device):
    cmd = nmea if nmea.endswith("\r\n") else nmea + "\r\n"
    msg = f"&{device}={cmd.encode('ascii').hex().upper()}\n".encode("ascii")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(_GPSD_SOCK_PATH)
        sock.sendall(msg)
        reply = sock.recv(4096).decode("ascii").strip()
    finally:
        sock.close()
    if reply != "OK":
        raise RuntimeError(f"gpsd: unexpected reply {reply!r} for {nmea!r}")


async def _gpsd_send_async(nmea, device):
    if _gpsd_send_lock is None:
        await anyio.to_thread.run_sync(lambda c=nmea: _gpsd_send(c, device))
        return
    async with _gpsd_send_lock:
        await anyio.to_thread.run_sync(lambda c=nmea: _gpsd_send(c, device))


# ============================================================================
# NMEA PARSERS
# ============================================================================

def _parse_gnrmc(line):
    global _buf_gps
    try:
        parts = line.split("*")[0].split(",")
        fix = len(parts) > 2 and parts[2] == "A"
        u = {"fix_valid": fix, "lat": None, "lon": None,
             "altitude_m": _buf_gps.get("altitude_m"), "service_timestamp": time.time()}
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
        u["track_deg"] = None
        if len(parts) > 8 and parts[8]:
            try: u["track_deg"] = float(parts[8])
            except ValueError: pass
        _buf_gps.update(u)
    except Exception:
        logger.debug(f"$GNRMC parse error: {line!r}", exc_info=True)


def _parse_gngga(line):
    global _buf_gps
    try:
        parts = line.split("*")[0].split(",")
        u = {"lat": _buf_gps.get("lat"), "lon": _buf_gps.get("lon"), "altitude_m": None}
        for idx, key, cast in [(6, "fix_quality", int), (7, "satellites", int),
                                (8, "hdop", float), (9, "altitude_m", float)]:
            if len(parts) > idx and parts[idx]:
                try: u[key] = cast(parts[idx])
                except ValueError: pass
        _buf_gps.update(u)
    except Exception:
        logger.debug(f"$GNGGA parse error: {line!r}", exc_info=True)


def _parse_pmitmag(line):
    p = line.split("*")[0].split(",")
    return {"timestamp": int(p[1]), "mag_x": float(p[2]), "mag_y": float(p[3]),
            "mag_z": float(p[4]), "service_timestamp": time.time()}


def _parse_pmitacc(line):
    p = line.split("*")[0].split(",")
    return {"timestamp": int(p[1]), "acc_x": float(p[2]), "acc_y": float(p[3]),
            "acc_z": float(p[4]), "service_timestamp": time.time()}


def _parse_pmitgyr(line):
    p = line.split("*")[0].split(",")
    return {"timestamp": int(p[1]), "gyr_x": float(p[2]), "gyr_y": float(p[3]),
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
            return
        status, tlc, fields = str(parts[1]), parts[2], parts[3:]

        if status != "0":
            logger.debug(f"$PMITSR error status={status!r} tlc={tlc!r}")
            _params["last_error"] = {"status": status, "tlc": tlc, "fields": fields}
            _reg["service_timestamp"] = time.time()
            return

        if tlc in _TLC_MAP:
            try: bits = [int(b) for b in fields[:10]]
            except ValueError: bits = fields[:10]
            _reg["registers"][tlc] = bits
            _reg["registers_named"][_TLC_MAP[tlc]] = _decode_dev_regs(_TLC_MAP[tlc], bits)

        elif tlc == "IM?" and len(fields) >= 5:
            _params["imu"].update(acc_odr=fields[0], gyr_odr=fields[1],
                                  ahiperf=int(fields[2]), aulp=int(fields[3]), glp=int(fields[4]))

        elif tlc == "MG?" and len(fields) >= 2:
            _params["mag"].update(ccr=int(fields[0]), updr=int(fields[1]))

        elif tlc == "R?" and len(fields) >= 3:
            # Firmware reports per-stream rates, but service polling uses one owned interval.
            _params["firmware_rates"] = {
                "telem_rate_s": int(fields[0]),
                "mag_rate_s": int(fields[1]),
                "imu_rate_s": int(fields[2]),
            }

        elif tlc == "TP?" and len(fields) >= 2:
            ts, te = int(fields[0]), int(fields[1])
            _params["time"] = {
                "time_source": ts, "time_source_label": _enum_label(_TIME_SOURCE_LABELS, ts),
                "time_epoch": te,  "time_epoch_label":  _enum_label(_TIME_EPOCH_LABELS, te),
            }

        else:
            _params[tlc] = fields

        _reg["service_timestamp"] = time.time()
    except Exception:
        logger.debug(f"$PMITSR parse error: {line!r}", exc_info=True)


# ============================================================================
# GPSD MONITOR + NMEA DISPATCH
# ============================================================================

async def _monitor_gpsd(client, service):
    global _startup_queries_sent
    logger.info(f"Connecting to gpsd at {service.gpsd_host}:{service.gpsd_port}")
    while True:
        try:
            async with await anyio.connect_tcp(service.gpsd_host, service.gpsd_port) as stream:
                await stream.send(_GPSD_WATCH_CMD)
                await _pub_event(client, service, "connection",
                                 {"type": "gpsd_connected",
                                  "message": f"Connected to gpsd at {service.gpsd_host}:{service.gpsd_port}"})

                if not _startup_queries_sent:
                    ok = True
                    for q in ["$PMITIM?*", "$PMITMG?*", "$PMITR?*", "$PMITTP?*",
                              *[i["query"] for i in _DEVICES.values()]]:
                        try:
                            cmd = _nmea_cksum(q)
                            await _gpsd_send_async(cmd, service.str_device)
                            logger.info(f"Startup query sent: {cmd}")
                        except Exception as exc:
                            ok = False
                            logger.warning(f"Startup query failed for {q}: {exc}")
                    _startup_queries_sent = ok

                buf = b""
                while True:
                    try:
                        chunk = await stream.receive(4096)
                    except anyio.EndOfStream:
                        logger.warning("gpsd TCP stream closed; reconnecting in 5s")
                        await _pub_event(client, service, "connection",
                                         {"type": "gpsd_disconnected", "message": "gpsd TCP stream closed"})
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode("ascii", errors="replace").strip()
                        if line:
                            await _dispatch_nmea(client, service, line)
        except OSError as exc:
            logger.warning(f"gpsd connection error: {exc}; retrying in 5s")
            await _pub_event(client, service, "connection", {"type": "gpsd_error", "message": str(exc)})
        await anyio.sleep(5)


async def _dispatch_nmea(client, service, line):
    global _buf_gps, _buf_mag, _buf_imu, _buf_hk

    if line.startswith("$PGPS") or line.startswith("$PGPN"):
        return
    if line.startswith("$") and not _nmea_verify(line):
        logger.debug(f"Bad checksum: {line!r}")
        return

    try:
        if line.startswith("$GNRMC"):
            _parse_gnrmc(line)
            await client.publish(service.topic_data_gps, msgspec.json.encode(_buf_gps))
        elif line.startswith("$GNGGA"):
            _parse_gngga(line)
            await client.publish(service.topic_data_gps, msgspec.json.encode(_buf_gps))
        elif line.startswith("$PMITMAG"):
            _buf_mag = _parse_pmitmag(line)
            await client.publish(service.topic_data_mag, msgspec.json.encode(_buf_mag))
        elif line.startswith("$PMITACC"):
            _buf_imu.update(_parse_pmitacc(line))
            await client.publish(service.topic_data_imu, msgspec.json.encode(_buf_imu))
        elif line.startswith("$PMITGYR"):
            _buf_imu.update(_parse_pmitgyr(line))
            await client.publish(service.topic_data_imu, msgspec.json.encode(_buf_imu))
        elif line.startswith("$PMITHK"):
            _buf_hk = _parse_pmithk(line)
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


async def _pub_event(client, service, etype, payload):
    await client.publish(f"{service.name}/event/{etype}",
                         msgspec.json.encode({**payload, "timestamp": time.time()}))


async def _handle_pmitsr(client, service, line):
    global _seq_reg, _seq_imu, _seq_mag, _seq_time
    _parse_pmitsr(line)

    parts = line.split("*")[0].split(",")
    if len(parts) < 3:
        return
    sc, tlc = str(parts[1]), parts[2]

    if sc != "0":
        await _pub_event(client, service, "error",
                         {"type": "firmware_error", "status": sc, "tlc": tlc, "fields": parts[3:]})
        return

    if tlc in _TLC_MAP:
        _seq_reg += 1
        await client.publish(service.topic_status_registers, msgspec.json.encode({
            "seq": _seq_reg, "timestamp": time.time(),
            "registers": _reg["registers"], "registers_named": _reg["registers_named"],
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


async def _send_announce(client, service):
    await client.publish(service.topic_announce, msgspec.json.encode({
        "title": "AFE service",
        "description": "Control and monitor MEP analog front-end instrument (RP2040)",
        "author": "John Marino <john.marino@colorado.edu>",
        "version": "2.0", "type": "service", "time_started": time.time(),
        "topics": {
            "command": service.topic_command, "response": f"{service.name}/response",
            "status": service.topic_status,
            "data": {"gps": service.topic_data_gps, "imu": service.topic_data_imu,
                     "mag": service.topic_data_mag, "hk": service.topic_data_hk},
            "event": f"{service.name}/event",
        },
        "command_subtopics": _command_topic_map(service.topic_command),
        "describe": _DESCRIBE,
    }), retain=True)


async def _send_status(client, service):
    global _seq
    _seq += 1
    await client.publish(service.topic_status, msgspec.json.encode({
        "seq": _seq, "timestamp": time.time(), "state": "online",
        "node_id": service.node_id, "device": service.str_device,
        "telemetry_logging_enabled": service.bool_logging_enabled,
        "telemetry_log_dir": service.str_log_dir,
        "service_log_mode": _log_mode(),
        "service_telem_workaround_enabled": _USE_SERVICE_TELEM_WORKAROUND,
        "service_telem_poll_interval_s": _effective_poll_interval_s(),
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
    for cmd in nmea_list:
        logger.info(f"  NMEA → {cmd!r}")
        try:
            await _gpsd_send_async(cmd, service.str_device)
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
                             "exception": "Deprecated command disabled. Use afe/command/polling with set_interval/get_interval.",
                             "subtopic": sub,
                         },
                         payload, sub)


async def _service_set_interval(client, service, args, payload):
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
        await _gpsd_send_async(cmd, service.str_device)
        await _send_response(client, service,
                             {"state": "ok", "message": "'telem_dump' accepted", "commands_sent": 1}, payload)
    except Exception as exc:
        await _send_response(client, service,
                             {"state": "error", "message": "Command write failed", "error": str(exc)}, payload)


async def _poll_telem(client, service):
    while True:
        try:
            interval = _effective_poll_interval_s()
            if _USE_SERVICE_TELEM_WORKAROUND and interval > 0:
                cmd = _nmea_cksum("$TELEM?*")
                await _gpsd_send_async(cmd, service.str_device)
                await anyio.sleep(interval)
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
                elif tn == "describe":
                    await _send_response(client, service, _DESCRIBE, payload)
                elif tn == "telem_dump":
                    await _service_telem_dump(client, service, payload)
                else:
                    await _send_response(client, service,
                                         {"exception": f"Unknown service command: {tn!r}"}, payload)

            elif sub in _HANDLERS:
                if tn == "describe":
                    await _send_response(client, service, _DESCRIBE.get(sub, {}), payload, sub)
                elif _USE_SERVICE_TELEM_WORKAROUND and sub in ("imu", "mag", "hk") and tn in ("set_rate", "get_rate"):
                    await _service_reject_deprecated_rate(client, service, sub, payload)
                else:
                    await _dispatch_nmea_cmd(client, service, _HANDLERS[sub], tn, args, payload, sub)

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
    elif tn == "enable_logging":
        service.bool_logging_enabled = True
        await _send_response(client, service, {"state": "ok", "telemetry_logging_enabled": True}, payload, s)
        await _send_status(client, service)
        await _pub_event(client, service, "logging", {"type": "logging_enabled", "log_dir": service.str_log_dir})
    elif tn == "disable_logging":
        service.bool_logging_enabled = False
        await _send_response(client, service, {"state": "ok", "telemetry_logging_enabled": False}, payload, s)
        await _send_status(client, service)
        await _pub_event(client, service, "logging", {"type": "logging_disabled"})
    elif tn == "get_log_status":
        await _send_response(client, service, {
            "telemetry_logging_enabled": service.bool_logging_enabled,
            "telemetry_log_dir": service.str_log_dir,
            "telemetry_log_rate_s": service.int_telem_rate,
            "service_log_mode": _log_mode(),
        }, payload, s)
    elif tn == "set_log_path":
        service.str_log_dir = str(args["path"])
        await _send_response(client, service, {"state": "ok", "telemetry_log_dir": service.str_log_dir}, payload, s)
        await _send_status(client, service)
    elif tn == "set_log_rate_sec":
        n = int(args["n"])
        if n < 1:
            await _send_response(client, service, {"exception": f"log_rate_s must be >= 1, got {n}"}, payload, s)
        else:
            service.int_telem_rate = n
            await _send_response(client, service, {"state": "ok", "telemetry_log_rate_s": n}, payload, s)
            await _send_status(client, service)
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
class AfeService:
    name: str = "afe"
    node_id: Optional[str] = None
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    gpsd_host: str = "localhost"
    gpsd_port: int = 2947
    str_device: str = "/dev/ttyGNSS1"
    int_telem_rate: int = 60
    str_log_dir: str = "/data/log_telemetry"
    bool_logging_enabled: bool = True

    topic_announce: str = dataclasses.field(init=False)
    topic_command: str = dataclasses.field(init=False)
    topic_status: str = dataclasses.field(init=False)
    topic_status_registers: str = dataclasses.field(init=False)
    topic_status_imu: str = dataclasses.field(init=False)
    topic_status_mag: str = dataclasses.field(init=False)
    topic_status_time: str = dataclasses.field(init=False)
    topic_data_gps: str = dataclasses.field(init=False)
    topic_data_imu: str = dataclasses.field(init=False)
    topic_data_mag: str = dataclasses.field(init=False)
    topic_data_hk: str = dataclasses.field(init=False)

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
        self.topic_data_gps         = f"{n}/data/gps"
        self.topic_data_imu         = f"{n}/data/imu"
        self.topic_data_mag         = f"{n}/data/mag"
        self.topic_data_hk          = f"{n}/data/hk"


# ============================================================================
# CSV LOGGING
# ============================================================================

async def _emit_csv(service):
    while True:
        await anyio.sleep(max(1, int(service.int_telem_rate)))
        if not service.bool_logging_enabled:
            continue
        try:
            now = datetime.now(timezone.utc)
            os.makedirs(service.str_log_dir, exist_ok=True)
            path = os.path.join(service.str_log_dir, f"telemetry_{now.strftime('%Y%m%d')}.csv")
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["# snapshot_utc", now.isoformat()])
                for label, buf in [("gnss", _buf_gps), ("mag", _buf_mag),
                                   ("imu", _buf_imu), ("housekeeping", _buf_hk)]:
                    if buf:
                        w.writerow([f"# {label}"])
                        w.writerow(list(buf.keys()))
                        w.writerow(list(buf.values()))
                w.writerow([])
            logger.info(f"Telemetry logged: {path}")
        except Exception:
            logger.exception("CSV write error")


# ============================================================================
# MAIN
# ============================================================================

async def main(service):
    global _gpsd_send_lock
    _gpsd_send_lock = anyio.Lock()
    will = aiomqtt.Will(
        service.topic_status,
        payload=msgspec.json.encode({"state": "offline", "seq": -1, "timestamp": time.time()}),
        qos=0, retain=True,
    )
    client = aiomqtt.Client(service.mqtt_host, service.mqtt_port, keepalive=60, will=will)
    while True:
        try:
            async with client:
                await client.subscribe(service.topic_command)
                await client.subscribe(service.topic_command + "/#")
                await _send_announce(client, service)
                await _send_status(client, service)
                with exceptiongroup.catch({Exception: lambda e: logger.error("Task exception", exc_info=e)}):
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(_monitor_gpsd, client, service)
                        tg.start_soon(_process_commands, client, service)
                        tg.start_soon(_poll_telem, client, service)
                        tg.start_soon(_emit_csv, service)
        except aiomqtt.MqttError:
            logger.warning("MQTT connection lost; reconnecting in 5s ...")
            await anyio.sleep(5)


if __name__ == "__main__":
    logger.info("Starting afe_service")
    service = jsonargparse.auto_cli(AfeService, env_prefix="AFE", default_env=True)
    anyio.run(main, service)
