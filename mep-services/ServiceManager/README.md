# ServiceManager

ServiceManager owns lifecycle and journal access for the repository's systemd services. It exposes only semantic, allowlisted operations through MQTT; clients never submit commands or arbitrary unit names.

## Topics

- `servicemanager/announce`
- `servicemanager/command`
- `servicemanager/response`
- `servicemanager/status`
- `servicemanager/event`
- `servicemanager/logs`

## Commands

- `get_status`
- `start_services`
- `stop_services`
- `restart_services`
- `get_logs`
- `start_log_stream`
- `stop_log_stream`

Service-scoped commands receive a non-empty `services` array containing exact allowlisted systemd unit names. `service-manager.service` is deliberately not managed, because stopping the MQTT process handling its own request would make acknowledgement and state publication unreliable.

At startup, ServiceManager discovers `mep-services/*/*.service`, matching the
start/stop/restart helpers. Adding a service unit to its service folder makes it
part of the managed list after ServiceManager restarts; no second registry needs
editing. The usual restart helper restarts ServiceManager after the other units.
Commands remain limited to these repository unit names.

MEPGui and MEPCli are applications, so neither appears as a systemd service.
Their `mep` and `mep_capture` commands are installed by `mep-services.yml`.

The retained announce payload publishes this allowlist as `managed_services`,
and retained status publishes one state row per unit. Clients such as MEPGui
must build their service selector from those fields rather than duplicating the
allowlist.

When started by `start_services.py`, the process runs as root under systemd.
The helper links the repository unit definitions for development use but does
not enable them at boot. ServiceManager therefore invokes allowlisted
`systemctl` lifecycle actions directly rather than through `sudo`.
