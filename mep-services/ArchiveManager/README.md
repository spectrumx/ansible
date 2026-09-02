# ArchiveManager

ArchiveManager is an independent service for data that already exists on the local
system. It is not part of RF capture orchestration, recorder control, or the
Ringbuffer service.

## Responsibilities

- Discover everything under `/data/captures`.
- Describe normalized identity, settings, issues, timestamps, file counts, and sizes.
- Maintain the current local inventory in memory.
- Delete and rename captures only by their stable capture ID.
- Publish inventory changes as MQTT events.

## Files

- `archive_manager.py` - service, inotify inventory, and MQTT API
- `archive-manager.service` - systemd unit
- `archive_manager.yaml` - service configuration
The service starts one inotify watcher, one low-frequency reconciliation
thread, and one command worker. `ArchiveManagerService` owns local inventory,
filesystem lifecycle, and the MQTT API. Clients use only the
`archivemanager/*` MQTT topics.

## MQTT topics

```text
archivemanager/announce
archivemanager/command
archivemanager/response
archivemanager/status
archivemanager/data
archivemanager/event
```

Discovery uses:

```text
+/announce
```

Announce and status are retained. Responses, data snapshots, and events are
not retained.

The retained status includes the complete current capture list so newly
connected CAP clients can render immediately. Managed records contain
`identity`, `settings`, `issues`, filesystem `first_seen`/`last_modified`
timestamps, and `state: managed`. Directories without a valid
`capture_identity.json` remain visible as `state: legacy` and are read-only,
except that the reserved `preview` directory can be manually deleted while no
preview recording is active.

## Commands

```text
get_status
get_captures
get_capture
delete_capture
delete_preview
rename_capture
```

Inventory is scanned once at startup, then updated from Linux inotify events.
The MQTT read commands return the latest in-memory snapshot and do not trigger
a filesystem scan. A slow reconciliation scan runs at `reconcile_interval_s`
to recover from missed or overflowed events.

The service completes its initial scan and establishes its inotify tree before
connecting to MQTT. `archivemanager/announce` and the initial
`archivemanager/status` are published once after connection. Later status and
data publications occur only for an inventory or watcher state change.
`archivemanager/data` carries the complete detailed snapshot and
`archivemanager/event` identifies the specific change. Each received command
produces exactly one correlated response.

Commands use `task_name`, `arguments`, and optional `session_id`.

ArchiveManager subscribes to retained CaptureOrchestrator and UploadManager
status. It rejects rename and deletion while the capture is recording or has
an active upload. It also rejects paths outside `data_root`.

## State ownership

```text
/data/captures
    actual RF, DigitalRF, spectrogram, and telemetry files

/data/captures/<capture_name>/capture_identity.json
    local stable identity written by CaptureOrchestrator

/data/captures/<capture_name>/capture_settings.json
    portable settings written by CaptureOrchestrator

/data/captures
    local capture files managed by this service

UploadManager
    separate service for upload jobs, SDS paths, and remote destinations
```

## SDS configuration

Upload commands belong to the separate UploadManager service. To start an
upload, send the selected capture's `capture_id`, destination, and temporary
credentials to `uploadmanager/command`. UploadManager resolves the capture
directory and owns the destination path and job lifecycle.

The service is an independent peer on MQTT alongside RFSoC, Recorder,
Ringbuffer, HostManager, AFEControl, TunerControl, and DockerManager services.
