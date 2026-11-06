# MEPCli

`mep_cli.py` is the command-line application alongside `MEPGui/mep_gui.py`.
Its installed command is **`mep_capture`**. It starts or stops RX acquisition
through public `mep_client` calls. CaptureOrchestrator owns controller sequencing,
duration, and sweeping. MEPCli has no service unit or background process.

## Installation and help

The existing `mep-services.yml` playbook makes `mep_cli.py` executable and links
`/usr/local/bin/mep_capture` directly to it. The `mep` command still opens MEPGui.
Restarting services alone does not install or update the command link.

```bash
mep_capture --help
```

Before the link task has run, invoke the application directly:

```bash
/opt/radiohound/python313/bin/python /opt/ansible/mep-services/MEPCli/mep_cli.py --help
```

`--help`, argument parsing, and malformed-file errors work without a broker,
display server, or hardware. Acquisition needs MQTT and CaptureOrchestrator;
settings resolution also needs the deployed Recorder presets.

## Common captures

No config file is required. CLI frequencies, IF, steps, and sample rates use
**MHz**; durations and dwell use **seconds**. JSON settings retain their existing
field units, including Hz for frequency and output sample rate.

```bash
# Direct reception, using service defaults for omitted choices
mep_capture --frequency-mhz 1775 --duration-s 15 --capture-name trial-01

# Select the RX channel and recorded output rate
mep_capture --frequency-mhz 1775 --channel D --sample-rate-mhz 1 \
  --duration-s 15 --capture-name trial-02

# External tuner, using normal IF and injection defaults
mep_capture --frequency-mhz 7000 --tuner enabled \
  --duration-s 15 --capture-name trial-03

# Sweep from 1700 to 1800 MHz in 1 MHz steps, dwelling 2 seconds per frequency
mep_capture --frequency-mhz 1700 --end-frequency-mhz 1800 \
  --step-mhz 1 --dwell-s 2 --capture-name sweep-01

# Descending sweep
mep_capture --frequency-mhz 1800 --end-frequency-mhz 1700 \
  --step-mhz -1 --dwell-s 2 --capture-name sweep-02

# Stop the current RX capture or sweep, including one started by another client
mep_capture --stop
```

`--stop` calls the existing RX Stop operation and cannot be combined with a
config, capture name, or acquisition settings. The GUI's existing Stop action
uses the orchestrator's broader abort operation; these are distinct public operations.

Omit `--duration-s` for an untimed single capture, unless a config supplies a
duration. Stop it with normal MEP controls or `mep_capture --stop`.
`--duration-s` selects single-capture mode; sweep end/step/dwell arguments select
sweep mode. A sweep needs its end frequency, signed step, and dwell from the
config and/or CLI. Single duration and sweep flags cannot be combined.

## Config files and precedence

Use a file for reusable experiments or detailed processing choices:

```bash
mep_capture experiment.json --capture-name trial-04
mep_capture experiment.json --frequency-mhz 1775 --duration-s 30 --capture-name trial-05
mep_capture experiment.json --tuner disabled --capture-name direct-01
```

Precedence is **service defaults → config settings → explicit CLI arguments**.
Omitting a flag preserves its config value. The CLI does not copy its own set of
equipment defaults. CaptureOrchestrator combines and validates the request.
An explicit single duration replaces a configured sweep; sweep flags replace a
configured single duration. Historical `record` contents never become commands.

For example, `experiment.json` can contain:

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

Detailed Recorder processing and register choices remain in the config. A saved
capture's `data/capture_settings.json` can also be reused. Existing recognized
capture documents are converted by CaptureOrchestrator, including when CLI
arguments override them. See the [settings contract](../CaptureOrchestrator/README.md).

## Tuner choices

`--tuner enabled` enables external conversion and uses the orchestrator's defaults
of **1090 MHz IF** and **high-side injection** for omitted conversion fields.
A config's explicit IF/injection values are preserved. `--tuner disabled`
overrides an enabled config. Without a config or tuner flag, reception is direct.
The local tuner model is resolved through the existing tuner service.

For an unusual conversion setup, optional overrides are available:

```bash
mep_capture --frequency-mhz 7000 --tuner enabled --if-mhz 1200 \
  --injection low --duration-s 15 --capture-name alternate-if
```

IF/injection flags do not enable the tuner by themselves. Conjugation normally
uses `auto`; `--conjugate force_on` or `force_off` explicitly overrides it.

## Options

| Option | Meaning |
| --- | --- |
| `CONFIG.json` | Optional positional settings file. |
| `--frequency-mhz MHZ` | RF target or sweep start frequency. Required without a config. |
| `--channel A\|B\|C\|D` | RX channel; service default A. |
| `--sample-rate-mhz MHZ` | Recorded/output rate; service default 10 MHz. Selects a deployed whole-MHz Recorder preset. |
| `--duration-s SECONDS` | Single-capture duration. |
| `--end-frequency-mhz MHZ` | Sweep end frequency. |
| `--step-mhz MHZ` | Signed sweep step. |
| `--dwell-s SECONDS` | Time at each sweep frequency. |
| `--tuner enabled\|disabled` | External conversion choice. |
| `--if-mhz MHZ` | Optional digitizer IF override. |
| `--injection high\|low` | Optional injection override. |
| `--conjugate auto\|force_on\|force_off` | Conjugation policy. |
| `--capture-name NAME` | Capture directory name; omit for preview storage. |
| `--stop` | Stop RX without a config or acquisition settings. |
| `--host HOST` | MEP bus host, default `localhost`. |
| `--port PORT` | MEP bus port, default `1883`. |
| `--timeout SECONDS` | Connection and per-response timeout, default 10 seconds. |
| `--help` | Show help without connecting. |

The output sample rate is not necessarily the Recorder input rate: for example,
`sr1MHz` can receive 64 MHz input and decimate twice. The owner validates the
processing chain. `--capture-name` names a directory under `/data/captures`;
without it, preview uses the existing replacement behavior. Recipe names and
historical output paths do not supply the new capture directory name.

## Responses and Python use

The application exits after the request is acknowledged, not after acquisition
finishes. A finite capture or sweep continues under CaptureOrchestrator after
the CLI disconnects. Closing or interrupting the CLI does not stop acquisition.
Local controls remain available, and no acquisition ownership lock is created.

Exit codes: **0** means the start/stop request succeeded, **2** means invalid input
or a rejected request, **3** means connection/publication failure or an uncertain
response, and **130** means the client was interrupted. A lost start response is
not proof that acquisition failed. Inspect status before submitting again; the
CLI does not automatically retry commands.

Python applications use the same public API without invoking the CLI:

```python
mep.capture.resolve_settings(document, settings_overrides=overrides, callback=show_settings)
mep.capture.start_document(document, capture_name="trial-01", callback=report_start)
mep.capture.stop_rx(callback=report_stop)
```

These calls are asynchronous. Resolution has no equipment or shared-staging
effects. Its returned document contains the identifier, complete settings, and
detailed record used by GUI Save and capture creation. Use the resolved response's
`document` when starting a resolved recipe.
