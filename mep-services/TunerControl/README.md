# TunerControl

TunerControl owns tuner discovery, initialization, configuration, and status
under the `tunercontrol/` MQTT root.

## Topics

- `tunercontrol/announce` - retained commands and tuner capabilities
- `tunercontrol/command` - command requests
- `tunercontrol/response` - correlated, non-retained responses
- `tunercontrol/status` - retained tuner state

Successful `init_tuner` handling publishes retained status and then sends that
status as a correlated response containing the request `session_id` and
`task_name`.

`ready` means a tuner was instantiated and is usable. PLL lock is an
independent observation reported through `lock_supported`, `lock_status`, and
`lock_error`; an unlocked PLL does not make initialization fail.

The systemd unit runs
`/opt/mep-services/TunerControl/src/tuner_control.py` with the RadioHound Python
3.13 environment.
