# ArchiveManager

ArchiveManager independently inventories and manages local capture directories under `/data/captures`.

Each directory name is the capture's local identifier. ArchiveManager reads portable settings from `data/capture_settings.json` and reports file counts, sizes, timestamps, settings, and filesystem issues.

## Commands

```text
get_status
get_captures
get_capture
delete_capture
delete_preview
rename_capture
```

`get_capture`, `delete_capture`, and `rename_capture` use `capture_name`. Rename and deletion are direct filesystem operations and are not coordinated with recording or upload services. If another service is using the directory, that service reports any resulting failure normally.

Inventory is maintained with Linux inotify and periodic reconciliation. The retained status contains the complete current capture list.

## Layout

```text
/data/captures/<capture_name>/
    data/
        capture_settings.json
        capture_telemetry.csv
        capture products
    log_upload/
        upload_status.json
```

ArchiveManager owns directory listing, rename, and deletion. CaptureOrchestrator owns capture dataset creation. UploadManager owns `log_upload/upload_status.json`.
