#!/usr/bin/env python3
"""Stop repository-owned MEP systemd services."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent
SERVICE_MANAGER_UNIT = "service-manager.service"


def _discover_unit_names() -> list[str]:
    return sorted(path.name for path in REPOSITORY_ROOT.glob("*/*.service"))


def _systemctl_command() -> list[str]:
    if os.geteuid() == 0:
        return ["systemctl"]
    if not shutil.which("sudo"):
        raise RuntimeError("This helper requires root or sudo access to systemd.")
    return ["sudo", "systemctl"]


def _run(command: list[str]) -> bool:
    print("+ " + " ".join(command))
    return subprocess.run(command, check=False).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stop every repository-owned MEP systemd service."
    )
    parser.add_argument("--list", action="store_true", help="show discovered units without changing systemd")
    args = parser.parse_args()

    unit_names = _discover_unit_names()
    if not unit_names:
        print(f"No service files found under {REPOSITORY_ROOT}", file=sys.stderr)
        return 1
    if args.list:
        print("\n".join(unit_names))
        return 0
    if sys.platform != "linux":
        print("Service stopping is supported only on the Linux target.", file=sys.stderr)
        return 2
    if not shutil.which("systemctl"):
        print("systemctl is not available on this target.", file=sys.stderr)
        return 2

    try:
        systemctl = _systemctl_command()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2

    failed = False
    if SERVICE_MANAGER_UNIT in unit_names:
        failed = not _run([*systemctl, "stop", SERVICE_MANAGER_UNIT])
    managed_units = [name for name in unit_names if name != SERVICE_MANAGER_UNIT]
    for unit in managed_units:
        if not _run([*systemctl, "stop", unit]):
            failed = True

    if failed:
        print("One or more MEP services failed to stop.", file=sys.stderr)
        return 1
    print("MEP services stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())