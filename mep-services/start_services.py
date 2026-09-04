#!/usr/bin/env python3
"""Link and start the MEP services."""

import subprocess
from pathlib import Path


units = sorted(Path(__file__).parent.glob("*/*.service"), key=lambda path: path.name)

for unit in units:
    print(f"Linking {unit.name}...", flush=True)
    subprocess.run(["sudo", "systemctl", "link", "--force", str(unit.resolve())], check=True)

print("Reloading systemd...", flush=True)
subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
service_names = [unit.name for unit in units]
service_names.remove("service-manager.service")
for service in [*service_names, "service-manager.service"]:
    print(f"Starting {service}...", flush=True)
    subprocess.run(["sudo", "systemctl", "restart", service], check=True)

print("MEP services started.")
print("Status: systemctl status <service>")
print("Logs:   journalctl -u <service> -f")