# MEP Services

This repository contains the controlled MEP services and the MEPGui client.
Each service owns its resource and publishes retained state under one canonical
MQTT root.

| Component | Executable | Unit | MQTT root |
| --- | --- | --- | --- |
| HostManager | `HostManager/host-manager.py` | `host-manager.service` | `hostmanager/` |
| ArchiveManager | `ArchiveManager/archive_manager.py` | `archive-manager.service` | `archivemanager/` |
| UploadManager | `UploadManager/upload_manager.py` | `upload-manager.service` | `uploadmanager/` |
| DockerManager | `DockerManager/docker_manager.py` | `docker-manager.service` | `dockermanager/` |
| Ringbuffer | `Ringbuffer/ringbuffer.py` | `ringbuffer.service` | `ringbuffer/` |
| CaptureOrchestrator | `CaptureOrchestrator/capture_orchestrator.py` | `capture-orchestrator.service` | `captureorchestrator/` |
| AFEControl | `AFEControl/afe_control.py` | `afe-control.service` | `afecontrol/` |
| TunerControl | `TunerControl/tuner_control.py` | `tuner-control.service` | `tunercontrol/` |
| MEPGui | `MEPGui/mep_gui.py` | none | MQTT client only |

The service source is installed at `/opt/ansible/mep-services`.

## Development service testing

Start the services from the current repository:

```bash
python3 start_services.py
```

The script links the repository's unit files into systemd and restarts them.
ServiceManager starts last.

Stop every repository-owned systemd service:

```bash
python3 stop_services.py
```

ServiceManager stops first, followed by each managed unit.

## External dependencies

MQTTBroker, RecorderControl, and the RFSoC service are external dependencies.
Their implementations and MQTT interfaces are not owned by this repository.
MEPGui and CaptureOrchestrator consume those interfaces without redefining
them.

Icarus is tabled until its external-server communicator contract is defined.

## MQTT convention

Controlled services use `service/announce`, `service/command`,
`service/response`, `service/status`, and service-specific data/event topics.
Announce and status publications are retained. Correlated responses include the
request `session_id` and are not retained.

The individual component READMEs describe exceptions and detailed payloads.

## Service source convention

Keep module-level settings together under one `CONFIGURATION` banner.
Use short comments within it for these groups:

1. Service identity and MQTT broker settings.
2. Timing settings, preserving the intervals and timeouts each service needs.
3. The canonical MQTT topic block.
4. Service-specific settings.

Large static command descriptions follow configuration under their own banner.
Hardware reference tables and schemas may also retain separate sections.
Preserve initialization dependencies when grouping settings.

Topic constants follow this complete block, including reserved topics:

```python
ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
STATUS_TOPIC = f"{SERVICE_NAME}/status"
DATA_TOPIC = f"{SERVICE_NAME}/data"
EVENT_TOPIC = f"{SERVICE_NAME}/event"
LOG_TOPIC = f"{SERVICE_NAME}/logs"
```

A constant establishes the topic's canonical name; it does not imply that a
service publishes there. Reserved topics remain in the source boilerplate.
Announcements describe the service's existing interface; adding a reserved
constant does not add an announcement field, publisher, or subscription.

Use `SERVICE_NAME`, `MQTT_BROKER`, and `MQTT_PORT` for the common settings.
Timing names include their units, such as `STATUS_INTERVAL_S` and
`COMMAND_TIMEOUT_S`. Event-driven services need not define an unused periodic
status interval or command timeout.

Static command metadata uses `COMMAND_DESCRIPTIONS` internally and remains
under `"commands"` in announcements. Each command retains its existing
description and argument schema. AFEControl's structured descriptions and
TunerControl's hardware-dependent command list retain their existing forms.

AFEControl's configured topic hierarchy, RadiohoundGateway's external
compatibility interface, and MEPGui's client role have different requirements;
they do not need the manager-service configuration layout. Services remain
independent, with no shared service base class or new runtime dependency.
Message schemas, command names, QoS, retention, and lifecycle behavior remain
defined by the existing service contracts.

## Spectrum client

`MEPClient.spectrum` exposes two callback interfaces on one subscription:

- `on_source_message(callback)` calls `callback(topic, payload)` with the original
  parsed JSON message, preserving encoded data and metadata. It does not decode
  spectrum samples or preserve the original MQTT byte serialization.
- `on_frame(callback)` is the canonical application interface. It calls
  `callback(frame)` with the decoded float32 spectrum row in dBFS, as used by
  MEPGui. Source-message access preserves the producer representation for
  forwarding or diagnostics.

Both follow the same `set_topic()`, `start()`, and `stop()` lifecycle. Spectrum
samples are decoded only when a frame callback is registered. Callbacks run on
the bus thread: treat payloads as read-only and enqueue any slow processing.
Consumers own display history and waterfall rendering.

Run the focused tests without a broker or hardware:

```text
python -B -m unittest discover -s mep-services/mep_client/tests -v
```
