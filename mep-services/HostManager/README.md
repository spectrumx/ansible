# HostManager

This folder contains a host-side MQTT service that follows a consistent topic layout.

The implementation currently uses direct Linux metrics. Jetson-specific
`tegrastats` support is tabled until its data sources are understood and
validated on the target hardware; this service does not invoke `tegrastats`.
Verified Orin NX data is collected directly from `/sys` and included under
`platform`.

## Canonical topic convention

Use the service name as the root namespace:

- `hostmanager/announce`
- `hostmanager/command`
- `hostmanager/status`
- `hostmanager/response`
- `hostmanager/event`

This is the pattern we should prefer going forward because it keeps announcement, command, and status tied to the actual service that owns them.

For a wildcard subscription that discovers all services, listen on:

- `+/announce`

This lets a client discover the service names and then subscribe to the specific service it wants.

## Why not `announce/hostmanager`?

The mixed form is harder to reason about because the root of the topic is not the service identity. With `service/announce`, the topic layout reads naturally:

- `tunercontrol/announce`
- `afecontrol/announce`
- `hostmanager/announce`

and the listener can do `+/announce` to discover all announce messages without guessing the namespace.

The service-controlled form is also more consistent with:

- `service/command`
- `service/status`
- `service/response`

## Example payloads

### announce

```json
{
  "service": "hostmanager",
  "type": "service",
  "timestamp": 1724600000.0,
  "identity": {"hostname": "jetson-01"},
  "topics": {
    "command": "hostmanager/command",
    "status": "hostmanager/status",
    "response": "hostmanager/response",
    "announce": "hostmanager/announce"
  },
  "commands": {}
}
```

### command

```json
{
  "session_id": "host-001",
  "task_name": "get_status",
  "arguments": {
    "disk_path": "/"
  }
}
```

### status

```json
{
  "service": "hostmanager",
  "state": "online",
  "timestamp": 1724600000.0,
  "seq": 193822,
  "identity": {"hostname": "jetson-01"},
  "cpu": {
    "used_percent": 38.2
  },
  "memory": {
    "used_percent": 59.4,
    "total_bytes": 16384,
    "used_bytes": 9728,
    "free_bytes": 6656
  },
  "platform": {
    "name": "nvidia_jetson_orin_nx",
    "gpu": {
      "utilization_percent": 73.4,
      "source": "/sys/devices/platform/gpu.0/load"
    },
    "power": {
      "rails": [
        {
          "name": "VDD_IN",
          "voltage_mv": 5048,
          "current_ma": 2872,
          "power_mw": 14503
        }
      ]
    }
  }
}
```

Responses are published on `hostmanager/response` and are not retained:

```json
{
  "success": true,
  "session_id": "host-001",
  "task_name": "get_status",
  "status_data": {},
  "error": null
}
```

Status is published every second by default. Change it at runtime with a
`set_status_interval` command containing `interval_s` between 0.1 and 60.

The status payload includes host identity, uptime, CPU, load, memory, swap,
filesystem usage, network interfaces and counters, and every readable
operating-system thermal zone. The single `hostmanager/event` topic reports
observed availability or state transitions and real collection errors; it does
not invent temperature or disk warnings.

The Orin NX platform section includes per-core CPU frequency, direct GPU load
from `/sys/devices/platform/gpu.0/load`, and INA3221 rails discovered through
`/sys/class/hwmon`. `seq` increments for each status publication so consumers
can detect missed snapshots.

## Files in this folder

- `host-manager.service` - systemd unit
- `host-manager.py` - service implementation and host metric collection
