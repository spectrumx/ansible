# Ringbuffer Service

Standalone DigitalRF ringbuffer lifecycle service.

The current prototype owns one ringbuffer. The intended multi-ringbuffer design
is one service process owning a configured collection of named ringbuffers.

## Responsibilities

- Own one `DigitalRFRingbuffer` instance.
- Create the configured ringbuffer directory when needed.
- Start, stop, and restart the ringbuffer.
- Publish current ringbuffer state.
- Accept structured MQTT commands.

It does not own RFSoC tuning, recorder configuration, capture naming, SDS
upload, or GUI behavior. The future SDS service is separate.

## Multiple ringbuffers

One service process can own several named ringbuffers, for example:

```text
ringbuffer service
├── preview
│   └── /data/captures/preview
├── short_term
│   └── /data/ringbuffers/short_term
└── archive
    └── /data/ringbuffers/archive
```

Each configured instance can have its own DigitalRF limits and rules, such as
size, duration, sample-count, polling interval, or whether metadata is
included. The service should create these instances from one configuration
table in `ringbuffer.py`, not accept arbitrary paths and constructor options
from MQTT commands.

Commands identify an instance in `arguments`:

```json
{
  "task_name": "stop",
  "session_id": "example-001",
  "arguments": {"ringbuffer_name": "preview"}
}
```

`get_status` can return all instances, or one selected instance. The retained
`ringbuffer/status` payload should contain an array of named ringbuffer states.
There is no need for a topic per directory.

## Startup behavior

When `ringbuffer.service` is enabled, systemd starts `ringbuffer.py` during
normal multi-user boot. The Python process:

1. Constructs the configured `DigitalRFRingbuffer` instances.
2. Creates missing directories.
3. Connects to the local MQTT broker.
4. Subscribes to `ringbuffer/command`.
5. Publishes retained `ringbuffer/announce` and `ringbuffer/status` messages.
6. Starts each configured ringbuffer that is enabled at startup.
7. Publishes periodic retained status.

If construction or startup of a required ringbuffer fails, the service should
report the failure on `ringbuffer/event` and let the service's defined startup
policy decide whether the process continues with other instances or exits for
systemd to restart. That policy should be explicit before multiple instances
are enabled.

Stopping the systemd service stops the MQTT loop and then stops every running
ringbuffer in the process.

## Files

- `ringbuffer.py` - service and DigitalRF lifecycle implementation
- `ringbuffer.service` - systemd unit
- `ringbuffer.yaml` - configured ringbuffer instances and retention limits

## Default path

The default path is:

```text
/data/captures/preview
```

Override it for testing without changing the file:

```bash
RINGBUFFER_PATH=/data/ringbuffer-test \
/usr/bin/python3 -u /opt/ansible/mep-services/Ringbuffer/ringbuffer.py
```

The service reads `ringbuffer.yaml` beside the Python file by default. Set
`RINGBUFFER_CONFIG` to use another configuration file during testing or
deployment.

The included systemd unit runs the project copy at
`/opt/ansible/mep-services/Ringbuffer/ringbuffer.py`, so it loads the adjacent
`/opt/ansible/mep-services/Ringbuffer/ringbuffer.yaml` automatically.

## MQTT topics

```text
ringbuffer/announce
ringbuffer/command
ringbuffer/response
ringbuffer/status
ringbuffer/data
ringbuffer/event
```

Service discovery remains:

```text
+/announce
```

`announce` and `status` are retained. Command responses and events are not
retained.

## Configuration

The YAML contains a top-level `ringbuffers` mapping. Each name identifies one
`DigitalRFRingbuffer` instance owned by the single service process:

```yaml
ringbuffers:
  preview:
    path: /data/captures/preview
    enabled: true
    size: -200000000
    count: null
    duration_ms: null

  short_term:
    path: /data/ringbuffers/short_term
    enabled: false
    size: 10000000000
    count: 1000
    duration_ms: 3600000
```

`size` is a total byte limit for the tracked files in that ringbuffer.
Negative `size` values mean available filesystem space minus that amount.
`count` is a maximum number of files per DigitalRF channel/group. `duration_ms`
is the maximum time span per channel/group in milliseconds. All configured
limits are passed to DigitalRF together, and the oldest files are expired when
any active limit is exceeded.

`enabled` controls whether an instance starts automatically when the service
starts. Disabled instances are still configured, announced, and available for
an explicit `start` command.

The YAML is part of the deployed service configuration. If Ansible later copies
the repository version to each system, it will overwrite local edits so all
systems remain identically configured. That is appropriate if identical
ringbuffer topology is the deployment policy; otherwise use a deliberately
selected per-system configuration file through `RINGBUFFER_CONFIG`.

## Commands

Start a response listener first:

```bash
mosquitto_sub -h localhost -p 1883 -t 'ringbuffer/response' -v
```

Get status:

```bash
mosquitto_pub -h localhost -p 1883 -t 'ringbuffer/command' \
  -m '{"task_name":"get_status","session_id":"test-001","arguments":{}}'
```

Stop:

```bash
mosquitto_pub -h localhost -p 1883 -t 'ringbuffer/command' \
  -m '{"task_name":"stop","session_id":"test-002","arguments":{}}'
```

Start:

```bash
mosquitto_pub -h localhost -p 1883 -t 'ringbuffer/command' \
  -m '{"task_name":"start","session_id":"test-003","arguments":{}}'
```
