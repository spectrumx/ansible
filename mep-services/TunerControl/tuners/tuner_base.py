# SPDX-FileCopyrightText: Copyright (c) 2026 University of Colorado
# SPDX-License-Identifier: Apache-2.0

"""Common tuner interface.

Authors:
    Nicholas Rainville <nicholas.rainville@colorado.edu>
    Ryan Volz <rvolz@mit.edu>
    John Marino <john.marino@colorado.edu> (09/2026)
"""


class Tuner:
    name = "tuner"
    supported_commands = ["set_frequency", "get_frequency"]

    def initialize(self):
        raise NotImplementedError

    def close(self):
        pass

    def capabilities(self):
        return {
            "frequency": "set_frequency" in self.supported_commands,
            "power": "set_power" in self.supported_commands,
            "external_reference": "set_external_reference" in self.supported_commands,
            "reference_frequency": "set_reference_frequency" in self.supported_commands,
            "lock_status": "get_lock_status" in self.supported_commands,
        }

    def status(self):
        capabilities = self.capabilities()
        return {
            "name": self.name,
            "capabilities": capabilities,
            "lock_supported": capabilities["lock_status"],
            "lock_status": None,
            "lock_error": None,
        }

    def set_frequency(self, frequency_mhz):
        raise NotImplementedError

    def get_frequency(self):
        raise NotImplementedError

    def set_power(self, power_dbm):
        raise RuntimeError(f"The {self.name} tuner does not support power control")

    def get_power(self):
        raise RuntimeError(f"The {self.name} tuner does not support power control")

    def set_external_reference(self, enabled):
        raise RuntimeError(f"The {self.name} tuner does not support external reference control")

    def get_external_reference(self):
        raise RuntimeError(f"The {self.name} tuner does not support external reference control")

    def set_reference_frequency(self, frequency_mhz):
        raise RuntimeError(f"The {self.name} tuner does not support reference frequency control")

    def get_reference_frequency(self):
        raise RuntimeError(f"The {self.name} tuner does not support reference frequency control")

    def get_lock_status(self):
        raise RuntimeError(f"The {self.name} tuner does not support lock status")
