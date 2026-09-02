# UploadManager

UploadManager is an independent background service for transferring completed
MEP captures to external destinations. SDS is the first supported destination;
the service name and MQTT contract are intentionally destination-neutral.

## Responsibilities

- Queue and persist upload jobs in SQLite.
- Build a file manifest for each capture.
- Run uploads in a background worker.
- Track progress, cancellation, retries, and restart recovery.
- Keep destination credentials out of status, events, logs, capture files, and UI display.
- Create a local lease while a capture is being uploaded.

ArchiveManager remains independent. It owns local capture inventory and deletion;
UploadManager does not need ArchiveManager to be running once it has a capture
reference.

## Files

- `upload_manager.py` - service, worker, SQLite state, and destination handling
- `upload-manager.service` - systemd unit
- `upload_manager.yaml` - service configuration
- `schema.sql` - upload-job schema

## MQTT topics

```text
uploadmanager/announce
uploadmanager/command
uploadmanager/response
uploadmanager/status
uploadmanager/data
uploadmanager/event
```

Announce and status are retained. Responses and events are not retained.
Discovery uses `+/announce`.

## Commands

```text
get_status
get_uploads
get_upload
get_upload_activity
check_sds
start_upload
pause_upload
resume_upload
stop_upload
retry_upload
delete_upload
```

`start_upload` receives an orchestrator-owned `capture_id`, a destination, and
optional credentials. UploadManager resolves the matching local directory from
`capture_identity.json` and owns the generated, persisted destination path.

## SDS configuration

`upload_manager.yaml` names the SDS host and the environment variable containing
the API key. CAP may instead submit `credentials: {"token": "..."}`. This token
is temporarily stored as plaintext in the job database so retries and service
restarts work. It is intentionally omitted from every status, response, event,
log, capture file, and GUI details payload. This is a temporary implementation,
not a secure credential-store design.

The SDS SDK call uploads the capture's `data/` directory as one operation. UploadManager
records a durable, timestamped activity timeline for queueing, scanning,
authentication, upload, retries, failures, cancellation, and restart recovery.
CAP displays this timeline for the selected job, and lifecycle events are also
written to the service journal. Credentials are redacted before captured output
is persisted or published.

It does not stage files or implement per-file upload control. SDK stdout and stderr
are mirrored unchanged to the service journal. Carriage-return progress redraws
are published as transient MQTT events for CAP's live progress line; they are not
written to SQLite. Durable SDK lines, lifecycle milestones, warnings, and failures
remain in the activity timeline.

After the SDK returns, UploadManager inspects its per-file results and lists the
destination path through the SDK. A job reaches `complete` only when every
manifest path exists remotely with the expected size. If remote verification
is temporarily unavailable, the job enters `verification_pending`. A requested
upload remains durable intent and retries indefinitely with exponential backoff
capped at two hours. Retries call the same SDK directory upload; file selection
and skip behavior remain owned by the SDK.

`check_sds` is a read-only capture-level check of current local `data/` paths
and sizes against the assigned SDS path. Manual checks run one at a time on a
separate verifier worker and start only while upload processing is idle. A hung
check does not block a later upload, MQTT handling, or the upload worker. Check
results replace the previous capture result rather than creating an unbounded
history. Upload completion performs the same verification automatically.

Stop requests are acknowledged immediately. A queued or scanning job stops at
the next local checkpoint. If authentication or upload is already inside the
synchronous SDS SDK call, the job remains in `cancelling` until that call
returns; the activity timeline states this explicitly.

Pause preserves upload intent and credentials while suppressing retries. If an
SDK call is active, the job remains `pause_requested` until the call returns.
Resume continues the same job. Stop permanently cancels intent and allows local
capture deletion as soon as `cancelling` is acknowledged.

SQLite retains at most 500 durable activity entries per job by default. Starting
a new intent removes superseded terminal jobs for that capture and destination.
Removing an inactive job deletes its manifest and activity through SQLite
cascades. Capture-to-SDS mappings retain the latest verification result.

## Upload states

```text
queued
scanning
authenticating
uploading
verifying
verification_pending
pause_requested
paused
cancelling
waiting_for_retry
waiting_for_credentials
complete
cancelled
failed
```

Jobs interrupted by service restart are requeued. Upload leases are removed
during startup recovery and on normal completion.
