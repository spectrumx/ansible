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
"""Valon 5015/5019 RF Synthesizer

Alisa Yurevich (Alisa.Yurevich@tufts.edu) 06/2025
Ryan Volz (rvolz@mit.edu) 01/2026
"""

import dataclasses
import logging
import os
import re
import time
import typing

import serial

from .tuner_base import TunerBase, TunerParamsBase

logger = logging.getLogger(__name__)
logger.setLevel(
    os.environ.get(f"{__name__.replace('.', '_').upper()}_LOG_LEVEL", "NOTSET")
)


@dataclasses.dataclass(kw_only=True)
class ValonTuner(TunerBase):
    """Valon 5015/5019 RF Synthesizer over a serial connection.

    This tool configures the synthesizer via USB serial connection.
    NOTE: Internal error from serial is thrown sometimes when the VALON is run for too
    long (hours). Possible solutions include:
        - a reset function (although this will introduce time delay in a sweep)
        - higher/lower baud rate
        - shorter sleep after send
    At a power input of 0, the relative output is equal to that of the LMX2820.

    """

    name: str = "valon"
    """Name of tuner instance"""
    port: str = "/dev/valon5015"  # default assumes persistent udev symlink
    """Serial device name"""
    baudrate: int = 9600
    """Serial device baud rate"""
    timeout: float = 1.0
    """Serial device read timeout in seconds"""
    pwr_dbm: typing.Optional[float] = None
    """Output power in dBm"""
    external_ref: typing.Optional[bool] = None
    """Whether or not to use an external reference source"""
    external_ref_freq_mhz: typing.Optional[float] = None
    """External reference source frequency in MHz"""
    info: typing.Optional[str] = None
    """Status string (set upon initialization)"""

    def __post_init__(self):
        self.ser = serial.Serial(
            port=self.port, baudrate=self.baudrate, timeout=self.timeout
        )
        # one way to clear -> can also turn dtr on and off
        self.ser.reset_input_buffer()

        logger.info("Getting VALON device status string:")
        self.info = self.send_cmd("STAT", wait=2.0)
        # if logger level is debug or lower, we have already logged the info
        if logger.getEffectiveLevel() > logging.DEBUG:
            logger.info(self.info)

        super().__post_init__()

        if self.pwr_dbm is not None:
            self.set_power(self.pwr_dbm)
        else:
            # sets the attribute
            self.get_power()

        if self.external_ref is not None:
            self.set_external_ref(self.external_ref)
        else:
            # sets the attribute
            self.get_external_ref()

        if self.external_ref_freq_mhz is not None:
            self.set_external_ref_freq(self.external_ref_freq_mhz)
        else:
            # sets the attribute
            self.get_external_ref_freq()

    def _send_cmd(self, command: str, wait: float = 0.1):
        """Send a command string to the Valon over serial.

        Appends carriage return. Returns any response.

        """
        self.ser.write((command + "\r").encode())
        time.sleep(wait)
        response = b""

        while self.ser.in_waiting:
            # read by line or until we get all waiting bytes or timeout
            response += self.ser.read_until(size=self.ser.in_waiting)

        return response.decode(errors="ignore")

    def send_cmd(self, command: str, wait: float = 0.1, retries: int = 3):
        """Send a command string to the Valon over serial.

        Appends carriage return. Returns any response.

        """
        for n in range(retries):
            try:
                logger.debug(f"Sending tuner command: {command}")
                response = self._send_cmd(command, wait=wait)
                logger.debug(response)
            except Exception as e:
                if n == (retries - 1):
                    raise e
                else:
                    logger.warning(f"Failed to send command: {command}", exc_info=True)
                    self.ser.reset_input_buffer()
                    self.ser.reset_output_buffer()
            else:
                break
        return response

    @staticmethod
    def _parse_freq_response(result):
        for line in result.splitlines():
            m = re.match(r"^F\s+(?P<freq_mhz>[\d\.]+)\s+MHz;", line)
            if m:
                freq_mhz = float(m["freq_mhz"])
                break
        else:
            msg = f"Could not get frequency from freq result: {result}"
            raise RuntimeError(msg)
        return freq_mhz

    def set_freq(self, freq_mhz: float, wait: float = 0.1):
        """Set the output frequency in MHz of the synthesizer."""
        logger.info(f"Setting local oscillator frequency to {freq_mhz} MHz")
        cmd = f"F{freq_mhz}MHz"
        result = self.send_cmd(cmd, wait=wait)
        act_freq_mhz = self._parse_freq_response(result)
        self.freq_mhz = act_freq_mhz
        return act_freq_mhz

    def get_freq(self, wait: float = 0.1):
        """Get the output frequency in MHz of the synthesizer."""
        cmd = "F?"
        result = self.send_cmd(cmd, wait=wait)
        act_freq_mhz = self._parse_freq_response(result)
        self.freq_mhz = act_freq_mhz
        return act_freq_mhz

    @staticmethod
    def _parse_power_response(result):
        for line in result.splitlines():
            m = re.match(r"^PWR\s+(?P<pwr_dbm>[\d\.]+);\s+//\s+dBm", line)
            if m:
                pwr_dbm = float(m["pwr_dbm"])
                break
        else:
            msg = f"Could not get power from set_power result: {result}"
            raise RuntimeError(msg)
        return pwr_dbm

    def set_power(self, pwr_dbm: float, wait: float = 0.1):
        """Set output power level in dBm.

        Valid Range -50 - 20. Can be brought lower configuring extra settings in the
        Valon.

        """
        logger.info(f"Setting output power level to {pwr_dbm} dBm")
        cmd = f"PWR {pwr_dbm}"
        result = self.send_cmd(cmd, wait=wait)
        act_pwr_dbm = self._parse_power_response(result)
        self.pwr_dbm = act_pwr_dbm
        return act_pwr_dbm

    def get_power(self, wait: float = 0.1):
        """Get the output power level in dBm."""
        cmd = "PWR?"
        result = self.send_cmd(cmd, wait=wait)
        act_pwr_dbm = self._parse_power_response(result)
        self.pwr_dbm = act_pwr_dbm
        return act_pwr_dbm

    @staticmethod
    def _parse_refs_response(result):
        for line in result.splitlines():
            m = re.match(r"^REFS\s+(?P<external_ref>\d);", line)
            if m:
                external_ref = bool(m["external_ref"])
                break
        else:
            msg = f"Could not get external_ref from REFS result: {result}"
            raise RuntimeError(msg)
        return external_ref

    def set_external_ref(self, enabled: bool, wait: float = 0.1):
        """Enable (True) or disable (False) the external reference source."""
        logger.info(f"Setting external reference source to enabled={enabled}")
        cmd = f"REFS {int(enabled)}"
        result = self.send_cmd(cmd, wait=wait)
        act_external_ref = self._parse_refs_response(result)
        self.external_ref = act_external_ref
        return act_external_ref

    def get_external_ref(self, wait: float = 0.1):
        """Get whether the external reference is enabled (T) or disabled (F)."""
        cmd = "REFS?"
        result = self.send_cmd(cmd, wait=wait)
        act_external_ref = self._parse_refs_response(result)
        self.external_ref = act_external_ref
        return act_external_ref

    @staticmethod
    def _parse_ref_freq_response(result):
        for line in result.splitlines():
            m = re.match(r"^REF\s+(?P<ref_freq_mhz>[\d\.]+)\s+MHz;", line)
            if m:
                ref_freq_mhz = float(m["ref_freq_mhz"])
                break
        else:
            msg = f"Could not get reference frequency from REF result: {result}"
            raise RuntimeError(msg)
        return ref_freq_mhz

    def set_external_ref_freq(self, freq_mhz: float, wait: float = 0.1):
        """Set the external reference source frequency in MHz."""
        logger.info(f"Setting reference source frequency to {freq_mhz} MHz")
        cmd = f"REF {freq_mhz}MHz"
        result = self.send_cmd(cmd, wait=wait)
        act_freq_mhz = self._parse_ref_freq_response(result)
        self.external_ref_freq_mhz = act_freq_mhz
        return act_freq_mhz
        return act_freq_mhz

    def get_external_ref_freq(self, wait: float = 0.1):
        """Get the external reference source frequency in MHz."""
        cmd = "REF?"
        result = self.send_cmd(cmd, wait=wait)
        act_freq_mhz = self._parse_ref_freq_response(result)
        self.external_ref_freq_mhz = act_freq_mhz
        return act_freq_mhz

    def get_lock_status(self, wait: float = 0.1):
        """Return the status of the PLL lock condition from Main and Sub PLLs"""
        logger.info("Getting PLL lock condition from Main and Sub PLLs")
        cmd = "LK"
        result = self.send_cmd(cmd, wait=wait)
        lock_line_received = False
        pll_locked_dict = {}
        for line in result.splitlines():
            if not lock_line_received:
                m = re.match(r"^LK", line)
                if m:
                    lock_line_received = True
            else:
                m = re.match(r"^(?P<pll_name>.+?)\s+:\s+(?P<status>.+?)$", line)
                if m:
                    pll_name = m["pll_name"].lower().replace(" ", "_")
                    locked = m["status"] == "locked"
                    pll_locked_dict[pll_name] = locked
        if not lock_line_received or not pll_locked_dict:
            msg = f"Could not parse get_lock_status: {result}"
            raise RuntimeError(msg)
        self.locked = all(pll_locked_dict.values())
        return pll_locked_dict


ValonTunerParams = dataclasses.make_dataclass(
    "ValonTunerParams",
    [(f.name, f.type, f) for f in dataclasses.fields(ValonTuner)]
    + [
        (
            "tuner_class",
            typing.ClassVar[TunerBase],
            dataclasses.field(default=ValonTuner),
        )
    ],
    bases=(TunerParamsBase,),
    kw_only=True,
)
