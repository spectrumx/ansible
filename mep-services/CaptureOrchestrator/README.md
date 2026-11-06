# CaptureOrchestrator

CaptureOrchestrator is the acquisition-workflow service. It is a cookbook of
multi-service recipes for RX capture, TX, and sweeps.

All interaction with those independent services is through MQTT.

## Files

- `capture_orchestrator.py` - workflow policy and MQTT coordination
- `capture_settings.py` - pure validation and mapping of portable RX settings
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

The service implements:

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
- `resolve_settings`: resolve a JSON capture document without changing equipment
  or shared staged configuration.

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

## Capture settings

A user-authored JSON file can contain just the choices that matter:

```json
{
  "document": {"type": "mep_capture_settings", "version": 2},
  "name": "1775 MHz observation",
  "settings": {
    "acquisition": {"frequency_hz": 1775000000, "duration_s": 15},
    "receive": {"channel": "D", "sample_rate_hz": 1000000}
  }
}
```

Only `settings` supplies executable choices. Optional `name` and `description`
describe the recipe; the caller supplies the output directory name separately.
Omitted choices use defined defaults, rather than another client's staged state.
Unknown operational fields and invalid values are rejected before acquisition
commands. `record` is descriptive and is ignored when reusing a document.
A format number alone does not reject an otherwise understood recipe.
Generated documents use the type identifier above and version 2 for the
`settings`/`record` structure. Existing `format_version: 1` recipes and the older
`input`/`effective` documents remain readable. The identifier is optional in
handwritten requests; the owning service adds it to the completed document.

| Settings field | Default and meaning | Existing operation |
| --- | --- | --- |
| `acquisition.frequency_hz` | Required RF target in Hz. | RFSoC target or external-tuner RF target. |
| `acquisition.mode` | `single`; alternatively `sweep`. | Existing single/sweep workflow. |
| `acquisition.duration_s` | `null`: run until stopped; otherwise positive seconds for single capture. | Orchestrator dwell timer. |
| `acquisition.sweep` | Required only for sweep: `end_frequency_hz`, signed `step_hz`, positive `dwell_s`. | Orchestrator frequency stepping. |
| `receive.channel` | `A`; supported values A–D. | Selected RFSoC RX path and Recorder UDP port. |
| `receive.sample_rate_hz` | `10000000`; selects a deployed whole-MHz output-rate preset. | `sr<N>MHz` Recorder preset and its resampling. |
| `receive.conjugate` | `auto`; also `force_on`, `force_off`. | Final Recorder conjugation override. |
| `receive.external_tuner` | `enabled: false`; when enabled, omitted IF/injection use `adc_if_mhz: 1090` and `injection: high`. Explicit overrides are supported. | Existing tuner/RFSoC conversion sequence. |
| `afe.misc` | Supported reference, trigger, bias, and antenna bits. | AFEControl multi-device register command. |
| `afe.rxa` / `rxb` / `rxc` / `rxd` | Only the selected RX path; supported bits and `attenuation_db`. | AFEControl register bits, including attenuation bit expansion. |
| `recorder.save_iq`, `save_metadata` | Selected preset values. | `pipeline.digital_rf`, `pipeline.metadata`. |
| `recorder.spectrum` | Preset values: `enabled`, `publish`, `window`, `segment_samples`, `fft_samples`, `overlap_samples`, `detrend`, `reduction`, `spectra_per_chunk`. | Spectrogram calculation and MQTT publication. |
| `recorder.images` | Preset values: `enabled`, `rows_per_image`, `width_inches`, `height_inches`, `dpi`, `columns`, `colour_map`, `snr_min_db`, `snr_max_db`. | Spectrogram image output. |
| `recorder.performance` | Preset values: `batch_size`, `batch_capacity`, `max_packet_size`, `chunk_samples`, `buffer_size`, `worker_threads`. | Packet buffers and scheduler. |
| `recorder.resampling` | Enabled preset stages; each may specify `stage`, `up`, `down`, `outrate_cutoff`, `outrate_transition_width`, `attenuation_db`. | Existing Recorder resamplers; stage names preserved on Save. |
| `recorder.overrides` | Optional existing dotted keys restricted to the portable choices above and `pipeline.int_converter`. Dedicated fields take precedence. | Recorder `config.set`; never arbitrary commands or machine paths. |

The output rate is distinct from the Recorder's input rate. For example, `sr1MHz`
can receive 64 MHz samples and divide by eight twice. Resolution checks the
resulting output rate and exports the enabled stages and concrete FFT/overlap
values. A saved recipe therefore carries the supported processing choices,
rather than relying solely on the preset's name. It still requires a compatible
deployed preset; this is not a replacement for installing Recorder configurations.

Public applications use:

```python
mep.capture.resolve_settings(document, callback=show_settings)
mep.capture.start_document(document, capture_name="trial-01", callback=report_start)
```

`resolve_settings` returns `document`, `recorder_model`, and `recorder_overrides`.
The returned document includes the complete `settings` and `record`, using the
same builder as capture creation. Optional `capture_name` describes the output
paths for that name without creating a directory or starting a capture. GUI Save
supplies its Capture Name control and writes the returned document unchanged.
Its optional `settings_overrides` object overlays explicit choices on a supplied
document before validation, including recognized historical documents. MEPCli
uses this for config-plus-argument requests. Omitted overrides preserve the
document's settings; ordinary GUI resolution calls are unchanged.
It has no hardware or shared-state effects. `start_document` validates again and
passes the request through the existing RX workflow. CaptureOrchestrator owns all
timing, and Stop remains available to any normal local client after the submitting
client disconnects. No capture lease or exclusive owner is introduced.

In MEPGui, RX **Settings → Load...** opens a file chooser, validates, and stages
controls. The user must press **Start/Update** to acquire. Save includes the current REC draft;
the existing REC **Apply on Next Record** action still controls when an edited
REC draft becomes pending for Start. CAP's selected-capture context menu offers
Use Settings, View Settings, and Export Settings.

GUI Load/Save needs MQTT and CaptureOrchestrator, with its Recorder preset files
available. It sends a read-only resolution request and does not configure
hardware. Connection and response errors identify these required services.

The original direct GUI/API acquisition interface remains supported. An ordinary
GUI Start retains its existing command behavior; loading a recipe selects the
explicit document interface. Both use the same RX workflow and Recorder preset
calculations. See [MEPCli](../MEPCli/README.md) for `mep_capture`.

## Saved capture record

For every named local capture, the orchestrator creates `data/` and `log_upload/`
and writes one `data/capture_settings.json` before recording begins. It contains
complete portable `settings` plus a generated `record` with:

- Capture name and available HostManager identity/interface MACs.
- Resolved RF target, channel, input/output rates, conjugation and tuner model.
- The full Recorder configuration assembled using the same ordered overrides
  as Recorder command generation, including channel port and output paths.
- Available component status and register readback from before configuration.

GUI Save uses this same document structure and includes the same detail. It
records the currently resolved configuration and available status without
applying settings. Starting a capture assembles the document again using the
submitted settings and status available then. With the same settings, output
name, presets, and available status, the two documents have identical contents.

Missing descriptive status is represented as unavailable and does not add a
capture prerequisite. Paths, observations, and snapshots in `record` are never
loaded as commands. Load/Save do not rewrite the original capture record.
Compatible capture-name reuse retains its existing document; incompatible
settings require a new name. ArchiveManager exposes old and new documents and
UploadManager continues uploading the ordinary `data/` directory unchanged.

An unnamed RX capture uses `/data/captures/preview`. Before recording starts,
CaptureOrchestrator rotates and clears the existing `preview/data` directory,
then configures RecorderControl to write the new preview there. The preview is
temporary. UploadManager owns `log_upload/upload_status.json`; the contents of
`data/` are the SDS payload.

## Existing staged configuration interface

`get_config`, `load_config`, `save_config`, `clear_config`, and direct `start_rx`
arguments remain supported for existing callers. Their `input`/`effective`/
`provenance` representation and explicit shared staging behavior are distinct
from the stateless document calls above. Defaults, staged values, then request
fields determine a legacy request. `clear_config` restores its defaults.

Existing capture JSON remains readable. Loading that known document shape into
the GUI converts its effective supported choices once, excluding machine paths
and derived observations; historical files are not migrated or rewritten.
