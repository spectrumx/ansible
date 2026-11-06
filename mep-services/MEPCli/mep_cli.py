#!/opt/radiohound/python313/bin/python
"""Start or stop RX captures through the public MEP client."""

import argparse
import json
import math
from pathlib import Path
import sys
import threading
import time


def capture_overrides(arguments):
    """Translate explicitly supplied CLI choices into the public settings fields."""
    acquisition, receive = {}, {}
    if arguments.frequency_mhz is not None:
        acquisition["frequency_hz"] = arguments.frequency_mhz * 1e6
    if arguments.duration_s is not None:
        acquisition.update(mode="single", duration_s=arguments.duration_s, sweep=None)
    sweep = {}
    for value, key, scale in (
        (arguments.end_frequency_mhz, "end_frequency_hz", 1e6),
        (arguments.step_mhz, "step_hz", 1e6),
        (arguments.dwell_s, "dwell_s", 1),
    ):
        if value is not None:
            sweep[key] = value * scale
    if sweep:
        acquisition.update(mode="sweep", duration_s=None, sweep=sweep)
    if arguments.channel is not None:
        receive["channel"] = arguments.channel
    if arguments.sample_rate_mhz is not None:
        receive["sample_rate_hz"] = arguments.sample_rate_mhz * 1e6
    if arguments.conjugate is not None:
        receive["conjugate"] = arguments.conjugate
    tuner = {}
    if arguments.tuner is not None:
        tuner["enabled"] = arguments.tuner == "enabled"
    if arguments.if_mhz is not None:
        tuner["adc_if_mhz"] = arguments.if_mhz
    if arguments.injection is not None:
        tuner["injection"] = arguments.injection
    if tuner:
        receive["external_tuner"] = tuner
    overrides = {}
    if acquisition:
        overrides["acquisition"] = acquisition
    if receive:
        overrides["receive"] = receive
    return overrides


def run(arguments, client_factory=None):
    document = {"format_version": 1, "settings": {}}
    if arguments.document:
        try:
            document = json.loads(Path(arguments.document).read_text(encoding="utf-8-sig"))
            if not isinstance(document, dict):
                raise ValueError("capture document must be a JSON object")
        except (OSError, ValueError) as error:
            print(f"Cannot load settings: {error}", file=sys.stderr)
            return 2
    overrides = capture_overrides(arguments)

    if client_factory is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from mep_client import MEPClient
        client_factory = MEPClient

    client = None
    try:
        client = client_factory(host=arguments.host, port=arguments.port)
        deadline = time.monotonic() + arguments.timeout
        waiting = threading.Event()
        while not client.is_connected():
            if time.monotonic() >= deadline:
                raise TimeoutError("Could not connect to the MEP bus; no acquisition command was sent")
            waiting.wait(0.05)

        def request(send, label):
            done = threading.Event()
            received = []

            def complete(response):
                received.append(response)
                done.set()

            sent = send(complete)
            if not sent.get("sent"):
                raise RuntimeError(f"Could not send {label}")
            if not done.wait(arguments.timeout):
                raise TimeoutError(f"No response to {label}; the outcome is uncertain. Do not automatically retry")
            response = received[0]
            data = response.get("status_data") or {}
            if not response.get("success") or data.get("state") in ("failed", "error"):
                raise ValueError(response.get("error") or data.get("error") or f"{label} failed")
            return data

        if arguments.stop:
            request(lambda callback: client.capture.stop_rx(callback=callback), "RX stop")
            print("RX stop request succeeded.")
            return 0
        resolved = request(lambda callback: client.capture.resolve_settings(document, callback=callback, settings_overrides=overrides), "settings validation")
        recipe = resolved["document"]
        if (arguments.if_mhz is not None or arguments.injection is not None) and not recipe["settings"]["receive"]["external_tuner"]["enabled"]:
            raise ValueError("IF/injection overrides require an enabled tuner; use --tuner enabled or enable it in the config")
        request(lambda callback: client.capture.start_document(recipe, arguments.capture_name, callback=callback), "capture start")
        acquisition = recipe["settings"]["acquisition"]
        receive = recipe["settings"]["receive"]
        print(f"Capture request accepted: {arguments.capture_name or 'preview'}")
        print(f"Frequency: {acquisition['frequency_hz'] / 1e6:g} MHz")
        print(f"Channel: {receive['channel']}")
        print(f"Output sample rate: {receive['sample_rate_hz'] / 1e6:g} MHz")
        print("CaptureOrchestrator continues independently; this command does not wait for completion.")
        return 0
    except KeyboardInterrupt:
        print("Client interrupted. Any submitted acquisition continues; use normal MEP controls to stop it.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(str(error), file=sys.stderr)
        return 2 if isinstance(error, ValueError) else 3
    finally:
        if client is not None:
            client.disconnect()


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(prog="mep_capture", description=__doc__, epilog="Explicit CLI choices override the optional JSON config. CaptureOrchestrator supplies omitted defaults and owns all acquisition timing.")
    parser.add_argument("document", nargs="?", help="Optional capture settings JSON file")
    parser.add_argument("--stop", action="store_true", help="Stop the current RX acquisition; no config is needed")
    parser.add_argument("--frequency-mhz", type=float, help="RF start frequency in MHz")
    parser.add_argument("--channel", type=str.upper, choices=("A", "B", "C", "D"), help="RX channel")
    parser.add_argument("--sample-rate-mhz", type=float, help="Recorder output sample rate in MHz")
    parser.add_argument("--duration-s", type=float, help="Single-capture duration in seconds; omit for untimed")
    parser.add_argument("--end-frequency-mhz", type=float, help="Sweep end frequency in MHz")
    parser.add_argument("--step-mhz", type=float, help="Signed sweep step in MHz")
    parser.add_argument("--dwell-s", type=float, help="Time at each sweep frequency in seconds")
    parser.add_argument("--tuner", choices=("enabled", "disabled"), help="External tuner use; omitted preserves config/service choice")
    parser.add_argument("--if-mhz", type=float, help="Optional digitizer IF override; normally use the service default")
    parser.add_argument("--injection", choices=("high", "low"), help="Optional tuner injection override")
    parser.add_argument("--conjugate", choices=("auto", "force_on", "force_off"), help="Conjugation policy")
    parser.add_argument("--capture-name", help="New capture directory name; omit for preview")
    parser.add_argument("--host", default="localhost", help="MEP bus host (default: localhost)")
    parser.add_argument("--port", type=int, default=1883, help="MEP bus port (default: 1883)")
    parser.add_argument("--timeout", type=float, default=10, help="Connection/response timeout in seconds")
    arguments = parser.parse_args(argv)
    if not math.isfinite(arguments.timeout) or arguments.timeout <= 0 or not 1 <= arguments.port <= 65535:
        parser.error("timeout must be positive and port must be between 1 and 65535")
    numbers = (arguments.frequency_mhz, arguments.sample_rate_mhz, arguments.duration_s, arguments.end_frequency_mhz, arguments.step_mhz, arguments.dwell_s, arguments.if_mhz)
    if any(value is not None and not math.isfinite(value) for value in numbers):
        parser.error("Capture arguments must be finite numbers")
    overrides = capture_overrides(arguments)
    if arguments.stop:
        if arguments.document or arguments.capture_name is not None or overrides:
            parser.error("--stop cannot be combined with a config or capture settings")
    elif not arguments.document and arguments.frequency_mhz is None:
        parser.error("Supply a config file or --frequency-mhz, or use --stop")
    if arguments.duration_s is not None and any(value is not None for value in (arguments.end_frequency_mhz, arguments.step_mhz, arguments.dwell_s)):
        parser.error("Use --duration-s for a single capture or sweep arguments with --dwell-s")
    return arguments


def main(argv=None):
    return run(parse_arguments(argv))


if __name__ == "__main__":
    raise SystemExit(main())
