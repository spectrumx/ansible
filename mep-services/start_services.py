#!/usr/bin/env python3
"""Temporarily link and restart repository-owned MEP systemd services."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent
SERVICE_MANAGER_UNIT = "service-manager.service"


def _discover_unit_paths() -> list[Path]:
    return sorted(REPOSITORY_ROOT.glob("*/*.service"), key=lambda path: path.name)


def _systemctl_command() -> list[str]:
    if os.geteuid() == 0:
        return ["systemctl"]
    if not shutil.which("sudo"):
        raise RuntimeError("This helper requires root or sudo access to systemd.")
    return ["sudo", "systemctl"]


def _run(command: list[str]) -> bool:
    print("+ " + " ".join(str(part) for part in command))
    return subprocess.run(command, check=False).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Temporarily link and restart every repository-owned MEP systemd service."
    )
    parser.add_argument("--list", action="store_true", help="show discovered units without changing systemd")
    args = parser.parse_args()

    unit_paths = _discover_unit_paths()
    if not unit_paths:
        print(f"No service files found under {REPOSITORY_ROOT}", file=sys.stderr)
        return 1
    if args.list:
        for path in unit_paths:
            print(f"{path.name}: {path}")
        return 0
    if sys.platform != "linux":
        print("Service launching is supported only on the Linux target.", file=sys.stderr)
        return 2
    if not shutil.which("systemctl"):
        print("systemctl is not available on this target.", file=sys.stderr)
        return 2

    try:
        systemctl = _systemctl_command()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2

    link_warnings = []
    for path in unit_paths:
        if not _run([*systemctl, "--force", "link", str(path)]):
            link_warnings.append(path.name)
    if link_warnings:
        print(
            "Could not replace links for these units; existing installed definitions will be used: "
            + ", ".join(link_warnings),
            file=sys.stderr,
        )
    if not _run([*systemctl, "daemon-reload"]):
        print("Failed to reload systemd unit definitions.", file=sys.stderr)
        return 1

    unit_names = [path.name for path in unit_paths]
    managed_units = [name for name in unit_names if name != SERVICE_MANAGER_UNIT]
    failed_units = [
        unit for unit in managed_units
        if not _run([*systemctl, "restart", unit])
    ]
    if SERVICE_MANAGER_UNIT in unit_names:
        if not _run([*systemctl, "restart", SERVICE_MANAGER_UNIT]):
            failed_units.append(SERVICE_MANAGER_UNIT)

    if failed_units:
        print("Failed to start: " + ", ".join(failed_units), file=sys.stderr)
    print("MEP service restart attempted under systemd (not enabled at boot).")
    print("Inspect them with: systemctl status <unit>")
    return 1 if failed_units else 0


if __name__ == "__main__":
    raise SystemExit(main())