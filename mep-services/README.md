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
