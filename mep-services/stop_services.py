#!/usr/bin/env python3
"""Stop the MEP services."""

import subprocess
from pathlib import Path


service_names = sorted(path.name for path in Path(__file__).parent.glob("*/*.service"))

service_names.remove("service-manager.service")
for service in ["service-manager.service", *service_names]:
    subprocess.run(["sudo", "systemctl", "stop", service], check=True)

print("MEP services stopped")