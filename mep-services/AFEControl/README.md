# AFEControl

AFEControl owns communication with the analog front-end firmware and exposes
it under the `afecontrol/` MQTT root.

## Transport and time

```text
AFE UART
	|
	v
gpsd                    Standard Linux package
	|
	+-- raw stream ------> AFEControl parses NMEA + $PMIT -> MQTT
	|
	`-- parsed GNSS/PPS -> chrony -> system clock
```

GPSD owns the UART and provides raw serial transport to AFEControl. GPSD and
chrony provisioning, device aliases, and system-time configuration remain
owned by the Ansible platform deployment.

GPSD independently parses standard GNSS and PPS data for chrony. AFEControl
deliberately reparses GPSD's raw stream for its MQTT contract and also parses
the proprietary `$PMIT...` firmware protocol that GPSD does not understand.
`GPSDTransport` encapsulates the two GPSD mechanisms used by AFEControl:

- a TCP WATCH connection that yields the raw line stream
- serialized Unix-socket device writes that carry validated `$PMIT...` commands

The MQTT API exposes semantic AFE operations, not arbitrary GPSD commands or
arbitrary UART writes. A bounded diagnostic lease can publish the existing raw
stream without retaining messages. Leases expire after at most 60 seconds and
are cleared by MQTT or GPSD reconnection; the default `gnss` mode excludes
high-rate `$PMIT...` telemetry, while explicit `all` mode includes every line.

AFEControl stages the standard NMEA sentences belonging to one GNSS epoch. A
distinct RMC date/time starts the next epoch and atomically commits the completed
prior epoch; a timeout flushes the final epoch if input stalls. MQTT and CSV
logging both consume that committed snapshot. The current announced field order
is the authoritative CSV and MQTT schema; schema changes are breaking changes.
An existing daily CSV with a different header is not modified or appended to.
AFEControl continues in the first compatible numbered file, such as
`telemetry_YYYYMMDD_2.csv`, creating one with the current header when needed.

The GNSS snapshot parses RMC, GGA, GSA, and GSV data, including fix state,
position, MSL and HAE altitude, geoid separation, speed, track, magnetic
variation, differential metadata, DOP values, and per-constellation used and
visible satellite counts.

## Telemetry ownership

```text
AFEControl
+-- /data/log_telemetry/telemetry_YYYYMMDD.csv
|   `-- Continuous platform telemetry log
|
`-- afecontrol/data/*
	`-- CaptureOrchestrator
		`-- <capture>/data/capture_telemetry.csv
			`-- Capture-specific telemetry log
```

CaptureOrchestrator does not read or reuse AFEControl's daily CSV. During an
active capture, it subscribes to the AFE MQTT telemetry streams and uses the
schema advertised in the retained `afecontrol/announce` payload to write its
own capture-scoped telemetry file.

## Topics

- `afecontrol/announce` - retained schemas, command subtopics, and capabilities
- `afecontrol/status` - retained service configuration and cached parameters
- `afecontrol/status/registers` - retained register state
- `afecontrol/status/gpsd` - retained GPSD connection and transport metadata
- `afecontrol/command` and its advertised subsystem subtopics
- `afecontrol/response` and corresponding response subtopics
- `afecontrol/data/gps`, `data/imu`, `data/mag`, and `data/hk`
- `afecontrol/data/raw` - non-retained, lease-controlled diagnostic stream
- `afecontrol/event`

Clients must discover detailed command and telemetry schemas from the retained
announce payload rather than maintaining independent field lists.

The base `refresh` command sends firmware queries for IMU, magnetometer, time,
and telemetry parameters. Its response means the queries were accepted; the
asynchronous firmware replies update retained status afterward.

Time and logging configuration commands validate the complete requested state
before issuing ordered firmware writes. Those external writes are not
rollback-capable transactions.

The systemd unit runs `/opt/mep-services/AFEControl/afe_control.py`.