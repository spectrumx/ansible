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
"""Dummy tuner for testing

Ryan Volz (rvolz@mit.edu) 02/2026
"""

import dataclasses
import logging
import os
import typing

from .tuner_base import TunerBase, TunerParamsBase

logger = logging.getLogger(__name__)
logger.setLevel(
    os.environ.get(f"{__name__.replace('.', '_').upper()}_LOG_LEVEL", "NOTSET")
)


@dataclasses.dataclass(kw_only=True)
class DummyTuner(TunerBase):
    """Dummy tuner for testing, does nothing but print to log"""

    name: str = "dummy"
    """Name of tuner instance"""

    def set_freq(self, freq_mhz: float):
        """Set the output frequency of the tuner"""
        logger.info(f"{self.name} tuner setting frequency to {freq_mhz} MHz")
        self.freq_mhz = freq_mhz
        return freq_mhz


DummyTunerParams = dataclasses.make_dataclass(
    "DummyTunerParams",
    [(f.name, f.type, f) for f in dataclasses.fields(DummyTuner)]
    + [
        (
            "tuner_class",
            typing.ClassVar[TunerBase],
            dataclasses.field(default=DummyTuner),
        )
    ],
    bases=(TunerParamsBase,),
    kw_only=True,
)
