# SPDX-FileCopyrightText: Copyright (c) 2026 Massachusetts Institute of Technology
# SPDX-License-Identifier: Apache-2.0

"""Valon 5015/5019 RF synthesizer support.

Authors:
    Alisa Yurevich <Alisa.Yurevich@tufts.edu> (06/2025)
    Ryan Volz <rvolz@mit.edu> (01/2026)
    John Marino <john.marino@colorado.edu> (09/2026)
"""

import logging
import re
import time

import serial

from .tuner_base import Tuner


logger = logging.getLogger(__name__)


class ValonTuner(Tuner):
    name = "valon"

    # Configure the Valon hardware installed on this device.
    device = "/dev/valon5015"
    baudrate = 9600
    timeout = 1.0
    startup_power_dbm = 10.0
    startup_external_reference = True
    startup_reference_frequency_mhz = 10.0

    supported_commands = Tuner.supported_commands + [
        "set_power",
        "get_power",
        "set_external_reference",
        "get_external_reference",
        "set_reference_frequency",
        "get_reference_frequency",
        "get_lock_status",
    ]

    def __init__(self):
        self.frequency_mhz = None
        self.power_dbm = None
        self.external_reference = None
        self.reference_frequency_mhz = None
        self.lock_status = None
        self.lock_error = None
        self.info = None
        self.serial = None

    def initialize(self):
        self.serial = serial.Serial(port=self.device, baudrate=self.baudrate, timeout=self.timeout)
        self.serial.reset_input_buffer()
        self.info = self.send_command("STAT", 2.0)
        self.get_frequency()
        self.set_power(self.startup_power_dbm)
        self.set_external_reference(self.startup_external_reference)
        self.set_reference_frequency(self.startup_reference_frequency_mhz)

        # Lock is useful status, but failure to read it does not make the tuner unusable.
        try:
            self.get_lock_status()
        except Exception as error:
            self.lock_error = str(error)
            logger.warning("Could not read Valon lock status: %s", error)

    def close(self):
        if self.serial is not None:
            self.serial.close()
            self.serial = None

    def send_command(self, command, wait=0.1, retries=3):
        for attempt in range(retries):
            try:
                self.serial.write((command + "\r").encode())
                time.sleep(wait)
                response = b""
                while self.serial.in_waiting:
                    response += self.serial.read_until(size=self.serial.in_waiting)
                return response.decode(errors="ignore")
            except Exception:
                if attempt == retries - 1:
                    raise
                logger.warning("Valon command failed; retrying: %s", command)
                self.serial.reset_input_buffer()
                self.serial.reset_output_buffer()

    def parse_frequency(self, response):
        match = re.search(r"^F\s+(?P<frequency>[\d.]+)\s+MHz;", response, re.MULTILINE)
        if not match:
            raise RuntimeError(f"Could not parse Valon frequency response: {response}")
        return float(match["frequency"])

    def set_frequency(self, frequency_mhz):
        response = self.send_command(f"F{frequency_mhz}MHz")
        self.frequency_mhz = self.parse_frequency(response)
        return self.frequency_mhz

    def get_frequency(self):
        response = self.send_command("F?")
        self.frequency_mhz = self.parse_frequency(response)
        return self.frequency_mhz

    def parse_power(self, response):
        match = re.search(r"^PWR\s+(?P<power>[-+]?[\d.]+);\s+//\s+dBm", response, re.MULTILINE)
        if not match:
            raise RuntimeError(f"Could not parse Valon power response: {response}")
        return float(match["power"])

    def set_power(self, power_dbm):
        response = self.send_command(f"PWR {power_dbm}")
        self.power_dbm = self.parse_power(response)
        return self.power_dbm

    def get_power(self):
        response = self.send_command("PWR?")
        self.power_dbm = self.parse_power(response)
        return self.power_dbm

    def parse_external_reference(self, response):
        match = re.search(r"^REFS\s+(?P<enabled>\d);", response, re.MULTILINE)
        if not match:
            raise RuntimeError(f"Could not parse Valon reference response: {response}")
        return bool(int(match["enabled"]))

    def set_external_reference(self, enabled):
        response = self.send_command(f"REFS {int(enabled)}")
        self.external_reference = self.parse_external_reference(response)
        return self.external_reference

    def get_external_reference(self):
        response = self.send_command("REFS?")
        self.external_reference = self.parse_external_reference(response)
        return self.external_reference

    def parse_reference_frequency(self, response):
        match = re.search(r"^REF\s+(?P<frequency>[\d.]+)\s+MHz;", response, re.MULTILINE)
        if not match:
            raise RuntimeError(f"Could not parse Valon reference frequency response: {response}")
        return float(match["frequency"])

    def set_reference_frequency(self, frequency_mhz):
        response = self.send_command(f"REF {frequency_mhz}MHz")
        self.reference_frequency_mhz = self.parse_reference_frequency(response)
        return self.reference_frequency_mhz

    def get_reference_frequency(self):
        response = self.send_command("REF?")
        self.reference_frequency_mhz = self.parse_reference_frequency(response)
        return self.reference_frequency_mhz

    def get_lock_status(self):
        response = self.send_command("LK")
        lines = response.splitlines()
        lock_line = False
        lock_status = {}
        for line in lines:
            if line.startswith("LK"):
                lock_line = True
            elif lock_line:
                match = re.match(r"^(?P<name>.+?)\s+:\s+(?P<status>.+?)$", line)
                if match:
                    name = match["name"].lower().replace(" ", "_")
                    lock_status[name] = match["status"] == "locked"
        if not lock_line or not lock_status:
            raise RuntimeError(f"Could not parse Valon lock response: {response}")
        self.lock_status = lock_status
        self.lock_error = None
        return lock_status

    def status(self):
        status = super().status()
        status["frequency_mhz"] = self.frequency_mhz
        status["power_dbm"] = self.power_dbm
        status["external_reference"] = self.external_reference
        status["reference_frequency_mhz"] = self.reference_frequency_mhz
        status["lock_status"] = self.lock_status
        status["lock_error"] = self.lock_error
        status["info"] = self.info
        return status
