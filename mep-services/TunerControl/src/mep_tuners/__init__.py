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
import logging
import os

from .dummy import DummyTuner, DummyTunerParams
from .lmx2820 import LMX2820Tuner, LMX2820TunerParams
from .tuner_base import TunerBase
from .valon import ValonTuner, ValonTunerParams

__all__ = (
    DummyTuner,
    DummyTunerParams,
    LMX2820Tuner,
    LMX2820TunerParams,
    TunerBase,
    ValonTuner,
    ValonTunerParams,
)

logger = logging.getLogger(__name__)
logger.setLevel(
    os.environ.get(f"{__name__.replace('.', '_').upper()}_LOG_LEVEL", "NOTSET")
)
logger.propagate = False
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.DEBUG)
logger.addHandler(_console_handler)
