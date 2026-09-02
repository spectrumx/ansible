# DockerManager

DockerManager owns Docker Engine/Compose lifecycle, status, and log execution
for the MEP application. It manages the Compose project at
`/opt/radiohound/docker` by default.

## MQTT topics

- `dockermanager/announce` - retained capabilities and command descriptions
- `dockermanager/status` - retained Docker Engine and Compose service state
- `dockermanager/command` - semantic command requests
- `dockermanager/response` - correlated command responses
- `dockermanager/event` - completed Compose actions
- `dockermanager/logs` - non-retained structured log stream records

## Commands

Service-scoped commands require a non-empty `services` array:

- `start_services`
- `stop_services`
- `restart_services`
- `up_services`, with optional `force_recreate`

Project-scoped commands are separate:

- `up_project`, with optional `force_recreate`
- `down_project`
- `pull_project`

`stop_services` always runs Compose `stop` for the named services. It never
runs Compose `down`.

`get_logs` returns a bounded snapshot. `start_log_stream` and
`stop_log_stream` manage explicitly named streams; records are published on
`dockermanager/logs` with their `stream_id`.

Commands use the common request envelope:

```json
{"task_name":"stop_services","session_id":"doc-001","arguments":{"services":["recorder"]}}
```

Responses are non-retained and preserve correlation:

```json
{"success":true,"session_id":"doc-001","task_name":"stop_services","status_data":{"action":"stop","scope":"services","services":["recorder"],"output":""},"error":null}
```

Retained status separates Docker Engine containers from Compose services under
`engine.containers` and `compose.services`. A log record contains `stream_id`,
`services`, `line`, `state`, `return_code`, and `timestamp`. An ended stream has
`line: null` and `state: "ended"`.

Starting an existing `stream_id` replaces that stream. Clients should stop
their stream during orderly shutdown. Streams are process-local and are not
persisted or replayed by DockerManager.

## Boundary

DockerManager exposes semantic lifecycle and log operations. It does not expose
arbitrary container command execution.

## Configuration

The systemd unit runs:

```text
/opt/mep-services/DockerManager/docker_manager.py
```

Set `DOCKER_COMPOSE_DIR`, `MQTT_BROKER`, `MQTT_PORT`, or `LOG_LEVEL` to override
the service defaults.