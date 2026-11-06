# MEP Services

This directory contains the MEP services, the shared `mep_client` API, and the
MEPGui application. Local services own their resources and expose status and
commands through MQTT. CaptureOrchestrator owns acquisition workflows; MEPGui
uses `mep_client` to interact with those services.

RadioHoundGateway is an independent compatibility adapter that observes local
data through `mep_client` and publishes it to RadioHound.

| Component | Executable | Unit | MQTT root |
| --- | --- | --- | --- |
| HostManager | `HostManager/host-manager.py` | `host-manager.service` | `hostmanager/` |
| ArchiveManager | `ArchiveManager/archive_manager.py` | `archive-manager.service` | `archivemanager/` |
| UploadManager | `UploadManager/upload_manager.py` | `upload-manager.service` | `uploadmanager/` |
| DockerManager | `DockerManager/docker_manager.py` | `docker-manager.service` | `dockermanager/` |
| ServiceManager | `ServiceManager/service_manager.py` | `service-manager.service` | `servicemanager/` |
| Ringbuffer | `Ringbuffer/ringbuffer.py` | `ringbuffer.service` | `ringbuffer/` |
| CaptureOrchestrator | `CaptureOrchestrator/capture_orchestrator.py` | `capture-orchestrator.service` | `captureorchestrator/` |
| AFEControl | `AFEControl/afe_control.py` | `afe-control.service` | `afecontrol/` |
| TunerControl | `TunerControl/tuner_control.py` | `tuner-control.service` | `tunercontrol/` |
| RadioHoundGateway | `RadiohoundGateway/radiohound_gateway.py` | `radiohound-gateway.service` | Remote `radiohound/clients/announce/` and `radiohound/clients/data/` |
| MEPGui | `MEPGui/mep_gui.py` | none | MQTT client only |
| MEPCli | `MEPCli/mep_cli.py` | none | MQTT client only |

The service source is installed at `/opt/ansible/mep-services`. Service units
and executable Python entrypoints use `/opt/radiohound/python313/bin/python`.

## Starting, stopping, and restarting

Start the services from the current repository:

```bash
/opt/radiohound/python313/bin/python /opt/ansible/mep-services/start_services.py
```

The script discovers `*/*.service`, links the unit files into systemd, reloads
unit definitions, and starts the services. ServiceManager starts last. These
helpers do not enable the units at boot.

After updating service code or unit definitions, restart the services:

```bash
/opt/radiohound/python313/bin/python /opt/ansible/mep-services/restart_services.py
```

The restart script links the units and reloads systemd before restarting them,
with ServiceManager last.

Stop every repository-owned systemd service:

```bash
/opt/radiohound/python313/bin/python /opt/ansible/mep-services/stop_services.py
```

ServiceManager stops first, followed by the other discovered units.

## RadioHoundGateway

[RadioHoundGateway](RadiohoundGateway/README.md) provides read-only node presence,
existing RFSoC, Recorder, tuner, and acquisition status, GNSS location, host
information, and forwarding of existing spectrum messages. A capture started
locally can be viewed through RadioHound's MEP spectrum viewer.

The gateway accesses local MEP data exclusively through the public `mep_client`
API. It accepts no remote acquisition commands, and other local services do not
depend on it. Gateway or remote-connection failures do not stop local MEP
operation. Its unit is discovered by the same lifecycle scripts as the other
services.

## Capture settings and applications

`mep` opens MEPGui. The RX **Settings** menu loads or saves a capture recipe
without starting equipment. Press **Start/Update** to apply loaded settings.
In CAP, right-click a capture (or press Shift+F10) to use, inspect, or export
its settings.

Load/Save resolution requires MQTT, CaptureOrchestrator, and its Recorder preset
files. It does not configure equipment.

[MEPCli](MEPCli/README.md) provides the command-line application:

```bash
mep_capture experiment.json --capture-name trial-01
```

The direct command link is installed by `mep-services.yml`; restarting services
alone does not install it. `mep` retains its existing GUI link. MEPCli has
no service unit and is not started by the lifecycle scripts.

Both applications use the same public CaptureOrchestrator document interface.
A JSON input can contain only selected `settings`. GUI Save and capture creation
use the same document builder: both include a `document` type/version identifier,
expanded `settings`, and the full descriptive `record`. Capture creation writes
that document to `data/capture_settings.json`; GUI Save writes it to the chosen file.
Only `settings` is executable. Configuration snapshots and observations in
`record` are not restored as commands. Existing capture JSON remains readable.
See [CaptureOrchestrator](CaptureOrchestrator/README.md) for defaults and fields.

## External dependencies

MQTTBroker, RecorderControl, and the RFSoC service are external dependencies.
Their implementations and MQTT interfaces are not owned by this repository.
MEPGui and CaptureOrchestrator consume those interfaces without redefining
them.

## MQTT convention

Controlled services use `service/announce`, `service/command`,
`service/response`, `service/status`, and service-specific data/event topics.
Local service announce and status publications are retained. Correlated responses
include the request `session_id` and are not retained. RadioHoundGateway's remote
announcements and spectrum publications are non-retained compatibility messages,
not local service command/status topics.

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
independent; the shared public client provides application access to their
interfaces.
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

From the repository root, run the focused client tests without a broker or
hardware:

```text
python -B -m unittest discover -s mep-services/mep_client/tests -v
```
