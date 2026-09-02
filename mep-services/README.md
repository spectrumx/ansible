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
| TunerControl | `TunerControl/src/tuner_control.py` | `tuner-control.service` | `tunercontrol/` |
| MEPGui | `MEPGui/mep_gui.py` | none | MQTT client only |

Controlled units load code from `/opt/mep-services`.

## Development service testing

Preview the repository-owned unit files from the repository root:

```bash
python3 start_services.py --list
```

Link the current repository unit files into systemd and restart each service:

```bash
python3 start_services.py
```

The launcher discovers the repository's `.service` files, uses `systemctl link`
so systemd reads those development definitions, reloads the systemd manager,
and restarts every unit. ServiceManager restarts last so the SVC tab can report the
resulting states. Units are not enabled at boot. The helper does not launch
MEPGui or manage MQTTBroker, RecorderControl, Ansible, RFSoC, or any other
external dependency.

`systemctl restart` also starts an inactive unit, so this single command both
brings up stopped services and reloads current Python code in running services.

Stop every repository-owned systemd service:

```bash
python3 stop_services.py
```

ServiceManager stops first, followed by each managed unit. A failure in one
unit does not prevent the helpers from attempting the remaining units.

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