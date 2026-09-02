# CaptureOrchestrator

CaptureOrchestrator is the acquisition-workflow service. It is a cookbook of
multi-service recipes for RX capture, TX, and sweeps.

All interaction with those independent services is through MQTT.

## Files

- `capture_orchestrator.py` - workflow policy and MQTT coordination
- `capture_orchestrator.yaml` - example reusable master configuration
- `capture-orchestrator.service` - systemd unit

## MQTT topics

```text
captureorchestrator/announce
captureorchestrator/command
captureorchestrator/response
captureorchestrator/status
captureorchestrator/data
captureorchestrator/event
```

Discovery:

```text
+/announce
```

Announce and status are retained. Responses and events are not retained.

## Workflows

The draft implements:

- `start_rx`: configure recorder, reset/configure RFSoC, optionally configure the tuner, arm RX on the next PPS, and enable the recorder. Supplying `freq_end_hz`, `step_hz`, and `dwell_s` makes this an RX frequency sweep.
- `stop_rx`: stop a single RX capture or the active RX sweep by disabling the recorder and resetting RFSoC.
- `start_tx`: optionally set the tuner LO, configure RFSoC TX, and start TX.
- `stop_tx`: stop RFSoC TX.
- `get_status`: return workflow state.
- `get_config`, `load_config`, `save_config`, and `clear_config`: manage the
  staged master configuration.
- `list_recorder_presets`: discover the recorder recipes deployed on this host.
- `preview_recorder_settings`: validate REC draft values and return calculated
  recorder metrics without starting a capture.

RX and TX are separate signal paths and have separate workflow state. `dwell`
is timing inside an RX sweep, not a standalone command. `tune` is not an
orchestrator command: RFSoC and tuner operations are sent directly to their
own services as visible steps in each recipe.

## Example command

```bash
mosquitto_sub -h localhost -p 1883 -t 'captureorchestrator/response' -v
```

```bash
mosquitto_pub -h localhost -p 1883 -t 'captureorchestrator/command' \
  -m '{"task_name":"start_rx","session_id":"rx-001","arguments":{"freq_start_hz":7000000000,"channel":"A","sample_rate_mhz":10}}'
```

## Boundary

```text
CaptureOrchestrator
    -> MQTT -> RFSoC service
    -> MQTT -> Tuner service
    -> MQTT -> Recorder service
  -> MQTT -> Ringbuffer service when a recipe requires it
  -> MQTT -> ArchiveManager service when a recipe requires it
```

Applications call this service for coordinated workflows. Direct tuner or
recorder operations remain direct commands to those owning services.

MEPGui delegates its main RX and TX Start/Stop controls exclusively to this
service. Its advanced low-level service tools remain independent diagnostic
controls and are not fallback acquisition workflows.

## Master configuration

The versioned capture-settings document accepts a sparse `input` tree and
resolves omitted values from service defaults. The capture automatically saves
its complete `effective` tree and per-setting `provenance`; loading that saved
file uses `effective` for exact replay. `clear_config` restores all defaults.

Portable receive settings describe the RF target and whether an external tuner
is enabled. When enabled, `adc_if_mhz` and high/low injection are required;
CaptureOrchestrator auto-resolves the local tuner model through TunerControl,
sets the RFSoC digitizer frequency, and computes the tuner LO.

CaptureOrchestrator also owns recorder recipe resolution and conjugate policy.
`receive.conjugate_policy` accepts `auto`, `force_on`, or `force_off`; the
resolved `receive.apply_conjugate` value is recorded with derived provenance
and applied to the recorder after user overrides. Direct
`recorder.overrides.packet.apply_conjugate` values are rejected.

For a workflow command, the precedence is:

1. Service defaults
2. Values loaded from YAML
3. Fields supplied in the individual MQTT request

The direct request interface remains available. For example, a request can
provide only `freq_start_hz` and use the staged RX channel and sample rate.

```json
{"task_name":"load_config","arguments":{"path":"/etc/mep/capture_orchestrator.yaml"}}
```

```json
{"task_name":"start_rx","session_id":"rx-001","arguments":{"freq_start_hz":7000000000}}
```

For every named local capture, the orchestrator writes two files under
`/data/captures/<capture_name>` before recording begins:

- `capture_settings.json`: the portable resolved configuration. It can be
  loaded or saved through the normal configuration commands.
- `capture_identity.json`: the local-only stable capture ID and creation time.
  It is not a portable recipe and is never regenerated when recording resumes.

An unnamed RX capture uses `/data/captures/preview`. Before recording starts,
CaptureOrchestrator rotates and clears the existing `preview/data` directory,
then configures RecorderControl to write the new preview there. The preview is
temporary and does not receive a stable capture identity.

SDS paths, remote identities, upload jobs, and upload manifests belong solely
to UploadManager's SQLite database.
