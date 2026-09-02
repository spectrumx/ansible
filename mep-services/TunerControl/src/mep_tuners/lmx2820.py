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
"""LMX2820

Ryan Volz (rvolz@mit.edu) 02/2026
"""

import dataclasses
import logging
import os
import typing

import pydantic

from .tuner_base import TunerBase, TunerParamsBase

logger = logging.getLogger(__name__)
logger.setLevel(
    os.environ.get(f"{__name__.replace('.', '_').upper()}_LOG_LEVEL", "NOTSET")
)


@dataclasses.dataclass(kw_only=True)
class LMX2820Tuner(TunerBase):
    """Tuner control for LMX2820 device"""

    name: str = "lmx2820"
    """Name of tuner instance"""
    ref_freq: float = 10e6
    """Reference frequency in Hz"""
    ref_doubler: typing.Literal[0, 1] = 0
    """Reference doubler"""
    ref_multiplier: typing.Literal[1, 3, 5, 7] = 1
    """Reference multiplier"""
    ref_pre_div: typing.Annotated[int, pydantic.Field(ge=1, lt=4096)] = 1
    """Reference pre-divider"""
    ref_post_div: typing.Annotated[int, pydantic.Field(ge=1, lt=256)] = 1
    """Reference post-divider"""

    def __post_init__(self):
        """Initialize LMX2820 specific hardware/resources"""
        # set environment variable to register the FT232H board
        os.environ["BLINKA_FT232H"] = "1"

        # don't import LMX2820 stuff until needed because the imports fail
        # when the device cannot be detected
        import board
        import busio
        import digitalio

        from .lmx2820_impl import LMX2820, LMX2820ChangeFreq, LMX2820StartUp

        self.tuner_impl = LMX2820(
            self.ref_freq,
            self.ref_doubler,
            self.ref_multiplier,
            self.ref_pre_div,
            self.ref_post_div,
        )
        self.spi = busio.SPI(
            board.SCK,  # clock
            board.MOSI,  # mosi
            board.MISO,  # miso
        )
        self.CSpin = digitalio.DigitalInOut(board.D4)
        self.CSpin.switch_to_output(value=True)

        # start tuner with initial settings
        LMX2820StartUp(self.tuner_impl, self.spi, self.CSpin)

        # complete parent init (will set frequency if specified in init params)
        super().__post_init__()

        self._set_freq = LMX2820ChangeFreq

    def set_freq(self, freq_mhz: float):
        """Set the output frequency of the tuner"""
        # actual frequency is set in integer Hertz
        f_lo_hz = int(freq_mhz * 1e6)
        logger.info(f"Setting local oscillator frequency to {f_lo_hz} Hz")
        self._set_freq(self.spi, self.CSpin, self.tuner_impl, f_lo_hz)
        self.freq_mhz = f_lo_hz / 1e6
        return self.freq_mhz


LMX2820TunerParams = dataclasses.make_dataclass(
    "LMX2820TunerParams",
    [(f.name, f.type, f) for f in dataclasses.fields(LMX2820Tuner)]
    + [
        (
            "tuner_class",
            typing.ClassVar[TunerBase],
            dataclasses.field(default=LMX2820Tuner),
        )
    ],
    bases=(TunerParamsBase,),
    kw_only=True,
)
