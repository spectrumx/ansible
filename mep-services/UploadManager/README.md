# UploadManager

UploadManager independently uploads capture datasets to SDS. It discovers captures by enumerating `/data/captures` and stores one state document per capture:

Service settings are constants in `upload_manager.py`: the capture root is `/data/captures`, the SDS host is `sds.crc.nd.edu`, and retained status is published every 10 seconds. There is no separate YAML configuration file. `SDS_SECRET_TOKEN` remains an optional credential fallback when a token is not supplied with the upload command.

```text
/data/captures/<capture_name>/log_upload/upload_status.json
```

The upload payload is exactly the contents of:

```text
/data/captures/<capture_name>/data/
```

UploadManager also keeps append-only activity records at:

```text
/data/captures/<capture_name>/log_upload/upload_activity.jsonl
```

The `log_upload` directory is never uploaded. Current upload state, credentials, the generated SDS path, progress, verification, and the current failure are persisted in `upload_status.json`. Credentials are omitted from MQTT status, responses, events, and logs.

## Commands

```text
get_status
get_uploads
get_upload
get_activity
check_sds
start_upload
stop_upload
```

All capture-specific commands use `capture_name`. There is no separate upload job identifier. Activity and SDK output are appended to `upload_activity.jsonl`, published live on `uploadmanager/event`, and available through `get_activity`. The service returns the latest 500 records.

The SDS directory is generated once as `<capture_name>_<hostname>_<random>` and retained in the state document. A later local rename does not change it. Pressing Upload again continues with the same SDS path. Stop retains that path so the upload can continue later. Dry runs use the same path while the SDS client's dry-run mode prevents remote modification.

SDK verbose output is enabled by default and can be disabled per upload. Output written by the SDK during authentication and upload is recorded in `upload_activity.jsonl` and forwarded live on `uploadmanager/event` for the GUI Details pane.

UploadManager does not parse SDK progress text. Transfer totals are marked complete only after the SDK upload call returns successfully. Automatic and manual verification publish activity while reading local files, authenticating, reading the SDS inventory, comparing files, and reporting the result.

Capture-specific current state and verification results remain in `log_upload/upload_status.json`; service milestones, SDK output, and failures are published as live MQTT events.

The GUI Verify action compares every local file under `data/` with the assigned SDS directory by relative path and size, then stores the result in `upload_status.json`.

Missing status means no upload has been initialized. An unreadable status file is reported as `Cannot read log_upload/upload_status.json` and is never replaced automatically.
