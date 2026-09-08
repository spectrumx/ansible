# SPDX-FileCopyrightText: Copyright (c) 2026 Massachusetts Institute of Technology
# SPDX-License-Identifier: Apache-2.0

"""Dummy tuner for testing.

Author: Ryan Volz <rvolz@mit.edu> (02/2026)
"""

import logging

from .tuner_base import Tuner


logger = logging.getLogger(__name__)


class DummyTuner(Tuner):
    name = "dummy"

    def __init__(self):
        self.frequency_mhz = None

    def initialize(self):
        self.frequency_mhz = None

    def set_frequency(self, frequency_mhz):
        logger.info("Setting dummy tuner frequency to %s MHz", frequency_mhz)
        self.frequency_mhz = frequency_mhz
        return frequency_mhz

    def get_frequency(self):
        return self.frequency_mhz

    def status(self):
        status = super().status()
        status["frequency_mhz"] = self.frequency_mhz
        return status
