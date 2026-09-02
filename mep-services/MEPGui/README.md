# MEPGui

MEPGui is a Tkinter frontend, not a service. It uses the shared Python
`MEPClient` API and does not import message-bus topics or transport code. Main
RX and TX workflows are sent to CaptureOrchestrator. Host, archive, upload,
AFE, tuner, Docker, and system-service views consume component status and send
semantic commands through the client.

The local `mep_client` package owns bus connectivity, subscriptions, wire
decoding, and component command APIs. Recorder recipe discovery, validation,
preview metrics, and conjugate resolution belong to CaptureOrchestrator.

## AFE telemetry boundary

The TLM tab is the single GUI surface for GNSS, IMU, magnetometer, and AFE
housekeeping telemetry. GNSS values come from `afecontrol/data/gps`; the GUI
does not parse GPSD data itself. The same view renders retained
`afecontrol/status/gpsd` transport state and can request a bounded,
non-retained `afecontrol/data/raw` diagnostic lease. It does not expose
arbitrary GPSD commands.

## Docker boundary

Docker Compose lifecycle, status, and log streaming use DockerManager through
`dockermanager/`. The GUI does not run local Docker commands for its DOC tab.

## System service boundary

Systemd lifecycle, status, and journal streaming use ServiceManager through
`servicemanager/`. The SVC tab discovers its manageable unit list from the
service announce/status contract; the GUI contains no systemd unit allowlist
and runs no local `systemctl` or `journalctl` commands.

## Run

```text
python3 /opt/mep-services/MEPGui/mep_gui.py
```