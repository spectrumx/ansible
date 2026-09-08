# TunerControl

TunerControl owns tuner discovery, initialization, configuration, and status
under the `tunercontrol/` MQTT root.

## Topics

- `tunercontrol/announce` - retained commands and tuner capabilities
- `tunercontrol/command` - command requests
- `tunercontrol/response` - correlated, non-retained responses
- `tunercontrol/status` - retained tuner state
- `tunercontrol/data` - reserved for future tuner telemetry
- `tunercontrol/event` - non-retained service and hardware events

The service discovers tuner hardware through the stable `/dev/valon5015` and
`/dev/lmx2820` names created by the system udev rules. If both are attached,
the current single-tuner service preserves the previous Valon-first priority.
It selects one backend for its lifetime and does not switch backends when
initialization fails.

Backend-specific settings are owned by their implementation files. `valon.py`
contains the Valon serial, power, and reference settings. `lmx2820.py` contains
the LMX2820 reference settings. `tuner_control.py` owns hardware discovery and
knows only which backend was selected.

## Configuration

TunerControl does not use environment variables for configuration.

- Set the MQTT broker in `tuner_control.py`.
- Set the Valon serial connection and startup values in `tuners/valon.py`.
- Set the LMX2820 reference path in `tuners/lmx2820.py`.

The dummy backend is available for controlled development use but is never
selected by hardware discovery.

The LMX2820 implementation sets `BLINKA_FT232H` internally before loading the
Adafruit hardware libraries. That flag selects their FT232H driver; it is not a
TunerControl configuration input.

`discover` rescans the udev device names. If no tuner has been selected yet, it
selects and initializes the first detected backend. It reports newly attached
hardware without replacing an already selected tuner.

The common commands are `status`, `discover`, `initialize`, `set_frequency`, and
`get_frequency`. Valon additionally supports power, external-reference,
reference-frequency, and PLL-lock commands. The LMX2820 currently supports
frequency commands only.

`ready` means a tuner was instantiated and is usable. PLL lock is an
independent observation reported through `lock_supported` and `lock_status`.
The Valon reports PLL lock. The LMX2820 currently does not support lock status,
so its status contains `lock_supported: false` and `lock_status: null`.

The systemd unit runs
`/opt/ansible/mep-services/TunerControl/tuner_control.py` with the RadioHound
Python 3.13 environment.
