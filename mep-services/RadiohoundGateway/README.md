# RadioHoundGateway

Compatibility adapter for RadioHound using the public `mep_client` API.
Phase I reports node presence, existing component status, and GPS location,
and forwards existing spectrum messages. It publishes an announcement every
10 seconds. It accepts no remote commands and does not change the MEP.
A locally started acquisition supplies spectrum without gateway Start/Stop actions.

## Files and configuration

- `radiohound_gateway.py` contains all code and configuration constants.
- `radiohound-gateway.service` follows the existing systemd service convention.

The normal startup follows the legacy direct-MQTT convention:

- Select the MAC from public HostManager network status in Icarus's preferred
  order: `en0`, `eth0`, `eth1`, `wlan0`, then another `e*` interface. Skip the
  known duplicated Jetson MAC and invalid/all-zero identities. Keep the selected
  identity for the process lifetime. No direct hardware probing is performed.
- Use `radiohound.ee.nd.edu:1883` as the legacy broker fallback. Icarus's direct
  connection did not configure MQTT username/password or TLS; neither does this
  gateway. No new password environment variable or credential file is required.
- If an existing host-side `system.json` is available, `SYSTEM_CONFIG_FILE` can
  point to it read-only. Its `broker_name` and `short_name` are honored. No file
  is created and Icarus is never imported or run. The old source used
  `/opt/radiohound/icarus/.cache/system.json` inside its container; that is not
  evidence of a host-side mount. The live container listing supplied for this
  work has no Icarus container, so no such mount has been established.

`NODE_ID`, `NODE_NAME`, and `REMOTE_HOST` remain optional overrides in the
Python file. `REMOTE_HOST` overrides `system.json`; otherwise its `broker_name`
overrides the legacy fallback. The default display name is `MEP-` followed by
four ID digits if no short name is configured. Local MQTT uses
`localhost:1883`. There are no required blank node/broker settings and no
unconfigured idle mode. If identity is not yet available, the gateway observes
local status and waits for HostManager rather than publishing as an invented
node. An explicit `NODE_ID` can identify a host without HostManager network data.

## Remote connection and SSH transport

The gateway first attempts direct MQTT to the selected remote broker. If that
connection attempt fails, it switches to SSH using the same broker host,
account `git`, port 22, and existing key `/opt/radiohound/.ssh/id_rsa`.
The foreground SSH child forwards `127.0.0.1:8083` to the remote host's
`127.0.0.1:1883` by default. The gateway's remote publisher then connects to
that tunnel entrance. This is a transport to the remote broker; the local MEP
broker is still accessed only through `MEPClient`.

The systemd unit runs as root so it can read the existing root-only key.
Key ownership and permissions are unchanged. No sudo subprocess or private-key
copy is needed. OpenSSH must be available at `/usr/bin/ssh`; authentication uses
the configured key noninteractively, without password prompts.

SSH accepts a host key on first use and records it in `ssh_known_hosts` inside
this gateway directory. A changed recorded host key is rejected. No global SSH
configuration or known-hosts files are changed. This generated host-key file
contains server public keys, not the private authentication key.

The gateway owns one foreground SSH master with daemonization and connection
persistence disabled. Each master has a unique `ssh-<id>.sock` control socket
inside this gateway directory. It requests the loopback-only
forward through that exact master's control socket. Remote MQTT starts only
after OpenSSH confirms the forwarding request succeeded. There is no Python
port-binding probe, and an existing listener is never treated as our tunnel.

A failed or exited child is reaped and retried every 10 seconds. SSH errors go
to the journal. Once switched, this process continues using SSH; a gateway
restart tries direct MQTT again. SIGINT/SIGTERM closes the owned SSH child
before waiting on MQTT shutdown: terminate, wait, then kill and reap if needed.
Its private control socket is removed after the child exits.

The unit explicitly uses `KillMode=control-group` and `SendSIGKILL=yes` with a
20-second stop timeout. Systemd cleans up remaining processes in this unit on
stop or failure. No runtime directory outside the gateway folder is created.
Other units, SSH processes, and listeners are not adopted or stopped.

No broker bridge is installed, no MQTT container is restarted, and no other
service is reconfigured. SSH/MQTT failures affect only gateway publication.
There is no offline data spool. SSH settings are constants alongside the other
configuration in `radiohound_gateway.py`.

## Status contract

Status announcements are non-retained, QoS 0:

```text
radiohound/clients/announce/<node_id>
```

The legacy envelope is `{"message": "ANNOUNCE", "payload": {...}}`. Its nested
payload contains `mac_address`, `short_name`, `broker_name`, and a `sensors` array with one
`type: "sensors.mep"` entry. Its `capabilities.serial` is the configured node
identity used for compatibility, not an independently measured hardware serial.
Existing HostManager values supply hostname, kernel release, hardware platform,
CPU/memory percentages, assigned interface IP addresses, and disk used/free.
The gateway translates public interface addresses into RadioHound's
space-separated `IP_addr` field, default interface first, excluding loopback,
link-local, unspecified, and multicast addresses. At most eight distinct
addresses are forwarded. No addresses are looked up independently by the gateway.
Disk values are converted from bytes
to GiB rounded to one decimal, matching Icarus. The website's `system_version`
field contains the reported system name and kernel release, such as
`Linux 5.15.148-tegra`; it does not claim a distribution version. Unavailable
values are not fabricated. Host fields are cleared to null on local
disconnection until updated.

| Capability | Public client source |
|---|---|
| `rfsoc` | `mep.rfsoc.on_status()` |
| `recorder` | `mep.recorder.on_status()` |
| `tuner_control` | `mep.tuner.on_status()` |
| `captureorchestrator` | `mep.capture.on_status()`, including separate `rx` and `tx` |
| `gnss` and root location fields | `mep.afe.on_gnss()` |
| `gateway` | Read-only marker and local connection status |
| Node identity/host fields | `mep.host.on_status()` |

Only selected state, error, and related status fields are exported. Strings and
lists are bounded and nonfinite numeric values become JSON null. There is no
arbitrary service-configuration forwarding or new health calculation.

Each component has `available` and `observed_at`. Availability means a status
was observed during the local connection, not that hardware is healthy. The
component's own reported state and errors remain authoritative. Missing status
is `unavailable`; when disconnected, the last observation is nested under
`last_known` instead of displayed as current. `observed_at` is gateway receipt
time, not a measurement timestamp. A retained status received on connection can
itself be old; no independent hardware-freshness guarantee is claimed. Quiet,
event-driven services are not marked failed merely for having no new updates.

Local and remote connection handling are independent. The cache holds one
latest snapshot per component. Remote outages do not accumulate announcements;
the next publication uses the current snapshot. At most one publication is
outstanding, so a connected peer that stops reading cannot build an unbounded
send backlog. Local connection failures are
recovered by recreating this gateway's own `MEPClient` through public APIs.
No service is restarted or reconfigured.

## GPS location

Existing AFE GNSS data supplies RadioHound's root `latitude`, `longitude`, and
`altitude` announcement fields. Altitude is meters above mean sea level from
`altitude_msl_m`. A valid fix with finite, in-range coordinates is required;
missing/invalid fixes or local disconnection produce null location fields.
There is no fabricated fallback location. The `gnss` capability also carries
fix validity/quality, available satellite counts, source timestamps, and both
MSL and ellipsoid altitude when supplied. Fix loss replaces the earlier fix;
after disconnection its status is explicitly last-known, as with other components.
No GNSS settings or antenna selection are changed. Source timestamps are
preserved; receipt does not independently establish the age of the physical fix.

## Spectrum forwarding and routing

The gateway subscribes through `mep.spectrum.on_source_message()` and
`mep.spectrum.start()`. Here `start()` starts the client's subscription, not
Recorder or acquisition. Only existing JSON periodograms with string `data`
and `metadata.data_type == "periodogram"` are eligible. Their source topics must
have the legacy `radiohound/clients/data/<12-hex-node-id>[/...]` shape.

Original parsed JSON and the source topic are forwarded unchanged in meaning,
non-retained at QoS 0. Encoded spectrum data and metadata are not decoded,
renamed, or reformatted; JSON whitespace may differ. The device ID and existing
browser UUID/session suffixes are preserved. No recipient is invented and no
acquisition request is interpreted as a viewing request.
`on_frame()` remains the canonical decoded interface for other consumers.

The local callback only hands off one latest message. Remote publication shares
the announcement connection, with at most one outstanding send; announcements
take priority. Slow connections replace waiting spectrum frames, and remote
disconnect/reconnect discards the waiting frame. There is no frame history,
offline spool, or replay queue. Waterfall accumulation remains in the consumer.

### Read-only viewing boundary

The intended Phase I experience is passive: start a capture locally, then view
its existing spectrum without pressing remote acquisition Start/Stop buttons.
The gateway automatically forwards available spectrum and never starts Recorder.
It has no remote command subscription, Start/Stop handlers, or browser selection
based on acquisition commands.

The supplied live source topic is `radiohound/clients/data/48b02dfbb0f9`.
Icarus obtained its device identity from the host MAC with colons removed;
`48b02dfbb0f9` has the MAC-shaped form `48:b0:2d:fb:b0:f9`. A browser UUID is a
separate recipient identifier for browser-addressed delivery. The gateway does
not need to discover a browser UUID to publish a node's existing stream.
Existing browser/session suffixes are preserved when present; node-only topics
remain node-only. The Web's `Spectrum.tsx` emits `join_mep` with the selected
node, indicating a node-viewing path separate from UUID-addressed scan output.
The supplied App snapshot has no matching `join_mep` handler, so those snapshots
do not establish end-to-end node-view delivery. This does not make browser UUID
registration a gateway requirement or justify adding acquisition handlers.
Optional future UUID/topic construction is retained in
[plan_radiohound_phase2.md](plan_radiohound_phase2.md); it is not a Phase I prerequisite.

## Lifecycle

The unit is directly in this directory so `start_services.py`,
`stop_services.py`, and `restart_services.py` discover it through their existing
`*/*.service` pattern. It runs as `root` using `/opt/radioconda/bin/python` and
imports the adjacent shared client from `/opt/ansible/mep-services`.
Other local services do not depend on it. No startup-helper changes are needed.

Once started, inspect or stop just this service with:

```text
systemctl status radiohound-gateway.service
journalctl -u radiohound-gateway.service -f
sudo systemctl stop radiohound-gateway.service
```

SIGINT/SIGTERM stop the gateway's connections without issuing MEP commands.
Logs go to the journal; no capture files, offline spool, or application log files
are written. Repeated remote connection failures are logged on state changes.

All gateway files live here. Before removing a gateway that has been started,
stop its unit while this directory still exists; the normal stop helper can
then still discover it. If a helper has linked the unit, `systemctl disable
radiohound-gateway.service` removes that systemd link; after removing the source
directory, `systemctl daemon-reload` clears its loaded definition. No local
service configuration needs to be reverted. Merely adding these files does
not install, enable, or start anything.

## RadioHound integration and scope

The supplied App accepts non-retained announcements, stores `sensors`, and uses
a 20-second node-activity window. The supplied Web renders RFSoC, Recorder, and
tuner state from the capability names above. It needs an external display
change to render acquisition RX/TX state and availability/last-known details.
Gateway presence alone must not be presented as proof of component health.

No acquisition controls are enabled.
Web, App, and Icarus are not installed or imported on the MEP.

The MVP includes selected node/host and RFSoC/Recorder/tuner/acquisition
visibility, GPS/location, and forwarding of existing routed periodograms.
AFE register views and capture inventory are not implemented in this
first-pass scope, nor are Icarus jobs, configuration distribution, or software
version management. The existing HostManager does not publish IP addresses or
all of Icarus's software metadata; those fields are not invented.

No SDS operations or upload visibility are included in Phase I.
Controls remain Phase II discussion, not implemented.
See [plan_radiohound_phase2.md](plan_radiohound_phase2.md) for the feature
inventory, concrete client-call examples, and decisions needed before additions.
The existing `mep_client` source-message and canonical-frame APIs are unchanged.
Live broker delivery, external rendering, and hardware operation require site
verification; they are not established by static inspection or mocks.
