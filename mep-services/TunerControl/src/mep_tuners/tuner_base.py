# SPDX-FileCopyrightText: Copyright (c) 2026 University of Colorado
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
"""
tuner_base.py

Abstract base class for MEP tuner devices defining the standard tuner interface.

Author: nicholas.rainville@colorado.edu
Author: rvolz@mit.edu
"""

import dataclasses
import typing
from abc import ABC, abstractmethod


@dataclasses.dataclass(kw_only=True)
class TunerBase(ABC):
    name: str = "tuner_base"
    """Name of tuner instance"""
    freq_mhz: typing.Optional[float] = None
    """Tuner frequency in MHz"""

    def __post_init__(self):
        if self.freq_mhz is not None:
            self.set_freq(self.freq_mhz)
        else:
            # on subclasses, may be used to query actual frequency and set attribute
            self.get_freq()

    @abstractmethod
    def set_freq(self, freq_mhz: float):
        """Set tuner frequency - must be implemented by child classes"""
        self.freq_mhz = freq_mhz

    def get_freq(self):
        """Set tuner frequency - must be implemented by child classes"""
        return self.freq_mhz


@dataclasses.dataclass
class TunerParamsBase:
    tuner_class: typing.ClassVar[TunerBase] = TunerBase

    def create_tuner(self):
        return self.tuner_class(**dataclasses.asdict(self))
