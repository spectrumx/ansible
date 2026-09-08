#!/usr/bin/env python3
"""
mep_gui.py

Tkinter GUI for MEP RFSoC sweep/record control via X11 forwarding.

All acquisition workflows go through the shared Python MEP client. This GUI
remains presentation-only: widgets, layout, and callbacks.

Usage:
    ssh -X mep@<jetson> python3 ~/mep-services/MEPGui/mep_gui.py

Author: john.marino@colorado.edu
"""

import sys
import os
import argparse
import shutil
import re
import math
import json
import time
import queue
import threading
import logging
from collections import deque
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import datetime
import numpy as np
from PIL import Image as _PILImage, ImageTk as _PILImageTk

MEP_SERVICES_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, MEP_SERVICES_ROOT)
from mep_client import MEPClient

CHANNEL_OPTIONS = MEPClient.CHANNEL_OPTIONS
RECORDER_CHANNEL_PORTS = MEPClient.RECORDER_CHANNEL_PORTS
CONJUGATE_POLICY_DEFAULT = MEPClient.CONJUGATE_POLICY_DEFAULT
CONJUGATE_POLICY_OPTIONS = MEPClient.CONJUGATE_POLICY_OPTIONS
TX_CHANNEL_OPTIONS = MEPClient.TX_CHANNEL_OPTIONS
TX_OFFSET_FREQ_MAX_MHZ = MEPClient.TX_OFFSET_FREQ_MAX_MHZ
TX_AMPLITUDE_BINS_MAX = MEPClient.TX_AMPLITUDE_BINS_MAX

# ===== LAYOUT CONSTANTS ===== #
LEFT_PANEL_WIDTH = 450
ADV_PANEL_WIDTH = 510
DEFAULT_WIN_HEIGHT = 750
MQTT_LOG_BUFFER_MAX_MESSAGES = 500
MQTT_LOG_WIDGET_MAX_LINES = 10000
APP_LOG_WIDGET_MAX_LINES = 5000
APP_LOG_PENDING_MAX_MESSAGES = 1000
# Max SPEC frames queued between render ticks. Bounds the MQTT->Tk handoff and
# never exceeds a screen of catch-up (waterfall height), so it drops only the
# oldest frames under sustained overrun, never silently coalesces fresh ones.
SPEC_PENDING_MAX_ROWS = 256


# ===== TEXT LOGGING HANDLER ===== #

class _TextHandler(logging.Handler):
    """Logging handler that appends records to a ScrolledText widget."""

    def __init__(
        self,
        widget: scrolledtext.ScrolledText,
        max_lines: int = APP_LOG_WIDGET_MAX_LINES,
    ):
        super().__init__()
        self.widget = widget
        self.max_lines = max_lines
        self._pending = queue.Queue(maxsize=APP_LOG_PENDING_MAX_MESSAGES)
        self._closed = False

    def emit(self, record: logging.LogRecord):
        if self._closed:
            return
        try:
            msg = self.format(record) + "\n"
        except Exception:
            self.handleError(record)
            return
        try:
            self._pending.put_nowait(msg)
        except queue.Full:
            try:
                self._pending.get_nowait()
            except queue.Empty:
                pass
            self._pending.put_nowait(msg)

    def flush_pending(self):
        if self._closed:
            return

        messages = []
        while True:
            try:
                messages.append(self._pending.get_nowait())
            except queue.Empty:
                break

        if not messages:
            return

        try:
            self.widget.configure(state="normal")
            for msg in messages:
                self.widget.insert(tk.END, msg)
            line_count = int(self.widget.index("end-1c").split(".")[0])
            if line_count > self.max_lines:
                self.widget.delete("1.0", f"{line_count - self.max_lines}.0")
            self.widget.see(tk.END)
            self.widget.configure(state="disabled")
        except tk.TclError:
            self._closed = True

    def close(self):
        self._closed = True
        super().close()


# ===== SPECTRUM HELPERS ===== #

def _spec_resample_1d(arr: np.ndarray, n_out: int) -> np.ndarray:
    """Linearly resample a 1-D float32 array to n_out samples."""
    n_in = len(arr)
    if n_in == n_out:
        return arr.astype(np.float32, copy=False)
    if n_in == 0 or n_out == 0:
        return np.zeros(n_out, dtype=np.float32)
    xp = np.linspace(0.0, 1.0, n_in)
    xq = np.linspace(0.0, 1.0, n_out)
    return np.interp(xq, xp, arr).astype(np.float32)


class SpectrumViewport:
    """Screen-resolution waterfall ring buffer. Owned exclusively by the Tk thread.

    Stores exactly ``height`` rows of ``width`` float32 dBFS values plus one
    metadata dict per row. New rows overwrite the oldest as they arrive.
    Capacity is always bounded by the visible canvas — no off-screen history.

    Not thread-safe. All methods must be called from the Tk thread.
    """

    def __init__(self, width: int = 1, height: int = 1):
        self._w = max(1, width)
        self._h = max(1, height)
        # Ring: _head is next write position. Newest row: (_head-1) % _h.
        self._values = np.full((self._h, self._w), np.nan, dtype=np.float32)
        self._meta: list = [None] * self._h
        self._head = 0
        self._count = 0  # valid rows: 0..height

    @property
    def width(self) -> int:
        return self._w

    @property
    def height(self) -> int:
        return self._h

    @property
    def valid_rows(self) -> int:
        """Number of rows written so far (up to height)."""
        return self._count

    def accept_row(self, native_row: np.ndarray, meta: dict) -> bool:
        """Resample native_row to viewport width and store it as the newest row.

        meta keys expected: ts, center_frequency, fmin, fmax, scan_time, n.
        Returns True on success, False if native_row is empty.
        """
        if native_row is None or len(native_row) == 0:
            return False
        row = _spec_resample_1d(native_row, self._w)
        self._values[self._head] = row
        self._meta[self._head] = meta or {}
        self._head = (self._head + 1) % self._h
        if self._count < self._h:
            self._count += 1
        return True

    def row_at_offset(self, offset: int):
        """Return (values_view, meta) for the row at offset from newest (0=newest).

        values_view is a direct view into the ring buffer — do not modify.
        Returns (None, None) if offset is out of range.
        """
        if offset < 0 or offset >= self._count:
            return None, None
        idx = (self._head - 1 - offset) % self._h
        return self._values[idx], self._meta[idx]

    def latest_meta(self):
        """Return the metadata dict of the newest row, or None."""
        if self._count == 0:
            return None
        return self._meta[(self._head - 1) % self._h]

    def value_range(self):
        """Return (min, max) across all valid finite values, or (None, None)."""
        if self._count == 0:
            return None, None
        indices = [(self._head - 1 - i) % self._h for i in range(self._count)]
        data = self._values[indices]
        mask = np.isfinite(data)
        if not np.any(mask):
            return None, None
        return float(np.min(data[mask])), float(np.max(data[mask]))

    def clear(self):
        """Reset all rows to empty."""
        self._values[:] = np.nan
        self._meta = [None] * self._h
        self._head = 0
        self._count = 0

    def resize(self, width: int, height: int):
        """Reallocate to (width, height), resampling and preserving recent rows."""
        w = max(1, width)
        h = max(1, height)
        if w == self._w and h == self._h:
            return
        # Snapshot existing rows newest-first; keep only as many as fit
        n_keep = min(self._count, h)
        saved_rows = []
        saved_meta = []
        for i in range(n_keep):
            idx = (self._head - 1 - i) % self._h
            saved_rows.append(self._values[idx].copy())
            saved_meta.append(self._meta[idx])
        self._w = w
        self._h = h
        self._values = np.full((h, w), np.nan, dtype=np.float32)
        self._meta = [None] * h
        self._head = 0
        self._count = 0
        # Re-insert oldest-first, resampled to new width
        for vals, meta in zip(reversed(saved_rows), reversed(saved_meta)):
            self._values[self._head] = _spec_resample_1d(vals, w)
            self._meta[self._head] = meta
            self._head = (self._head + 1) % h
            self._count += 1


# ===== MAIN GUI CLASS ===== #

class MEPGui:

    def __init__(self, root: tk.Tk, mqtt_host: str = MEPClient.DEFAULT_HOST, mqtt_port: int = MEPClient.DEFAULT_PORT):
        self.root = root
        self.root.title("MEP")
        self.root.resizable(True, True)
        self._mqtt_host = mqtt_host
        self._mqtt_port = mqtt_port

        self._sweep_thread: threading.Thread = None
        self._afe_updating = False
        self._afe_atten_pending = {}
        self._afe_atten_initialized = set()
        self._afe_atten_request_counter = 0
        self._afe_schema_signature = None
        self._afe_time_source_signature = None
        self._tlm_latest_gps = {}
        self._tlm_latest_gpsd = {}
        
        # MQTT streaming
        self._mqtt_buffer_max_messages = MQTT_LOG_BUFFER_MAX_MESSAGES
        self._mqtt_widget_max_lines = MQTT_LOG_WIDGET_MAX_LINES
        self._mqtt_messages = deque(maxlen=self._mqtt_buffer_max_messages)
        self._mqtt_lock = threading.Lock()
        self._mqtt_rendered_count = 0
        self._mqtt_paused = False
        self._gui_queue = queue.SimpleQueue()
        self._gui_queue_closed = False
        
        # DockerManager client is constructed after the shared MQTT bus.
        self.docker = None
        self._docker_suppress_tree_stream = False
        self._service_journal_user_paused = False
        
        # RFSoC monitoring
        self._monitor_rfsoc_tlm = None
        self._monitor_rfsoc_log_key = None
        self._monitor_rfsoc_tlm_lock = threading.Lock()
        self._monitor_rfsoc_tlm_event = threading.Event()
        
        # SPEC tab state
        self._spec_lock = threading.Lock()
        # _spec_lock guards _spec_pending only (MQTT-to-Tk handoff).
        # All other spec state is Tk-thread-only.
        self._spec_topic = ""             # populated from the client after construction
        self._spec_stream_requested = True    # SPEC streams by default at startup
        self._spec_tab_visible = False        # SPEC tab is currently showing
        self._spec_is_active = False          # derived: stream_requested AND tab_visible
        self._spec_pending = deque(maxlen=SPEC_PENDING_MAX_ROWS)  # bounded MQTT->Tk frame queue
        self._spec_latest_entry = None        # newest entry for line-plot native resolution
        self._spec_viewport = SpectrumViewport()   # screen-resolution ring (Tk thread only)
        self._spec_bins = None               # line-plot resolution override (None = native)
        self._spec_render_interval_ms = 40   # render cadence (~25 FPS)
        self._spec_render_after_id = None
        self._spec_force_render = False
        self._spec_last_arrival = None
        self._spec_log_dt = False
        self._spec_color_lut = self._spec_build_color_lut()
        # Waterfall image (Tk thread only)
        self._spec_pixels = None             # (h, w, 3) uint8 pixel buffer
        self._spec_photo = None              # one persistent ImageTk.PhotoImage
        self._spec_wf_image_id = None        # canvas item id for the waterfall image
        self._spec_wf_resize_after_id = None
        self._spec_wf_target_size = None
        # Persistent canvas items (created once, updated via coords/itemconfig)
        self._spec_line_item = None
        self._spec_line_labels = {}
        self._spec_wf_labels = {}

        # False until root.mainloop() is running; _gui_call routes to _gui_queue until then,
        # so emit-cached callbacks drain via _pump_gui_queue after the window is first rendered.
        self._mainloop_started = False
        self._conjugate_policy_user_override = False
        self._rec_pending_overrides = {}
        self._rec_preview_request_seq = 0

        self._vars = {}
        self._init_shared_vars()
        self._install_text_widget_bindings()
        print("  Building UI widget tree...", flush=True)
        self._build_ui()
        self._setup_logging()

        # ---- MQTT bus (always-on) ----
        print("  Connecting to MQTT broker...", flush=True)
        self.mep = MEPClient(host=self._mqtt_host, port=self._mqtt_port)
        self.capture_orchestrator = self.mep.capture
        self.archive_manager = self.mep.archive
        self.upload_manager = self.mep.upload
        self.host_manager = self.mep.host
        self.docker = self.mep.docker
        self.service_manager = self.mep.systemd

        # ---- Register listeners on bus (always active) ----
        # NOTE: announce MUST be registered before registers so that
        # emit-cached fires _on_afe_announce first, populating _afe_reg_pins
        # before register data tries to use them.
        self.mep.diagnostics.on_message(self._on_mqtt_message)
        self.mep.on_connection_state(self._on_mqtt_connection_state)
        self.mep.recorder.on_status(self._on_recorder_status)
        self.mep.recorder.on_status(lambda data: self._gui_call(self._rec_status_ui_update, data))
        self.mep.rfsoc.on_status(self._on_rfsoc_status)
        self.mep.rfsoc.on_status(lambda data: self._gui_call(self._soc_apply, data))
        self.mep.rfsoc.on_status(lambda data: self._gui_call(self._tx_apply, data))
        self.mep.rfsoc.on_pll_config(lambda data: self._gui_call(self._soc_apply_pll_config, data))
        self.mep.tuner.on_status(self._on_tuner_status)
        self.mep.tuner.on_status(lambda data: self._gui_call(self._tun_refresh))
        self.mep.tuner.on_response(lambda data: self._gui_call(self._tun_handle_response, data) if "task_name" in data and "value" in data else None)
        self.mep.afe.on_status(self._on_afe_status)
        self.mep.afe.on_gnss(self._on_gnss)
        self.mep.afe.on_imu(self._on_imu)
        self.mep.afe.on_magnetometer(self._on_mag)
        self.mep.afe.on_housekeeping(self._on_hk)
        self.mep.afe.on_gpsd_status(self._on_gpsd_status)
        self.mep.afe.on_raw_data(self._on_afe_raw)
        self.mep.afe.on_announce(self._on_afe_announce)
        self.mep.afe.on_registers(self._on_afe_registers)
        self.mep.afe.on_polling_response(self._on_afe_polling_response)
        self.mep.afe.on_logging_response(self._on_afe_logging_response)
        self.capture_orchestrator.on_status(lambda data: self._gui_call(self._apply_orchestrator_status, data))
        self.archive_manager.on_status(lambda data: self._gui_call(self._cap_apply_service_status, data))
        self.upload_manager.on_status(lambda data: self._gui_call(self._cap_apply_service_status, data))
        self.upload_manager.on_activity(lambda data: self._gui_call(self._cap_upload_activity_event, data))
        self.host_manager.on_announce(lambda data: self._gui_call(self._host_manager_apply_announce, data))
        self.host_manager.on_status(lambda data: self._gui_call(self._host_manager_apply_status, data))
        self.docker.on_status(lambda data: self._gui_call(self._docker_apply_status))
        self.service_manager.on_status(lambda data: self._gui_call(self._service_apply_status, data))
        self.mep.spectrum.on_frame(self._on_spec_data)

        # Refresh status grid from any cached state.
        self._refresh_status_grid()
        print("  Startup complete — entering event loop.", flush=True)

        # ---- SPEC source ----
        self._spec_topic = self.mep.spectrum.topic
        if "spec_topic" in self._vars:
            self._vars["spec_topic"].set(self._spec_topic)

        # Start with Advanced Options open on the SPEC tab. The bus and SPEC
        # listener must exist before activating the default stream.
        self._adv_frame.grid()
        self._adv_btn_text.set("Hide Advanced Options \u25c0")
        self._adv_nb.select(self._tab_frames["SPEC"])
        self._ensure_tab_built("SPEC")
        self._on_adv_tab_changed()

        # ---- Startup sequence with intentional delays ----
        self.root.after(20, self._pump_gui_queue)
        self.root.after(50, self._pump_text_log)
        self.root.after(100, self._rec_request_presets)

    def _init_shared_vars(self):
        """Initialize cross-tab state once, independent of lazy tab construction."""
        self._rec_presets_loaded = False
        self._vars["time_source"] = tk.StringVar(value="")
        self._vars["epoch_mode"] = tk.StringVar(value="")
        # Blank/0 until the service reports the real value — never guess.
        self._vars["poll_interval_s"] = tk.IntVar(value=0)
        self._vars["log_enabled"] = tk.StringVar(value="enabled")
        # Blank/0 until afecontrol/announce reports the real state - never guess.
        self._vars["log_path"] = tk.StringVar(value="")
        self._vars["log_rate"] = tk.DoubleVar(value=0.0)
        self._vars["conjugate_policy"] = tk.StringVar(value="")
        self._vars["conjugate_actual"] = tk.StringVar(value="—")

    def _add_tooltip(self, widget, text, wraplength=320):
        """Add a simple tooltip to a widget that shows on hover."""
        def on_enter(event):
            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")
            label = ttk.Label(
                tooltip, text=text, background="#ffffe0", relief="solid",
                borderwidth=1, wraplength=wraplength, justify="left",
            )
            label.pack()
            widget._tooltip = tooltip
        
        def on_leave(event):
            if hasattr(widget, '_tooltip'):
                widget._tooltip.destroy()
                del widget._tooltip
        
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)

    # ------------------------------------------------------------------ #
    #  MQTT listener callbacks (called from MQTT thread → schedule to GUI)
    # ------------------------------------------------------------------ #

    def _on_mqtt_message(self, topic: str, payload: bytes):
        """Global listener: log every MQTT message in the MQTT tab.

        No topic is dropped here — what the human sees is governed entirely by
        the Suppress checkboxes (Announce / +/data/+ / Status) at display time
        in _mqtt_flush_buffer_to_widget(). Spectrum frames match the '+/data/+'
        suppression, so they are hidden by default without being special-cased.
        """
        self._mqtt_capture_message(topic, payload)

    def _on_mqtt_connection_state(self, status: dict):
        self._gui_call(self._refresh_status_grid)
        if status.get("connected"):
            self._gui_call(self._rec_request_presets)
            self.archive_manager.refresh()
            self.upload_manager.refresh()

    def _on_recorder_status(self, data: dict):
        state = data.get("state", "—")
        logging.info(f"Recorder: {state}")
        self._gui_call(self._refresh_status_grid)

    def _on_rfsoc_status(self, data: dict):
        f_c = float(data.get("f_c_hz", 0)) / 1e6
        pps = data.get("pps_count", "?")
        state = data.get("state", "?")
        logging.info(f"RFSoC: {state}  f_c={f_c:.2f} MHz  pps={pps}")
        self._gui_call(self._refresh_status_grid)

    def _on_tuner_status(self, data: dict):
        if not ("task_name" in data and "value" in data and "state" not in data):
            state = data.get("state", "?")
            logging.info(f"Tuner: {state}")
        self._gui_call(self._update_tuner_selection, data)
        self._gui_call(self._refresh_status_grid)

    def _update_tuner_selection(self, data: dict):
        tuner_name = data.get("name") if isinstance(data, dict) else None
        if not tuner_name and isinstance(data, dict):
            tuner_name = data.get("backend")
        self._advertised_tuner = str(tuner_name).upper() if tuner_name else None

        tuner_selection = self._vars.get("tuner_selection")
        if tuner_selection is None:
            return
        tuner_enabled = tuner_selection.get() != "Disabled"
        values = ["Disabled"]
        if self._advertised_tuner:
            values.append(self._advertised_tuner)
        for tuner_combo in self._tuner_combos:
            tuner_combo.configure(values=values)
        tuner_selection.set(
            self._advertised_tuner if tuner_enabled and self._advertised_tuner else "Disabled"
        )

    def _on_afe_status(self, data: dict):
        state = data.get("state", "?")
        logging.info(f"AFE: {state}")
        self._on_afe_logging_response(data)
        self._on_afe_polling_response(data)
        self._gui_call(self._afe_apply_state, data)
        self._gui_call(self._refresh_status_grid)

    def _on_gnss(self, data: dict):
        self._tlm_latest_gps = dict(data)
        self._gui_call(self._tlm_gps_update, data)

    def _on_gpsd_status(self, data: dict):
        self._tlm_latest_gpsd = dict(data)
        self._gui_call(self._tlm_gpsd_update, data)

    def _on_afe_raw(self, data: dict):
        self._gui_call(self._tlm_raw_append, data)

    def _on_imu(self, data: dict):
        self._gui_call(self._tlm_imu_update, data)

    def _on_mag(self, data: dict):
        self._gui_call(self._tlm_mag_update, data)

    def _on_hk(self, data: dict):
        self._gui_call(self._tlm_hk_update, data)

    def _on_afe_registers(self, data: dict):
        self._gui_call(self._afe_apply_state, data)
        self._gui_call(self._refresh_status_grid)

    def _on_afe_polling_response(self, data: dict):
        """Update poll_interval_s from get_interval/set_interval responses — the only source of truth."""
        n = data.get("configured_interval_s", data.get("service_telem_poll_interval_s"))
        if n is None:
            return
        try:
            self._gui_call(self._vars["poll_interval_s"].set, int(n))
        except (TypeError, ValueError):
            pass

    def _on_afe_logging_response(self, data: dict):
        """Update logging widgets from afecontrol/response/logging - the only source of truth."""
        if "exception" in data:
            logging.error(f"TLM logging command failed: {data['exception']}")
            return

        enabled = data.get("telemetry_logging_enabled")
        if enabled is not None:
            self._gui_call(self._vars["log_enabled"].set, "enabled" if enabled else "disabled")

        log_dir = data.get("telemetry_log_dir")
        if log_dir is not None:
            self._gui_call(self._vars["log_path"].set, str(log_dir))

        rate = data.get("telemetry_log_rate_s")
        if rate is not None:
            try:
                self._gui_call(self._vars["log_rate"].set, float(rate))
            except (TypeError, ValueError):
                pass

    def _on_afe_announce(self, data: dict):
        """Handle afecontrol/announce retained message - populate dynamic widgets."""
        logging.info("GUI: announce data applied to all dynamic widgets")
        self._gui_call(self._apply_afe_announce, data)
        self._gui_call(self._afe_refresh)

    def _apply_afe_announce(self, data: dict):
        """Populate all announce-driven widgets on the GUI thread."""
        describe = data.get("describe", {})

        # ---- AFE tab (registers, devices) ---- #
        self._afe_populate_from_announce(data)
        self._tlm_populate_gps_fields(data)

        # ---- Polling interval range/current (service is the sole source of truth) ---- #
        polling_ref = describe.get("polling", {}).get("reference", {})
        poll_range = polling_ref.get("poll_interval_range")
        if poll_range and hasattr(self, "_poll_interval_spin"):
            self._poll_interval_spin.configure(from_=poll_range[0], to=poll_range[1])
        poll_current = polling_ref.get("poll_interval_current")
        if poll_current is not None:
            try:
                self._vars["poll_interval_s"].set(int(poll_current))
            except (TypeError, ValueError):
                pass

        # ---- Time source radio buttons (dynamic from announce) ---- #
        time_ref = describe.get("time", {}).get("reference", {})
        source_opts = time_ref.get("time_source_options", {})
        if source_opts and hasattr(self, "_time_source_frame"):
            time_source_options = tuple(
                (str(code), str(label))
                for code, label in sorted(source_opts.items())
                if str(label).lower() != "notset"
            )
            if time_source_options != self._afe_time_source_signature:
                for w in self._time_source_frame.winfo_children():
                    w.destroy()
                col = 0
                first_val = None
                for _code, label in time_source_options:
                    val = label.lower()
                    if first_val is None:
                        first_val = val
                    ttk.Radiobutton(self._time_source_frame, text=label,
                                    variable=self._vars["time_source"],
                                    value=val,
                                    command=self._tlm_apply_time_config).grid(row=0, column=col, sticky="w", padx=2)
                    col += 1
                self._afe_time_source_signature = time_source_options
                if first_val:
                    self._vars["time_source"].set(first_val)

        # ---- Epoch combobox (from announce dict) ---- #
        epoch_opts = time_ref.get("time_epoch_options", {})
        if epoch_opts and hasattr(self, "_epoch_combo"):
            labels = [v.lower() for _k, v in sorted(epoch_opts.items())
                      if v.lower() != "notset"]
            self._epoch_combo["values"] = labels
            if labels:
                self._vars["epoch_mode"].set(labels[0])

        # ---- Logging ---- #
        # No range is advertised or enforced client-side; the service validates on set.
        log_ref = describe.get("logging", {}).get("reference", {})

        # Only set path/rate fields when the service actually reports them —
        # no fabricated fallback. If absent, the field stays blank/0 (unknown).
        log_path_current = log_ref.get("log_path_current")
        if log_path_current:
            self._vars["log_path"].set(str(log_path_current))

        log_rate_current = log_ref.get("log_rate_current")
        if log_rate_current is not None:
            try:
                self._vars["log_rate"].set(float(log_rate_current))
            except (TypeError, ValueError):
                pass

    def _on_spec_data(self, frame: dict):
        """Spectrum callback: client thread to bounded GUI ingestion queue."""
        self._spec_handle_stream_message(frame)

    def _gui_call(self, func, *args, **kwargs):
        if self._gui_queue_closed:
            return
        if threading.current_thread() is threading.main_thread() and self._mainloop_started:
            func(*args, **kwargs)
            return
        self._gui_queue.put((func, args, kwargs))

    def _pump_gui_queue(self):
        """Drain queued GUI callbacks, yielding to the event loop between items.

        Each tick processes one item then reschedules immediately (after(0))
        so that Tk can handle rendering and user input between heavy callbacks
        (e.g. _afe_populate_from_announce creating dozens of X11 widgets).
        When the queue is empty, drops back to the 20ms polling interval.
        """
        if self._gui_queue_closed:
            return
        try:
            func, args, kwargs = self._gui_queue.get_nowait()
        except queue.Empty:
            try:
                self.root.after(20, self._pump_gui_queue)
            except tk.TclError:
                self._gui_queue_closed = True
            return
        try:
            func(*args, **kwargs)
        except tk.TclError:
            self._gui_queue_closed = True
            return
        except Exception:
            logging.exception("GUI dispatch callback failed")
        # Reschedule immediately to drain remaining items, but yield so Tk
        # can process expose/configure events between each callback.
        try:
            self.root.after(0, self._pump_gui_queue)
        except tk.TclError:
            self._gui_queue_closed = True

    # ------------------------------------------------------------------ #
    #  TLM tab updaters (run on GUI thread)
    # ------------------------------------------------------------------ #

    def _set_var(self, key: str, val):
        if key in self._vars:
            value = str(val) if val is not None else "—"
            if self._vars[key].get() != value:
                self._vars[key].set(value)

    def _afe_next_session_id(self, prefix: str) -> str:
        self._afe_atten_request_counter += 1
        return f"{prefix}-{self._afe_atten_request_counter}"

    def _afe_format_atten_value(self, value) -> str:
        if value is None:
            return "—"
        try:
            return f"{int(value)} dB"
        except (TypeError, ValueError):
            return "—"

    def _afe_extract_confirmed_attenuation(self, data: dict, device: str):
        atten_map = data.get("attenuation_db")
        if isinstance(atten_map, dict) and device in atten_map:
            value = atten_map.get(device)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        regs_named = data.get("registers_named", {})
        if isinstance(regs_named, dict):
            dev_regs = regs_named.get(device, {})
            if isinstance(dev_regs, dict) and "ATTENUATION_DB" in dev_regs:
                value = dev_regs.get("ATTENUATION_DB")
                if isinstance(value, dict):
                    value = value.get("value")
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def _afe_update_atten_ui_state(self, device: str, confirmed=None):
        pending = self._afe_atten_pending.get(device)
        confirmed_key = f"afe_{device}_atten_confirmed"
        state_key = f"afe_{device}_atten_state"
        if confirmed_key in self._vars:
            self._vars[confirmed_key].set(self._afe_format_atten_value(confirmed))

        if pending is None:
            state_text = ""
        elif pending.get("failed"):
            detail = pending.get("error") or "failed"
            state_text = f"failed: {detail}"
        elif confirmed is not None and confirmed == pending.get("requested"):
            state_text = ""
        else:
            requested = pending.get("requested")
            state_text = f"pending -> {requested} dB"

        if state_key in self._vars:
            self._vars[state_key].set(state_text)

    def _afe_clamp_attenuation_value(self, raw_value, lo: int, hi: int, fallback=None) -> int:
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError):
            if fallback is not None:
                value = int(fallback)
            else:
                value = lo
        return max(lo, min(hi, value))

    def _afe_submit_attenuation(self, device: str):
        bounds = getattr(self, "_afe_atten_range", [0, 31])
        lo, hi = int(bounds[0]), int(bounds[1])
        requested_key = f"afe_{device}_atten_requested"
        confirmed = self._afe_extract_confirmed_attenuation(
            self.mep.afe.get_register_status(),
            device,
        )
        value = self._afe_clamp_attenuation_value(
            self._vars[requested_key].get() if requested_key in self._vars else None,
            lo,
            hi,
            fallback=confirmed,
        )
        if requested_key in self._vars:
            self._vars[requested_key].set(str(value))

        session_id = self._afe_next_session_id(f"atten-{device}")
        self._afe_atten_pending[device] = {
            "session_id": session_id,
            "requested": value,
            "failed": False,
            "error": None,
        }
        self.mep.afe.set_attenuation(device, value, session_id=session_id)
        self._afe_update_atten_ui_state(device, confirmed=confirmed)
        self.root.after(150, self._afe_refresh)



    def _tlm_gps_update(self, data: dict):
        lat = data.get("lat", data.get("latitude", "—"))
        lon = data.get("lon", data.get("longitude", "—"))
        if lat is None:
            lat = "—"
        if lon is None:
            lon = "—"
        self._set_var("tlm_gps_time", data.get("utc_time", data.get("timestamp", "—")))
        self._set_var("tlm_gps_fix", data.get("fix_valid", data.get("fix", "—")))
        self._set_var("tlm_gps_latlon", f"({lat}, {lon})")
        self._set_var("tlm_gps_speed", data.get("speed_knots", "—"))
        for key, item in getattr(self, "_tlm_gps_field_items", {}).items():
            value = data.get(key)
            self._tlm_gps_fields.item(item, values=("—" if value is None else value,
                                                    self._tlm_gps_field_units.get(key, "")))

    def _tlm_populate_gps_fields(self, announce: dict):
        if not hasattr(self, "_tlm_gps_fields"):
            return
        fields = announce.get("describe", {}).get("gps", {}).get("reference", {}).get("data_fields", [])
        if not isinstance(fields, list):
            return
        definitions = [
            {"key": "timestamp", "label": "GNSS Timestamp", "unit": "UTC epoch"},
            *[field for field in fields if isinstance(field, dict) and field.get("key")],
            {"key": "service_timestamp", "label": "Service Timestamp", "unit": "UTC epoch"},
        ]
        self._tlm_gps_fields.delete(*self._tlm_gps_fields.get_children())
        self._tlm_gps_field_items = {}
        self._tlm_gps_field_units = {}
        for definition in definitions:
            key = str(definition["key"])
            item = self._tlm_gps_fields.insert("", "end", text=str(definition.get("label", key)),
                                               values=("—", definition.get("unit", "")))
            self._tlm_gps_field_items[key] = item
            self._tlm_gps_field_units[key] = str(definition.get("unit", ""))
        self._tlm_gps_update(self._tlm_latest_gps)

    def _tlm_gpsd_update(self, data: dict):
        fields = {
            "tlm_gpsd_connected": data.get("connected"),
            "tlm_gpsd_endpoint": f"{data.get('host', '—')}:{data.get('port', '—')}",
            "tlm_gpsd_device": data.get("reported_device") or data.get("configured_device"),
            "tlm_gpsd_driver": data.get("driver"),
            "tlm_gpsd_version": data.get("version"),
            "tlm_gpsd_baud": data.get("baud"),
            "tlm_gpsd_cycle": data.get("cycle_s"),
            "tlm_gpsd_last_rx": data.get("last_receive_timestamp"),
            "tlm_gpsd_error": data.get("last_error"),
        }
        for key, value in fields.items():
            self._set_var(key, value)
        enabled = bool(data.get("raw_stream_enabled"))
        mode = data.get("raw_stream_mode") or "—"
        expires = data.get("raw_stream_expires_timestamp")
        if enabled and isinstance(expires, (int, float)):
            expires_text = datetime.datetime.fromtimestamp(expires).strftime("%H:%M:%S")
            lease = f"live ({mode}) until {expires_text}"
        else:
            lease = "stopped"
        self._set_var("tlm_raw_state", lease)

    def _tlm_raw_append(self, data: dict):
        if not hasattr(self, "_tlm_raw_text") or not isinstance(data, dict):
            return
        line = data.get("line")
        if not isinstance(line, str):
            return
        timestamp = data.get("timestamp")
        if isinstance(timestamp, (int, float)):
            prefix = datetime.datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3]
        else:
            prefix = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._tlm_raw_text.insert("end", f"{prefix}  {line}\n")
        self._tlm_raw_text.see("end")
        lines = int(self._tlm_raw_text.index("end-1c").split(".")[0])
        if lines > 800:
            self._tlm_raw_text.delete("1.0", f"{lines - 800}.0")

    def _tlm_start_raw_stream(self):
        self.mep.afe.start_raw_stream(
            duration_s=float(self._vars["tlm_raw_duration"].get()),
            mode=self._vars["tlm_raw_mode"].get().lower(),
        )

    def _tlm_stop_raw_stream(self):
        self.mep.afe.stop_raw_stream()

    def _tlm_imu_update(self, data: dict):
        acc_x = data.get("acc_x", "—")
        acc_y = data.get("acc_y", "—")
        acc_z = data.get("acc_z", "—")
        gyr_x = data.get("gyr_x", "—")
        gyr_y = data.get("gyr_y", "—")
        gyr_z = data.get("gyr_z", "—")
        self._set_var("tlm_acc_x", acc_x)
        self._set_var("tlm_acc_y", acc_y)
        self._set_var("tlm_acc_z", acc_z)
        self._set_var("tlm_gyr_x", gyr_x)
        self._set_var("tlm_gyr_y", gyr_y)
        self._set_var("tlm_gyr_z", gyr_z)

    def _tlm_mag_update(self, data: dict):
        mag_x = data.get("mag_x", "—")
        mag_y = data.get("mag_y", "—")
        mag_z = data.get("mag_z", "—")
        self._set_var("tlm_mag_x", mag_x)
        self._set_var("tlm_mag_y", mag_y)
        self._set_var("tlm_mag_z", mag_z)

    def _tlm_hk_update(self, data: dict):
        keys = (
            "ocxo_locked", "spi_ok", "mag_ok", "imu_ok", "sw_temp_c", "mag_temp_c",
            "imu_temp_c", "imu_active", "imu_tilt",
        )
        for key in keys:
            self._set_var(f"tlm_hk_{key}", data.get(key, "—"))

    # ------------------------------------------------------------------ #
    #  UI Construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=0)
        self.root.rowconfigure(0, weight=1)
        self.root.minsize(LEFT_PANEL_WIDTH, 600)

        # ---- Left pane (fixed width, full height) ---- #
        left = ttk.Frame(self.root, width=LEFT_PANEL_WIDTH, height=DEFAULT_WIN_HEIGHT)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)

        # ---- Top-level notebook: RX / TX (independent operational paths) ---- #
        self._top_notebook = ttk.Notebook(left)
        self._top_notebook.grid(row=0, column=0, padx=10, pady=(4, 6), sticky="ew")

        # Advanced toggle overlaid at the notebook's top-right corner — same
        # visual row as the RX/TX tab strip, no extra row or column.
        ttk.Style().configure("Small.TButton", font=("TkDefaultFont", 8))
        self._adv_btn_text = tk.StringVar(value="Show Advanced Options \u25b6")
        adv_btn = ttk.Button(left, textvariable=self._adv_btn_text,
                              command=self._toggle_advanced, style="Small.TButton")
        adv_btn.place(in_=self._top_notebook, relx=1.0, x=-2, y=-2, anchor="ne")

        rx_tab = ttk.Frame(self._top_notebook, padding=4)
        self._top_notebook.add(rx_tab, text="RX")
        rx_tab.columnconfigure(0, weight=1)

        self._build_tune_section(rx_tab, row=0)
        self._build_record_section(rx_tab, row=1)
        self._build_updown_section(rx_tab, row=2)
        self._build_rx_control_section(rx_tab, row=3)

        tx_tab = ttk.Frame(self._top_notebook, padding=4)
        self._top_notebook.add(tx_tab, text="TX")
        tx_tab.columnconfigure(0, weight=1)

        # Same flow as RX: Tune, then Up/Down Convert (shared — binds to the
        # same StringVars as the RX instance above), then Control.
        self._build_tx_tune_section(tx_tab, row=0)
        self._build_updown_section(tx_tab, row=1)
        self._build_tx_control_section(tx_tab, row=2)

        # A ttk.Notebook sizes itself to whichever tab currently has the most
        # content, which was pushing RX's tab (and the log box below it) around
        # as TX content changed. Pin both tabs to RX's natural height instead.
        rx_tab.update_idletasks()
        fixed_tab_height = rx_tab.winfo_reqheight()
        for tab in (rx_tab, tx_tab):
            tab.configure(height=fixed_tab_height)
            tab.grid_propagate(False)

        # Synth LO preview depends on which tab is active (RX Start vs TX Center
        # Freq) — switching tabs changes that without changing any variable.
        self._top_notebook.bind("<<NotebookTabChanged>>", lambda _e: self._update_synth_lo())

        # ---- Status bar ---- #
        status_frame = ttk.LabelFrame(left, text="Status")
        status_frame.grid(row=1, column=0, padx=10, pady=4, sticky="ew")
        status_frame.columnconfigure(0, weight=1, uniform="status")
        status_frame.columnconfigure(1, weight=1, uniform="status")
        status_frame.columnconfigure(2, weight=1, uniform="status")
        self._status_var = tk.StringVar(value="Idle")
        self._status_cells = {}
        self._status_tooltip = None
        self._status_tooltip_label = None
        self._build_status_grid(status_frame)

        # ---- Log box ---- #
        log_frame = ttk.LabelFrame(left, text="Log")
        log_frame.grid(row=2, column=0, padx=10, pady=6, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        log_ctl = ttk.Frame(log_frame)
        log_ctl.grid(row=0, column=0, sticky="ew", padx=5, pady=(4, 0))
        ttk.Label(log_ctl, text="Level").pack(side="left")
        self._vars["log_level"] = tk.StringVar(value="INFO")
        log_level_combo = ttk.Combobox(
            log_ctl, textvariable=self._vars["log_level"], width=10, state="readonly",
            values=("DEBUG", "INFO", "WARNING", "ERROR"),
        )
        log_level_combo.pack(side="left", padx=(4, 0))
        log_level_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_log_level())

        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=15, width=80, state="disabled",
            font=("Courier", 9),
        )
        self._log_text.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")
        self._bind_copy_menu(self._log_text)

        # ---- Right pane: Advanced Options ---- #
        self._build_advanced_section(self.root)

    # ---- Section builders ---- #

    def _build_status_grid(self, parent: ttk.Frame):
        specs = [
            ("mqtt", "MQTT", 0, 0),
            ("rfsoc", "RFSOC", 0, 1),
            ("afe", "AFE", 0, 2),
            ("tuner", "Tuner", 1, 0),
            ("recorder", "Recorder", 1, 1),
        ]

        for key, label, row, col in specs:
            cell = ttk.Frame(parent)
            cell.grid(row=row, column=col, padx=2, pady=2, sticky="ew")
            cell.columnconfigure(1, weight=1)

            led = tk.Canvas(cell, width=12, height=12, highlightthickness=0, bd=0)
            led.grid(row=0, column=0, padx=(0, 3), sticky="w")
            oval = led.create_oval(2, 2, 10, 10, fill="#777777", outline="#555555")

            text_var = tk.StringVar(value="—")
            ttk.Label(cell, textvariable=text_var, anchor="w", font=("TkDefaultFont", 8)).grid(
                row=0, column=1, sticky="ew"
            )

            cell.bind("<Enter>", lambda e, k=key: self._status_tooltip_show(k, e))
            cell.bind("<Leave>", lambda e: self._status_tooltip_hide())
            led.bind("<Enter>", lambda e, k=key: self._status_tooltip_show(k, e))
            led.bind("<Leave>", lambda e: self._status_tooltip_hide())

            self._status_cells[key] = {
                "label": label,
                "canvas": led,
                "oval": oval,
                "text_var": text_var,
                "detail": "",
            }

        # Keep 2x3 geometry; leave final slot intentionally empty.
        spacer = ttk.Frame(parent)
        spacer.grid(row=1, column=2, padx=2, pady=2, sticky="ew")

        self._set_status_cell("mqtt", "gray", "unknown")
        self._set_status_cell("rfsoc", "gray", "unknown")
        self._set_status_cell("afe", "gray", "unknown")
        self._set_status_cell("tuner", "gray", "unknown")
        self._set_status_cell("recorder", "gray", "unknown")

    def _status_led_color(self, level: str) -> str:
        return {
            "green": "#26a269",
            "yellow": "#e5a50a",
            "red": "#c01c28",
            "gray": "#777777",
        }.get(level, "#777777")

    def _set_status_cell(self, key: str, level: str, text: str, detail: str = None):
        cell = self._status_cells.get(key)
        if not cell:
            return
        color = self._status_led_color(level)
        cell["canvas"].itemconfigure(cell["oval"], fill=color)
        text = self._compact_status_text(text)
        cell["text_var"].set(f"{cell['label']}: {text}")
        cell["detail"] = str(detail if detail is not None else text)

    def _status_tooltip_show(self, key: str, event):
        cell = self._status_cells.get(key)
        if not cell:
            return
        detail = (cell.get("detail") or "").strip()
        if not detail:
            return
        if self._status_tooltip is None:
            self._status_tooltip = tk.Toplevel(self.root)
            self._status_tooltip.wm_overrideredirect(True)
            self._status_tooltip.attributes("-topmost", True)
            self._status_tooltip_label = ttk.Label(
                self._status_tooltip,
                text="",
                justify="left",
                relief="solid",
                borderwidth=1,
                padding=(4, 2),
                background="#ffffe0",
            )
            self._status_tooltip_label.pack()
        self._status_tooltip_label.configure(text=detail)
        x = event.x_root + 10
        y = event.y_root + 10
        self._status_tooltip.geometry(f"+{x}+{y}")
        self._status_tooltip.deiconify()

    def _status_tooltip_hide(self):
        if self._status_tooltip is not None:
            self._status_tooltip.withdraw()

    def _compact_status_text(self, text: str, max_len: int = 18) -> str:
        s = str(text or "—").strip().replace("\n", " ")
        if len(s) <= max_len:
            return s
        return s[: max_len - 1] + "…"

    def _safe_float(self, value, default=None):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _refresh_status_grid(self):
        conn = self.mep.get_connection_status()
        mqtt_ok = bool(conn.get("connected"))
        if mqtt_ok:
            self._set_status_cell(
                "mqtt",
                "green",
                "connected",
                detail=f"Connected to {conn.get('host')}:{conn.get('port')}",
            )
        else:
            err = conn.get("last_error") or "offline"
            self._set_status_cell(
                "mqtt",
                "red",
                "offline",
                detail=f"Disconnected from {conn.get('host')}:{conn.get('port')} ({err})",
            )

        tlm = self.mep.rfsoc.get_status()
        if tlm:
            state = str(tlm.get("state", "?")).lower()
            f_if_hz = self._safe_float(tlm.get("f_if_hz"), 0.0)
            f_if_mhz = f_if_hz / 1e6
            f_if_mhz_rounded = round(f_if_mhz)
            if state in {"error", "offline", "failed"}:
                level = "red"
            elif state in {"ready", "active", "inactive", "idle", "online"}:
                level = "green"
            else:
                level = "yellow"
            if state not in {"ready", "active", "inactive", "idle", "online"}:
                activity = state
            else:
                rx_active = str(tlm.get("state_RX", "")).lower() == "active"
                tx_active = str(tlm.get("state_TX", "")).lower() == "active"
                if rx_active and tx_active:
                    activity = "RX+TX active"
                elif rx_active:
                    activity = "RX active"
                elif tx_active:
                    activity = "TX active"
                else:
                    activity = "ready"
            pps = tlm.get("pps_count", "?")
            self._set_status_cell(
                "rfsoc",
                level,
                activity,
                detail=f"state={state}, f_if_hz={f_if_hz}, pps={pps}",
            )
        else:
            level = "yellow" if mqtt_ok else "red"
            self._set_status_cell("rfsoc", level, "unknown", detail="No RFSoC telemetry in cache")

        afe_status = self.mep.afe.get_status()
        afe_regs = self.mep.afe.get_register_status()
        afe_any = afe_status if isinstance(afe_status, dict) else afe_regs
        if isinstance(afe_any, dict):
            afe_state = str(afe_any.get("state", "online")).lower()
            level = "red" if afe_state in {"error", "offline", "disconnected"} else "green"
            self._set_status_cell("afe", level, afe_state, detail=f"AFE status: {afe_state}")
        else:
            level = "yellow" if mqtt_ok else "red"
            self._set_status_cell("afe", level, "no data", detail="No AFE status/register messages in cache")

        tuner_status = self.mep.tuner.get_status()
        if tuner_status:
            tuner_name = tuner_status.get("name") or tuner_status.get("backend") or "—"
            lo_val = self._safe_float(tuner_status.get("frequency_mhz"))
            lo_txt = f"LO={lo_val:.1f}" if lo_val is not None else "LO=—"
            t_state = str(tuner_status.get("state", "unknown")).lower()
            level = "red" if t_state in {"error", "offline", "disconnected"} else "green"
            self._set_status_cell(
                "tuner",
                level,
                tuner_name,
                detail=f"state={t_state}, tuner={tuner_name}, {lo_txt} MHz",
            )
        else:
            level = "yellow" if mqtt_ok else "red"
            self._set_status_cell("tuner", level, "no data", detail="No tuner status in cache")

        rec_status = self.mep.recorder.get_status()
        if isinstance(rec_status, dict):
            rec_state = str(rec_status.get("state", "unknown")).lower()
            if rec_state in {"error", "offline", "failed"}:
                level = "red"
            elif rec_state in {"starting", "configuring", "unknown"}:
                level = "yellow"
            else:
                level = "green"
            rec_output_path = rec_status.get("output_path", "?")
            rec_timestamp = rec_status.get("timestamp")
            if isinstance(rec_timestamp, (int, float)):
                rec_timestamp_txt = datetime.datetime.fromtimestamp(rec_timestamp).isoformat(sep=" ", timespec="seconds")
            else:
                rec_timestamp_txt = str(rec_timestamp) if rec_timestamp is not None else "?"
            self._set_status_cell(
                "recorder",
                level,
                rec_state,
                detail=f"state={rec_state}, output_path={rec_output_path}, timestamp={rec_timestamp_txt}",
            )
        else:
            sweep_active = self._sweep_thread and self._sweep_thread.is_alive()
            if sweep_active:
                self._set_status_cell("recorder", "yellow", "starting", detail="Sweep active, waiting for recorder status")
            else:
                level = "yellow" if mqtt_ok else "red"
                self._set_status_cell("recorder", level, "no data", detail="No recorder status in cache")

    def _build_tune_section(self, parent: ttk.Frame, row: int):
        frame = ttk.LabelFrame(parent, text="Tune")
        frame.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        frame.columnconfigure(1, weight=1)

        rx_f = frame

        self._vars["freq_start"] = tk.StringVar(value="7000")
        if "dwell" not in self._vars:
            self._vars["dwell"] = tk.StringVar(value="5")

        # RX enable states:
        #
        #   End   Step  Dwell   Operation
        #   ----- ----- -----   -------------------------------
        #   off   off   off     Single capture; run until Stop
        #   off   off   on      Single capture for the dwell time
        #   on    on    on      Sweep from Start through End by Step
        #
        # End and Step are separate checkboxes but form one sweep pair.
        # Enabling either enables both and Dwell. Disabling either clears
        # both sweep checkboxes and Dwell. Dwell may be enabled by itself.
        # Disabling Dwell also clears the sweep pair when it is active.
        self._vars["end_enabled"] = tk.BooleanVar(value=False)
        self._vars["step_enabled"] = tk.BooleanVar(value=False)
        self._vars["dwell_enabled"] = tk.BooleanVar(value=False)

        rx_fields = [
            ("Start (MHz)", "freq_start", "7000"),
            ("End (MHz)",   "freq_end",   "8000"),
            ("Step (MHz)",  "step",       "10"),
        ]
        for row_index, (label, key, default) in enumerate(rx_fields):
            if key not in self._vars:
                self._vars[key] = tk.StringVar(value=default)
            ttk.Label(rx_f, text=label).grid(
                row=row_index, column=0, sticky="w", padx=5, pady=4)
            entry = ttk.Entry(rx_f, textvariable=self._vars[key], width=20)
            entry.grid(row=row_index, column=1, sticky="ew", padx=5, pady=4)
            if key != "freq_start":
                entry.configure(state="disabled")
                entry_name = "end" if key == "freq_end" else key
                setattr(self, f"_rx_{entry_name}_entry", entry)
        ttk.Checkbutton(
            rx_f,
            text="Enable",
            variable=self._vars["end_enabled"],
            command=self._toggle_rx_end,
        ).grid(row=1, column=2, sticky="w", padx=5, pady=4)
        ttk.Checkbutton(
            rx_f,
            text="Enable",
            variable=self._vars["step_enabled"],
            command=self._toggle_rx_step,
        ).grid(row=2, column=2, sticky="w", padx=5, pady=4)

        ttk.Label(rx_f, text="Dwell (s)").grid(
            row=3, column=0, sticky="w", padx=5, pady=4)
        dwell_entry = ttk.Entry(
            rx_f,
            textvariable=self._vars["dwell"],
            width=20,
            state="disabled",
        )
        dwell_entry.grid(row=3, column=1, sticky="ew", padx=5, pady=4)
        self._rx_dwell_entry = dwell_entry
        ttk.Checkbutton(
            rx_f,
            text="Enable",
            variable=self._vars["dwell_enabled"],
            command=self._toggle_rx_dwell,
        ).grid(row=3, column=2, sticky="w", padx=5, pady=4)

    def _build_placeholder_entry(self, parent, var: tk.StringVar, placeholder: str, width: int = 20):
        """A tk.Entry showing grayed-out hint text when empty and unfocused.

        The hint is never written into ``var`` — it's a display-only overlay on
        top of a plain Entry (not bound via textvariable), synced to ``var``
        manually so callers reading ``var.get()`` always see "" when blank.
        """
        normal_fg = "black"
        placeholder_fg = "grey"
        entry = tk.Entry(parent, width=width)
        entry._showing_placeholder = False

        def _show_placeholder():
            entry.delete(0, "end")
            entry.insert(0, placeholder)
            entry.configure(foreground=placeholder_fg)
            entry._showing_placeholder = True

        def _clear_placeholder():
            if entry._showing_placeholder:
                entry.delete(0, "end")
                entry.configure(foreground=normal_fg)
                entry._showing_placeholder = False

        def _on_focus_in(_e):
            _clear_placeholder()

        def _on_focus_out(_e):
            var.set("" if entry._showing_placeholder else entry.get())
            if not entry.get():
                _show_placeholder()

        def _on_keyrelease(_e):
            if not entry._showing_placeholder:
                var.set(entry.get())

        entry.bind("<FocusIn>", _on_focus_in)
        entry.bind("<FocusOut>", _on_focus_out)
        entry.bind("<KeyRelease>", _on_keyrelease)

        if var.get():
            entry.insert(0, var.get())
        else:
            _show_placeholder()

        return entry

    def _build_record_section(self, parent: ttk.Frame, row: int):
        frame = ttk.LabelFrame(parent, text="Record")
        frame.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(frame, text="Capture Name").grid(
            row=0, column=0, sticky="w", padx=5, pady=4)
        self._vars["capture_name"] = tk.StringVar(value="")
        self._build_placeholder_entry(
            frame, self._vars["capture_name"], "leave blank for preview", width=20,
        ).grid(row=0, column=1, columnspan=3, sticky="ew", padx=5, pady=4)

        ttk.Label(frame, text="Channel").grid(
            row=1, column=0, sticky="w", padx=5, pady=4)
        self._vars["channel"] = tk.StringVar(value="A")
        ttk.Combobox(
            frame, textvariable=self._vars["channel"],
            values=CHANNEL_OPTIONS, width=16, state="readonly",
        ).grid(row=1, column=1, sticky="ew", padx=5, pady=4)

        ttk.Label(frame, text="Sample Rate (MHz)").grid(
            row=1, column=2, sticky="w", padx=5, pady=4)
        self._vars["sample_rate_mhz"] = tk.StringVar(value="10")
        self._sample_rate_combo = ttk.Combobox(
            frame, textvariable=self._vars["sample_rate_mhz"],
            values=(), width=16, state="readonly",
        )
        self._sample_rate_combo.grid(row=1, column=3, sticky="ew", padx=5, pady=4)

    def _build_updown_section(self, parent: ttk.Frame, row: int):
        """Build one instance of the Up/Down Convert panel.

        Called once per tab (RX, TX) — both instances bind to the same shared
        StringVars, since there is only one physical tuner/oscillator. Widgets
        that need their enabled state toggled are tracked in lists so every
        instance stays in sync via _on_tuner_change.
        """
        first_build = "tuner_selection" not in self._vars

        frame = ttk.LabelFrame(parent, text="Up/Down Convert")
        frame.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        if first_build:
            self._vars["tuner_selection"] = tk.StringVar(value="Disabled")
            self._vars["adc_if_mhz"] = tk.StringVar(value="1090")
            self._vars["injection_mode"] = tk.StringVar(value="High")
            self._vars["synth_lo"] = tk.StringVar(value="—")
            self._tuner_combos = []
            self._if_entries = []
            self._injection_combos = []

        ttk.Label(frame, text="External Tuner").grid(
            row=0, column=0, sticky="w", padx=5, pady=4)
        tuner_values = ["Disabled"]
        advertised_tuner = getattr(self, "_advertised_tuner", None)
        if advertised_tuner:
            tuner_values.append(advertised_tuner)
        tuner_combo = ttk.Combobox(
            frame, textvariable=self._vars["tuner_selection"],
            values=tuner_values, width=16, state="readonly",
        )
        tuner_combo.grid(row=0, column=1, sticky="ew", padx=5, pady=4)
        self._tuner_combos.append(tuner_combo)

        ttk.Label(frame, text="Injection Mode").grid(
            row=0, column=2, sticky="w", padx=5, pady=4)
        injection_combo = ttk.Combobox(
            frame, textvariable=self._vars["injection_mode"],
            values=["High", "Low"], width=16, state="readonly",
        )
        injection_combo.grid(row=0, column=3, sticky="ew", padx=5, pady=4)
        self._injection_combos.append(injection_combo)

        ttk.Label(frame, text="RFSoC IF (MHz)").grid(
            row=1, column=0, sticky="w", padx=5, pady=4)
        if_entry = ttk.Entry(
            frame, textvariable=self._vars["adc_if_mhz"], width=16, state="disabled")
        if_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=4)
        self._if_entries.append(if_entry)

        ttk.Label(frame, text="Synth LO (MHz)").grid(
            row=1, column=2, sticky="w", padx=5, pady=4)
        ttk.Entry(
            frame, textvariable=self._vars["synth_lo"], width=16, state="disabled",
        ).grid(row=1, column=3, sticky="ew", padx=5, pady=4)

        if first_build:
            self._vars["tuner_selection"].trace_add("write", self._on_tuner_change)
            self._vars["freq_start"].trace_add("write", self._update_synth_lo)
            self._vars["adc_if_mhz"].trace_add("write", self._update_synth_lo)
            self._vars["injection_mode"].trace_add("write", self._update_synth_lo)
            self._on_tuner_change()
            self._update_synth_lo()

    def _build_rx_control_section(self, parent: ttk.Frame, row: int):
        frame = ttk.LabelFrame(parent, text="Receive Control")
        frame.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        ttk.Button(frame, text="Start/Update", width=14,
                   command=self._start).grid(row=0, column=0, padx=4, pady=3, sticky="ew")
        ttk.Button(frame, text="Stop", width=14,
                   command=self._stop_all).grid(row=0, column=1, padx=4, pady=3, sticky="ew")

    def _build_advanced_section(self, parent: ttk.Frame):
        frame = ttk.LabelFrame(parent, text="Advanced Options", width=ADV_PANEL_WIDTH)
        frame.grid(row=0, column=1, padx=(0, 10), pady=6, sticky="nsew")
        frame.grid_propagate(False)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._adv_frame = frame
        frame.grid_remove()

        nb = ttk.Notebook(frame)
        nb.grid(row=0, column=0, padx=5, pady=5, sticky="nsew")
        self._adv_nb = nb

        # Lazy tab loading: create empty frames, build contents on first view.
        # This avoids ~400 X11 round-trips at startup over SSH.
        self._tab_builders = {
            "SPEC": self._build_spec_tab,
            "AFE":  self._build_afe_tab,
            "TUN":  self._build_tun_tab,
            "SOC":  self._build_soc_tab,
            "REC":  self._build_rec_tab,
            "DOC":  self._build_docker_tab,
            "SVC":  self._build_service_tab,
            "BUS":  self._build_mqtt_tab,
            "JET":  self._build_jetson_health_tab,
            "TLM":  self._build_tlm_tab,
            "CAP":  self._build_cap_tab,
        }
        self._tab_frames = {}
        self._tabs_built: set[str] = set()

        for name in self._tab_builders:
            f = ttk.Frame(nb, padding=8)
            nb.add(f, text=name)
            self._tab_frames[name] = f

        nb.bind("<<NotebookTabChanged>>", self._on_adv_tab_changed)

        # AFE tab needs self._afe_frame set early so _afe_populate_from_announce
        # can build dynamic widgets before the tab is ever viewed.
        self._afe_frame = self._tab_frames["AFE"]
        self._afe_frame.columnconfigure(0, weight=1)

    def _ensure_tab_built(self, tab_text: str):
        """Build tab contents on first access (lazy loading)."""
        if tab_text in self._tabs_built:
            return
        builder = self._tab_builders.get(tab_text)
        if builder is None:
            return
        frame = self._tab_frames[tab_text]
        builder(frame)
        self._tabs_built.add(tab_text)
        # afecontrol/announce is retained and already fired before this tab existed, so replay
        # it now to populate widgets the builder just created (epoch, time source, ...).
        cached = self.mep.afe.get_announce()
        if isinstance(cached, dict):
            try:
                self._apply_afe_announce(cached)
            except Exception:
                logging.exception("Announce replay failed for tab %s", tab_text)

    def _get_current_tab_text(self) -> str:
        """Return the text label of the currently selected advanced tab."""
        idx = self._adv_nb.index("current")
        return self._adv_nb.tab(idx, "text")

    def _is_adv_tab_selected(self, tab_text: str) -> bool:
        """Return True only when Advanced is visible and the named tab is active."""
        if not hasattr(self, "_adv_frame") or not hasattr(self, "_adv_nb"):
            return False
        if self._adv_nb is None:
            return False
        if not self._adv_frame.winfo_viewable():
            return False
        try:
            return self._get_current_tab_text() == tab_text
        except Exception:
            return False

    def _on_adv_tab_changed(self, event=None):
        try:
            tab_text = self._get_current_tab_text()
        except Exception:
            return
        tab_was_built = tab_text in self._tabs_built
        self._ensure_tab_built(tab_text)
        if tab_text == "REC" and tab_was_built:
            self._rec_request_presets()
        # Keep SPEC subscription active only when the tab is actually showing
        new_spec_visible = (tab_text == "SPEC")
        if new_spec_visible != self._spec_tab_visible:
            self._spec_tab_visible = new_spec_visible
            self._spec_update_stream_state()
        if tab_text == "BUS":
            self._mqtt_render_from_buffer()
        elif tab_text == "SVC":
            self._service_flush_buffer_to_widget()
        elif tab_text == "JET":
            self._host_manager_render()

    def _cap_apply_service_status(self, data: dict):
        if not isinstance(data, dict):
            return
        if hasattr(self, "_cap_tree"):
            self._render_cap_tab()

    @staticmethod
    def _vertical_scroll_tab(frame: ttk.Frame) -> ttk.Frame:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        canvas = tk.Canvas(frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(canvas_window, width=max(event.width - 8, 1)),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        content.columnconfigure(0, weight=1)
        return content

    def _apply_orchestrator_status(self, data: dict):
        if not isinstance(data, dict):
            return
        if not self._rec_presets_loaded:
            self._rec_request_presets()
        rx = data.get("rx") if isinstance(data.get("rx"), dict) else {}
        tx = data.get("tx") if isinstance(data.get("tx"), dict) else {}
        apply_conjugate = rx.get("apply_conjugate")
        if apply_conjugate is not None:
            self._vars["conjugate_actual"].set(str(bool(apply_conjugate)))
            policy = rx.get("conjugate_policy")
            current_policy = self._vars["conjugate_policy"].get()
            if policy in CONJUGATE_POLICY_OPTIONS and (
                not self._conjugate_policy_user_override or policy == current_policy
            ):
                self._vars["conjugate_policy"].set(policy)
                self._conjugate_policy_user_override = False
        rx_state = str(rx.get("state") or "unknown")
        if rx_state == "failed" and rx.get("error"):
            self._status_var.set(f"RX failed: {rx['error']}")
        elif rx_state == "running":
            operation = rx.get("operation") or "capture"
            self._status_var.set(f"RX {operation} running")
        elif rx_state == "starting":
            self._status_var.set("RX starting...")
        else:
            self._status_var.set("Idle")

        if "tx_st_transmitting" in self._vars:
            tx_state = str(tx.get("state") or "unknown")
            if tx_state == "running":
                self._vars["tx_st_transmitting"].set("Transmitting")
            elif tx_state == "starting":
                self._vars["tx_st_transmitting"].set("Starting")
            elif tx_state == "failed":
                self._vars["tx_st_transmitting"].set(f"Failed: {tx.get('error') or 'unknown error'}")
            else:
                self._vars["tx_st_transmitting"].set("Not transmitting")
        self._cap_apply_service_status(data)

    def _service_apply_status(self, data: dict):
        manager = getattr(self, "service_manager", None)
        if manager is None:
            return
        if isinstance(data, dict):
            manager._on_status(data)
        if "service_services_summary" not in self._vars:
            return

        running = sum(
            1 for service in manager.service_names
            if str(manager.services.get(service, {}).get("active_state", "")).lower() == "active"
        )
        self._vars["service_manager_status"].set(
            str(manager.get_status().get("state") or "unavailable")
        )
        self._vars["service_services_summary"].set(f"{running}/{len(manager.service_names)}")
        self._vars["service_last_refresh"].set(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._service_render_service_list()

        error = data.get("error") if isinstance(data, dict) else None
        if error:
            logging.warning("SVC: %s", error)

    def _build_cap_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.rowconfigure(3, weight=2)

        captures_frame = ttk.LabelFrame(frame, text="Captures")
        captures_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=(4, 4))
        captures_frame.columnconfigure(0, weight=1)
        captures_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            captures_frame,
            columns=("name", "bytes", "files", "upload"),
            show="headings",
            height=10,
        )
        tree.grid(row=0, column=0, sticky="nsew")
        capture_scrollbar = ttk.Scrollbar(captures_frame, orient="vertical", command=tree.yview)
        capture_scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=capture_scrollbar.set)
        tree.heading("name", text="Capture")
        tree.heading("bytes", text="Size")
        tree.heading("files", text="Files")
        tree.heading("upload", text="SDS")
        tree.column("name", width=240, minwidth=120, anchor="w", stretch=True)
        tree.column("bytes", width=75, anchor="e")
        tree.column("files", width=52, anchor="e")
        tree.column("upload", width=105, anchor="w")
        self._cap_tree = tree
        self._cap_capture_by_iid = {}
        self._cap_activity_by_capture = {}
        self._cap_details_capture_name = None
        tree.bind("<<TreeviewSelect>>", self._cap_selection_changed)

        detail = ttk.LabelFrame(frame, text="Selected Capture")
        detail.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        detail.columnconfigure(1, weight=1)
        detail.columnconfigure(3, weight=1)
        self._cap_rename_var = tk.StringVar()
        self._cap_size_var = tk.StringVar(value="-")
        self._cap_files_var = tk.StringVar(value="-")
        self._cap_modified_var = tk.StringVar(value="-")
        self._cap_recording_var = tk.StringVar(value="No")
        ttk.Label(detail, text="Name").grid(row=0, column=0, sticky="w", padx=(6, 2), pady=(6, 3))
        ttk.Entry(detail, textvariable=self._cap_rename_var).grid(row=0, column=1, columnspan=3, sticky="ew", padx=(2, 6), pady=(6, 3))
        ttk.Label(detail, text="Size").grid(row=1, column=0, sticky="w", padx=(6, 2), pady=2)
        ttk.Label(detail, textvariable=self._cap_size_var).grid(row=1, column=1, sticky="w", padx=2, pady=2)
        ttk.Label(detail, text="Files").grid(row=1, column=2, sticky="w", padx=(12, 2), pady=2)
        ttk.Label(detail, textvariable=self._cap_files_var).grid(row=1, column=3, sticky="w", padx=2, pady=2)
        ttk.Label(detail, text="Modified").grid(row=2, column=0, sticky="w", padx=(6, 2), pady=2)
        ttk.Label(detail, textvariable=self._cap_modified_var).grid(row=2, column=1, sticky="w", padx=2, pady=2)
        ttk.Label(detail, text="Recording").grid(row=2, column=2, sticky="w", padx=(12, 2), pady=2)
        ttk.Label(detail, textvariable=self._cap_recording_var).grid(row=2, column=3, sticky="w", padx=2, pady=2)
        capture_actions = ttk.Frame(detail)
        capture_actions.grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=(3, 6))
        self._cap_rename_btn = ttk.Button(capture_actions, text="Rename", command=self._cap_rename)
        self._cap_rename_btn.grid(row=0, column=0, padx=2)
        self._cap_delete_btn = ttk.Button(capture_actions, text="Delete", command=self._cap_delete)
        self._cap_delete_btn.grid(row=0, column=1, padx=2)

        upload_frame = ttk.LabelFrame(frame, text="SDS Upload")
        upload_frame.grid(row=2, column=0, sticky="ew", padx=4, pady=(0, 4))
        upload_frame.columnconfigure(1, weight=1)
        upload_frame.columnconfigure(3, weight=1)
        self._cap_upload_state_var = tk.StringVar(value="-")
        self._cap_remote_var = tk.StringVar(value="-")
        self._cap_verified_bytes_var = tk.StringVar(value="-")
        self._cap_verified_files_var = tk.StringVar(value="-")

        ttk.Label(upload_frame, text="Remote").grid(row=0, column=0, sticky="w", padx=(6, 2), pady=(5, 2))
        remote_entry = ttk.Entry(upload_frame, textvariable=self._cap_remote_var, state="readonly")
        remote_entry.grid(row=0, column=1, sticky="ew", padx=2, pady=(5, 2))
        self._bind_copy_menu(remote_entry, strvar=self._cap_remote_var, allow_paste=False)
        ttk.Label(upload_frame, text="Verified").grid(row=0, column=2, sticky="w", padx=(12, 2), pady=(5, 2))
        ttk.Label(upload_frame, textvariable=self._cap_verified_bytes_var).grid(row=0, column=3, sticky="w", padx=(2, 6), pady=(5, 2))

        ttk.Label(upload_frame, text="State").grid(row=1, column=0, sticky="w", padx=(6, 2), pady=2)
        ttk.Label(upload_frame, textvariable=self._cap_upload_state_var).grid(row=1, column=1, sticky="w", padx=2, pady=2)
        ttk.Label(upload_frame, text="Files").grid(row=1, column=2, sticky="w", padx=(12, 2), pady=2)
        ttk.Label(upload_frame, textvariable=self._cap_verified_files_var).grid(row=1, column=3, sticky="w", padx=(2, 6), pady=2)

        actions = ttk.Frame(upload_frame)
        actions.grid(row=2, column=0, columnspan=4, sticky="ew", padx=6, pady=(3, 6))
        actions.columnconfigure(1, weight=1)
        self._cap_token_var = tk.StringVar()
        self._cap_dry_run_var = tk.BooleanVar(value=False)
        self._cap_verbose_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text="Dry run", variable=self._cap_dry_run_var).grid(row=0, column=2, sticky="w", padx=4)
        ttk.Checkbutton(actions, text="Verbose", variable=self._cap_verbose_var).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(actions, text="Token").grid(row=0, column=0, sticky="w")
        ttk.Entry(actions, textvariable=self._cap_token_var, show="*").grid(row=0, column=1, sticky="ew", padx=4)
        self._cap_upload_btn = ttk.Button(actions, text="Upload", command=self._cap_start_upload)
        self._cap_upload_btn.grid(row=1, column=2, padx=2, pady=(4, 0))
        self._cap_stop_btn = ttk.Button(actions, text="Stop", command=self._cap_stop_upload)
        self._cap_stop_btn.grid(row=1, column=3, padx=2, pady=(4, 0))
        self._cap_verify_btn = ttk.Button(actions, text="Verify", command=self._cap_verify_upload)
        self._cap_verify_btn.grid(row=1, column=4, padx=2, pady=(4, 0))
        self._cap_action_reason_var = tk.StringVar(value="Select a capture to manage or upload")
        ttk.Label(actions, textvariable=self._cap_action_reason_var, foreground="grey").grid(row=2, column=0, columnspan=5, sticky="w", pady=(5, 0))

        activity_frame = ttk.LabelFrame(frame, text="Details")
        activity_frame.grid(row=3, column=0, sticky="nsew", padx=4, pady=(0, 6))
        activity_frame.columnconfigure(0, weight=1)
        activity_frame.rowconfigure(0, weight=1)
        self._cap_upload_activity_text = scrolledtext.ScrolledText(activity_frame, height=10, wrap="word", state="disabled", font=("TkFixedFont", 8))
        self._cap_upload_activity_text.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self._bind_copy_menu(self._cap_upload_activity_text, allow_paste=False)
        self._cap_set_details("Select a capture to view upload details")

        self._render_cap_tab()

    def _cap_service_response(self, response, show_error=False):
        if not response.get("success"):
            error = response.get("error") or "Unknown service error"
            logging.error("CAP service request failed: %s", error)
            if show_error:
                messagebox.showerror("CAP Action Failed", error)
        self._render_cap_tab()

    def _render_cap_tab(self):
        tree = getattr(self, "_cap_tree", None)
        if tree is None:
            return
        selection = tree.selection()
        selected_capture = self._cap_capture_by_iid.get(selection[0]) if selection else None
        selected_name = str(selected_capture.get("name") or "") if selected_capture else ""
        captures = self.archive_manager.list_captures()
        uploads = self.upload_manager.get_uploads()
        self._cap_uploads_by_capture = {
            str(upload.get("capture_name") or ""): upload
            for upload in uploads
            if isinstance(upload, dict) and upload.get("capture_name")
        }
        orchestrator = self.capture_orchestrator.get_status()
        rx = orchestrator.get("rx") if isinstance(orchestrator.get("rx"), dict) else {}
        rx_active = rx.get("state") in {"starting", "running"}
        tree.delete(*tree.get_children())
        self._cap_capture_by_iid = {}
        selected_iid = None
        for capture in captures:
            name = str(capture.get("name") or "unknown")
            recording = rx_active and name == (rx.get("capture_name") or "preview")
            upload = self._cap_uploads_by_capture.get(name, {})
            values = (
                name,
                self._cap_format_bytes(capture.get("size_bytes"), decimals=0),
                capture.get("file_count") or 0,
                self._cap_format_sds_state(upload),
            )
            iid = tree.insert("", "end", values=values)
            self._cap_capture_by_iid[iid] = capture
            if name == selected_name:
                selected_iid = iid
        children = tree.get_children()
        if selected_iid or children:
            tree.selection_set(selected_iid or children[0])

        self._cap_update_selected_capture()

    def _cap_selection(self):
        selection = self._cap_tree.selection()
        capture = self._cap_capture_by_iid.get(selection[0]) if selection else None
        capture_name = str(capture.get("name") or "") if capture else ""
        upload = self._cap_uploads_by_capture.get(capture_name)
        return capture, upload

    def _cap_selection_changed(self, event=None):
        capture, _upload = self._cap_selection()
        capture_name = str(capture.get("name") or "") if capture else ""
        self._cap_rename_var.set(capture_name)
        if capture_name != self._cap_details_capture_name:
            self._cap_details_capture_name = capture_name or None
            if not capture_name:
                self._cap_set_details("Select a capture to view upload details")
            else:
                activity = self._cap_activity_by_capture.get(capture_name)
                self._cap_set_details(
                    "\n".join(self._cap_format_activity(item) for item in activity)
                    if activity
                    else "Loading upload activity"
                )
                self.upload_manager.get_activity(
                    capture_name,
                    callback=lambda response: self._gui_call(self._cap_activity_response, response),
                )
        self._cap_update_selected_capture()

    def _cap_activity_response(self, response):
        status_data = response.get("status_data") if isinstance(response, dict) else None
        if not response.get("success") or not isinstance(status_data, dict):
            return
        capture_name = str(status_data.get("capture_name") or "")
        records = status_data.get("activity")
        if not capture_name or not isinstance(records, list):
            return
        persisted = [item for item in records if isinstance(item, dict)]
        live = list(self._cap_activity_by_capture.get(capture_name, ()))
        persisted_keys = {
            (item.get("timestamp"), item.get("event"), item.get("message"))
            for item in persisted
        }
        activity = deque(
            persisted + [
                item for item in live
                if (item.get("timestamp"), item.get("event"), item.get("message")) not in persisted_keys
            ],
            maxlen=500,
        )
        self._cap_activity_by_capture[capture_name] = activity
        if capture_name == self._cap_details_capture_name:
            self._cap_set_details(
                "\n".join(self._cap_format_activity(item) for item in activity)
                if activity
                else "No upload activity"
            )

    def _cap_upload_activity_event(self, data):
        if not hasattr(self, "_cap_tree") or not isinstance(data, dict):
            return
        status_data = data.get("status_data")
        if not isinstance(status_data, dict):
            return
        event_type = data.get("event_type")
        if event_type in {"sds_check_started", "sds_check_completed"}:
            capture, _upload = self._cap_selection()
            capture_name = str(status_data.get("capture_name") or "")
            capture_selected = capture and str(capture.get("name") or "") == capture_name
            if capture_selected:
                if event_type == "sds_check_started":
                    self._cap_verified_bytes_var.set("Checking...")
                    self._cap_verified_files_var.set("Checking...")
                    self._cap_verify_btn.configure(state="disabled")
                else:
                    state = str(status_data.get("state") or "unknown").replace("_", " ")
                    self._cap_action_reason_var.set(f"SDS verification: {state}")
            return
        if event_type == "upload_failed":
            activity = {
                "timestamp": data.get("timestamp") or time.time(),
                "level": "error",
                "event": "failed",
                "message": status_data.get("error") or "Upload failed",
            }
        elif event_type == "upload_activity":
            activity = status_data.get("activity")
        else:
            return
        capture_name = str(status_data.get("capture_name") or "")
        if not capture_name or not isinstance(activity, dict):
            return
        capture, _upload = self._cap_selection()
        if not capture or capture_name != str(capture.get("name") or ""):
            self._cap_activity_by_capture.setdefault(capture_name, deque(maxlen=500)).append(activity)
            return
        self._cap_activity_by_capture.setdefault(capture_name, deque(maxlen=500)).append(activity)
        self._cap_append_detail(self._cap_format_activity(activity))

    @staticmethod
    def _cap_format_activity(activity):
        try:
            timestamp = datetime.datetime.fromtimestamp(float(activity.get("timestamp"))).strftime("%H:%M:%S")
        except (TypeError, ValueError, OSError):
            timestamp = "--:--:--"
        level = str(activity.get("level") or "info").upper()
        message = activity.get("message") or activity.get("event") or "-"
        return f"{timestamp} {level:<7} {message}"

    def _cap_set_details(self, text):
        widget = self._cap_upload_activity_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _cap_append_detail(self, text):
        widget = self._cap_upload_activity_text
        current = widget.get("1.0", "end-1c")
        if current in {"Listening for live upload activity", "Select a capture to view upload details"}:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
        else:
            widget.configure(state="normal")
            widget.insert("end", "\n")
        widget.insert("end", text)
        widget.configure(state="disabled")
        widget.see("end")

    def _cap_update_selected_capture(self):
        capture, upload = self._cap_selection()
        sds_check = self.upload_manager.get_sds_check(capture.get("name")) if capture else {}
        orchestrator = self.capture_orchestrator.get_status()
        rx = orchestrator.get("rx") if isinstance(orchestrator.get("rx"), dict) else {}
        recording = bool(
            capture
            and rx.get("state") in {"starting", "running"}
            and str(capture.get("name") or "") == str(rx.get("capture_name") or "preview")
        )
        self._cap_size_var.set(self._cap_format_bytes(capture.get("size_bytes"), decimals=0) if capture else "-")
        self._cap_files_var.set(str(capture.get("file_count") or 0) if capture else "-")
        self._cap_modified_var.set(self._cap_format_time(capture.get("last_modified")) if capture else "-")
        self._cap_recording_var.set("Active" if recording else "Done")

        verification = upload.get("verification") if upload and isinstance(upload.get("verification"), dict) else {}
        verified_bytes = verification.get("verified_bytes")
        expected_bytes = verification.get("expected_bytes")
        verified_files = verification.get("verified_files")
        expected_files = verification.get("expected_files")
        self._cap_upload_state_var.set(str(upload.get("state") or "-").replace("_", " ").title() if upload else "-")
        self._cap_verified_bytes_var.set(
            f"{self._cap_format_bytes(verified_bytes)} / {self._cap_format_bytes(expected_bytes)}"
            if verified_bytes is not None and expected_bytes is not None
            else "Checking..." if verification.get("state") == "checking" else "-"
        )
        self._cap_verified_files_var.set(
            f"{verified_files} / {expected_files} files"
            if verified_files is not None and expected_files is not None
            else "Checking..." if verification.get("state") == "checking" else "-"
        )
        self._cap_remote_var.set(str(upload.get("remote_path") or "-") if upload else "-")

        data_ready = self.archive_manager.is_available()
        upload_ready = self.upload_manager.is_available()
        active_states = {
            "queued",
            "scanning",
            "authenticating",
            "uploading",
            "verifying",
            "stopping",
        }
        upload_state = upload.get("state") if upload else None
        capture_uploadable = bool(capture and upload_ready and upload_state not in active_states)
        self._cap_rename_btn.configure(state="normal" if capture and data_ready else "disabled")
        self._cap_delete_btn.configure(state="normal" if capture and data_ready else "disabled")
        self._cap_upload_btn.configure(
            state="normal" if capture_uploadable else "disabled",
        )
        self._cap_stop_btn.configure(
            state="normal" if upload_ready and upload_state in active_states - {"stopping"} else "disabled"
        )
        self._cap_verify_btn.configure(
            state="normal"
            if upload_ready and upload and upload.get("remote_path") and upload_state not in active_states and str(upload.get("verification", {}).get("state") or "") != "checking"
            else "disabled"
        )
        if capture is None:
            reason = "Select a capture to manage or upload"
        elif upload_state == "waiting_for_credentials":
            reason = "Credentials required: enter an SDS token and click Upload"
        elif upload_state == "failed":
            reason = str(upload.get("error") or "Upload failed; click Upload to continue")
        elif upload_state == "stopping":
            reason = "Stop requested; waiting for the current SDK operation to return"
        elif not upload_ready:
            reason = "UploadManager is unavailable"
        else:
            reason = "Ready"
        self._cap_action_reason_var.set(reason)

    @staticmethod
    def _cap_format_sds_state(upload):
        if not isinstance(upload, dict):
            return "-"
        state = str(upload.get("state") or "")
        if state in {"queued", "scanning", "authenticating", "uploading", "verifying", "stopping"}:
            return state.replace("_", " ").title()
        verification = upload.get("verification")
        if isinstance(verification, dict):
            verification_state = str(verification.get("state") or "")
            if verification_state in {"checking", "verified", "incomplete", "unavailable"}:
                return verification_state.replace("_", " ").title()
        if state == "error":
            return "Cannot read status"
        return state.replace("_", " ").title() or "-"

    def _cap_rename(self):
        capture, _upload = self._cap_selection()
        new_name = self._cap_rename_var.get().strip()
        if not capture or not new_name:
            return
        self.archive_manager.rename_capture(
            capture["name"],
            new_name,
            callback=lambda response: self._gui_call(self._cap_service_response, response, True),
        )

    def _cap_delete(self):
        capture, _upload = self._cap_selection()
        if not capture or not messagebox.askokcancel("Delete Capture", f"Permanently delete {capture.get('name')!r} and all of its local files?"):
            return
        callback = lambda response: self._gui_call(self._cap_service_response, response, True)
        if capture.get("name") == "preview":
            self.archive_manager.delete_preview(callback=callback)
        else:
            self.archive_manager.delete_capture(capture["name"], callback=callback)

    def _cap_start_upload(self):
        capture, _upload = self._cap_selection()
        if not capture:
            return
        if not self.upload_manager.is_available():
            messagebox.showerror(
                "Upload Unavailable",
                "UploadManager has no online retained status. Start or restart the UploadManager service, then refresh CAP.",
            )
            return
        token = self._cap_token_var.get()
        credentials = {"token": token} if token else None
        self._cap_set_details("Waiting for live upload output")
        self.upload_manager.start_upload(
            capture["name"],
            credentials=credentials,
            dry_run=self._cap_dry_run_var.get(),
            verbose=self._cap_verbose_var.get(),
            callback=lambda response: self._gui_call(self._cap_service_response, response, True),
        )

    def _cap_stop_upload(self):
        _capture, upload = self._cap_selection()
        if upload:
            self._cap_action_reason_var.set("Stop requested; waiting for UploadManager acknowledgement")
            self._cap_append_detail("Stop requested; waiting for the current SDK operation to return")
            self.upload_manager.stop_upload(
                upload["capture_name"],
                callback=lambda response: self._gui_call(self._cap_service_response, response, True),
            )

    def _cap_verify_upload(self):
        _capture, upload = self._cap_selection()
        if not upload:
            return
        token = self._cap_token_var.get().strip()
        credentials = {"token": token} if token else None
        self._cap_action_reason_var.set("SDS verification requested")
        self._cap_verify_btn.configure(state="disabled")
        self.upload_manager.check_sds(
            upload["capture_name"],
            credentials=credentials,
            callback=lambda response: self._gui_call(self._cap_service_response, response, True),
        )

    @staticmethod
    def _cap_format_bytes(value, decimals=1):
        size = float(value or 0)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if size < 1024 or unit == "TiB":
                return f"{size:.{decimals}f} {unit}"
            size /= 1024

    @staticmethod
    def _cap_format_time(value):
        if not isinstance(value, (int, float)):
            return "-"
        return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")

    # ---- DOCKER tab ---- #
    # (built by _build_docker_tab below the DOCKER helpers section)

    def _build_mqtt_tab(self, frame: ttk.Frame):
        """MQTT tab: live log of all incoming MQTT messages + manual publish."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)   # log row expands

        # Initialize MQTT stream state
        self._mqtt_paused = False
        self._vars["mqtt_stream_state"] = tk.StringVar(value="live")

        # ---- Logging frame (Stream/Pause controls) ---- #
        log_ctl_f = ttk.LabelFrame(frame, text="Logging")
        log_ctl_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        log_ctl_f.columnconfigure(0, weight=1)
        log_ctl_f.columnconfigure(1, weight=1)
        log_ctl_f.columnconfigure(2, weight=1)

        # Status label for live/paused state
        state_lbl = ttk.Label(
            log_ctl_f,
            textvariable=self._vars["mqtt_stream_state"],
            foreground="grey",
            font=("TkFixedFont", 8),
        )
        state_lbl.grid(row=0, column=0, columnspan=3, sticky="w", padx=5, pady=(4, 1))

        ttk.Button(log_ctl_f, text="Stream",
                   command=self._mqtt_stream_resume).grid(
            row=1, column=0, padx=5, pady=(0, 2), sticky="ew")
        ttk.Button(log_ctl_f, text="Pause",
                   command=self._mqtt_stream_pause).grid(
            row=1, column=1, padx=5, pady=(0, 2), sticky="ew")
        ttk.Button(log_ctl_f, text="Clear",
                   command=self._mqtt_clear_buffer_and_widget).grid(
            row=1, column=2, padx=5, pady=(0, 2), sticky="ew")

        # ---- Suppress filters ---- #
        suppress_f = ttk.LabelFrame(log_ctl_f, text="Suppress")
        suppress_f.grid(row=2, column=0, columnspan=3, sticky="ew", padx=5, pady=(0, 4))
        suppress_f.columnconfigure(0, weight=1)
        suppress_f.columnconfigure(1, weight=1)
        suppress_f.columnconfigure(2, weight=1)

        self._vars["mqtt_suppress_announce"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(suppress_f, text="Announce",
                        variable=self._vars["mqtt_suppress_announce"]).grid(
            row=0, column=0, sticky="w", padx=5, pady=(2, 4))
        self._vars["mqtt_suppress_announce"].trace_add(
            "write", lambda *_: self._mqtt_render_from_buffer())

        self._vars["mqtt_suppress_data"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(suppress_f, text="+/data/+",
                        variable=self._vars["mqtt_suppress_data"]).grid(
            row=0, column=1, sticky="w", padx=5, pady=(2, 4))
        self._vars["mqtt_suppress_data"].trace_add(
            "write", lambda *_: self._mqtt_render_from_buffer())

        self._vars["mqtt_suppress_status"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(suppress_f, text="Status",
                        variable=self._vars["mqtt_suppress_status"]).grid(
            row=0, column=2, sticky="w", padx=5, pady=(2, 4))
        self._vars["mqtt_suppress_status"].trace_add(
            "write", lambda *_: self._mqtt_render_from_buffer())

        retention_f = ttk.Frame(log_ctl_f)
        retention_f.grid(row=3, column=0, columnspan=3, sticky="ew", padx=5, pady=(0, 4))
        retention_f.columnconfigure(1, weight=1)
        retention_f.columnconfigure(3, weight=1)

        self._vars["mqtt_buffer_max_messages"] = tk.IntVar(value=self._mqtt_buffer_max_messages)
        self._vars["mqtt_widget_max_lines"] = tk.IntVar(value=self._mqtt_widget_max_lines)

        ttk.Label(retention_f, text="Buffer Msgs").grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Spinbox(
            retention_f,
            from_=100,
            to=200000,
            increment=100,
            textvariable=self._vars["mqtt_buffer_max_messages"],
            width=10,
        ).grid(row=0, column=1, sticky="w")

        ttk.Label(retention_f, text="Max Lines").grid(row=0, column=2, sticky="w", padx=(12, 4))
        ttk.Spinbox(
            retention_f,
            from_=100,
            to=50000,
            increment=100,
            textvariable=self._vars["mqtt_widget_max_lines"],
            width=10,
        ).grid(row=0, column=3, sticky="w")

        ttk.Button(retention_f, text="Apply Retention",
                   command=self._mqtt_apply_retention_settings).grid(
            row=0, column=4, padx=(12, 0), sticky="e")

        # ---- Message log (shorter to leave room for publish panel) ---- #
        self._mqtt_text = scrolledtext.ScrolledText(
            frame, height=12, wrap="word", font=("TkFixedFont", 9),
            background="#f5f5f5", exportselection=False)
        self._mqtt_text.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 2))
        self._mqtt_text.bind("<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A"))
                      else "break")
        self._bind_copy_menu(self._mqtt_text, allow_paste=False)

        # ---- Manual publish ---- #
        pub_f = ttk.LabelFrame(frame, text="Publish Message")
        pub_f.grid(row=2, column=0, sticky="ew", padx=4, pady=(2, 6))
        pub_f.columnconfigure(1, weight=1)

        ttk.Label(pub_f, text="Topic").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["mqtt_pub_topic"] = tk.StringVar(value="tunercontrol/command")
        self._mqtt_pub_topic_entry = ttk.Entry(pub_f, textvariable=self._vars["mqtt_pub_topic"])
        self._mqtt_pub_topic_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        self._bind_copy_menu(self._mqtt_pub_topic_entry, strvar=self._vars["mqtt_pub_topic"], allow_paste=True)

        ttk.Label(pub_f, text="Payload").grid(
            row=1, column=0, sticky="nw", padx=5, pady=3)
        self._mqtt_pub_payload = tk.Text(pub_f, height=4, wrap="word",
                                         font=("TkFixedFont", 9))
        self._mqtt_pub_payload.grid(row=1, column=1, sticky="ew", padx=5, pady=3)
        self._bind_copy_menu(self._mqtt_pub_payload, allow_paste=True)
        self._mqtt_pub_payload.insert(
            "1.0",
            json.dumps({"arguments": {}, "task_name": "status"}, indent=2),
        )

        ttk.Button(pub_f, text="Publish",
                   command=self._mqtt_publish_manual).grid(
            row=2, column=0, columnspan=2, padx=5, pady=(0, 5), sticky="ew")

        self._add_copyable_note(
            frame,
            "Source: MQTT broker on localhost:1883 (subscribed status topics only; spectrum is opt-in via Stream)",
            row=3,
            wraplength=420,
        )
        self._mqtt_render_from_buffer()

    def _build_spec_tab(self, frame: ttk.Frame):
        """SPEC tab: live FFT line plot and rolling waterfall from MQTT spectrum frames."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        frame.rowconfigure(3, weight=1)

        cfg_f = ttk.LabelFrame(frame, text="Stream")
        cfg_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        cfg_f.columnconfigure(1, weight=1)
        ttk.Label(cfg_f, text="Topic").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["spec_topic"] = tk.StringVar(value=self._spec_topic)
        self._spec_topic_entry = ttk.Entry(cfg_f, textvariable=self._vars["spec_topic"], exportselection=False)
        self._spec_topic_entry.grid(
            row=0, column=1, sticky="ew", padx=5, pady=3
        )
        self._bind_copy_menu(self._spec_topic_entry)
        ttk.Button(cfg_f, text="Stream", command=self._spec_stream_on).grid(
            row=0, column=2, padx=(2, 2), pady=3
        )
        ttk.Button(cfg_f, text="Pause", command=self._spec_stream_off).grid(
            row=0, column=3, padx=(2, 5), pady=3
        )
        self._vars["spec_stream_state"] = tk.StringVar(value="paused")
        ttk.Label(cfg_f, textvariable=self._vars["spec_stream_state"], foreground="grey").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=5, pady=(0, 2)
        )
        self._vars["spec_log_dt"] = tk.BooleanVar(value=self._spec_log_dt)
        ttk.Checkbutton(
            cfg_f, text="Log frame timing (dt)",
            variable=self._vars["spec_log_dt"],
            command=lambda: setattr(self, "_spec_log_dt", self._vars["spec_log_dt"].get()),
        ).grid(row=1, column=2, columnspan=2, sticky="e", padx=5, pady=(0, 2))

        ctl_f = ttk.LabelFrame(frame, text="Display")
        ctl_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        ctl_f.columnconfigure(1, weight=1)
        ttk.Button(ctl_f, text="Autoscale", command=self._spec_autoscale_now).grid(
            row=0, column=0, sticky="w", padx=5, pady=2)
        ttk.Label(ctl_f, text="Min").grid(row=0, column=1, sticky="e", padx=(5, 2), pady=2)
        self._vars["spec_vmin"] = tk.DoubleVar(value=-250.0)
        self._spec_min_scale = ttk.Scale(ctl_f, from_=-250.0, to=50.0, variable=self._vars["spec_vmin"],
                                         command=lambda _v: self._spec_apply_color_range())
        self._spec_min_scale.grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        ttk.Label(ctl_f, text="Max").grid(row=0, column=3, sticky="e", padx=(8, 2), pady=2)
        self._vars["spec_vmax"] = tk.DoubleVar(value=-100.0)
        self._spec_max_scale = ttk.Scale(ctl_f, from_=-250.0, to=50.0, variable=self._vars["spec_vmax"],
                                         command=lambda _v: self._spec_apply_color_range())
        self._spec_max_scale.grid(row=0, column=4, sticky="ew", padx=2, pady=2)
        ttk.Button(ctl_f, text="Clear", command=self._spec_clear_now).grid(
            row=0, column=5, sticky="e", padx=5, pady=2)
        ttk.Label(ctl_f, text="Num Points").grid(row=1, column=0, sticky="w", padx=5, pady=(0, 2))
        self._vars["spec_bins"] = tk.StringVar(value="native" if self._spec_bins is None else str(self._spec_bins))
        bin_vals = (None, 64, 256, 512, 1024, 2048, 4096)
        bin_f = ttk.Frame(ctl_f)
        bin_f.grid(row=1, column=1, columnspan=4, sticky="w", padx=(2, 2), pady=(0, 2))
        self._spec_bin_buttons = {}
        for i, n in enumerate(bin_vals):
            btn = tk.Button(
                bin_f,
                text="native" if n is None else str(n),
                width=4,
                relief="raised",
                padx=2,
                pady=1,
                command=lambda v=n: self._spec_apply_bins(v),
            )
            btn.grid(row=0, column=i, padx=(0 if i == 0 else 2, 0), pady=0, sticky="w")
            self._spec_bin_buttons[n] = btn
        ctl_f.columnconfigure(2, weight=1)
        ctl_f.columnconfigure(4, weight=1)
        ttk.Label(ctl_f, text="Refresh (ms)").grid(row=2, column=0, sticky="w", padx=5, pady=(0, 4))
        self._vars["spec_render_interval_ms"] = tk.IntVar(value=self._spec_render_interval_ms)
        _ri_scale = ttk.Scale(
            ctl_f, from_=10, to=500,
            variable=self._vars["spec_render_interval_ms"],
            command=lambda v: self._spec_apply_render_interval(int(float(v))),
        )
        _ri_scale.grid(row=2, column=1, columnspan=3, sticky="ew", padx=2, pady=(0, 4))
        self._spec_render_interval_label = ttk.Label(ctl_f, text=f"{self._spec_render_interval_ms} ms")
        self._spec_render_interval_label.grid(row=2, column=4, sticky="w", padx=(4, 5), pady=(0, 4))
        self._spec_update_bin_button_states()

        self._spec_line_canvas = tk.Canvas(frame, height=170, background="#111111", highlightthickness=1,
                                           highlightbackground="#333333")
        self._spec_line_canvas.grid(row=2, column=0, padx=4, pady=(2, 2), sticky="nsew")
        self._spec_line_canvas.bind("<Motion>", lambda e: self._spec_cursor_update(e, from_waterfall=False))
        self._spec_line_canvas.bind("<Button-1>", lambda e: self._spec_cursor_update(e, from_waterfall=False))

        self._spec_wf_canvas = tk.Canvas(frame, height=220, background="#000000", highlightthickness=1,
                                         highlightbackground="#333333")
        self._spec_wf_canvas.grid(row=3, column=0, padx=4, pady=(2, 2), sticky="nsew")
        self._spec_wf_canvas.bind("<Motion>", lambda e: self._spec_cursor_update(e, from_waterfall=True))
        self._spec_wf_canvas.bind("<Button-1>", lambda e: self._spec_cursor_update(e, from_waterfall=True))
        self._spec_wf_canvas.bind("<Configure>", lambda e: self._spec_wf_resize_request(e.width, e.height))

        self._vars["spec_summary"] = tk.StringVar(value="Paused. Press Stream to start SPEC updates")
        ttk.Label(frame, textvariable=self._vars["spec_summary"], foreground="grey",
                  font=("TkDefaultFont", 8)).grid(row=4, column=0, sticky="w", padx=4, pady=(0, 2))
        self._vars["spec_cursor"] = tk.StringVar(value="Cursor: —")
        ttk.Label(frame, textvariable=self._vars["spec_cursor"], foreground="grey",
                  font=("TkDefaultFont", 8)).grid(row=5, column=0, sticky="w", padx=4, pady=(0, 2))

        self._add_copyable_note(
            frame,
            "Source: MQTT topic radiohound/clients/data/<device-id> (base64 float32 bins)",
            row=6,
            wraplength=420,
        )

    def _spec_wf_resize_request(self, w: int, h: int):
        """Debounce canvas resize events to avoid repeated buffer reallocations."""
        self._spec_wf_target_size = (max(4, int(w)), max(4, int(h)))
        if self._spec_wf_resize_after_id is not None:
            return
        self._spec_wf_resize_after_id = self.root.after(30, self._spec_wf_resize_commit)

    def _spec_wf_resize_commit(self):
        self._spec_wf_resize_after_id = None
        if not self._spec_wf_target_size:
            return
        w, h = self._spec_wf_target_size
        self._spec_wf_resize(w, h)

    def _spec_wf_resize(self, w: int, h: int):
        """Reallocate pixel buffer and viewport on canvas resize.

        The viewport resamples and preserves as many rows as fit the new height.
        The old PhotoImage is released after Tk has been given the new one.
        """
        w = max(4, w)
        h = max(4, h)
        if self._spec_pixels is not None and self._spec_pixels.shape[:2] == (h, w):
            return
        # Resize viewport (resamples existing rows to new width, trims to new height)
        self._spec_viewport.resize(w, h)
        # Allocate new pixel buffer
        self._spec_pixels = np.zeros((h, w, 3), dtype=np.uint8)
        # Allocate new PhotoImage, release old one after Tk has the new reference
        pil = _PILImage.fromarray(self._spec_pixels, "RGB")
        new_photo = _PILImageTk.PhotoImage(image=pil)
        if self._spec_wf_image_id is None:
            self._spec_wf_image_id = self._spec_wf_canvas.create_image(
                0, 0, anchor="nw", image=new_photo
            )
        else:
            self._spec_wf_canvas.itemconfig(self._spec_wf_image_id, image=new_photo)
        old_photo = self._spec_photo
        self._spec_photo = new_photo
        if old_photo is not None:
            del old_photo
        # Repaint from viewport if any data is present
        vmin, vmax = self._spec_color_range()
        if vmin is not None and self._spec_viewport.valid_rows > 0:
            self._spec_repaint_all(vmin, vmax)

    def _spec_reset_canvas(self):
        """Zero the pixel buffer and clear canvas overlay items.

        Does not touch the viewport — call viewport.clear() separately when
        a complete data reset is needed (e.g. Stream button press).
        """
        if self._spec_pixels is not None and self._spec_photo is not None:
            self._spec_pixels[:] = 0
            pil = _PILImage.fromarray(self._spec_pixels, "RGB")
            self._spec_photo.paste(pil)
        for item in self._spec_wf_labels.values():
            self._spec_wf_canvas.delete(item)
        self._spec_wf_labels = {}
        if self._spec_line_item is not None:
            self._spec_line_canvas.delete("all")
            self._spec_line_item = None
            self._spec_line_labels = {}

    def _spec_stream_on(self):
        """User clicked Stream: reset display and request active streaming."""
        topic = self._vars["spec_topic"].get().strip()
        try:
            self.mep.spectrum.set_topic(topic)
        except ValueError as exc:
            logging.error("SPEC: %s", exc)
            return
        self._spec_topic = self.mep.spectrum.topic
        self._spec_stream_requested = True
        # Reset for a fresh stream start (user-initiated only)
        self._spec_viewport.clear()
        with self._spec_lock:
            self._spec_pending.clear()
        self._spec_latest_entry = None
        self._spec_reset_canvas()
        self._spec_update_stream_state()

    def _spec_stream_off(self):
        """User clicked Pause: freeze the current display and stop streaming."""
        self._spec_stream_requested = False
        self._spec_update_stream_state()

    def _spec_update_stream_state(self):
        """Derive effective stream state from user intent and tab visibility.

        Subscribes and starts the render loop only when the user has requested
        streaming AND the SPEC tab is currently visible. When either condition is
        false, the display freezes at its last state without clearing.
        """
        should_be_active = self._spec_stream_requested and self._spec_tab_visible
        if should_be_active == self._spec_is_active:
            return
        self._spec_is_active = should_be_active
        if should_be_active:
            self.mep.spectrum.start()
            self._spec_start_render_loop()
            if "spec_stream_state" in self._vars:
                self._vars["spec_stream_state"].set("streaming")
            if "spec_summary" in self._vars:
                self._vars["spec_summary"].set(f"Listening on {self._spec_topic}")
        else:
            self.mep.spectrum.stop()
            self._spec_stop_render_loop()
            with self._spec_lock:
                self._spec_pending.clear()
            if "spec_stream_state" in self._vars:
                self._vars["spec_stream_state"].set("paused")
            if not self._spec_stream_requested and "spec_summary" in self._vars:
                self._vars["spec_summary"].set(f"Paused on {self._spec_topic}")

    def _spec_update_bin_button_states(self):
        if not hasattr(self, "_spec_bin_buttons"):
            return
        for n, btn in self._spec_bin_buttons.items():
            if n == self._spec_bins:
                btn.configure(relief="sunken", bd=3)
            else:
                btn.configure(relief="raised", bd=2)

    def _spec_apply_bins(self, n="__from_var__"):
        allowed = {None, 64, 256, 512, 1024, 2048, 4096}
        if n == "__from_var__":
            token = str(self._vars["spec_bins"].get()).strip().lower()
            if token in ("none", "native", ""):
                n = None
            else:
                n = int(token)
        else:
            n = None if n is None else int(n)
        if n not in allowed:
            logging.error("SPEC: line points must be one of none, 64, 256, 512, 1024, 2048, 4096")
            self._vars["spec_bins"].set("native" if self._spec_bins is None else str(self._spec_bins))
            self._spec_update_bin_button_states()
            return
        self._vars["spec_bins"].set("native" if n is None else str(n))
        self._spec_bins = n
        self._spec_update_bin_button_states()
        # Line resample controls only the line-plot resolution; the waterfall renders
        # at native resolution, so no buffer reset is needed — just redraw.
        self._spec_request_render()

    def _spec_apply_render_interval(self, ms: int):
        ms = max(10, min(500, ms))
        self._spec_render_interval_ms = ms
        if hasattr(self, "_spec_render_interval_label"):
            self._spec_render_interval_label.config(text=f"{ms} ms")

    def _spec_build_color_lut(self):
        """Build a (256, 3) uint8 numpy array: index → (R, G, B) colormap."""
        lut = np.zeros((256, 3), dtype=np.uint8)
        for idx in range(256):
            t = idx / 255.0
            if t < 0.33:
                u = t / 0.33
                r, g, b = 0, int(255 * u), int(128 + 127 * u)
            elif t < 0.66:
                u = (t - 0.33) / 0.33
                r, g, b = int(255 * u), 255, int(255 * (1.0 - u))
            else:
                u = (t - 0.66) / 0.34
                r, g, b = 255, int(255 * (1.0 - u)), 0
            lut[idx] = (r, g, b)
        return lut

    def _spec_row_to_rgb(self, db_row, w: int, vmin: float, vmax: float):
        """Map one native dB row to a (w, 3) uint8 RGB strip via the colormap.

        The native spectrum is resampled directly to the pixel width here — a
        single resampling step (no intermediate bin-count round-trip).
        """
        vals = _spec_resample_1d(db_row, w)
        scale = 255.0 / (vmax - vmin)
        idx = np.clip((vals - vmin) * scale, 0, 255).astype(np.uint8)
        return self._spec_color_lut[idx]

    def _spec_blit_new_rows(self, count: int, vmin: float, vmax: float):
        """Scroll the pixel buffer down by ``count`` rows and paint the newest rows.

        The ``count`` newest viewport rows (offset 0 = newest = top) are
        colour-mapped and the whole buffer is pasted into the persistent
        PhotoImage in a single X11 op, so catching up on a burst of frames costs
        one blit regardless of how many rows arrived since the last tick.
        """
        if self._spec_pixels is None or self._spec_photo is None or count <= 0:
            return
        h, w, _ = self._spec_pixels.shape
        if count >= h:
            # Every visible row is new — rebuild from the viewport at one scale.
            self._spec_repaint_all(vmin, vmax)
            return
        # Shift existing rows down by ``count``. NumPy buffers the overlapping
        # slice assignment, so the down-shift is correct.
        self._spec_pixels[count:] = self._spec_pixels[:-count]
        # Paint the ``count`` newest rows at the top (pixel row i = offset i).
        for i in range(count):
            row_vals, _ = self._spec_viewport.row_at_offset(i)  # 0 = newest
            if row_vals is not None:
                self._spec_pixels[i] = self._spec_row_to_rgb(row_vals, w, vmin, vmax)
        pil = _PILImage.fromarray(self._spec_pixels, "RGB")
        self._spec_photo.paste(pil)

    def _spec_repaint_all(self, vmin: float, vmax: float):
        """Rebuild the entire pixel buffer from viewport rows at a consistent scale.

        Called on colour-range changes and canvas resize so every visible row
        uses the same mapping — prevents colour drift across the image.
        """
        if self._spec_pixels is None or self._spec_photo is None:
            return
        h, w, _ = self._spec_pixels.shape
        self._spec_pixels[:] = 0
        n = min(self._spec_viewport.valid_rows, h)
        for i in range(n):
            row_vals, _ = self._spec_viewport.row_at_offset(i)  # 0=newest=top
            if row_vals is not None:
                self._spec_pixels[i] = self._spec_row_to_rgb(row_vals, w, vmin, vmax)
        pil = _PILImage.fromarray(self._spec_pixels, "RGB")
        self._spec_photo.paste(pil)

    def _spec_freq_axis(self, latest: dict, n_bins: int):
        """Return (lo, hi, is_hz). Absolute Hz when metadata is present, else bin indices."""
        cf = latest.get("center_frequency")
        fmin = latest.get("fmin")
        fmax = latest.get("fmax")
        if isinstance(cf, (int, float)) and isinstance(fmin, (int, float)) and isinstance(fmax, (int, float)):
            return float(cf + fmin), float(cf + fmax), True
        return 0.0, float(max(0, n_bins - 1)), False

    def _spec_fmt_axis(self, value: float, is_hz: bool):
        return f"{value / 1e6:.3f} MHz" if is_hz else f"bin {int(round(value))}"

    def _spec_fmt_amp(self, amp: float):
        return f"{float(amp):.2f} dBFS"

    def _spec_cursor_update(self, event, from_waterfall: bool):
        """Report Frequency, Time, and Intensity under the cursor.

        Waterfall: reads numeric values directly from the viewport ring so the
        reported intensity is the actual dBFS value, not a reverse-engineered
        colour. Line plot: reads from the most recent native-resolution entry.
        """
        canvas = self._spec_wf_canvas if from_waterfall else self._spec_line_canvas
        w = max(10, canvas.winfo_width())
        x = max(0, min(w - 1, event.x))

        if from_waterfall:
            h = max(1, self._spec_wf_canvas.winfo_height())
            y = max(0, min(h - 1, event.y))
            n_valid = self._spec_viewport.valid_rows
            if n_valid == 0 or y >= n_valid:
                if "spec_cursor" in self._vars:
                    self._vars["spec_cursor"].set("Cursor: no data at this row")
                return
            # Viewport offset 0 = newest = pixel row 0 (top of waterfall)
            row_vals, meta = self._spec_viewport.row_at_offset(y)
            if row_vals is None or meta is None:
                return
            vw = self._spec_viewport.width
            col = max(0, min(vw - 1, int(round(x * (vw - 1) / max(1, w - 1)))))
            f0, f1, is_hz = self._spec_freq_axis(meta, vw)
            freq = f0 + (f1 - f0) * (col / max(1, vw - 1))
            amp = float(row_vals[col])
            ts = meta.get("ts", "?")
            label = (
                f"Cursor: f={self._spec_fmt_axis(freq, is_hz)}"
                f"  t={ts}  pwr={self._spec_fmt_amp(amp)}"
            )
        else:
            latest = self._spec_latest_entry
            if latest is None:
                return
            native = latest["row"]
            display = native if self._spec_bins is None else _spec_resample_1d(native, self._spec_bins)
            n = len(display)
            if n <= 0:
                return
            h = max(10, self._spec_line_canvas.winfo_height())
            y = max(0, min(h - 1, event.y))
            idx = max(0, min(n - 1, int(round(x * (n - 1) / max(1, w - 1)))))
            f0, f1, is_hz = self._spec_freq_axis(latest, len(native))
            freq = f0 + (f1 - f0) * (idx / max(1, n - 1))
            trace_amp = float(display[idx])
            vmin, vmax = self._spec_color_range()
            if vmin is None:
                vmin = float(np.min(display))
                vmax = float(np.max(display))
                if vmax <= vmin:
                    vmax = vmin + 1.0
            amp = float(vmax - (y / max(1, h - 1)) * (vmax - vmin))
            ts = latest.get("ts", "?")
            label = (
                f"Cursor: f={self._spec_fmt_axis(freq, is_hz)}"
                f"  t={ts}  pwr={self._spec_fmt_amp(amp)}"
                f" (trace {self._spec_fmt_amp(trace_amp)})"
            )
        if "spec_cursor" in self._vars:
            self._vars["spec_cursor"].set(label)

    def _spec_handle_stream_message(self, entry: dict):
        """Producer thread: install a decoded frame in the bounded GUI queue."""
        if not self._spec_is_active:
            return
        now = time.monotonic()
        if self._spec_log_dt and self._spec_last_arrival is not None:
            logging.info(f"SPEC frame dt={(now - self._spec_last_arrival) * 1000:.0f} ms")
        self._spec_last_arrival = now
        with self._spec_lock:
            self._spec_pending.append(entry)

    def _spec_request_render(self):
        """Request a single redraw soon (used by the color-scale controls)."""
        self._spec_force_render = True
        if self._spec_render_after_id is None:
            self._spec_render_after_id = self.root.after(0, self._spec_render)

    def _spec_start_render_loop(self):
        """Begin the fixed-cadence render timer (idempotent)."""
        if self._spec_render_after_id is None:
            self._spec_render_after_id = self.root.after(
                self._spec_render_interval_ms, self._spec_render
            )

    def _spec_stop_render_loop(self):
        """Cancel the render timer if running."""
        if self._spec_render_after_id is not None:
            try:
                self.root.after_cancel(self._spec_render_after_id)
            except Exception:
                pass
            self._spec_render_after_id = None

    def _spec_render(self):
        """Consumer (Tk thread): drain all pending frames, update viewport, render.

        Every frame queued since the last tick is drained (oldest first) and
        accepted into the viewport, then the waterfall is advanced by the number
        of new rows in a single block blit. This catches up on bursts without
        dropping fresh frames and without one PhotoImage paste per row.
        """
        self._spec_render_after_id = None
        force = self._spec_force_render
        self._spec_force_render = False

        # Drain every frame queued since the last tick (oldest first).
        with self._spec_lock:
            entries = list(self._spec_pending)
            self._spec_pending.clear()

        new_count = 0
        for entry in entries:
            meta = {
                "ts": entry.get("ts"),
                "center_frequency": entry.get("center_frequency"),
                "fmin": entry.get("fmin"),
                "fmax": entry.get("fmax"),
                "scan_time": entry.get("scan_time"),
                "n": entry.get("n"),
            }
            if self._spec_viewport.accept_row(entry["row"], meta):
                self._spec_latest_entry = entry
                new_count += 1

        latest = self._spec_latest_entry
        if latest is not None and (new_count or force):
            vmin, vmax = self._spec_color_range()
            if vmin is not None:
                if new_count:
                    self._spec_blit_new_rows(new_count, vmin, vmax)
                elif force:
                    self._spec_repaint_all(vmin, vmax)
                self._spec_update_line(latest, vmin, vmax)
                self._spec_update_wf_labels(latest)
                self._spec_update_summary(latest)

        if self._spec_is_active:
            self._spec_render_after_id = self.root.after(
                self._spec_render_interval_ms, self._spec_render
            )

    def _spec_color_range(self):
        """Return the active (vmin, vmax) from the sliders, or (None, None)."""
        vmin = float(self._vars["spec_vmin"].get())
        vmax = float(self._vars["spec_vmax"].get())
        if not (math.isfinite(vmin) and math.isfinite(vmax)) or vmax <= vmin:
            return None, None
        return vmin, vmax

    def _spec_autoscale_now(self):
        """Fit the colour range to all valid rows in the current viewport."""
        vmin, vmax = self._spec_viewport.value_range()
        if vmin is None:
            return
        if vmax <= vmin:
            vmax = vmin + 1.0
        self._vars["spec_vmin"].set(round(vmin, 1))
        self._vars["spec_vmax"].set(round(vmax, 1))
        self._spec_apply_color_range()

    def _spec_clear_now(self):
        """Clear the spectrogram display without changing stream state."""
        self._spec_viewport.clear()
        with self._spec_lock:
            self._spec_pending.clear()
        self._spec_latest_entry = None
        self._spec_reset_canvas()

    def _spec_apply_color_range(self):
        """Recolor the entire waterfall under the current range, then redraw once.

        Repainting the whole buffer (rather than only new rows) keeps every
        visible row on the same scale, so the image never mixes color mappings.
        """
        vmin, vmax = self._spec_color_range()
        if vmin is None:
            return
        self._spec_repaint_all(vmin, vmax)
        self._spec_request_render()

    def _spec_ensure_line_items(self):
        """Create the persistent FFT line + label items once."""
        if self._spec_line_item is not None:
            return
        c = self._spec_line_canvas
        self._spec_line_item = c.create_line(0, 0, 0, 0, fill="#6ad7ff", width=1)
        self._spec_line_labels = {
            "title": c.create_text(6, 6, anchor="nw", fill="#cccccc", text="Live FFT"),
            "ylab": c.create_text(6, 0, anchor="w", fill="#aaaaaa", text="Power (dBFS)"),
            "vmax": c.create_text(6, 20, anchor="nw", fill="#888888", text=""),
            "vmin": c.create_text(6, 0, anchor="sw", fill="#888888", text=""),
            "f0": c.create_text(6, 0, anchor="sw", fill="#aaaaaa", text=""),
            "f1": c.create_text(0, 0, anchor="se", fill="#aaaaaa", text=""),
            "fmid": c.create_text(0, 0, anchor="s", fill="#aaaaaa", text="Frequency"),
        }

    def _spec_update_line(self, latest: dict, vmin: float, vmax: float):
        """Update the FFT line + labels in place (resampled to the chosen bin count)."""
        c = self._spec_line_canvas
        self._spec_ensure_line_items()
        native = latest["row"]
        vals = native if self._spec_bins is None else _spec_resample_1d(native, self._spec_bins)
        w = max(10, c.winfo_width())
        h = max(10, c.winfo_height())
        n = len(vals)
        xs = (np.arange(n) * (w - 1) / max(1, n - 1)).astype(int)
        y_norm = np.clip((vals - vmin) / (vmax - vmin), 0.0, 1.0)
        ys = ((1.0 - y_norm) * (h - 1)).astype(int)
        pts = np.empty(n * 2, dtype=int)
        pts[0::2] = xs
        pts[1::2] = ys
        c.coords(self._spec_line_item, *pts.tolist())
        f0, f1, is_hz = self._spec_freq_axis(latest, len(native))
        lbl = self._spec_line_labels
        c.coords(lbl["ylab"], 6, h // 2)
        c.itemconfig(lbl["vmax"], text=f"max {self._spec_fmt_amp(vmax)}")
        c.coords(lbl["vmin"], 6, h - 20)
        c.itemconfig(lbl["vmin"], text=f"min {self._spec_fmt_amp(vmin)}")
        c.coords(lbl["f0"], 6, h - 4)
        c.itemconfig(lbl["f0"], text=self._spec_fmt_axis(f0, is_hz))
        c.coords(lbl["f1"], w - 6, h - 4)
        c.itemconfig(lbl["f1"], text=self._spec_fmt_axis(f1, is_hz))
        c.coords(lbl["fmid"], w // 2, h - 4)

    def _spec_ensure_wf_items(self):
        """Create the persistent waterfall overlay label items once."""
        if self._spec_wf_labels:
            return
        c = self._spec_wf_canvas
        self._spec_wf_labels = {
            "title": c.create_text(6, 6, anchor="nw", fill="#cccccc", text="Waterfall"),
            "now": c.create_text(0, 6, anchor="n", fill="#aaaaaa", text="now"),
            "span": c.create_text(0, 0, anchor="s", fill="#aaaaaa", text=""),
            "tlab": c.create_text(0, 0, anchor="s", fill="#aaaaaa", text="Time"),
            "f0": c.create_text(6, 0, anchor="sw", fill="#aaaaaa", text=""),
            "f1": c.create_text(0, 0, anchor="se", fill="#aaaaaa", text=""),
        }
        for item in self._spec_wf_labels.values():
            c.tag_raise(item)

    def _spec_update_wf_labels(self, latest: dict):
        """Update waterfall overlay labels in place (kept above the image item)."""
        c = self._spec_wf_canvas
        self._spec_ensure_wf_items()
        w = max(10, c.winfo_width())
        h = max(10, c.winfo_height())
        n = len(latest["row"])
        f0, f1, is_hz = self._spec_freq_axis(latest, n)
        lbl = self._spec_wf_labels
        c.coords(lbl["now"], w // 2, 6)
        scan_t = latest.get("scan_time")
        if isinstance(scan_t, (int, float)) and scan_t > 0:
            span = scan_t * max(1, self._spec_viewport.valid_rows)
            c.coords(lbl["span"], w // 2, h - 4)
            c.itemconfig(lbl["span"], text=f"-{span:.1f} s", state="normal")
        else:
            c.itemconfig(lbl["span"], state="hidden")
        c.coords(lbl["tlab"], w // 2, h - 20)
        c.coords(lbl["f0"], 6, h - 18)
        c.itemconfig(lbl["f0"], text=self._spec_fmt_axis(f0, is_hz))
        c.coords(lbl["f1"], w - 6, h - 18)
        c.itemconfig(lbl["f1"], text=self._spec_fmt_axis(f1, is_hz))
        for item in lbl.values():
            c.tag_raise(item)

    def _spec_update_summary(self, latest: dict):
        if "spec_summary" in self._vars:
            line_mode = "native" if self._spec_bins is None else str(self._spec_bins)
            self._vars["spec_summary"].set(
                f"ts={latest.get('ts', '?')}   cf={latest.get('center_frequency', '?')} Hz   "
                f"sr={latest.get('sample_rate', '?')} Hz   line={line_mode} (src {latest.get('n', '?')})"
            )

    def _mqtt_publish_manual(self):
        """Publish an arbitrary MQTT message from the manual publish panel."""
        topic = self._vars["mqtt_pub_topic"].get().strip()
        payload = self._mqtt_pub_payload.get("1.0", "end-1c").strip()
        if not topic:
            logging.error("MQTT publish: topic is empty")
            return
        try:
            self.mep.diagnostics.publish(topic, payload)
            logging.info(f"MQTT published → {topic}")
        except Exception as e:
            logging.error(f"MQTT publish failed: {e}")

    def _mqtt_stream_pause(self):
        """Pause the MQTT stream log."""
        self._mqtt_paused = True
        self._vars["mqtt_stream_state"].set("paused")

    def _mqtt_stream_resume(self):
        """Resume the MQTT stream log."""
        self._mqtt_paused = False
        self._vars["mqtt_stream_state"].set("live")

    def _mqtt_format_entry(self, ts: str, topic: str, payload: bytes) -> str:
        try:
            decoded = payload.decode("utf-8")
            stripped = decoded.strip()
            if stripped:
                try:
                    parsed = json.loads(stripped)
                    pretty = json.dumps(parsed, indent=2, sort_keys=True)
                    body = "\n".join(f"  {ln}" for ln in pretty.splitlines())
                except Exception:
                    body = "\n".join(f"  {ln}" for ln in decoded.rstrip().splitlines())
            else:
                body = "  <empty>"
        except Exception:
            body = f"  {repr(payload)}"
        return f"{ts}  {topic}\n{body}\n"

    def _mqtt_capture_message(self, topic: str, payload: bytes):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with self._mqtt_lock:
            self._mqtt_messages.append((ts, topic, payload))

    def _mqtt_apply_retention_settings(self):
        try:
            buffer_max = int(self._vars["mqtt_buffer_max_messages"].get())
            widget_max = int(self._vars["mqtt_widget_max_lines"].get())
        except (KeyError, tk.TclError, ValueError):
            logging.error("MQTT: retention settings must be integers")
            return

        if buffer_max < 100 or widget_max < 100:
            logging.error("MQTT: retention settings must be at least 100")
            return

        with self._mqtt_lock:
            entries = list(self._mqtt_messages)
            self._mqtt_buffer_max_messages = buffer_max
            self._mqtt_messages = deque(entries[-buffer_max:], maxlen=buffer_max)

        self._mqtt_widget_max_lines = widget_max
        self._mqtt_rendered_count = 0
        if hasattr(self, "_mqtt_text"):
            self._mqtt_render_from_buffer()

        logging.info(
            "MQTT: retention updated (buffer=%s messages, widget=%s lines)",
            buffer_max,
            widget_max,
        )

    def _mqtt_topic_is_suppressed(self, topic: str) -> bool:
        topic = (topic or "").strip().lower()
        parts = [p for p in topic.split("/") if p]

        if bool(self._vars.get("mqtt_suppress_announce", tk.BooleanVar(value=False)).get()):
            if "announce" in parts:
                return True

        if bool(self._vars.get("mqtt_suppress_data", tk.BooleanVar(value=False)).get()):
            if "data" in parts:
                return True

        if bool(self._vars.get("mqtt_suppress_status", tk.BooleanVar(value=False)).get()):
            if "status" in parts:
                return True
        return False

    def _mqtt_flush_buffer_to_widget(self):
        if not hasattr(self, "_mqtt_text"):
            return
        if not self._is_adv_tab_selected("BUS"):
            return
        # Do not log if stream is paused
        if self._mqtt_paused:
            return

        with self._mqtt_lock:
            entries = list(self._mqtt_messages)
            start = min(self._mqtt_rendered_count, len(entries))
            tail = entries[start:]

        if not tail:
            return

        lines = []
        for ts, topic, payload in tail:
            if self._mqtt_topic_is_suppressed(topic):
                continue
            lines.append(self._mqtt_format_entry(ts, topic, payload))

        if lines:
            self._mqtt_text.insert("end", "".join(lines))
            self._mqtt_text.see("end")

        self._mqtt_rendered_count = len(entries)
        self._mqtt_trim_widget_lines()

    def _mqtt_trim_widget_lines(self, max_lines: int = None):
        if not hasattr(self, "_mqtt_text"):
            return
        if max_lines is None:
            max_lines = self._mqtt_widget_max_lines
        lines = int(self._mqtt_text.index("end-1c").split(".")[0])
        if lines > max_lines:
            self._mqtt_text.delete("1.0", f"{lines - max_lines}.0")

    def _mqtt_render_from_buffer(self):
        if not hasattr(self, "_mqtt_text"):
            return
        self._mqtt_text.delete("1.0", "end")
        self._mqtt_rendered_count = 0
        self._mqtt_flush_buffer_to_widget()

    def _mqtt_clear_buffer_and_widget(self):
        with self._mqtt_lock:
            self._mqtt_messages.clear()
        self._mqtt_rendered_count = 0
        if hasattr(self, "_mqtt_text"):
            self._mqtt_text.delete("1.0", "end")

    def _install_text_widget_bindings(self):
        for class_name in ("Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Text"):
            self.root.bind_class(class_name, "<Button-3>", self._show_text_context_menu, add="+")
            self.root.bind_class(
                class_name,
                "<Control-c>",
                lambda event: (self._copy_widget_text(event.widget), "break")[1],
            )
            self.root.bind_class(
                class_name,
                "<Control-v>",
                lambda event: (self._paste_widget_text(event.widget), "break")[1],
            )

    @staticmethod
    def _widget_accepts_paste(widget):
        try:
            return str(widget.cget("state")).lower() not in {"disabled", "readonly"}
        except (tk.TclError, AttributeError):
            return True

    def _widget_copy_text(self, widget):
        try:
            if isinstance(widget, (tk.Text, scrolledtext.ScrolledText)):
                ranges = widget.tag_ranges("sel")
                return widget.get(ranges[0], ranges[1]) if len(ranges) >= 2 else widget.get("1.0", "end-1c")
            else:
                try:
                    return widget.selection_get() if widget.selection_present() else widget.get()
                except (tk.TclError, AttributeError):
                    return widget.get()
        except (tk.TclError, AttributeError):
            return ""

    def _copy_widget_text(self, widget, text=None):
        if text is None:
            text = self._widget_copy_text(widget)
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _paste_widget_text(self, widget):
        if not self._widget_accepts_paste(widget):
            return
        try:
            widget.event_generate("<<Paste>>")
        except tk.TclError:
            return

    def _show_text_context_menu(self, event):
        widget = event.widget
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Copy", command=lambda: self._copy_widget_text(widget))
        if self._widget_accepts_paste(widget):
            menu.add_command(label="Paste", command=lambda: self._paste_widget_text(widget))
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _bind_copy_menu(self, widget, strvar=None, allow_paste=True):
        """Attach right-click Copy (and optionally Paste) + Ctrl+C/V for Entry/Text widgets."""
        menu = tk.Menu(widget, tearoff=0)
        popup_text = {"value": None}

        def _popup_menu(e):
            # Capture before tk_popup transfers focus and potentially clears the selection.
            popup_text["value"] = self._widget_copy_text(widget)
            menu.tk_popup(e.x_root, e.y_root)
            return "break"

        def _copy(use_popup_selection=False):
            if isinstance(widget, (tk.Label, ttk.Label)):
                try:
                    variable_name = str(widget.cget("textvariable"))
                    text = self.root.getvar(variable_name) if variable_name else widget.cget("text")
                    self.root.clipboard_clear()
                    self.root.clipboard_append(text)
                except Exception:
                    return
            else:
                text = None
                if isinstance(widget, (tk.Text, scrolledtext.ScrolledText)):
                    try:
                        ranges = widget.tag_ranges("sel")
                        if len(ranges) >= 2:
                            text = widget.get(ranges[0], ranges[1])
                    except tk.TclError:
                        pass
                if not text and use_popup_selection:
                    text = popup_text["value"]
                self._copy_widget_text(widget, text)

        def _paste():
            self._paste_widget_text(widget)
            if strvar is not None:
                try:
                    strvar.set(widget.get())
                except (tk.TclError, AttributeError):
                    pass

        menu.add_command(label="Copy", command=lambda: _copy(use_popup_selection=True))
        if allow_paste:
            menu.add_command(label="Paste", command=_paste)
        widget.bind("<Button-3>", _popup_menu)
        widget.bind("<Control-c>", lambda e: (_copy(), "break")[1])
        if allow_paste:
            widget.bind("<Control-v>", lambda e: (_paste(), "break")[1])

    def _add_copyable_note(self, parent, text: str, row: int, wraplength: int = 420):
        """Render subtle gray footer text that still allows selection/copy."""
        wraplength = min(int(wraplength), 320)
        est_lines = max(1, min(3, (len(text) // max(40, wraplength // 7)) + 1))
        note = tk.Text(
            parent,
            height=est_lines,
            wrap="word",
            font=("TkDefaultFont", 8),
            foreground="grey",
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            padx=0,
            pady=0,
            background=self.root.cget("bg"),
        )
        note.grid(row=row, column=0, padx=4, pady=(0, 2), sticky="ew")
        note.insert("1.0", text)
        note.configure(state="disabled")
        note.bind(
            "<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A"))
                      else "break",
        )
        self._bind_copy_menu(note)
        return note

    def _jetson_health_set(self, key: str, value):
        val = "-" if value is None else str(value)
        if key in self._vars and self._vars[key].get() != val:
            self._vars[key].set(val)

    def _jetson_health_apply(self, data: dict):
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            self._jetson_health_set(key, value)
        self._jetson_service_status_apply()

    def _jetson_service_status_apply(self):
        if "jh_capture_orchestrator" not in self._vars:
            return
        for key, adapter in (
            ("jh_capture_orchestrator", self.capture_orchestrator),
            ("jh_archive_manager", self.archive_manager),
            ("jh_upload_manager", self.upload_manager),
        ):
            status = adapter.get_status()
            self._jetson_health_set(key, status.get("state") or ("available" if status else "no retained status"))

    def _jetson_nvpmodel_choice_values(self) -> list[str]:
        return [f"{mode_id}: {name}" for mode_id, name in self._jetson_nvpmodel_modes]

    def _jetson_nvpmodel_name_for_id(self, mode_id):
        if mode_id is None:
            return None
        mode_id = str(mode_id)
        for candidate_id, name in self._jetson_nvpmodel_modes:
            if candidate_id == mode_id:
                return name
        return None

    def _jetson_nvpmodel_choice_for_id(self, mode_id):
        name = self._jetson_nvpmodel_name_for_id(mode_id)
        if name is None or mode_id is None:
            return None
        return f"{mode_id}: {name}"

    def _jetson_nvpmodel_id_from_choice(self, choice: str):
        m = re.match(r"\s*(\d+)\s*:", choice or "")
        return m.group(1) if m else None

    def _format_nvpmodel_display(self, mode_id, mode_name):
        if mode_name and mode_id:
            return f"{mode_name} (ID {mode_id})"
        if mode_name:
            return mode_name
        if mode_id:
            known_name = self._jetson_nvpmodel_name_for_id(mode_id)
            if known_name:
                return f"{known_name} (ID {mode_id})"
            return f"ID {mode_id}"
        return None

    def _jetson_health_collect(self, include_tegrastats: bool = False) -> dict:
        resources = self.host.get_resources()
        network = self.host.get_network()
        thermal = self.host.get_thermal()
        power = self.host.get_power(include_tegrastats)
        data = {
            "jh_host_metrics_status": "",
            "jh_net_status": network.get("status", "Offline"),
            "jh_net_mac": network.get("mac", "-"),
            "jh_net_ip": network.get("ipv4", "-"),
            "jh_net_reason": "",
            "jh_thermal_reason": thermal.get("detail", "") if thermal.get("error_code") else "",
            "jh_nvpmodel": self._format_nvpmodel_display(
                (power.get("mode") or {}).get("id"), (power.get("mode") or {}).get("name")
            ),
            "jh_nvpmodel_default": self._jetson_nvpmodel_choice_for_id(power.get("default_mode_id")),
        }
        cpu = resources.get("cpu_usage_percent")
        if cpu is not None:
            data["jh_cpu_usage"] = f"{cpu:.1f}%"
        memory = resources.get("memory", {})
        if memory.get("total_kb"):
            used_kb = memory.get("used_kb", 0)
            total_kb = memory["total_kb"]
            data["jh_ram"] = f"{used_kb / 1024:.0f}/{total_kb / 1024:.0f} MB ({used_kb * 100 / total_kb:.1f}%)"
        disk = resources.get("disk", {})
        if disk.get("total_bytes"):
            free_bytes = disk.get("free_bytes", 0)
            total_bytes = disk["total_bytes"]
            data["jh_disk"] = f"{free_bytes / (1024 ** 3):.1f}/{total_bytes / (1024 ** 3):.1f} GiB free ({(total_bytes - free_bytes) * 100 / total_bytes:.1f}% used)"
        for index in range(1, 7):
            zone_index = index - 1
            if zone_index < len(thermal.get("temps", [])):
                name, value = thermal["temps"][zone_index]
                data[f"jh_temp_name_{index}"] = name
                data[f"jh_temp_val_{index}"] = f"{value:.1f} C"
            else:
                data[f"jh_temp_name_{index}"] = f"Temp {index}"
                data[f"jh_temp_val_{index}"] = "-"
        if include_tegrastats:
            data["jh_tegrastats_last"] = datetime.datetime.now().strftime("Last queried: %Y-%m-%d %H:%M:%S")
            for index in range(1, 4):
                rail_index = index - 1
                if rail_index < len(power.get("rails", [])):
                    rail = power["rails"][rail_index]
                    data[f"jh_pwr_name_{index}"] = rail["name"]
                    data[f"jh_pwr_val_{index}"] = rail["value"]
                else:
                    data[f"jh_pwr_name_{index}"] = f"Rail {rail_index}"
                    data[f"jh_pwr_val_{index}"] = "-"
        return data

    def _jetson_health_poll(self):
        # Keep background load minimal unless the user is actively viewing JET.
        if not self._vars.get("jh_auto_refresh", tk.BooleanVar(value=True)).get():
            return
        if not self._adv_frame.winfo_viewable():
            return
        try:
            current = self._adv_nb.index("current")
            if self._adv_nb.tab(current, "text") != "JET":
                return
        except Exception:
            return

        if self._jetson_health_busy:
            return

        self._jetson_health_busy = True

        def _worker():
            try:
                data = self._jetson_health_collect(False)
                self._gui_call(self._jetson_health_apply, data)
            finally:
                self._gui_call(setattr, self, "_jetson_health_busy", False)

        threading.Thread(target=_worker, daemon=True).start()

    def _jetson_health_refresh_now(self):
        if self._jetson_health_busy:
            return

        def _worker():
            try:
                data = self._jetson_health_collect(False)
                self._gui_call(self._jetson_health_apply, data)
            finally:
                self._gui_call(setattr, self, "_jetson_health_busy", False)

        self._jetson_health_busy = True
        threading.Thread(target=_worker, daemon=True).start()

    def _jetson_health_sync_nvpmodel_choice(self):
        def _worker():
            result = self.host.get_current_power_mode()
            if result:
                mode_id, mode_name = result
            else:
                mode_id, mode_name = None, None
            display = self._format_nvpmodel_display(mode_id, mode_name)
            choice = self._jetson_nvpmodel_choice_for_id(mode_id)

            def _apply_current():
                if display:
                    self._jetson_health_set("jh_nvpmodel", display)
                if choice and "jh_nvpmodel_select" in self._vars:
                    self._vars["jh_nvpmodel_select"].set(choice)

            self._gui_call(_apply_current)

        threading.Thread(target=_worker, daemon=True).start()

    def _confirm_nvpmodel_reboot(self, mode_id: str, choice: str) -> bool:
        target = choice or f"ID {mode_id}"
        return messagebox.askokcancel(
            title="Apply Power Mode",
            message=(
                f"Applying {target} will immediately reboot this Jetson.\n\n"
                "Press OK to apply the mode and reboot now.\n"
                "Press Cancel to leave the current mode unchanged."
            ),
            parent=self.root,
        )

    def _jetson_health_apply_nvpmodel(self):
        if self._jetson_nvpmodel_busy:
            logging.warning("JET: nvpmodel mode change already in progress")
            return

        choice_var = self._vars.get("jh_nvpmodel_select")
        choice = choice_var.get().strip() if choice_var is not None else ""
        mode_id = self._jetson_nvpmodel_id_from_choice(choice)
        if mode_id is None:
            logging.error("JET: select a valid nvpmodel mode before applying")
            return

        if not self._confirm_nvpmodel_reboot(mode_id, choice):
            logging.info("JET: nvpmodel mode change cancelled")
            return

        self._jetson_nvpmodel_busy = True
        logging.warning("JET: applying nvpmodel mode %s and rebooting now", choice or mode_id)

        def _worker():
            display = self._jetson_nvpmodel_choice_for_id(mode_id) or f"ID {mode_id}"
            try:
                result = self.host.set_power_mode(mode_id)
                if not result.get("ok"):
                    logging.error(
                        "JET: failed to set nvpmodel mode %s (%s): %s",
                        mode_id,
                        result.get("error_code") or "unknown",
                        result.get("detail") or "no detail",
                    )
                    return

                def _apply_result():
                    if display:
                        self._jetson_health_set("jh_nvpmodel", display)
                    if "jh_nvpmodel_select" in self._vars:
                        self._vars["jh_nvpmodel_select"].set(display)

                self._gui_call(_apply_result)
                logging.info(
                    "JET: nvpmodel accepted %s; reboot in progress. %s",
                    display,
                    result.get("detail") or "",
                )
            except Exception as e:
                logging.error(f"JET: failed to set nvpmodel mode {mode_id}: {e}")
                return
            finally:
                self._gui_call(setattr, self, "_jetson_nvpmodel_busy", False)

        threading.Thread(target=_worker, daemon=True).start()

    def _jetson_health_poll_tegrastats(self):
        """Update power rows on demand using a one-shot tegrastats snapshot."""
        def _worker():
            import datetime

            rails, _temps = self.host.get_power_snapshot()
            queried = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data = {
                "jh_pwr_name_1": "VDD_IN",
                "jh_pwr_val_1": rails.get("VDD_IN", "-"),
                "jh_tegrastats_last": f"Last queried: {queried}",
            }
            other = [name for name in sorted(rails.keys()) if name != "VDD_IN"]

            if other:
                data["jh_pwr_name_2"] = other[0]
                data["jh_pwr_val_2"] = rails.get(other[0], "-")
            else:
                data["jh_pwr_name_2"] = "Rail 1"
                data["jh_pwr_val_2"] = "-"

            if len(other) > 1:
                data["jh_pwr_name_3"] = other[1]
                data["jh_pwr_val_3"] = rails.get(other[1], "-")
            else:
                data["jh_pwr_name_3"] = "Rail 2"
                data["jh_pwr_val_3"] = "-"

            self._gui_call(self._jetson_health_apply, data)

        threading.Thread(target=_worker, daemon=True).start()

    def _build_jetson_health_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        def _readout(parent, row, label, key, columnspan=1):
            value = tk.StringVar(value="-")
            self._vars[key] = value
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=2)
            entry = ttk.Entry(parent, textvariable=value, state="readonly")
            entry.grid(row=row, column=1, columnspan=columnspan, sticky="ew", padx=5, pady=2)
            self._bind_copy_menu(entry, value)

        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 3))
        header.columnconfigure(0, weight=1)
        self._host_manager_summary_var = tk.StringVar(value="Waiting for HostManager retained status")
        ttk.Label(header, textvariable=self._host_manager_summary_var, foreground="darkblue").grid(row=0, column=0, sticky="w")

        top = ttk.Frame(frame)
        top.grid(row=1, column=0, sticky="ew", padx=4, pady=2)
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        top.rowconfigure(0, weight=1)
        top.rowconfigure(1, weight=1)

        system = ttk.LabelFrame(top, text="System")
        system.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 2))
        system.columnconfigure(1, weight=1)
        for row, (label, key) in enumerate((
            ("Host", "jh_host"),
            ("Platform", "jh_platform"),
            ("Python", "jh_python"),
            ("CPU Usage", "jh_cpu_usage"),
            ("Load (1/5/15m)", "jh_load"),
            ("Memory", "jh_ram"),
            ("Swap", "jh_swap"),
            ("Storage", "jh_disk"),
            ("Host Uptime", "jh_uptime"),
            ("Service Uptime", "jh_service_uptime"),
        )):
            _readout(system, row, label, key)

        polling = ttk.LabelFrame(top, text="HostManager Polling")
        polling.grid(row=0, column=1, sticky="nsew", padx=(2, 0))
        polling.columnconfigure(1, weight=1)
        self._vars["jh_live_updates"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            polling,
            text="Live display updates",
            variable=self._vars["jh_live_updates"],
            command=self._host_manager_toggle_updates,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(8, 5))
        ttk.Label(polling, text="Service interval (s)").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self._vars["jh_status_interval_s"] = tk.DoubleVar(value=1.0)
        ttk.Spinbox(
            polling,
            from_=0.1,
            to=60.0,
            increment=0.1,
            textvariable=self._vars["jh_status_interval_s"],
            width=8,
        ).grid(row=1, column=1, sticky="w", padx=6, pady=4)
        ttk.Button(polling, text="Apply Interval", command=self._host_manager_set_interval).grid(
            row=2, column=0, sticky="ew", padx=6, pady=4
        )
        ttk.Button(polling, text="Refresh Now", command=self._host_manager_refresh).grid(
            row=2, column=1, sticky="ew", padx=6, pady=4
        )

        network = ttk.LabelFrame(top, text="Network")
        network.grid(row=1, column=1, sticky="nsew", padx=(2, 0), pady=(2, 0))
        network.columnconfigure(1, weight=1)
        for row, (label, key) in enumerate((
            ("Primary Interface", "jh_net_interface"),
            ("Link", "jh_net_status"),
            ("MAC", "jh_net_mac"),
            ("Traffic", "jh_net_traffic"),
        )):
            _readout(network, row, label, key)

        details = ttk.Notebook(frame)
        details.grid(row=3, column=0, sticky="nsew", padx=4, pady=2)

        thermal = ttk.Frame(details)
        details.add(thermal, text="Thermal")
        thermal.columnconfigure(0, weight=1)
        thermal.rowconfigure(0, weight=1)
        self._host_manager_thermal_tree = ttk.Treeview(
            thermal,
            columns=("zone", "current", "trip_points", "error"),
            show="headings",
            height=7,
        )
        for key, label, width in (
            ("zone", "Zone", 150),
            ("current", "Current", 80),
            ("trip_points", "Trip Points", 280),
            ("error", "Error", 180),
        ):
            self._host_manager_thermal_tree.heading(key, text=label)
            self._host_manager_thermal_tree.column(key, width=width, anchor="w")
        self._host_manager_thermal_tree.grid(row=0, column=0, sticky="nsew")
        thermal_scrollbar = ttk.Scrollbar(
            thermal, orient="vertical", command=self._host_manager_thermal_tree.yview
        )
        thermal_scrollbar.grid(row=0, column=1, sticky="ns")
        thermal_x_scrollbar = ttk.Scrollbar(
            thermal, orient="horizontal", command=self._host_manager_thermal_tree.xview
        )
        thermal_x_scrollbar.grid(row=1, column=0, sticky="ew")
        self._host_manager_thermal_tree.configure(
            yscrollcommand=thermal_scrollbar.set,
            xscrollcommand=thermal_x_scrollbar.set,
        )

        cores = ttk.Frame(details)
        details.add(cores, text="CPU Cores")
        cores.columnconfigure(0, weight=1)
        cores.rowconfigure(0, weight=1)
        self._host_manager_cores_tree = ttk.Treeview(
            cores,
            columns=("core", "usage", "frequency"),
            show="headings",
            height=7,
        )
        for key, label, width in (
            ("core", "Core", 100),
            ("usage", "Usage", 120),
            ("frequency", "Frequency", 150),
        ):
            self._host_manager_cores_tree.heading(key, text=label)
            self._host_manager_cores_tree.column(key, width=width, anchor="w")
        self._host_manager_cores_tree.grid(row=0, column=0, sticky="nsew")
        cores_scrollbar = ttk.Scrollbar(
            cores, orient="vertical", command=self._host_manager_cores_tree.yview
        )
        cores_scrollbar.grid(row=0, column=1, sticky="ns")
        cores_x_scrollbar = ttk.Scrollbar(
            cores, orient="horizontal", command=self._host_manager_cores_tree.xview
        )
        cores_x_scrollbar.grid(row=1, column=0, sticky="ew")
        self._host_manager_cores_tree.configure(
            yscrollcommand=cores_scrollbar.set,
            xscrollcommand=cores_x_scrollbar.set,
        )

        interfaces_frame = ttk.Frame(details)
        details.add(interfaces_frame, text="Interfaces")
        interfaces_frame.columnconfigure(0, weight=1)
        interfaces_frame.rowconfigure(0, weight=1)
        self._host_manager_interfaces_tree = ttk.Treeview(
            interfaces_frame,
            columns=("name", "state", "speed", "mtu", "mac", "traffic", "errors"),
            show="headings",
            height=7,
        )
        for key, label, width in (
            ("name", "Interface", 90),
            ("state", "State", 65),
            ("speed", "Speed", 80),
            ("mtu", "MTU", 60),
            ("mac", "MAC", 125),
            ("traffic", "Traffic", 170),
            ("errors", "Errors / Drops", 150),
        ):
            self._host_manager_interfaces_tree.heading(key, text=label)
            self._host_manager_interfaces_tree.column(key, width=width, anchor="w")
        self._host_manager_interfaces_tree.grid(row=0, column=0, sticky="nsew")
        interfaces_scrollbar = ttk.Scrollbar(
            interfaces_frame,
            orient="vertical",
            command=self._host_manager_interfaces_tree.yview,
        )
        interfaces_scrollbar.grid(row=0, column=1, sticky="ns")
        interfaces_x_scrollbar = ttk.Scrollbar(
            interfaces_frame,
            orient="horizontal",
            command=self._host_manager_interfaces_tree.xview,
        )
        interfaces_x_scrollbar.grid(row=1, column=0, sticky="ew")
        self._host_manager_interfaces_tree.configure(
            yscrollcommand=interfaces_scrollbar.set,
            xscrollcommand=interfaces_x_scrollbar.set,
        )

        power = ttk.LabelFrame(frame, text="Power and Jetson")
        power.grid(row=2, column=0, sticky="ew", padx=4, pady=2)
        power.columnconfigure(1, weight=1)
        power.columnconfigure(2, weight=1)
        self._vars["jh_power_mode"] = tk.StringVar(value="-")
        ttk.Label(power, text="Current Mode").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        current_mode = ttk.Entry(power, textvariable=self._vars["jh_power_mode"], state="readonly")
        current_mode.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(current_mode, self._vars["jh_power_mode"])
        self._vars["jh_power_mode_select"] = tk.StringVar(value="")
        self._host_manager_power_mode_combo = ttk.Combobox(
            power,
            textvariable=self._vars["jh_power_mode_select"],
            values=(),
            state="readonly",
        )
        self._host_manager_power_mode_combo.grid(row=0, column=2, sticky="ew", padx=5, pady=2)
        self._host_manager_power_mode_button = ttk.Button(
            power,
            text="Apply",
            command=self._host_manager_apply_power_mode,
        )
        self._host_manager_power_mode_button.grid(row=0, column=3, sticky="ew", padx=5, pady=2)
        self._host_manager_power_mode_ids = {}
        self._jetson_nvpmodel_busy = False
        for row, (label, key) in enumerate((
            ("Default Mode", "jh_power_default"),
            ("GPU Usage", "jh_gpu_usage"),
            ("Rail 1", "jh_power_rail_1"),
            ("Rail 2", "jh_power_rail_2"),
            ("Rail 3", "jh_power_rail_3"),
        ), start=1):
            _readout(power, row, label, key, columnspan=3)

        self._host_manager_render()

    def _host_manager_apply_announce(self, data: dict):
        if not isinstance(data, dict):
            return
        identity = data.get("identity")
        if isinstance(identity, dict) and identity.get("hostname"):
            title = f"{identity['hostname']} MEP"
            if self.root.title() != title:
                self.root.title(title)
        if self._is_adv_tab_selected("JET"):
            self._host_manager_render()

    def _host_manager_apply_status(self, data: dict):
        if not isinstance(data, dict):
            return
        if not self._vars.get("jh_live_updates", tk.BooleanVar(value=True)).get():
            return
        if not self._is_adv_tab_selected("JET"):
            return
        identity = data.get("identity")
        if isinstance(identity, dict) and identity.get("hostname"):
            title = f"{identity['hostname']} MEP"
            if self.root.title() != title:
                self.root.title(title)
        self._host_manager_render()

    def _host_manager_toggle_updates(self):
        if self._vars["jh_live_updates"].get():
            self._host_manager_render()
        else:
            self._host_manager_summary_var.set("HostManager display paused")

    def _host_manager_set_interval(self):
        try:
            interval_s = float(self._vars["jh_status_interval_s"].get())
        except (tk.TclError, TypeError, ValueError):
            messagebox.showerror("HostManager Polling", "Interval must be between 0.1 and 60 seconds")
            return
        if not 0.1 <= interval_s <= 60.0:
            messagebox.showerror("HostManager Polling", "Interval must be between 0.1 and 60 seconds")
            return
        self.host_manager.set_status_interval(
            interval_s,
            callback=lambda response: self._gui_call(self._host_manager_interval_response, response),
        )

    def _host_manager_interval_response(self, response):
        if not response.get("success"):
            messagebox.showerror("HostManager Polling", response.get("error") or "Could not set polling interval")
            return
        status_data = response.get("status_data")
        if isinstance(status_data, dict) and status_data.get("status_interval_s") is not None:
            self._vars["jh_status_interval_s"].set(float(status_data["status_interval_s"]))

    def _host_manager_refresh(self):
        self.host_manager.refresh(
            callback=lambda response: self._gui_call(self._host_manager_refresh_response, response)
        )

    def _host_manager_refresh_response(self, response):
        if not response.get("success"):
            logging.error("HostManager refresh failed: %s", response.get("error") or "unknown error")
            return
        status_data = response.get("status_data")
        if isinstance(status_data, dict):
            self._host_manager_render(status_data)

    def _host_manager_render(self, status_override=None):
        if not hasattr(self, "_host_manager_summary_var") or "jh_host" not in self._vars:
            return
        status = status_override if isinstance(status_override, dict) else self.host_manager.get_status()
        announce = self.host_manager.get_announce()
        identity = status.get("identity") if isinstance(status.get("identity"), dict) else {}
        cpu = status.get("cpu") if isinstance(status.get("cpu"), dict) else {}
        load = status.get("load") if isinstance(status.get("load"), dict) else {}
        memory = status.get("memory") if isinstance(status.get("memory"), dict) else {}
        swap = status.get("swap") if isinstance(status.get("swap"), dict) else {}
        disk = status.get("disk") if isinstance(status.get("disk"), dict) else {}
        network = status.get("network") if isinstance(status.get("network"), dict) else {}
        thermal = status.get("thermal") if isinstance(status.get("thermal"), dict) else {}
        power_mode = status.get("power_mode") if isinstance(status.get("power_mode"), dict) else {}
        platform = status.get("platform") if isinstance(status.get("platform"), dict) else {}

        self._jetson_health_set("jh_host", identity.get("hostname"))
        platform_text = " ".join(str(value) for value in (identity.get("system"), identity.get("release"), identity.get("machine")) if value)
        self._jetson_health_set("jh_platform", platform_text or platform.get("name"))
        self._jetson_health_set("jh_python", identity.get("python"))
        cpu_percent = cpu.get("used_percent")
        self._jetson_health_set("jh_cpu_usage", f"{cpu_percent:.1f}%" if isinstance(cpu_percent, (int, float)) else None)
        self._jetson_health_set("jh_load", " / ".join(self._host_manager_number(load.get(key)) for key in ("1m", "5m", "15m")))
        self._jetson_health_set("jh_ram", self._host_manager_usage(memory))
        self._jetson_health_set("jh_swap", self._host_manager_usage(swap))
        self._jetson_health_set("jh_disk", self._host_manager_usage(disk))
        self._jetson_health_set("jh_uptime", self._host_manager_duration(status.get("host_uptime_seconds")))
        self._jetson_health_set("jh_service_uptime", self._host_manager_duration(status.get("uptime_seconds")))

        interface_name = network.get("default_interface")
        interfaces = network.get("interfaces") if isinstance(network.get("interfaces"), list) else []
        interface = next((item for item in interfaces if isinstance(item, dict) and item.get("name") == interface_name), {})
        self._jetson_health_set("jh_net_interface", interface_name)
        speed = interface.get("speed_mbps")
        link = interface.get("state") or "unknown"
        self._jetson_health_set("jh_net_status", f"{link}; {speed} Mbps" if speed is not None else link)
        self._jetson_health_set("jh_net_mac", interface.get("mac"))
        statistics = interface.get("statistics") if isinstance(interface.get("statistics"), dict) else {}
        self._jetson_health_set(
            "jh_net_traffic",
            f"RX {self._cap_format_bytes(statistics.get('rx_bytes'))} | TX {self._cap_format_bytes(statistics.get('tx_bytes'))}",
        )
        interface_rows = []
        for item in interfaces:
            if not isinstance(item, dict):
                continue
            item_statistics = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
            interface_rows.append((
                str(item.get("name") or len(interface_rows)),
                (
                    item.get("name") or "-",
                    item.get("state") or "-",
                    f"{item['speed_mbps']} Mbps" if item.get("speed_mbps") is not None else "-",
                    item.get("mtu") if item.get("mtu") is not None else "-",
                    item.get("mac") or "-",
                    f"RX {self._cap_format_bytes(item_statistics.get('rx_bytes'))} | TX {self._cap_format_bytes(item_statistics.get('tx_bytes'))}",
                    (
                        f"RX {item_statistics.get('rx_errors') or 0}/{item_statistics.get('rx_dropped') or 0} | "
                        f"TX {item_statistics.get('tx_errors') or 0}/{item_statistics.get('tx_dropped') or 0}"
                    ),
                ),
            ))
        self._host_manager_update_tree(self._host_manager_interfaces_tree, interface_rows)

        zones = thermal.get("zones") if isinstance(thermal.get("zones"), list) else []
        thermal_rows = []
        for index, zone in enumerate(zones):
            if not isinstance(zone, dict):
                continue
            temperature = zone.get("temperature_c")
            trip_points = []
            for key, value in sorted(zone.items()):
                if key.startswith("trip_point_") and key.endswith("_temp"):
                    trip_points.append(
                        f"{key.removeprefix('trip_point_').removesuffix('_temp')}: {value:.1f} C"
                        if isinstance(value, (int, float))
                        else f"{key}: -"
                    )
            thermal_rows.append((
                f"zone-{index}",
                (
                    zone.get("name") or f"thermal_zone{index}",
                    f"{temperature:.1f} C" if isinstance(temperature, (int, float)) else "-",
                    ", ".join(trip_points) if trip_points else "-",
                    zone.get("error") or "-",
                ),
            ))
        self._host_manager_update_tree(self._host_manager_thermal_tree, thermal_rows)

        core_rows = []
        cpu_cores = status.get("cpu_cores") if isinstance(status.get("cpu_cores"), list) else []
        for index, core in enumerate(cpu_cores):
            if not isinstance(core, dict):
                continue
            core_id = core.get("id", index)
            usage = core.get("used_percent")
            frequency = core.get("frequency_mhz")
            core_rows.append((
                f"core-{core_id}",
                (
                    f"CPU {core_id}",
                    f"{usage:.1f}%" if isinstance(usage, (int, float)) else "-",
                    f"{frequency:.1f} MHz" if isinstance(frequency, (int, float)) else "-",
                ),
            ))
        self._host_manager_update_tree(self._host_manager_cores_tree, core_rows)

        available_modes = power_mode.get("available") if isinstance(power_mode.get("available"), list) else []
        mode_choices = [
            f"{mode.get('id')}: {mode.get('name')}"
            for mode in available_modes
            if isinstance(mode, dict) and mode.get("id") is not None
        ]
        self._host_manager_power_mode_ids = {
            choice: choice.split(":", 1)[0].strip() for choice in mode_choices
        }
        if tuple(self._host_manager_power_mode_combo.cget("values")) != tuple(mode_choices):
            self._host_manager_power_mode_combo.configure(values=mode_choices)
        current_name = power_mode.get("current")
        current_id = power_mode.get("current_id")
        if current_id is None and current_name:
            current_id = next(
                (
                    mode.get("id")
                    for mode in available_modes
                    if isinstance(mode, dict) and mode.get("name") == current_name
                ),
                None,
            )
        current_display = (
            f"{current_id}: {current_name}"
            if current_id is not None and current_name
            else str(current_name or current_id or "-")
        )
        self._jetson_health_set("jh_power_mode", current_display)
        selected_mode = self._vars["jh_power_mode_select"].get()
        if selected_mode not in self._host_manager_power_mode_ids:
            preferred = next(
                (choice for choice, mode_id in self._host_manager_power_mode_ids.items() if mode_id == str(current_id)),
                mode_choices[0] if mode_choices else "",
            )
            self._vars["jh_power_mode_select"].set(preferred)
        desired_button_state = "normal" if mode_choices and not self._jetson_nvpmodel_busy else "disabled"
        if str(self._host_manager_power_mode_button.cget("state")) != desired_button_state:
            self._host_manager_power_mode_button.configure(state=desired_button_state)
        self._jetson_health_set("jh_power_default", power_mode.get("default_id"))
        gpu = platform.get("gpu") if isinstance(platform.get("gpu"), dict) else {}
        gpu_percent = gpu.get("utilization_percent")
        self._jetson_health_set("jh_gpu_usage", f"{gpu_percent:.1f}%" if isinstance(gpu_percent, (int, float)) else None)
        power = platform.get("power") if isinstance(platform.get("power"), dict) else {}
        rails = power.get("rails") if isinstance(power.get("rails"), list) else []
        for index in range(3):
            rail = rails[index] if index < len(rails) and isinstance(rails[index], dict) else {}
            self._jetson_health_set(f"jh_power_rail_{index + 1}", self._host_manager_power_rail(rail))

        state = status.get("state") or ("announced" if announce else "unavailable")
        seq = status.get("seq")
        suffix = f"; sample {seq}" if seq is not None else ""
        self._host_manager_summary_var.set(f"HostManager: {state}{suffix}")

    def _host_manager_apply_power_mode(self):
        if self._jetson_nvpmodel_busy:
            return
        choice = self._vars["jh_power_mode_select"].get()
        mode_id = self._host_manager_power_mode_ids.get(choice)
        if mode_id is None:
            messagebox.showerror("Power Mode", "Select an available power mode")
            return
        if not self._confirm_nvpmodel_reboot(mode_id, choice):
            return
        self._jetson_nvpmodel_busy = True
        self._host_manager_power_mode_button.configure(state="disabled")
        self._host_manager_summary_var.set(f"HostManager: applying power mode {choice}")
        self.host_manager.set_power_mode(
            mode_id,
            callback=lambda response: self._gui_call(self._host_manager_power_mode_response, response),
        )

    def _host_manager_power_mode_response(self, response):
        self._jetson_nvpmodel_busy = False
        status_data = response.get("status_data") if isinstance(response, dict) else None
        succeeded = bool(response.get("success") and isinstance(status_data, dict) and status_data.get("ok"))
        if not succeeded:
            detail = (
                status_data.get("detail")
                if isinstance(status_data, dict)
                else response.get("error") if isinstance(response, dict) else None
            )
            messagebox.showerror("Power Mode", detail or "Could not apply the selected power mode")
            self._host_manager_render()
            return
        logging.warning("JET: HostManager accepted power mode %s", status_data.get("mode_id"))
        self._host_manager_summary_var.set("HostManager: power mode accepted; reboot may be in progress")

    @staticmethod
    def _host_manager_update_tree(tree, rows):
        desired_ids = {iid for iid, _ in rows}
        for index, (iid, values) in enumerate(rows):
            if tree.exists(iid):
                current_values = tuple(str(value) for value in tree.item(iid, "values"))
                desired_values = tuple(str(value) for value in values)
                if current_values != desired_values:
                    tree.item(iid, values=values)
                if tree.index(iid) != index:
                    tree.move(iid, "", index)
            else:
                tree.insert("", "end", iid=iid, values=values)
        for iid in tree.get_children():
            if iid not in desired_ids:
                tree.delete(iid)

    @staticmethod
    def _host_manager_number(value):
        return f"{value:.2f}" if isinstance(value, (int, float)) else "-"

    @classmethod
    def _host_manager_usage(cls, value):
        used = value.get("used_bytes")
        total = value.get("total_bytes")
        percent = value.get("used_percent")
        if not isinstance(used, (int, float)) or not isinstance(total, (int, float)):
            return "-"
        suffix = f" ({percent:.1f}%)" if isinstance(percent, (int, float)) else ""
        return f"{cls._cap_format_bytes(used)} / {cls._cap_format_bytes(total)}{suffix}"

    @staticmethod
    def _host_manager_duration(value):
        if not isinstance(value, (int, float)):
            return "-"
        seconds = int(value)
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, _ = divmod(seconds, 60)
        return f"{days}d {hours:02d}:{minutes:02d}" if days else f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def _host_manager_power_rail(rail):
        if not rail:
            return "-"
        name = rail.get("name") or "Rail"
        power_mw = rail.get("power_mw")
        voltage_mv = rail.get("voltage_mv")
        current_ma = rail.get("current_ma")
        values = [name]
        if isinstance(power_mw, (int, float)):
            values.append(f"{power_mw / 1000:.2f} W")
        if isinstance(voltage_mv, (int, float)):
            values.append(f"{voltage_mv} mV")
        if isinstance(current_ma, (int, float)):
            values.append(f"{current_ma} mA")
        return " | ".join(values)

    def _build_soc_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)

        def _ro_row(parent, row, label, key, unit=""):
            sv = tk.StringVar(value="—")
            self._vars[key] = sv
            ttk.Label(parent, text=label).grid(
                row=row, column=0, sticky="w", padx=5, pady=2)
            e = ttk.Entry(parent, textvariable=sv, state="readonly", width=18)
            e.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
            self._bind_copy_menu(e, sv)
            if unit:
                ttk.Label(parent, text=unit, foreground="grey").grid(
                    row=row, column=2, sticky="w")

        # ── Status ────────────────────────────────────────────────────────
        # Read-only. Updated by live rfsoc/status; Refresh Status forces an explicit re-query.
        st_f = ttk.LabelFrame(frame, text="Current Status")
        st_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        st_f.columnconfigure(1, weight=1)
        for key in ("soc_state", "soc_state_RX", "soc_state_TX"):
            self._vars[key] = tk.StringVar(value="—")
        state_row = ttk.Frame(st_f)
        state_row.grid(row=0, column=0, columnspan=3, sticky="ew", padx=5, pady=2)
        for column in range(3):
            state_row.columnconfigure(column * 2 + 1, weight=1)
        for column, (label, key) in enumerate((
            ("State", "soc_state"),
            ("State_RX", "soc_state_RX"),
            ("State_TX", "soc_state_TX"),
        )):
            ttk.Label(state_row, text=label).grid(
                row=0, column=column * 2, sticky="w", padx=(0, 4)
            )
            state_value = ttk.Entry(
                state_row, textvariable=self._vars[key], state="readonly", width=12
            )
            state_value.grid(row=0, column=column * 2 + 1, sticky="ew", padx=(0, 8))
            self._bind_copy_menu(state_value, self._vars[key])
        _ro_row(st_f, 1, "Center Freq Metadata", "soc_fc",  "MHz")
        _ro_row(st_f, 2, "NCO Frequency (IF)",   "soc_fif", "MHz")
        _ro_row(st_f, 3, "Sample Rate",          "soc_fs",  "MHz")
        _ro_row(st_f, 4, "PPS Count",            "soc_pps")
        _ro_row(st_f, 5, "Active Channels",      "soc_channels")
        _ro_row(st_f, 6, "PPS Publish Interval", "soc_pps_publish_interval", "s")
        ctrl_row = ttk.Frame(st_f)
        ctrl_row.grid(row=7, column=0, columnspan=3, sticky="w", padx=5, pady=(4, 4))
        refresh_btn = ttk.Button(st_f, text="Refresh Status", command=self._soc_refresh)
        refresh_btn = ttk.Button(ctrl_row, text="Refresh Status", command=self._soc_refresh)
        refresh_btn.pack(side="left")
        self._add_tooltip(
            refresh_btn,
            "MQTT: {\"task_name\": \"get\", \"arguments\": [\"tlm\"]}\n\n"
            "Requests the RFSoC to re-publish its current state to rfsoc/status. "
            "Status also updates passively on every RFSoC event.",
        )

        # ── Manual Control ────────────────────────────────────────────────
        mc_f = ttk.LabelFrame(frame, text="Manual Control")
        mc_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        mc_f.columnconfigure(1, weight=1)

        ttk.Label(mc_f, text="NCO Frequency (MHz)").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["soc_if_test"] = tk.StringVar(value="1090")
        ttk.Entry(mc_f, textvariable=self._vars["soc_if_test"], width=12).grid(
            row=0, column=1, sticky="ew", padx=5, pady=3)
        nco_btn = ttk.Button(mc_f, text="Set", command=self._soc_set_if_test)
        nco_btn.grid(row=0, column=2, padx=5, pady=3, sticky="ew")
        self._add_tooltip(
            nco_btn,
            "MQTT: {\"task_name\": \"set\", \"arguments\": \"freq_IF <MHz>\"}\n\n"
            "Tunes the ADC digital mixer (NCO) on all tiles to -freq_IF. "
            "Also updates the sample rate metadata register. "
            "Avoid changing during an active sweep.",
        )

        ttk.Label(mc_f, text="Freq Metadata (MHz)").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        self._vars["soc_freq_metadata"] = tk.StringVar(value="")
        ttk.Entry(mc_f, textvariable=self._vars["soc_freq_metadata"], width=12).grid(
            row=1, column=1, sticky="ew", padx=5, pady=3)
        meta_btn = ttk.Button(mc_f, text="Set", command=self._soc_set_freq_metadata)
        meta_btn.grid(row=1, column=2, padx=5, pady=3, sticky="ew")
        self._add_tooltip(
            meta_btn,
            "MQTT: {\"task_name\": \"set\", \"arguments\": \"freq_metadata <Hz>\"}\n\n"
            "Updates the frequency tag written into UDP packet headers, "
            "independently of the NCO. Used when an external tuner shifts "
            "the true RF center frequency. Enter MHz; converted to Hz before sending.",
        )

        ttk.Label(mc_f, text="PPS Publish Interval (s)").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        self._vars["soc_pps_publish_interval_set"] = tk.IntVar(value=30)
        ttk.Entry(mc_f, textvariable=self._vars["soc_pps_publish_interval_set"], width=12).grid(
            row=2, column=1, sticky="ew", padx=5, pady=3)
        pps_pub_btn = ttk.Button(mc_f, text="Set", command=self._soc_set_pps_publish_interval)
        pps_pub_btn.grid(row=2, column=2, padx=5, pady=3, sticky="ew")
        self._add_tooltip(
            pps_pub_btn,
            "MQTT: {\"task_name\": \"set_pps_publish_interval\", \"arguments\": <seconds>}\n\n"
            "Sets periodic PPS-status publish interval on RFSoC. "
            "Use 0 to disable periodic PPS publish.",
        )

        self._vars["soc_ch_A"] = tk.BooleanVar(value=False)
        self._vars["soc_ch_B"] = tk.BooleanVar(value=False)
        self._vars["soc_ch_C"] = tk.BooleanVar(value=False)
        self._vars["soc_ch_D"] = tk.BooleanVar(value=False)

        ttk.Label(mc_f, text="Channels").grid(row=4, column=0, sticky="nw", padx=5, pady=(6, 2))
        cb_row = ttk.Frame(mc_f)
        cb_row.grid(row=4, column=1, columnspan=2, sticky="w", padx=5, pady=(6, 2))
        for i, ch in enumerate(("A", "B", "C", "D")):
            port = RECORDER_CHANNEL_PORTS.get(ch, "?")
            cb = ttk.Checkbutton(
                cb_row,
                text=f"{ch} ({port})",
                variable=self._vars[f"soc_ch_{ch}"],
            )
            cb.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 12), pady=(0, 2))

        ch_set_btn = ttk.Button(mc_f, text="Set", command=self._soc_set_channels)
        ch_set_btn.grid(row=4, column=2, sticky="se", padx=5, pady=(6, 2))
        self._add_tooltip(
            ch_set_btn,
            "MQTT: {\"task_name\": \"set\", \"arguments\": \"channel <A,B,...>\"}\n\n"
            "Sets which ADC channels stream UDP packets. Multiple channels are "
            "supported (e.g. A,B). Sending this command resets the FPGA control "
            "register — restart the UDP stream after setting.",
        )
        ttk.Label(
            mc_f,
            text="remember to 'set' desired changes before pressing 'start'",
            foreground="grey", font=("TkDefaultFont", 8),
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 4))
        # ── UDP Stream ────────────────────────────────────────────────────
        # Controls whether the FPGA ADC-to-UDP IP cores are streaming packets.
        udp_f = ttk.LabelFrame(frame, text="UDP Stream")
        udp_f.grid(row=2, column=0, padx=4, pady=(2, 6), sticky="ew")
        udp_f.columnconfigure(0, weight=1)
        udp_f.columnconfigure(1, weight=1)

        self._vars["soc_start_mode"] = tk.StringVar(value="pps")
        mode_row = ttk.Frame(udp_f)
        mode_row.grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=(6, 2))
        ttk.Radiobutton(
            mode_row, text="PPS Sync",
            variable=self._vars["soc_start_mode"], value="pps",
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            mode_row, text="Immediate",
            variable=self._vars["soc_start_mode"], value="immediate",
        ).pack(side="left")
        ttk.Label(
            mode_row, text="← start mode",
            foreground="grey", font=("TkDefaultFont", 8),
        ).pack(side="left", padx=(8, 0))

        start_btn = ttk.Button(udp_f, text="Start", command=self._soc_start_stream)
        start_btn.grid(row=1, column=0, padx=(5, 2), pady=(2, 6), sticky="ew")
        self._add_tooltip(
            start_btn,
            "PPS Sync — MQTT: {\"task_name\": \"capture_next_pps\"}\n"
            "Arms the FPGA to begin streaming on the next GPS PPS pulse. "
            "Calculates sample index offset for the next UTC second boundary. "
            "Requires GPS lock for accurate timestamps.\n\n"
            "Immediate — MQTT: {\"task_name\": \"capture\"}\n"
            "Starts streaming instantly with sample index offset = 0. "
            "Use when GPS/PPS is unavailable.",
        )
        stop_btn = ttk.Button(udp_f, text="Stop", command=self._soc_stop_stream)
        stop_btn.grid(row=1, column=1, padx=(2, 5), pady=(2, 6), sticky="ew")
        self._add_tooltip(
            stop_btn,
            "MQTT: {\"task_name\": \"reset\"}\n\n"
            "Writes CTRL=RESET to all active FPGA ADC-to-UDP IP cores. "
            "Stops packet output immediately. Does not affect the recorder.",
        )

        # Populate with cached data so tab shows current state immediately on first open.
        cached = self.mep.rfsoc.get_status()
        if isinstance(cached, dict):
            self._soc_apply(cached)

        # ── PLL Query ───────────────────────────────────────────────────────
        pll_f = ttk.LabelFrame(frame, text="PLL Query")
        pll_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        pll_f.columnconfigure(0, weight=1)

        ctl_row = ttk.Frame(pll_f)
        ctl_row.grid(row=0, column=0, sticky="ew", padx=5, pady=(4, 1))
        ctl_row.columnconfigure(1, weight=1)
        ctl_row.columnconfigure(3, weight=1)

        ttk.Label(ctl_row, text="Converter").grid(row=0, column=0, sticky="w", padx=(0, 4), pady=0)
        self._vars["soc_pll_converter"] = tk.StringVar(value="ADC")
        ttk.Combobox(
            ctl_row,
            textvariable=self._vars["soc_pll_converter"],
            values=["ADC", "DAC"],
            state="readonly",
            width=8,
        ).grid(row=0, column=1, sticky="w", padx=(0, 8), pady=0)

        ttk.Label(ctl_row, text="Tile").grid(row=0, column=2, sticky="w", padx=(0, 4), pady=0)
        self._vars["soc_pll_tile"] = tk.StringVar(value="0")
        ttk.Combobox(
            ctl_row,
            textvariable=self._vars["soc_pll_tile"],
            values=["0", "1", "2", "3"],
            state="readonly",
            width=8,
        ).grid(row=0, column=3, sticky="w", padx=(0, 8), pady=0)

        ttk.Button(
            ctl_row,
            text="Query",
            command=lambda: self._soc_query_pll(
                self._vars["soc_pll_converter"].get().lower(),
                int(self._vars["soc_pll_tile"].get()),
            ),
        ).grid(row=0, column=4, sticky="e", pady=0)

        self._soc_pll_text = scrolledtext.ScrolledText(
            pll_f,
            height=4,
            wrap="word",
            font=("TkFixedFont", 9),
            background="#f5f5f5",
            exportselection=False,
        )
        self._soc_pll_text.grid(row=1, column=0, sticky="ew", padx=5, pady=(1, 4))
        self._soc_pll_text.insert("1.0", "No PLL data yet. Choose converter/tile and click Query.\n")
        self._bind_copy_menu(self._soc_pll_text, allow_paste=False)

        # Shift rows below PLL section
        mc_f.grid_configure(row=2)
        udp_f.grid_configure(row=3)

        # ---- TX tab ---- #

    def _build_tx_tune_section(self, frame: ttk.Frame, row: int):
        """TX 'Tune' frame: staged center/offset/amplitude/channel plus the one
        status readout worth keeping at a glance (the rest duplicated the staged
        fields and added nothing — see _tx_apply, which still updates the other
        tx_st_* vars for any future consumer, just not shown here).
        """
        def _range_hint(text):
            return dict(text=text, foreground="grey", font=("TkDefaultFont", 8))

        tune_f = ttk.LabelFrame(frame, text="Tune")
        tune_f.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        tune_f.columnconfigure(1, weight=0)

        ttk.Label(tune_f, text="Center Freq (MHz)").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["tx_center_freq"] = tk.StringVar(value="0")
        ttk.Entry(tune_f, textvariable=self._vars["tx_center_freq"], width=10).grid(row=0, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(tune_f, **_range_hint("MHz (no enforced limit)")).grid(row=0, column=2, sticky="w", padx=5, pady=3)
        self._vars["tx_center_freq"].trace_add("write", self._update_synth_lo)

        ttk.Label(tune_f, text="Offset Freq (MHz)").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        self._vars["tx_offset_freq"] = tk.StringVar(value="0")
        ttk.Entry(tune_f, textvariable=self._vars["tx_offset_freq"], width=10).grid(row=1, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(tune_f, **_range_hint(f"|offset| < {TX_OFFSET_FREQ_MAX_MHZ} MHz")).grid(row=1, column=2, sticky="w", padx=5, pady=3)

        ttk.Label(tune_f, text="Amplitude (bins)").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        self._vars["tx_amplitude_bins"] = tk.IntVar(value=0)
        ttk.Spinbox(tune_f, from_=0, to=TX_AMPLITUDE_BINS_MAX, increment=1, textvariable=self._vars["tx_amplitude_bins"], width=8).grid(row=2, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(tune_f, **_range_hint(f"0 to {TX_AMPLITUDE_BINS_MAX}")).grid(row=2, column=2, sticky="w", padx=5, pady=3)

        ttk.Label(tune_f, text="Channel").grid(row=3, column=0, sticky="w", padx=5, pady=3)
        self._vars["tx_channel"] = tk.StringVar(value=TX_CHANNEL_OPTIONS[0])
        ttk.Combobox(tune_f, textvariable=self._vars["tx_channel"], values=list(TX_CHANNEL_OPTIONS), width=8, state="readonly").grid(row=3, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(tune_f, **_range_hint(", ".join(TX_CHANNEL_OPTIONS))).grid(row=3, column=2, sticky="w", padx=5, pady=3)

        # Live status now shown in Transmit Control (above the warning), not here.
        self._vars["tx_st_transmitting"] = tk.StringVar(value="Unknown")

        # No longer displayed (duplicated the staged fields above), but _tx_apply
        # still updates these from live status — keep the vars so it doesn't KeyError.
        self._vars["tx_st_channels"] = tk.StringVar(value="-")
        self._vars["tx_st_center_freq"] = tk.StringVar(value="-")
        self._vars["tx_st_offset_freq"] = tk.StringVar(value="-")
        self._vars["tx_st_amplitude"] = tk.StringVar(value="-")

        # Populate with cached data so the tab shows current state immediately on
        # first open. self.mep doesn't exist yet when this is built eagerly in
        # _build_ui — the live RFSoC status listener (registered later in
        # __init__) populates this shortly after the bus connects either way.
        if hasattr(self, "bus"):
            cached = self.mep.rfsoc.get_status()
            if isinstance(cached, dict):
                self._tx_apply(cached)

    def _build_tx_control_section(self, frame: ttk.Frame, row: int):
        act_f = ttk.LabelFrame(frame, text="Transmit Control")
        act_f.grid(row=row, column=0, padx=10, pady=6, sticky="ew")
        act_f.columnconfigure(0, weight=1)
        act_f.columnconfigure(1, weight=1)

        status_f = ttk.Frame(act_f)
        status_f.grid(row=0, column=0, columnspan=2, sticky="ew", padx=5, pady=(4, 2))
        status_f.columnconfigure(1, weight=1)
        ttk.Label(status_f, text="DAC Status").grid(row=0, column=0, sticky="w")
        status_entry = ttk.Entry(status_f, textvariable=self._vars["tx_st_transmitting"], state="readonly")
        status_entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self._bind_copy_menu(status_entry, self._vars["tx_st_transmitting"])

        ttk.Label(
            act_f,
            text=("\u26a0 TRANSMIT WARNING \u26a0 \nDO NOT TRANSMIT unless you are legally permitted "
      "to do so on the selected frequency and at the selected power. "),
            foreground="#b30000",
            font=("TkDefaultFont", 8, "bold"),
            wraplength=LEFT_PANEL_WIDTH - 110,
            justify="center",
            anchor="center",
        ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=(4, 6))

        ttk.Button(act_f, text="Start / Update", width=14, command=self._tx_start_update_click).grid(
            row=2, column=0, sticky="ew", padx=5, pady=(0, 4))
        ttk.Button(act_f, text="Stop", width=14, command=self._tx_stop_click).grid(
            row=2, column=1, sticky="ew", padx=5, pady=(0, 4))

    # ---- TUN tab ---- #

    def _build_tun_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)

        # Summary
        sum_f = ttk.LabelFrame(frame, text="Tuner Summary")
        sum_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        sum_f.columnconfigure(1, weight=1)

        ttk.Label(sum_f, text="State").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self._vars["tun_state"] = tk.StringVar(value="—")
        _s = ttk.Entry(sum_f, textvariable=self._vars["tun_state"],
                       state="readonly", width=18)
        _s.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_s, self._vars["tun_state"])

        ttk.Label(sum_f, text="Name").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self._vars["tun_name"] = tk.StringVar(value="—")
        _n = ttk.Entry(sum_f, textvariable=self._vars["tun_name"],
                       state="readonly", width=18)
        _n.grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_n, self._vars["tun_name"])

        ttk.Label(sum_f, text="Lock Status (Valon only)").grid(
            row=2, column=0, sticky="w", padx=5, pady=2)
        self._vars["tun_lock_status"] = tk.StringVar(value="N/A")
        self._tun_lock_entry = ttk.Entry(
            sum_f,
            textvariable=self._vars["tun_lock_status"],
            state="readonly",
            width=18,
        )
        self._tun_lock_entry.grid(row=2, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(self._tun_lock_entry, self._vars["tun_lock_status"])
        _lock_get_btn = ttk.Button(sum_f, text="Get", command=self._tun_check_lock)
        _lock_get_btn.grid(row=2, column=2, padx=(2, 5), pady=2, sticky="e")

        # Status dump
        st_f = ttk.LabelFrame(frame, text="Tuner Status (full)")
        st_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        st_f.columnconfigure(0, weight=1)
        self._tun_status_text = scrolledtext.ScrolledText(
            st_f, height=14, wrap="word", font=("TkFixedFont", 9),
            background="#f5f5f5", state="disabled")
        self._tun_status_text.configure(state="normal")
        self._tun_status_text.insert("end", "no status received")
        self._tun_status_text.configure(state="disabled")
        self._tun_status_signature = "no status received"
        self._tun_status_text.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self._tun_status_text.bind("<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A"))
                      else "break")
        self._bind_copy_menu(self._tun_status_text, allow_paste=False)

        # Controls
        ctrl_f = ttk.LabelFrame(frame, text="Manual Control")
        ctrl_f.grid(row=2, column=0, padx=4, pady=(2, 4), sticky="ew")
        ctrl_f.columnconfigure(1, weight=1)

        ttk.Label(ctrl_f, text="Freq (MHz)").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        self._vars["tun_set_freq"] = tk.StringVar(value="")
        self._tun_freq_entry = ttk.Entry(ctrl_f, textvariable=self._vars["tun_set_freq"], width=10)
        self._tun_freq_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(ctrl_f, text="Set",
                   command=self._tun_set_freq).grid(row=0, column=2, padx=2, pady=3)
        ttk.Button(ctrl_f, text="Get",
                   command=self._tun_get_freq).grid(row=0, column=3, padx=2, pady=3)

        ttk.Label(ctrl_f, text="Power (dBm)").grid(
            row=1, column=0, sticky="w", padx=5, pady=3)
        self._vars["tun_set_power"] = tk.StringVar(value="")
        _pw_entry = ttk.Entry(ctrl_f, textvariable=self._vars["tun_set_power"], width=10)
        _pw_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=3)
        _pw_set_btn = ttk.Button(ctrl_f, text="Set", command=self._tun_set_power)
        _pw_set_btn.grid(row=1, column=2, padx=2, pady=3)
        _pw_get_btn = ttk.Button(ctrl_f, text="Get", command=self._tun_get_power)
        _pw_get_btn.grid(row=1, column=3, padx=2, pady=3)
        ttk.Label(ctrl_f, text="(Valon only)",
                  foreground="grey", font=("TkDefaultFont", 8)).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=5, pady=(0, 4))

        ttk.Separator(ctrl_f, orient="horizontal").grid(
            row=3, column=0, columnspan=4, sticky="ew", padx=4, pady=2)

        ttk.Button(ctrl_f, text="Discover Tuners",
                   command=self._tun_discover).grid(
            row=4, column=0, columnspan=4, padx=4, pady=3, sticky="ew")
        ttk.Button(ctrl_f, text="Initialize Tuner",
                   command=self._tun_init).grid(
            row=5, column=0, columnspan=4, padx=4, pady=3, sticky="ew")
        ttk.Button(ctrl_f, text="Get Status",
                   command=self._tun_send_status).grid(
            row=6, column=0, columnspan=4, padx=4, pady=3, sticky="ew")

        self._tuner_power_widgets = [_pw_entry, _pw_set_btn, _pw_get_btn]
        self._tuner_lock_widgets = [_lock_get_btn]
        self._vars["tun_name"].trace_add(
            "write", lambda *_: self._tun_update_capability_buttons())
        self._tun_update_capability_buttons()

        # Populate with cached data so tab shows current state immediately on first open.
        self._tun_refresh()

        # Register tab-specific MQTT → UI. Emit-cached fires inline if data exists.
        # Periodic status (state/tuner) arrives on the status topic; command
        # replies (get_lock_status, get_frequency, get_power) arrive on the dedicated
        # response topic and carry task_name/value.
    # ---- TLM tab ---- #

    def _build_tlm_tab(self, frame: ttk.Frame):
        frame = self._vertical_scroll_tab(frame)
        frame.columnconfigure(0, weight=1)

        def _ro_value(parent, row, col, key, width=24):
            sv = tk.StringVar(value="—")
            self._vars[key] = sv
            e = ttk.Entry(parent, textvariable=sv, state="readonly", width=width)
            e.grid(row=row, column=col, sticky="ew", padx=2, pady=1)
            self._bind_copy_menu(e, sv, allow_paste=False)
            return e

        # ---- GPS ---- #
        gps_f = ttk.LabelFrame(frame, text="GPS")
        gps_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        gps_f.columnconfigure(1, weight=1)
        
        # Left: Display
        left_f = ttk.Frame(gps_f)
        left_f.grid(row=0, column=0, sticky="n", padx=(5, 10), pady=4)
        left_f.columnconfigure(1, weight=1)
        ttk.Label(left_f, text="UTC Time").grid(row=0, column=0, sticky="w", padx=2, pady=1)
        _ro_value(left_f, 0, 1, "tlm_gps_time", width=18)
        ttk.Label(left_f, text="Fix").grid(row=1, column=0, sticky="w", padx=2, pady=1)
        _ro_value(left_f, 1, 1, "tlm_gps_fix", width=18)
        ttk.Label(left_f, text="Lat/Lon").grid(row=2, column=0, sticky="w", padx=2, pady=1)
        _ro_value(left_f, 2, 1, "tlm_gps_latlon", width=18)
        ttk.Label(left_f, text="Speed (kt)").grid(row=3, column=0, sticky="w", padx=2, pady=1)
        _ro_value(left_f, 3, 1, "tlm_gps_speed", width=18)
        
        # Right: Time controls (GPS has no rate command — display only)
        right_f = ttk.Frame(gps_f)
        right_f.grid(row=0, column=1, sticky="n", padx=(10, 5), pady=4)
        right_f.columnconfigure(1, weight=1)
        
        ttk.Label(right_f, text="Time Source", font=("TkDefaultFont", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        self._time_source_frame = ttk.Frame(right_f)
        self._time_source_frame.grid(row=1, column=0, columnspan=2, sticky="w")
        # Radio buttons created dynamically by _apply_afe_announce
        
        ttk.Label(right_f, text="Epoch", font=("TkDefaultFont", 9, "bold")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 2))
        self._epoch_combo = ttk.Combobox(right_f, textvariable=self._vars["epoch_mode"],
                     values=[], state="readonly",
                     width=10)
        self._epoch_combo.grid(row=3, column=0, columnspan=2, sticky="w")
        self._epoch_combo.bind("<<ComboboxSelected>>", lambda _e: self._tlm_apply_time_config())

        details_nb = ttk.Notebook(gps_f, height=190)
        details_nb.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=5, pady=(2, 5))

        fields_tab = ttk.Frame(details_nb, padding=4)
        details_nb.add(fields_tab, text="GNSS")
        fields_tab.columnconfigure(0, weight=1)
        fields_tab.rowconfigure(0, weight=1)
        self._tlm_gps_fields = ttk.Treeview(
            fields_tab,
            columns=("value", "unit"),
            show="tree headings",
            height=7,
        )
        self._tlm_gps_fields.heading("#0", text="Field")
        self._tlm_gps_fields.heading("value", text="Value")
        self._tlm_gps_fields.heading("unit", text="Unit")
        self._tlm_gps_fields.column("#0", width=180, stretch=True)
        self._tlm_gps_fields.column("value", width=150, stretch=True)
        self._tlm_gps_fields.column("unit", width=80, stretch=False)
        fields_scroll = ttk.Scrollbar(fields_tab, orient="vertical", command=self._tlm_gps_fields.yview)
        self._tlm_gps_fields.configure(yscrollcommand=fields_scroll.set)
        self._tlm_gps_fields.grid(row=0, column=0, sticky="nsew")
        fields_scroll.grid(row=0, column=1, sticky="ns")
        self._tlm_gps_field_items = {}
        self._tlm_gps_field_units = {}

        gpsd_tab = ttk.Frame(details_nb, padding=4)
        details_nb.add(gpsd_tab, text="GPSD")
        gpsd_tab.columnconfigure(0, weight=1)

        status_f = ttk.Frame(gpsd_tab)
        status_f.grid(row=0, column=0, sticky="ew")
        for column in range(4):
            status_f.columnconfigure(column, weight=1 if column % 2 else 0)
        status_fields = (
            ("Connected", "tlm_gpsd_connected"),
            ("Endpoint", "tlm_gpsd_endpoint"),
            ("Device", "tlm_gpsd_device"),
            ("Driver", "tlm_gpsd_driver"),
            ("Version", "tlm_gpsd_version"),
            ("Baud", "tlm_gpsd_baud"),
            ("Cycle (s)", "tlm_gpsd_cycle"),
            ("Last RX", "tlm_gpsd_last_rx"),
            ("Last Error", "tlm_gpsd_error"),
        )
        for index, (label, key) in enumerate(status_fields):
            row = index // 2
            pair = index % 2
            ttk.Label(status_f, text=label).grid(row=row, column=pair * 2, sticky="w", padx=(2, 4), pady=1)
            _ro_value(status_f, row, pair * 2 + 1, key, width=15)

        stream_tab = ttk.Frame(details_nb, padding=4)
        details_nb.add(stream_tab, text="Stream")
        stream_tab.columnconfigure(0, weight=1)
        stream_tab.rowconfigure(1, weight=1)

        raw_controls = ttk.Frame(stream_tab)
        raw_controls.grid(row=0, column=0, sticky="ew", pady=(3, 2))
        raw_controls.columnconfigure(0, weight=1)
        self._vars["tlm_raw_state"] = tk.StringVar(value="stopped")
        self._vars["tlm_raw_mode"] = tk.StringVar(value="All")
        self._vars["tlm_raw_duration"] = tk.IntVar(value=10)
        ttk.Label(raw_controls, textvariable=self._vars["tlm_raw_state"], foreground="grey").grid(
            row=0, column=0, sticky="w", padx=(2, 6)
        )
        ttk.Combobox(raw_controls, textvariable=self._vars["tlm_raw_mode"],
                     values=("All", "GNSS", "PMIT"), state="readonly", width=5).grid(
            row=0, column=1, padx=2
        )
        ttk.Label(raw_controls, text="Lease (s)").grid(row=0, column=2, padx=(8, 2))
        ttk.Spinbox(raw_controls, textvariable=self._vars["tlm_raw_duration"],
                    from_=1, to=60, increment=1, width=2).grid(row=0, column=3, padx=2)
        ttk.Button(raw_controls, text="Start", width=5,
                   command=self._tlm_start_raw_stream).grid(
            row=0, column=4, padx=(5, 2)
        )
        ttk.Button(raw_controls, text="Stop", width=5,
                   command=self._tlm_stop_raw_stream).grid(
            row=0, column=5, padx=2
        )
        ttk.Button(raw_controls, text="Clear", width=5,
                   command=lambda: self._tlm_raw_text.delete("1.0", "end")).grid(
            row=0, column=6, padx=(2, 0)
        )

        self._tlm_raw_text = tk.Text(
            stream_tab, height=6, wrap="none", font=("TkFixedFont", 9), exportselection=False
        )
        raw_scroll = ttk.Scrollbar(stream_tab, orient="vertical", command=self._tlm_raw_text.yview)
        self._tlm_raw_text.configure(yscrollcommand=raw_scroll.set)
        self._tlm_raw_text.grid(row=1, column=0, sticky="nsew")
        raw_scroll.grid(row=1, column=1, sticky="ns")
        self._tlm_raw_text.bind(
            "<Key>",
            lambda event: None if (event.state & 0x4 and event.keysym in ("c", "C", "a", "A")) else "break",
        )
        self._bind_copy_menu(self._tlm_raw_text, allow_paste=False)

        self._tlm_gps_update(self._tlm_latest_gps)
        self._tlm_gpsd_update(self._tlm_latest_gpsd)

        # ---- IMU + MAG ---- #
        sensor_f = ttk.LabelFrame(frame, text="IMU + MAG")
        sensor_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        sensor_f.columnconfigure(1, weight=1)
        sensor_f.columnconfigure(2, weight=1)
        sensor_f.columnconfigure(3, weight=1)

        sensor_headers = ("Accelerometer [g]", "Gyroscope [deg/s]", "Magnetometer")
        for column, label in enumerate(sensor_headers, start=1):
            ttk.Label(sensor_f, text=label, font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=column, sticky="w", padx=(7, 6), pady=(4, 2)
            )

        sensor_rows = (
            ("X", "tlm_acc_x", "tlm_gyr_x", "tlm_mag_x"),
            ("Y", "tlm_acc_y", "tlm_gyr_y", "tlm_mag_y"),
            ("Z", "tlm_acc_z", "tlm_gyr_z", "tlm_mag_z"),
            ("ODR", "tlm_imu_acc_odr", "tlm_imu_gyr_odr", None),
            ("CCR", None, None, "tlm_mag_ccr"),
            ("UPDR", None, None, "tlm_mag_updr"),
        )
        for row_index, (label, acc_key, gyr_key, mag_key) in enumerate(sensor_rows, start=1):
            ttk.Label(sensor_f, text=label).grid(
                row=row_index, column=0, sticky="w", padx=(7, 8), pady=1
            )
            for column, key in enumerate((acc_key, gyr_key, mag_key), start=1):
                if key is not None:
                    _ro_value(sensor_f, row_index, column, key, width=12)

        # ---- Housekeeping ---- #
        hk_f = ttk.LabelFrame(frame, text="Housekeeping (HK)")
        hk_f.grid(row=2, column=0, padx=4, pady=(2, 2), sticky="ew")
        hk_f.columnconfigure(0, weight=1)
        hk_f.columnconfigure(1, weight=1)
        hk_f.columnconfigure(2, weight=1)

        hk_items = [
            ("ocxo_locked", "ocxo_locked"),
            ("spi_ok", "spi_ok"),
            ("mag_ok", "mag_ok"),
            ("imu_ok", "imu_ok"),
            ("sw_temp_c", "sw_temp_c"),
            ("mag_temp_c", "mag_temp_c"),
            ("imu_temp_c", "imu_temp_c"),
            ("imu_active", "imu_active"),
            ("imu_tilt", "imu_tilt"),
        ]
        for idx, (label, key) in enumerate(hk_items):
            r = idx // 3
            c = idx % 3
            cell = ttk.Frame(hk_f)
            cell.grid(row=r, column=c, sticky="ew", padx=2, pady=1)
            cell.columnconfigure(1, weight=1)
            ttk.Label(cell, text=label).grid(row=0, column=0, sticky="w", padx=1)
            _ro_value(cell, 0, 1, f"tlm_hk_{key}", width=12)

        # ---- Polling rate for IMU+MAG+HK ---- #
        poll_f = ttk.LabelFrame(frame, text="Polling rate for IMU+MAG+HK")
        poll_f.grid(row=4, column=0, padx=4, pady=(2, 2), sticky="ew")
        poll_f.columnconfigure(1, weight=1)
        ttk.Label(poll_f, text="Interval (s)").grid(row=0, column=0, sticky="w", padx=5, pady=4)
        self._poll_interval_spin = ttk.Spinbox(
            poll_f, from_=1, to=3600, increment=1, textvariable=self._vars["poll_interval_s"], width=8
        )
        self._poll_interval_spin.grid(row=0, column=1, sticky="w", padx=5, pady=4)
        ttk.Button(poll_f, text="Get", width=8,
                   command=self._tlm_get_polling_interval).grid(row=0, column=2, padx=2, pady=4)
        ttk.Button(poll_f, text="Set", width=8,
                   command=self._tlm_set_polling_interval).grid(row=0, column=3, padx=(2, 5), pady=4)
        ttk.Button(poll_f, text="Refresh", width=8,
               command=self._tlm_refresh_telemetry).grid(row=0, column=4, padx=(2, 5), pady=4)

        # ---- Always-on Log Telemetry to CSV ---- #
        ttk.Separator(frame, orient="horizontal").grid(
            row=5, column=0, sticky="ew", padx=6, pady=(8, 6)
        )
        log_f = ttk.LabelFrame(frame, text="Always-on Log Telemetry to CSV")
        log_f.grid(row=6, column=0, padx=4, pady=(2, 4), sticky="ew")
        log_f.columnconfigure(1, weight=1)

        ttk.Label(
            log_f,
            text="Includes GPS, IMU, MAG, HK, and AFE register information.",
            foreground="gray50",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=5, pady=(4, 0))
        ttk.Label(
            log_f,
            text="Independent of Capture-triggered telemetry logging.",
            foreground="gray50",
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=5, pady=(0, 2))

        rb_f = ttk.Frame(log_f)
        rb_f.grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=(4, 2))
        ttk.Radiobutton(rb_f, text="Enable", variable=self._vars["log_enabled"], value="enabled").pack(side="left", padx=(0, 8))
        ttk.Radiobutton(rb_f, text="Disable", variable=self._vars["log_enabled"], value="disabled").pack(side="left")

        ttk.Label(log_f, text="Log Path").grid(row=3, column=0, sticky="w", padx=5, pady=2)
        self._tlm_log_path_entry = ttk.Entry(log_f, textvariable=self._vars["log_path"])
        self._tlm_log_path_entry.grid(row=3, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(self._tlm_log_path_entry, self._vars["log_path"], allow_paste=True)

        ttk.Label(log_f, text="Log Interval (s)").grid(row=4, column=0, sticky="w", padx=5, pady=2)
        self._tlm_log_rate_spin = ttk.Spinbox(
            log_f,
            from_=0,
            to=86400,
            increment=0.1,
            textvariable=self._vars["log_rate"],
            width=10,
        )
        self._tlm_log_rate_spin.grid(row=4, column=1, sticky="w", padx=5, pady=2)
        ttk.Button(log_f, text="Get", width=8,
                   command=self._tlm_get_logging).grid(row=4, column=2, padx=2, pady=2)
        ttk.Button(log_f, text="Set", width=8,
                   command=self._tlm_set_logging).grid(row=4, column=3, padx=(2, 5), pady=2)

        # Status may have arrived before this lazily-built tab existed.
        self._afe_apply_state(self.mep.afe.get_status())

    # ---- AFE tab ---- #

    def _build_afe_tab(self, frame: ttk.Frame):
        # _afe_frame and columnconfigure already set in _build_advanced_section.
        # If _afe_populate_from_announce already ran (from emit-cached before
        # the user opened this tab), dynamic widgets are already present.
        if not hasattr(self, "_afe_reg_pins"):
            self._afe_placeholder = ttk.Label(frame, text="Waiting for AFE service announce…",
                                              foreground="grey")
            self._afe_placeholder.grid(row=0, column=0, padx=20, pady=20)

        # Bottom buttons (always present)
        btn_f = ttk.Frame(frame)
        btn_f.grid(row=99, column=0, padx=4, pady=(0, 6), sticky="ew")
        btn_f.columnconfigure(0, weight=1)
        btn_f.columnconfigure(1, weight=1)
        ttk.Button(btn_f, text="Refresh State",
                   command=self._afe_refresh).grid(
            row=0, column=0, padx=(0, 2), sticky="ew")
        ttk.Button(btn_f, text="Reset to Defaults",
                   command=self._afe_reset_defaults).grid(
            row=0, column=1, padx=(2, 0), sticky="ew")

    def _afe_reg_default(self, reg: dict):
        if not isinstance(reg, dict):
            return 0
        if "service_default_override" in reg:
            return reg.get("service_default_override")
        return reg.get("default", 0)

    def _afe_enabled_bit(self, reg: dict):
        """Return which raw bit means the positive boolean state, or None if not boolean."""
        if not isinstance(reg, dict):
            return None
        zero = str(reg.get("0", "")).strip().lower()
        one = str(reg.get("1", "")).strip().lower()
        for true_label, false_label in (
            ("enabled", "disabled"),
            ("on", "off"),
            ("asserted", "not asserted"),
            ("bypassed", "filtered"),
            ("bypassed", "enabled"),
            ("blanked", "not blanked"),
        ):
            if zero == true_label and one == false_label:
                return 0
            if zero == false_label and one == true_label:
                return 1
        return None

    def _afe_reserved_register(self, reg: dict):
        if not isinstance(reg, dict):
            return False
        zero = str(reg.get("0", "")).strip().lower()
        one = str(reg.get("1", "")).strip().lower()
        return zero == "reserved" and one == "reserved"

    def _afe_raw_to_choice(self, reg: dict, raw_val):
        try:
            bit = int(raw_val)
        except (TypeError, ValueError):
            bit = 0
        bit = bit if bit in (0, 1) else 0
        return str(reg.get(str(bit), str(bit)))

    def _afe_choice_to_raw(self, reg: dict, choice):
        choice_s = str(choice)
        if choice_s == str(reg.get("1", "1")):
            return 1
        return 0

    def _afe_set_control_var_from_raw(self, key: str, reg: dict, raw_val):
        if key not in self._vars:
            return
        if self._afe_enabled_bit(reg) is None:
            self._vars[key].set(self._afe_raw_to_choice(reg, raw_val))
        else:
            self._vars[key].set(self._afe_raw_to_checked(reg, raw_val))

    def _afe_add_register_control(self, parent, row_i: int, device: str, reg: dict,
                                  key: str, label: str, columnspan: int = 1):
        if self._afe_enabled_bit(reg) is None:
            reg_f = ttk.Frame(parent)
            reg_f.grid(row=row_i, column=0, columnspan=columnspan,
                       sticky="ew", padx=6, pady=(2, 1))
            reg_f.columnconfigure(2, weight=1)
            ttk.Label(reg_f, text=f"{label}:").grid(row=0, column=0, sticky="w")
            self._vars[key] = tk.StringVar(
                value=self._afe_raw_to_choice(reg, self._afe_reg_default(reg))
            )

            def _selector_cb(device=device, name=reg["name"], key=key):
                if self._afe_updating:
                    return
                self.mep.afe.set_register(device, name, self._afe_choice_to_raw(reg, self._vars[key].get()))

            self._vars[key].trace_add("write", lambda *_, cb=_selector_cb: cb())
            ttk.Radiobutton(
                reg_f,
                text=str(reg.get("0", "0")),
                variable=self._vars[key],
                value=str(reg.get("0", "0")),
            ).grid(row=0, column=1, sticky="w", padx=(6, 10))
            ttk.Radiobutton(
                reg_f,
                text=str(reg.get("1", "1")),
                variable=self._vars[key],
                value=str(reg.get("1", "1")),
            ).grid(row=0, column=2, sticky="w")
            return

        self._vars[key] = tk.BooleanVar(
            value=self._afe_raw_to_checked(reg, self._afe_reg_default(reg))
        )

        def _check_cb(device=device, name=reg["name"], key=key, reg=reg):
            if self._afe_updating:
                return
            v = self._vars[key].get()
            self.mep.afe.set_register(device, name, self._afe_checked_to_raw(reg, v))

        self._vars[key].trace_add("write", lambda *_, cb=_check_cb: cb())
        ttk.Checkbutton(parent, text=label, variable=self._vars[key]).grid(
            row=row_i, column=0, columnspan=columnspan, sticky="w", padx=6, pady=1)

    def _afe_raw_to_checked(self, reg: dict, raw_val):
        """Map raw bit value to checkbox state, enforcing checked=enabled where labels define it."""
        try:
            bit = int(raw_val)
        except (TypeError, ValueError):
            return False
        enabled_bit = self._afe_enabled_bit(reg)
        if enabled_bit is None:
            return bool(bit)
        return bit == enabled_bit

    def _afe_checked_to_raw(self, reg: dict, checked):
        """Map checkbox state to raw bit value, enforcing checked=enabled where labels define it."""
        enabled_bit = self._afe_enabled_bit(reg)
        if enabled_bit is None:
            return int(bool(checked))
        return enabled_bit if checked else (1 - enabled_bit)

    def _afe_populate_from_announce(self, announce: dict):
        """Build AFE register widgets from afecontrol/announce describe data."""
        describe = announce.get("describe", {})
        reg_ref = describe.get("registers", {}).get("reference", {})
        reg_pins = reg_ref.get("register_pins", {})
        devices = reg_ref.get("devices", [])
        rx_devices = reg_ref.get("rx_devices", [])
        atten_range = reg_ref.get("attenuation_db_range", [0, 31])
        if not isinstance(reg_pins, dict) or not reg_pins:
            logging.warning("AFE: announce has no register_pins — cannot populate tab")
            return
        tx_devices = [d for d in devices if d.startswith("tx")]
        schema_signature = json.dumps(
            {
                "register_pins": reg_pins,
                "devices": devices,
                "rx_devices": rx_devices,
                "tx_devices": tx_devices,
                "attenuation_db_range": atten_range,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        self._afe_reg_pins = reg_pins
        self._afe_atten_range = atten_range
        if schema_signature == self._afe_schema_signature:
            return
        self._afe_schema_signature = schema_signature

        # Remove placeholder
        if hasattr(self, "_afe_placeholder") and self._afe_placeholder.winfo_exists():
            self._afe_placeholder.destroy()

        # Destroy old dynamic content if repopulating
        for attr in ("_afe_main_f", "_afe_rx_outer", "_afe_tx_outer"):
            w = getattr(self, attr, None)
            if w is not None and w.winfo_exists():
                w.destroy()

        frame = self._afe_frame

        # ---- Main Block (misc) ---- #
        misc_pins = reg_pins.get("misc", [])
        if misc_pins:
            self._afe_main_f = ttk.LabelFrame(frame, text="Main Block (misc)")
            self._afe_main_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
            self._afe_main_f.columnconfigure(0, weight=1)

            row_i = 0
            for reg in misc_pins:
                name = reg["name"]
                if name.startswith("NOT_USED") or self._afe_reserved_register(reg):
                    continue

                label = reg.get("label", name.replace("_", " ").title())
                key = f"afe_misc_{name}"
                self._afe_add_register_control(self._afe_main_f, row_i, "misc", reg, key, label)
                row_i += 1

        # ---- RX Channels ---- #
        if rx_devices:
            self._afe_rx_outer = ttk.LabelFrame(frame, text="RX Channels")
            self._afe_rx_outer.grid(row=1, column=0, padx=4, pady=(2, 4), sticky="ew")
            self._afe_rx_outer.columnconfigure(0, weight=1)

            rx_nb = ttk.Notebook(self._afe_rx_outer)
            rx_nb.grid(row=0, column=0, padx=4, pady=4, sticky="ew")

            for device in rx_devices:
                pins = reg_pins.get(device, [])
                ch_label = device.upper()
                ch_f = ttk.Frame(rx_nb, padding=6)
                ch_f.columnconfigure(1, weight=1)
                rx_nb.add(ch_f, text=ch_label)

                row_i = 0
                for reg in pins:
                    name = reg["name"]
                    if name.startswith("NOT_USED") or self._afe_reserved_register(reg):
                        continue
                    if name.startswith("ATTEN_"):
                        continue

                    key = f"afe_{device}_{name}"
                    label = reg.get("label", name.replace("_", " ").title())
                    self._afe_add_register_control(ch_f, row_i, device, reg, key, label, columnspan=2)
                    row_i += 1

                # Attenuation controls
                ttk.Separator(ch_f, orient="horizontal").grid(
                    row=row_i, column=0, columnspan=4, sticky="ew", pady=4)
                row_i += 1
                ttk.Label(ch_f, text="Attenuation:").grid(row=row_i, column=0, sticky="w")
                requested_key = f"afe_{device}_atten_requested"
                confirmed_key = f"afe_{device}_atten_confirmed"
                state_key = f"afe_{device}_atten_state"
                self._vars[requested_key] = tk.StringVar(value="0")
                self._vars[confirmed_key] = tk.StringVar(value="—")
                self._vars[state_key] = tk.StringVar(value="")
                ttk.Label(ch_f, textvariable=self._vars[confirmed_key]).grid(
                    row=row_i, column=1, sticky="w", padx=(4, 10))
                atten_spin = ttk.Spinbox(
                    ch_f,
                    from_=atten_range[0],
                    to=atten_range[1],
                    increment=1,
                    textvariable=self._vars[requested_key],
                    width=6,
                )
                atten_spin.grid(row=row_i, column=2, sticky="w", padx=5)
                atten_spin.bind("<Return>", lambda _event, device=device: self._afe_submit_attenuation(device))
                ttk.Label(ch_f, text="0-31 dB", foreground="grey").grid(row=row_i, column=3, sticky="w")
                ttk.Button(
                    ch_f,
                    text="Set",
                    command=lambda device=device: self._afe_submit_attenuation(device),
                ).grid(row=row_i, column=4, sticky="w", padx=(6, 0))
                ttk.Label(ch_f, textvariable=self._vars[state_key], foreground="#8a5a00").grid(
                    row=row_i, column=5, sticky="w", padx=(10, 0))

                row_i += 1

                self._afe_update_atten_ui_state(device)

        # ---- TX Channels ---- #
        if tx_devices:
            self._afe_tx_outer = ttk.LabelFrame(frame, text="TX Channels")
            self._afe_tx_outer.grid(row=2, column=0, padx=4, pady=(2, 4), sticky="ew")
            self._afe_tx_outer.columnconfigure(0, weight=1)

            tx_nb = ttk.Notebook(self._afe_tx_outer)
            tx_nb.grid(row=0, column=0, padx=4, pady=4, sticky="ew")

            for device in tx_devices:
                pins = reg_pins.get(device, [])
                ch_label = device.upper()
                ch_f = ttk.Frame(tx_nb, padding=6)
                ch_f.columnconfigure(0, weight=1)
                tx_nb.add(ch_f, text=ch_label)

                row_i = 0
                for reg in pins:
                    name = reg["name"]
                    if name.startswith("NOT_USED") or self._afe_reserved_register(reg):
                        continue

                    key = f"afe_{device}_{name}"
                    label = reg.get("label", name.replace("_", " ").title())
                    self._afe_add_register_control(ch_f, row_i, device, reg, key, label)
                    row_i += 1

        logging.info("AFE tab populated from announce data")

    # ---- REC tab ---- #

    def _build_rec_tab(self, frame: ttk.Frame):
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        # Create scrollable area
        canvas = tk.Canvas(frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(
                canvas_window, width=max(event.width - 8, 1)
            ),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        scrollable_frame.columnconfigure(0, weight=1)

        row = 0

        # ===== SUMMARY =====
        status_frame = ttk.LabelFrame(scrollable_frame, text="Recorder Summary")
        status_frame.grid(row=row, column=0, padx=4, pady=(4, 2), sticky="ew")
        status_frame.columnconfigure(1, weight=1)
        row += 1

        self._vars["rec_status"] = tk.StringVar(value="—")
        self._vars["rec_config_source"] = tk.StringVar(value="—")
        self._vars["rec_draft_error"] = tk.StringVar(value="")
        for summary_row, (label, key) in enumerate((
            ("Recorder state", "rec_status"),
            ("Preset file", "rec_config_source"),
        )):
            ttk.Label(status_frame, text=label).grid(
                row=summary_row, column=0, sticky="w", padx=5, pady=2)
            if key == "rec_config_source":
                entry = tk.Entry(
                    status_frame,
                    textvariable=self._vars[key],
                    state="readonly",
                    relief="flat",
                    readonlybackground="#f3f3f3",
                    font=("TkDefaultFont", 8),
                )
            else:
                entry = ttk.Entry(
                    status_frame, textvariable=self._vars[key], state="readonly", width=18
                )
            entry.grid(row=summary_row, column=1, sticky="ew", padx=5, pady=2)
        ttk.Button(
            status_frame,
            text="Refresh Presets",
            command=self._rec_request_presets,
        ).grid(row=0, column=2, rowspan=2, sticky="ns", padx=(0, 5), pady=2)
        ttk.Label(
            status_frame,
            text="Applied REC settings take effect on the next recording; running recorders are not reconfigured.",
            wraplength=455,
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=5, pady=(4, 2))
        ttk.Label(
            status_frame,
            textvariable=self._vars["rec_draft_error"],
            foreground="#a33a2b",
            wraplength=455,
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 5))

        # ===== DIGITAL RF IQ =====
        drf_frame = ttk.LabelFrame(scrollable_frame, text="DigitalRF IQ")
        drf_frame.grid(row=row, column=0, padx=4, pady=6, sticky="ew")
        drf_frame.columnconfigure(1, weight=1)
        row += 1

        self._vars["sg_digital_rf"] = tk.BooleanVar(value=False)
        self._vars["sg_metadata"] = tk.BooleanVar(value=False)

        cb_drf = ttk.Checkbutton(
            drf_frame,
            text="Save DigitalRF IQ to disk",
            variable=self._vars["sg_digital_rf"],
        )
        cb_drf.grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=(4, 2))
        self._add_tooltip(cb_drf, "pipeline.digital_rf")

        cb_metadata = ttk.Checkbutton(
            drf_frame,
            text="Save DigitalRF metadata to disk",
            variable=self._vars["sg_metadata"],
        )
        cb_metadata.grid(row=1, column=0, columnspan=2, sticky="w", padx=5, pady=(0, 4))
        self._add_tooltip(cb_metadata, "pipeline.metadata")

        ttk.Separator(drf_frame, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=5, pady=(2, 4)
        )

        ttk.Label(drf_frame, text="Apply conjugate").grid(
            row=3, column=0, sticky="w", padx=5, pady=4)
        policy_frame = ttk.Frame(drf_frame)
        policy_frame.grid(row=3, column=1, sticky="w", padx=5, pady=4)
        policy_labels = {
            "auto": "Auto",
            "force_on": "Force On",
            "force_off": "Force Off",
        }
        for idx, policy in enumerate(CONJUGATE_POLICY_OPTIONS):
            ttk.Radiobutton(
                policy_frame,
                text=policy_labels.get(policy, policy),
                variable=self._vars["conjugate_policy"],
                value=policy,
                command=self._conjugate_policy_changed,
            ).grid(row=0, column=idx, sticky="w", padx=2)

        ttk.Label(drf_frame, text="Actual state").grid(
            row=4, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(
            drf_frame,
            textvariable=self._vars["conjugate_actual"],
            state="readonly",
            width=14,
        ).grid(row=4, column=1, sticky="w", padx=5, pady=4)

        # ===== THROUGHPUT CONTROLS =====
        throughput_frame = ttk.LabelFrame(scrollable_frame, text="Throughput Controls")
        throughput_frame.grid(row=row, column=0, padx=4, pady=6, sticky="ew")
        row += 1

        throughput_row = 0
        ttk.Label(throughput_frame, text="Batch size (packets)").grid(
            row=throughput_row, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_batch_size"] = tk.StringVar(value="")
        ent_batch_size = ttk.Entry(
            throughput_frame, textvariable=self._vars["sg_batch_size"], width=14
        )
        ent_batch_size.grid(row=throughput_row, column=1, sticky="w", padx=5, pady=3)
        self._add_tooltip(
            ent_batch_size,
            "Packets processed per batch by the network receiver.",
        )

        throughput_row += 1
        ttk.Label(throughput_frame, text="Max packet size (bytes)").grid(
            row=throughput_row, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_max_packet_size"] = tk.StringVar(value="")
        ent_max_packet_size = ttk.Entry(
            throughput_frame, textvariable=self._vars["sg_max_packet_size"], width=14
        )
        ent_max_packet_size.grid(row=throughput_row, column=1, sticky="w", padx=5, pady=3)
        self._add_tooltip(
            ent_max_packet_size,
            "Maximum payload size expected from the sender. This should match the "
            "packet format; it is not a tuning knob for the spectrogram pipeline.",
        )

        throughput_row += 1
        ttk.Label(throughput_frame, text="Chunk size (samples)").grid(
            row=throughput_row, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_chunk_size"] = tk.StringVar(value="")
        ent_chunk_size = ttk.Entry(
            throughput_frame, textvariable=self._vars["sg_chunk_size"], width=14
        )
        ent_chunk_size.grid(row=throughput_row, column=1, sticky="w", padx=5, pady=3)
        self._add_tooltip(
            ent_chunk_size,
            "Number of samples per recorder chunk. The preset chunk size is the "
            "main throughput unit that flows through the recorder graph.",
        )

        throughput_row += 1
        ttk.Label(throughput_frame, text="Batch capacity").grid(
            row=throughput_row, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_batch_capacity"] = tk.StringVar(value="")
        ent_batch_capacity = ttk.Entry(
            throughput_frame, textvariable=self._vars["sg_batch_capacity"], width=14
        )
        ent_batch_capacity.grid(row=throughput_row, column=1, sticky="w", padx=5, pady=3)
        self._add_tooltip(
            ent_batch_capacity,
            "Number of packet batches that can queue before the receiver starts "
            "to apply backpressure or drop old work.",
        )

        throughput_row += 1
        ttk.Label(throughput_frame, text="RX buffer size (chunks)").grid(
            row=throughput_row, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_buffer_size"] = tk.StringVar(value="")
        ent_buffer_size = ttk.Entry(
            throughput_frame, textvariable=self._vars["sg_buffer_size"], width=14
        )
        ent_buffer_size.grid(row=throughput_row, column=1, sticky="w", padx=5, pady=3)
        self._add_tooltip(
            ent_buffer_size,
            "Number of recorder chunks that can be buffered. Larger values give "
            "more headroom for bursts, at the cost of more memory.",
        )

        throughput_row += 1
        ttk.Label(throughput_frame, text="Scheduler worker threads").grid(
            row=throughput_row, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_worker_threads"] = tk.StringVar(value="")
        ent_worker_threads = ttk.Entry(
            throughput_frame, textvariable=self._vars["sg_worker_threads"], width=14
        )
        ent_worker_threads.grid(row=throughput_row, column=1, sticky="w", padx=5, pady=3)
        self._add_tooltip(
            ent_worker_threads,
            "Worker threads used by the event-based scheduler.",
        )

        self._vars["calc_throughput_formula"] = tk.StringVar(value="—")
        ttk.Label(
            throughput_frame,
            textvariable=self._vars["calc_throughput_formula"],
            wraplength=455,
            justify="left",
            font=("TkDefaultFont", 9),
        ).grid(row=throughput_row, column=0, columnspan=2, sticky="w", padx=5, pady=(2, 4))

        throughput_row += 1
        ttk.Label(
            throughput_frame,
            text="Chunk relation: batch_size × packet samples per packet = chunk size",
            wraplength=455,
        ).grid(row=throughput_row, column=0, columnspan=2, sticky="w", padx=5, pady=(2, 4))

        # ===== SPECTROGRAMS =====
        disp_frame = ttk.LabelFrame(scrollable_frame, text="Spectrograms")
        disp_frame.grid(row=row, column=0, padx=4, pady=6, sticky="ew")
        disp_frame.columnconfigure(2, weight=1)
        row += 1

        # Keep derived values available for preview/status even if not all are displayed inline.
        self._vars["calc_freq_formula"] = tk.StringVar(value="—")
        self._vars["calc_hop_formula"] = tk.StringVar(value="—")
        self._vars["calc_segments"] = tk.StringVar(value="—")
        self._vars["calc_cadence_formula"] = tk.StringVar(value="—")
        self._vars["calc_resamplers"] = tk.StringVar(value="—")
        self._vars["calc_waterfall_formula"] = tk.StringVar(value="—")

        row_i = 0
        ttk.Label(disp_frame, text="nperseg").grid(row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_nperseg"] = tk.StringVar(value="")
        ttk.Entry(disp_frame, textvariable=self._vars["sg_nperseg"], width=14).grid(
            row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="nfft").grid(row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_nfft"] = tk.StringVar(value="")
        ttk.Entry(disp_frame, textvariable=self._vars["sg_nfft"], width=14).grid(
            row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="noverlap").grid(row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_noverlap"] = tk.StringVar(value="")
        ttk.Entry(disp_frame, textvariable=self._vars["sg_noverlap"], width=14).grid(
            row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="window").grid(row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_window"] = tk.StringVar(value="")
        ttk.Combobox(
            disp_frame,
            textvariable=self._vars["sg_window"],
            values=["hann", "hamming", "blackman", "rectangular"],
            width=14,
            state="readonly",
        ).grid(row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="Spectrum row cadence").grid(
            row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_num_spectra_per_chunk"] = tk.StringVar(value="")
        ttk.Entry(disp_frame, textvariable=self._vars["sg_num_spectra_per_chunk"], width=14).grid(
            row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="Reduction op").grid(
            row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_reduce_op"] = tk.StringVar(value="")
        ttk.Combobox(
            disp_frame,
            textvariable=self._vars["sg_reduce_op"],
            values=["max", "median", "mean"],
            width=14,
            state="readonly",
        ).grid(row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="rows/output").grid(
            row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_spectra_per_output"] = tk.StringVar(value="")
        ttk.Entry(disp_frame, textvariable=self._vars["sg_spectra_per_output"], width=8).grid(
            row=row_i, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(disp_frame, textvariable=self._vars["calc_waterfall_formula"], justify="left").grid(
            row=row_i, column=2, sticky="w", padx=8, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="Color scale min (dB)").grid(
            row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_snr_min"] = tk.StringVar(value="")
        ttk.Entry(disp_frame, textvariable=self._vars["sg_snr_min"], width=14).grid(
            row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="Color scale max (dB)").grid(
            row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_snr_max"] = tk.StringVar(value="")
        ttk.Entry(disp_frame, textvariable=self._vars["sg_snr_max"], width=14).grid(
            row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="Colormap").grid(row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_cmap"] = tk.StringVar(value="")
        ttk.Combobox(
            disp_frame,
            textvariable=self._vars["sg_cmap"],
            values=["viridis", "plasma", "inferno", "magma", "hot", "jet"],
            width=14,
            state="readonly",
        ).grid(row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="DPI").grid(row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_dpi"] = tk.StringVar(value="")
        ttk.Entry(disp_frame, textvariable=self._vars["sg_dpi"], width=14).grid(
            row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        ttk.Label(disp_frame, text="Figure size (w,h inches)").grid(
            row=row_i, column=0, sticky="w", padx=5, pady=3)
        self._vars["sg_figsize"] = tk.StringVar(value="")
        ttk.Entry(disp_frame, textvariable=self._vars["sg_figsize"], width=18).grid(
            row=row_i, column=1, sticky="w", padx=5, pady=3)

        row_i += 1
        self._vars["sg_compute"] = tk.BooleanVar(value=False)
        self._vars["sg_mqtt"] = tk.BooleanVar(value=False)
        self._vars["sg_output"] = tk.BooleanVar(value=False)
        checks_frame = ttk.Frame(disp_frame)
        checks_frame.grid(row=row_i, column=0, columnspan=3, sticky="w", padx=5, pady=2)

        cb_compute = ttk.Checkbutton(checks_frame, text="Compute", variable=self._vars["sg_compute"])
        cb_compute.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._add_tooltip(cb_compute, "pipeline.compute")

        cb_mqtt = ttk.Checkbutton(checks_frame, text="Stream MQTT", variable=self._vars["sg_mqtt"])
        cb_mqtt.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self._add_tooltip(cb_mqtt, "pipeline.mqtt")

        cb_output = ttk.Checkbutton(checks_frame, text="Save to disk", variable=self._vars["sg_output"])
        cb_output.grid(row=0, column=2, sticky="w")
        self._add_tooltip(cb_output, "pipeline.output")

        # ===== YAML-ONLY SETTINGS =====
        yaml_only_frame = ttk.LabelFrame(scrollable_frame, text="Set via YAML only")
        yaml_only_frame.grid(row=row, column=0, padx=4, pady=6, sticky="ew")
        yaml_only_frame.columnconfigure(0, weight=1)
        row += 1
        ttk.Label(
            yaml_only_frame,
            text="• Configured chain:",
            font=("TkDefaultFont", 9),
        ).grid(row=0, column=0, sticky="w", padx=5, pady=(2, 0))
        ttk.Label(
            yaml_only_frame,
            textvariable=self._vars["calc_resamplers"],
            justify="left",
            wraplength=450,
            font=("TkDefaultFont", 9),
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 2))
        ttk.Label(
            yaml_only_frame,
              text="• pipeline.converter (int\u2192float): Enabled\n"
                  "• pipeline.int_converter (float\u2192int before DRF sink): Enabled when DigitalRF IQ is on\n"
                  "• resampler chain can change effective chunk size\n"
                  "• num_spectra_per_chunk must divide the effective chunk",
            justify="left",
            font=("TkDefaultFont", 9),
        ).grid(row=2, column=0, sticky="w", padx=5, pady=(2, 4))

        # ===== NEXT-RECORD ACTIONS =====
        action_frame = ttk.LabelFrame(scrollable_frame, text="APPLY changes")
        action_frame.grid(row=row, column=0, padx=4, pady=6, sticky="ew")
        for column in range(2):
            action_frame.columnconfigure(column, weight=1)
        self._rec_stage_button = ttk.Button(
            action_frame, text="Apply On Next Record", command=self._stage_rec_settings
        )
        self._rec_stage_button.grid(row=0, column=0, padx=4, pady=6, sticky="ew")
        ttk.Button(action_frame, text="Reset to Config File", command=self._rec_reset_config).grid(
            row=0, column=1, padx=4, pady=6, sticky="ew")

        def _bind_rec_copy_menus(container):
            for widget in container.winfo_children():
                if isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Spinbox, ttk.Spinbox)):
                    allow_paste = str(widget.cget("state")) not in ("readonly", "disabled")
                    self._bind_copy_menu(widget, allow_paste=allow_paste)
                elif isinstance(widget, (tk.Label, ttk.Label)):
                    self._bind_copy_menu(widget, allow_paste=False)
                _bind_rec_copy_menus(widget)

        _bind_rec_copy_menus(scrollable_frame)

        # Register tab-specific MQTT → UI. Emit-cached fires inline if data exists.
        self._update_conjugate_actual_display()
        self._rec_trace_busy = False
        self._rec_request_presets()
        # Populate with cached data so tab shows current state immediately on first open.
        cached = self.mep.recorder.get_status()
        if isinstance(cached, dict):
            self._rec_status_ui_update(cached)
        self._vars["sample_rate_mhz"].trace_add("write", self._on_rec_sample_rate_change)
        for key in (
            "sg_nperseg", "sg_nfft", "sg_noverlap", "sg_window", "sg_reduce_op",
            "sg_num_spectra_per_chunk", "sg_chunk_size", "sg_batch_capacity",
            "sg_buffer_size", "sg_worker_threads", "sg_spectra_per_output", "sg_snr_min",
            "sg_snr_max", "sg_cmap", "sg_dpi", "sg_figsize", "sg_compute",
            "sg_mqtt", "sg_output", "sg_digital_rf", "sg_metadata",
        ):
            self._vars[key].trace_add("write", self._rec_preview_draft)

    def _conjugate_policy_changed(self, *_):
        policy = self._vars["conjugate_policy"].get()
        if policy not in CONJUGATE_POLICY_OPTIONS:
            logging.warning("Ignoring invalid conjugate policy selection: %r", policy)
            self._vars["conjugate_policy"].set(CONJUGATE_POLICY_DEFAULT)
            return

        self._conjugate_policy_user_override = True
        self._vars["conjugate_actual"].set("pending")
        logging.info(f"Conjugate policy set to: {policy}")

    def _update_conjugate_actual_display(self):
        policy = self._vars["conjugate_policy"].get()
        if policy not in CONJUGATE_POLICY_OPTIONS:
            self._vars["conjugate_policy"].set(CONJUGATE_POLICY_DEFAULT)
        self._vars["conjugate_actual"].set("pending")

    # ------------------------------------------------------------------ #
    #  AFE helpers
    # ------------------------------------------------------------------ #

    def _afe_refresh(self):
        """Request current register state from AFE via MQTT."""
        self.mep.afe.get_registers("all")
        logging.info("AFE: register refresh requested")

    def _afe_apply_state(self, data: dict):
        """Update all AFE widgets from the client's register status stream."""
        params = data.get("parameters", data.get("params", {}))
        if isinstance(params, dict):
            imu_params = params.get("imu", {})
            if isinstance(imu_params, dict):
                self._set_var("tlm_imu_acc_odr", imu_params.get("acc_odr", "—"))
                self._set_var("tlm_imu_gyr_odr", imu_params.get("gyr_odr", "—"))

            mag_params = params.get("mag", params.get("magnetometer", {}))
            if isinstance(mag_params, dict):
                self._set_var("tlm_mag_ccr", mag_params.get("ccr", "—"))
                self._set_var("tlm_mag_updr", mag_params.get("updr", "—"))

        reg_pins = getattr(self, "_afe_reg_pins", None)
        if not reg_pins:
            logging.warning("AFE: register data arrived before announce — skipping")
            return

        regs = data.get("registers_named", data)
        if not isinstance(regs, dict):
            return

        self._afe_updating = True
        try:
            for device, pins in reg_pins.items():
                dev_regs = regs.get(device, {})
                if not isinstance(dev_regs, dict):
                    continue

                for reg in pins:
                    name = reg.get("name")
                    if (not name or name.startswith("NOT_USED") or name.startswith("ATTEN_")
                            or self._afe_reserved_register(reg)):
                        continue
                    key = f"afe_{device}_{name}"
                    raw_val = dev_regs.get(name)
                    if isinstance(raw_val, dict):
                        raw_val = raw_val.get("value")

                    if raw_val is None or key not in self._vars:
                        continue

                    try:
                        self._afe_set_control_var_from_raw(key, reg, raw_val)
                    except (TypeError, ValueError):
                        pass

                confirmed = self._afe_extract_confirmed_attenuation(data, device)
                requested_key = f"afe_{device}_atten_requested"
                if confirmed is not None and requested_key in self._vars and device not in self._afe_atten_initialized:
                    self._vars[requested_key].set(str(confirmed))
                    self._afe_atten_initialized.add(device)

                pending = self._afe_atten_pending.get(device)
                if pending and confirmed is not None and confirmed == pending.get("requested"):
                    self._afe_atten_pending.pop(device, None)

                self._afe_update_atten_ui_state(device, confirmed=confirmed)
        finally:
            self._afe_updating = False
        logging.debug("AFE widgets updated from MQTT register data")

    def _afe_reset_defaults(self):
        """Restore all AFE widget vars to defaults from cached announce data."""
        reg_pins = getattr(self, "_afe_reg_pins", None)
        if not reg_pins:
            logging.warning("AFE: cannot reset — no announce data")
            return

        for device, pins in reg_pins.items():
            for reg in pins:
                name = reg["name"]
                if (name.startswith("NOT_USED") or name.startswith("ATTEN_")
                        or self._afe_reserved_register(reg)):
                    continue
                key = f"afe_{device}_{name}"
                reg_default = self._afe_reg_default(reg)
                if key in self._vars:
                    self._afe_set_control_var_from_raw(key, reg, reg_default)
            # Reset attenuation for RX devices
            requested_key = f"afe_{device}_atten_requested"
            if requested_key in self._vars:
                self._vars[requested_key].set("0")
            self._afe_atten_pending.pop(device, None)
            self._afe_update_atten_ui_state(device)
        logging.info("AFE: all registers reset to defaults")

    # ------------------------------------------------------------------ #
    #  TLM helpers
    # ------------------------------------------------------------------ #

    # NOTE: GPS has no rate command — removed _tlm_apply_gnss_rate

    def _tlm_apply_time_config(self):
        source = self._vars["time_source"].get()
        epoch_mode = self._vars["epoch_mode"].get()
        ts = int(time.time())
        self.mep.afe.configure_time(source, epoch_mode, ts)
        logging.info(f"TLM: time config set (source={source}, epoch={epoch_mode}, ts={ts})")

    def _tlm_get_time_params(self):
        self.mep.afe.get_time_params()
        logging.info("TLM: time params query sent")

    def _tlm_get_hk(self):
        self.mep.afe.telemetry_dump()
        logging.info("TLM: HK telemetry refresh requested")

    def _tlm_set_hk(self):
        self.mep.afe.telemetry_dump()
        logging.info("TLM: HK set requested (no settable HK params; refreshed telemetry)")

    def _tlm_get_polling_interval(self):
        self.mep.afe.get_polling_interval()
        logging.info("TLM: polling interval query sent")

    def _tlm_set_polling_interval(self):
        try:
            n = self._vars["poll_interval_s"].get()
        except tk.TclError:
            logging.error("TLM: invalid polling interval value")
            return
        self.mep.afe.set_polling_interval(n)
        logging.info(f"TLM: polling interval set to {n} s")

    def _tlm_refresh_telemetry(self):
        self.mep.afe.refresh()
        logging.info("TLM: full refresh requested")

    def _tlm_get_logging(self):
        self.mep.afe.get_log_status()
        logging.info("TLM: logging status query sent")

    def _tlm_set_logging(self):
        try:
            mode = self._vars["log_enabled"].get()
            path = self._vars["log_path"].get()
            rate = self._vars["log_rate"].get()
        except tk.TclError as e:
            logging.error(f"TLM: invalid logging setting: {e}")
            return

        self.mep.afe.configure_logging(mode == "enabled", path, rate)
        logging.info(f"TLM: logging configuration requested ({mode}, path={path}, rate={rate} s)")

    # ------------------------------------------------------------------ #
    #  SERVICE helpers
    # ------------------------------------------------------------------ #

    def _service_set_action_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for widget in getattr(self, "_service_action_widgets", []):
            try:
                widget.configure(state=state)
            except Exception:
                pass

    def _service_selected_service(self):
        tree = getattr(self, "_service_services_tree", None)
        manager = getattr(self, "service_manager", None)
        if tree is None or manager is None:
            return None
        selected = [service for service in tree.selection() if service in manager.services]
        return selected[0] if selected else None

    def _service_apply_selected_service(self):
        service = self._service_selected_service()
        row = self.service_manager.services.get(service, {}) if service else {}
        path = row.get("fragment_path")
        service_text = f"{service}  ({path})" if service and path else (service or "—")
        self._vars["service_selected_name"].set(service_text)
        self._vars["service_selected_state"].set(row.get("active_state") or "—")
        self._vars["service_selected_substate"].set(row.get("sub_state") or "—")
        self._vars["service_selected_install"].set(row.get("unit_file_state") or "—")

    def _service_preview_command(self, action: str):
        services = self._service_action_targets()
        if not services:
            return "Select at least one service or choose All"
        return "systemctl " + action + " " + " ".join(services)

    def _service_set_command_preview(self, text: str = ""):
        self._vars["service_cmd_preview"].set(
            text or "Hover over an action to preview the systemctl command"
        )

    def _service_bind_command_preview(self, widget, action: str):
        widget.bind("<Enter>", lambda _e: self._service_set_command_preview(self._service_preview_command(action)))
        widget.bind("<Leave>", lambda _e: self._service_set_command_preview())

    def _service_refresh_status_async(self):
        manager = getattr(self, "service_manager", None)
        if manager is None:
            logging.error("SVC: ServiceManager client is unavailable")
            return
        if manager.refresh_busy:
            return
        if not self.mep.is_connected():
            logging.error("SVC: MQTT is disconnected; status refresh was not sent")
            return
        manager.refresh(callback=lambda response: self._gui_call(self._service_refresh_complete, response))

    def _service_refresh_complete(self, response: dict):
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else "invalid response"
            logging.error("SVC: refresh failed: %s", error or "unknown error")
            return
        status = response.get("status_data")
        self._service_apply_status(status if isinstance(status, dict) else {})

    def _service_render_service_list(self):
        tree = getattr(self, "_service_services_tree", None)
        manager = getattr(self, "service_manager", None)
        if tree is None or manager is None:
            return

        desired_ids = set(manager.service_names)
        for index, service in enumerate(manager.service_names):
            row = manager.services.get(service, {})
            values = (
                service.removesuffix(".service"),
                row.get("active_state", "—"),
                row.get("main_pid", "—"),
                row.get("description", "—"),
            )
            if tree.exists(service):
                if tuple(tree.item(service, "values")) != tuple(str(value) for value in values):
                    tree.item(service, values=values)
                if tree.index(service) != index:
                    tree.move(service, "", index)
            else:
                tree.insert("", "end", iid=service, values=values)
        for iid in tree.get_children():
            if iid not in desired_ids:
                tree.delete(iid)

        self._service_apply_selected_service()

    def _service_selected_services(self) -> list[str]:
        manager = getattr(self, "service_manager", None)
        tree = getattr(self, "_service_services_tree", None)
        if manager is None or tree is None:
            return []
        return [service for service in tree.selection() if service in manager.service_names]

    def _service_on_tree_select(self, _event=None):
        manager = getattr(self, "service_manager", None)
        if manager is None:
            return
        self._service_apply_selected_service()
        if not self._service_selected_services():
            return
        if not manager.log_busy:
            if not self._service_journal_user_paused:
                self._service_stream_start()
            return
        if self._vars.get("service_log_mode", tk.StringVar(value="selected")).get() == "selected":
            self._service_stream_start(restart=True)

    def _service_action_targets(self) -> list[str]:
        manager = getattr(self, "service_manager", None)
        if manager is None:
            return []
        scope = self._vars.get("service_action_scope", tk.StringVar(value="selected")).get()
        return self._service_selected_services() if scope == "selected" else list(manager.service_names)

    def _service_run_action(self, action: str):
        manager = getattr(self, "service_manager", None)
        if manager is None or not self.mep.is_connected() or not manager.is_available():
            logging.error("SVC: ServiceManager is unavailable; action was not sent")
            return
        if manager.action_busy:
            logging.warning("SVC: another action is already in progress")
            return

        services = self._service_action_targets()
        if not services:
            logging.error("SVC: no services are available for the selected scope")
            return
        if action in {"stop", "restart"}:
            if not messagebox.askokcancel(
                title=f"Services: {action.capitalize()}",
                message=f"{action.capitalize()} these services?\n\n" + "\n".join(services),
                parent=self.root,
            ):
                logging.info("SVC: %s cancelled", action)
                return

        self._service_set_action_busy(True)
        target_desc = ", ".join(services)
        logging.info("SVC: requesting %s on %s", action, target_desc)

        def _done(response):
            self._service_set_action_busy(False)
            if isinstance(response, dict) and response.get("success"):
                logging.info("SVC: %s complete on %s", action, target_desc)
            else:
                error = response.get("error") if isinstance(response, dict) else "invalid response"
                logging.error("SVC: %s failed on %s: %s", action, target_desc, error or "unknown error")
            self._service_refresh_status_async()

        manager.run_action(action, services, callback=lambda response: self._gui_call(_done, response))

    def _service_log_targets(self) -> list[str]:
        manager = getattr(self, "service_manager", None)
        if manager is None:
            return []
        mode = self._vars.get("service_log_mode", tk.StringVar(value="selected")).get()
        return self._service_selected_services() if mode == "selected" else list(manager.service_names)

    def _service_stream_start(self, *, restart: bool = False):
        manager = getattr(self, "service_manager", None)
        if manager is None or not self.mep.is_connected() or not manager.is_available():
            logging.error("SVC: ServiceManager is unavailable; journal stream was not started")
            return
        if manager.log_busy and not restart:
            self._service_stream_resume()
            return

        services = self._service_log_targets()
        if not services:
            logging.error("SVC: select at least one service or wait for the advertised service list")
            return

        tail_count = 0 if manager.log_paused else 30
        scope = tuple(services)
        if restart and manager.log_scope not in (None, scope):
            tail_count = 15
            self._service_clear_buffer_and_widget()
        self._vars["service_stream_state"].set("live")

        def _on_line(_line):
            if self._is_adv_tab_selected("SVC"):
                self._gui_call(self._service_flush_buffer_to_widget)

        def _on_exit(return_code):
            def _done():
                self._vars["service_stream_state"].set("paused")
                if return_code not in (None, 0):
                    logging.warning("SVC: journal stream exited with code %s", return_code)
            self._gui_call(_done)

        manager.stream_start(services, tail=tail_count, on_line=_on_line, on_exit=_on_exit)

    def _service_stream_pause(self):
        manager = getattr(self, "service_manager", None)
        if manager is None:
            return
        self._service_journal_user_paused = True
        if manager.log_busy:
            manager.stream_stop()
        else:
            manager.log_paused = True
        self._vars["service_stream_state"].set("paused")

    def _service_stream_resume(self):
        manager = getattr(self, "service_manager", None)
        if manager is None:
            return
        self._service_journal_user_paused = False
        if not manager.log_busy:
            self._service_stream_start()
            return
        manager.log_paused = False
        self._vars["service_stream_state"].set("live")
        self._service_flush_buffer_to_widget()

    def _service_on_log_mode_changed(self):
        manager = getattr(self, "service_manager", None)
        if manager is not None and manager.log_busy:
            self._service_stream_start(restart=True)

    def _service_flush_buffer_to_widget(self):
        manager = getattr(self, "service_manager", None)
        if manager is None or not hasattr(self, "_service_log_text"):
            return
        if not self._is_adv_tab_selected("SVC") or manager.log_paused:
            return
        entries = manager.get_new_log_entries()
        if not entries:
            return
        for timestamp, line in entries:
            clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).rstrip()
            self._service_log_text.insert("end", f"{timestamp}  ", ("service_ts",))
            self._service_log_text.insert("end", clean + "\n", ("service_msg",))
        self._service_log_text.see("end")
        self._service_trim_widget_lines()

    def _service_trim_widget_lines(self, max_lines: int = 800):
        if not hasattr(self, "_service_log_text"):
            return
        lines = int(self._service_log_text.index("end-1c").split(".")[0])
        if lines > max_lines:
            self._service_log_text.delete("1.0", f"{lines - max_lines}.0")

    def _service_clear_buffer_and_widget(self):
        manager = getattr(self, "service_manager", None)
        if manager is not None:
            manager.clear_log()
        if hasattr(self, "_service_log_text"):
            self._service_log_text.delete("1.0", "end")

    def _build_service_tab(self, frame: ttk.Frame):
        """SVC tab: systemd unit status, journal streams, and service controls."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        self._vars["service_manager_status"] = tk.StringVar(value="unknown")
        self._vars["service_services_summary"] = tk.StringVar(value="0/0")
        self._vars["service_last_refresh"] = tk.StringVar(value="never")
        self._vars["service_selected_name"] = tk.StringVar(value="—")
        self._vars["service_selected_state"] = tk.StringVar(value="—")
        self._vars["service_selected_substate"] = tk.StringVar(value="—")
        self._vars["service_selected_install"] = tk.StringVar(value="—")

        status_frame = ttk.LabelFrame(frame, text="Status")
        status_frame.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        status_frame.columnconfigure(1, weight=1)
        status_frame.columnconfigure(3, weight=1)
        ttk.Label(status_frame, text="Manager").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        manager_entry = ttk.Entry(status_frame, textvariable=self._vars["service_manager_status"], state="readonly")
        manager_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(manager_entry, self._vars["service_manager_status"])
        ttk.Label(status_frame, text="Services").grid(row=0, column=2, sticky="w", padx=5, pady=2)
        summary_entry = ttk.Entry(status_frame, textvariable=self._vars["service_services_summary"], state="readonly")
        summary_entry.grid(row=0, column=3, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(summary_entry, self._vars["service_services_summary"])
        ttk.Label(status_frame, text="Last Refresh").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        refresh_entry = ttk.Entry(status_frame, textvariable=self._vars["service_last_refresh"], state="readonly")
        refresh_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(refresh_entry, self._vars["service_last_refresh"])
        ttk.Button(status_frame, text="Refresh", width=9, command=self._service_refresh_status_async).grid(
            row=0, column=4, rowspan=2, padx=(8, 6), pady=2, sticky="nsew"
        )

        services_frame = ttk.LabelFrame(frame, text="Services")
        services_frame.grid(row=2, column=0, padx=4, pady=2, sticky="nsew")
        services_frame.columnconfigure(1, weight=1)
        services_frame.columnconfigure(3, weight=1)
        services_frame.columnconfigure(4, weight=0)
        services_frame.rowconfigure(3, weight=1)
        ttk.Label(services_frame, text="Service").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        selected_name = ttk.Entry(services_frame, textvariable=self._vars["service_selected_name"], state="readonly")
        selected_name.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(selected_name, self._vars["service_selected_name"])
        ttk.Label(services_frame, text="Install").grid(row=0, column=2, sticky="w", padx=5, pady=2)
        ttk.Entry(services_frame, textvariable=self._vars["service_selected_install"], state="readonly", width=16).grid(row=0, column=3, sticky="ew", padx=5, pady=2)
        ttk.Label(services_frame, text="State").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        ttk.Entry(services_frame, textvariable=self._vars["service_selected_state"], state="readonly", width=16).grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        ttk.Label(services_frame, text="Substate").grid(row=1, column=2, sticky="w", padx=5, pady=2)
        ttk.Entry(services_frame, textvariable=self._vars["service_selected_substate"], state="readonly", width=16).grid(row=1, column=3, sticky="ew", padx=5, pady=2)
        self._service_services_tree = ttk.Treeview(
            services_frame,
            columns=("unit", "state", "pid", "description"),
            show="headings",
            selectmode="extended",
            height=9,
        )
        headings = {
            "unit": ("Unit", 150),
            "state": ("State", 70),
            "pid": ("PID", 85),
            "description": ("Description", 320),
        }
        for column, (label, width) in headings.items():
            self._service_services_tree.heading(column, text=label)
            self._service_services_tree.column(column, width=width, minwidth=60, anchor="w")
        service_ysb = ttk.Scrollbar(services_frame, orient="vertical", command=self._service_services_tree.yview)
        service_xsb = ttk.Scrollbar(services_frame, orient="horizontal", command=self._service_services_tree.xview)
        self._service_services_tree.configure(yscrollcommand=service_ysb.set, xscrollcommand=service_xsb.set)
        self._service_services_tree.grid(row=3, column=0, columnspan=4, sticky="nsew")
        service_ysb.grid(row=3, column=4, sticky="ns")
        service_xsb.grid(row=4, column=0, columnspan=4, sticky="ew")
        self._service_services_tree.bind("<<TreeviewSelect>>", self._service_on_tree_select)

        controls_frame = ttk.Frame(services_frame)
        controls_frame.grid(row=5, column=0, columnspan=5, sticky="ew", padx=4, pady=4)
        for column in range(6):
            controls_frame.columnconfigure(column, weight=1 if column >= 3 else 0)
        self._vars["service_action_scope"] = tk.StringVar(value="selected")
        ttk.Label(controls_frame, text="Scope:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Radiobutton(controls_frame, text="Selected", value="selected", variable=self._vars["service_action_scope"]).grid(
            row=0, column=1, sticky="w", padx=(0, 4)
        )
        ttk.Radiobutton(controls_frame, text="All", value="all", variable=self._vars["service_action_scope"]).grid(
            row=0, column=2, sticky="w", padx=(0, 8)
        )
        start_button = ttk.Button(controls_frame, text="Start", command=lambda: self._service_run_action("start"))
        stop_button = ttk.Button(controls_frame, text="Stop", command=lambda: self._service_run_action("stop"))
        restart_button = ttk.Button(controls_frame, text="Restart", command=lambda: self._service_run_action("restart"))
        start_button.grid(row=0, column=3, sticky="ew", padx=2)
        stop_button.grid(row=0, column=4, sticky="ew", padx=2)
        restart_button.grid(row=0, column=5, sticky="ew", padx=2)
        self._service_action_widgets = [start_button, stop_button, restart_button]
        self._vars["service_cmd_preview"] = tk.StringVar(value="Hover over an action to preview the systemctl command")
        ttk.Label(controls_frame, text="Command:").grid(row=1, column=0, columnspan=2, sticky="w", padx=2, pady=(2, 2))
        ttk.Label(controls_frame, textvariable=self._vars["service_cmd_preview"], foreground="grey", font=("TkDefaultFont", 8)).grid(
            row=1, column=2, columnspan=4, sticky="w", padx=2, pady=(2, 2)
        )
        self._service_bind_command_preview(start_button, "start")
        self._service_bind_command_preview(stop_button, "stop")
        self._service_bind_command_preview(restart_button, "restart")

        log_frame = ttk.LabelFrame(frame, text="Journal")
        log_frame.grid(row=3, column=0, padx=4, pady=(2, 2), sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        log_controls = ttk.Frame(log_frame)
        log_controls.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        log_controls.columnconfigure(0, weight=1)
        self._vars["service_log_mode"] = tk.StringVar(value="selected")
        mode_frame = ttk.Frame(log_controls)
        mode_frame.grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Label(mode_frame, text="Logs:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Radiobutton(
            mode_frame, text="Selected", value="selected", variable=self._vars["service_log_mode"],
            command=self._service_on_log_mode_changed,
        ).grid(row=0, column=1, sticky="w", padx=(0, 4))
        ttk.Radiobutton(
            mode_frame, text="All", value="all", variable=self._vars["service_log_mode"],
            command=self._service_on_log_mode_changed,
        ).grid(row=0, column=2, sticky="w")
        self._vars["service_stream_state"] = tk.StringVar(value="paused")
        ttk.Label(log_controls, textvariable=self._vars["service_stream_state"], foreground="grey").grid(
            row=0, column=1, sticky="w", padx=(0, 8)
        )
        ttk.Button(log_controls, text="Stream", command=self._service_stream_start).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(log_controls, text="Pause", command=self._service_stream_pause).grid(row=0, column=3, padx=(0, 4))
        ttk.Button(log_controls, text="Clear", command=self._service_clear_buffer_and_widget).grid(row=0, column=4)

        self._service_log_text = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            wrap="word",
            font=("TkFixedFont", 9),
            background="#f5f5f5",
            exportselection=False,
        )
        self._service_log_text.tag_configure("service_ts", foreground="#6b7280")
        self._service_log_text.tag_configure("service_msg", foreground="#111827")
        self._service_log_text.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self._service_log_text.bind(
            "<Key>",
            lambda event: None if (event.state & 0x4 and event.keysym in ("c", "C", "a", "A")) else "break",
        )
        self._bind_copy_menu(self._service_log_text, allow_paste=False)

        self._service_apply_status({})
        self.root.after(100, self._service_refresh_status_async)

    # ------------------------------------------------------------------ #
    #  DOCKER helpers
    # ------------------------------------------------------------------ #

    def _docker_set_action_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for w in getattr(self, "_docker_action_widgets", []):
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _docker_refresh_status_async(self):
        if self.docker.refresh_busy:
            return
        if not self.mep.is_connected():
            logging.error("DOCKER: MQTT is disconnected; status refresh was not sent")
            return
        self.docker.refresh(callback=lambda response: self._gui_call(self._docker_refresh_complete, response))

    def _docker_refresh_complete(self, response: dict):
        if not response.get("success"):
            logging.error("DOCKER: refresh failed: %s", response.get("error") or "unknown error")
            return
        status = response.get("status_data")
        if isinstance(status, dict):
            self.docker._on_status(status)
        self._docker_apply_status()

    def _docker_apply_status(self):
        if "docker_engine_status" not in self._vars:
            return
        self._vars["docker_engine_status"].set(self.docker.engine_status)
        self._vars["docker_compose_dir"].set(self.docker.compose_dir)
        running = sum(
            1 for service in self.docker.service_names
            if str(self.docker.services.get(service, {}).get("state", "")).lower() == "running"
        )
        self._vars["docker_services_summary"].set(f"{running}/{len(self.docker.service_names)}")
        self._vars["docker_last_refresh"].set(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        selected = self._vars["docker_service"].get().strip()
        if selected and selected not in self.docker.services:
            self._vars["docker_service"].set("")
        self._docker_render_service_list()
        self._docker_apply_selected_service()
        if self.docker.status_error:
            logging.warning("DOCKER: %s", self.docker.status_error)

    def _docker_render_service_list(self):
        tree = getattr(self, "_docker_services_tree", None)
        if tree is None:
            return

        desired_ids = set(self.docker.service_names)
        for index, svc in enumerate(self.docker.service_names):
            row = self.docker.services.get(svc, {})
            values = (
                row.get("state", "—"),
                row.get("container", "—"),
                row.get("command", "—"),
                row.get("ports", "—"),
            )
            if tree.exists(svc):
                if tuple(tree.item(svc, "values")) != tuple(str(value) for value in values):
                    tree.item(svc, values=values)
                if tree.index(svc) != index:
                    tree.move(svc, "", index)
            else:
                tree.insert("", "end", iid=svc, values=values)
        for iid in tree.get_children():
            if iid not in desired_ids:
                tree.delete(iid)

        selected = self._vars["docker_service"].get().strip()
        if selected and selected in self.docker.services and tree.selection() != (selected,):
            self._docker_suppress_tree_stream = True
            tree.selection_set(selected)
            tree.focus(selected)
            tree.see(selected)
            self._docker_suppress_tree_stream = False

    def _docker_on_service_tree_select(self, _event=None):
        tree = getattr(self, "_docker_services_tree", None)
        if tree is None:
            return

        selected = list(tree.selection())
        if selected:
            self._vars["docker_service"].set(selected[0])
        self._docker_apply_selected_service()

        mode = self._vars.get("docker_log_mode", tk.StringVar()).get().strip().lower()
        if not self._docker_suppress_tree_stream:
            mode = self._vars.get("docker_log_mode", tk.StringVar()).get().strip().lower()
            if mode == "selected":
                self._docker_stream_start(restart=self.docker.log_busy)

    def _docker_apply_selected_service(self, *_):
        svc = self._vars.get("docker_service", tk.StringVar()).get().strip()
        row = self.docker.services.get(svc, {}) if svc else {}
        self._vars["docker_selected_state"].set(row.get("state", "—"))
        self._vars["docker_selected_container"].set(row.get("container", "—"))
        self._vars["docker_selected_command"].set(row.get("command", "—"))
        self._vars["docker_selected_ports"].set(row.get("ports", "—"))

    def _docker_selected_services(self) -> list[str]:
        tree = getattr(self, "_docker_services_tree", None)
        if tree is None:
            svc = self._docker_selected_service()
            return [svc] if svc else []

        picked = [s for s in tree.selection() if s in self.docker.services]
        if picked:
            return picked

        svc = self._docker_selected_service()
        return [svc] if svc else []

    def _docker_selected_service(self):
        svc = self._vars.get("docker_service", tk.StringVar()).get().strip()
        return svc or None

    def _docker_confirm_all_action(self, action_label: str) -> bool:
        return messagebox.askokcancel(
            title=f"Docker: {action_label}",
            message=(
                f"This will {action_label.lower()} all services in the compose project.\n\n"
                "Press OK to continue, or Cancel to abort."
            ),
            parent=self.root,
        )

    def _docker_run_compose_action_async(
        self,
        task_name: str,
        *,
        services: list[str] | None = None,
        confirm_all: bool = False,
        force_recreate: bool = False,
    ):
        if self.docker.action_busy:
            logging.warning("DOCKER: another action is already in progress")
            return
        if not self.mep.is_connected() or not self.docker.is_available():
            logging.error("DOCKER: DockerManager is unavailable; action was not sent")
            return

        target_services = [s for s in (services or []) if s]

        if confirm_all:
            if not self._docker_confirm_all_action(task_name.replace("_", " ").capitalize()):
                logging.info("DOCKER: %s cancelled", task_name)
                return
        target_desc = ", ".join(target_services) if target_services else "project"
        self._docker_set_action_busy(True)
        logging.info("DOCKER: requesting %s on %s", task_name, target_desc)

        def _done(response):
            self._docker_set_action_busy(False)
            if response.get("success"):
                logging.info("DOCKER: %s complete on %s", task_name, target_desc)
            else:
                logging.error("DOCKER: %s failed on %s: %s", task_name, target_desc, response.get("error"))
            self._docker_refresh_status_async()
            if self.docker.log_busy:
                self._docker_stream_start(restart=True)

        self.docker.run_action(
            task_name,
            services=target_services or None,
            force_recreate=force_recreate,
            callback=lambda response: self._gui_call(_done, response),
        )

    def _docker_action_targets(self, action: str, *, emit_errors: bool = True):
        scope = self._vars.get("docker_action_scope", tk.StringVar(value="selected")).get().strip().lower()
        if scope == "selected":
            picked = self._docker_selected_services()
            if action == "down":
                if emit_errors:
                    logging.error("DOCKER: 'down' applies to the whole compose project; switch scope to all")
                return None, None, None
            if not picked:
                if emit_errors:
                    logging.error("DOCKER: select at least one service first")
                return None, None, None
            return picked, False, "selected"

        # all scope
        confirm_all = action in ("stop", "restart", "down")
        return [], confirm_all, "all"

    def _docker_preview_command(self, action: str):
        targets, _confirm_all, scope = self._docker_action_targets(action, emit_errors=False)
        if scope is None:
            return "invalid action scope"
        services = targets
        if scope == "all" and action in {"start", "stop", "restart"}:
            services = self.docker.service_names
            if not services:
                return "DockerManager service status unavailable"
        task_name = f"{action}_services" if services else f"{action}_project"
        force_recreate = action == "up" and self._vars.get("docker_up_force_recreate", tk.BooleanVar(value=False)).get()
        return self.docker.preview_action(task_name, services=services, force_recreate=force_recreate)

    def _docker_set_hover_preview(self, text: str = ""):
        sv = self._vars.get("docker_cmd_preview")
        if sv is None:
            return
        if text:
            sv.set(text)
        else:
            sv.set("Hover over an action to preview the DockerManager command")

    def _docker_bind_hover_preview(self, widget, action: str):
        widget.bind("<Enter>", lambda _e: self._docker_set_hover_preview(self._docker_preview_command(action)))
        widget.bind("<Leave>", lambda _e: self._docker_set_hover_preview(""))

    def _docker_run_from_controls(self, action: str):
        targets, confirm_all, scope = self._docker_action_targets(action)
        if scope is None:
            return
        services = targets
        if scope == "all" and action in {"start", "stop", "restart"}:
            services = self.docker.service_names
            if not services:
                logging.error("DOCKER: service status is unavailable; refresh before applying %s to all", action)
                return
        task_name = f"{action}_services" if services else f"{action}_project"
        self._docker_run_compose_action_async(
            task_name,
            services=services,
            confirm_all=bool(confirm_all),
            force_recreate=action == "up" and self._vars.get("docker_up_force_recreate", tk.BooleanVar(value=False)).get(),
        )

    def _docker_stream_start(self, *, restart: bool = False):
        if self.docker.log_busy and not restart:
            self._docker_stream_resume()
            return
        if not self.mep.is_connected() or not self.docker.is_available():
            logging.error("DOCKER: DockerManager is unavailable; log stream was not started")
            return

        mode = self._vars["docker_log_mode"].get().strip().lower() or "selected"
        service = self._docker_selected_service()
        scope = service if mode == "selected" else "all"

        tail_count = "0" if self.docker.log_paused else "30"
        if restart and self.docker.log_scope not in (None, scope):
            tail_count = "15"
            self._docker_clear_buffer_and_widget()

        if mode == "selected" and not service:
            logging.error("DOCKER: select a service to stream selected logs")
            return

        self._vars["docker_stream_state"].set("live")

        def _on_line(_line):
            if self._is_adv_tab_selected("DOC"):
                self._gui_call(self._docker_flush_buffer_to_widget)

        def _on_exit(rc):
            def _done():
                self._vars["docker_stream_state"].set("paused")
                if rc not in (None, 0):
                    logging.warning("DOCKER: log stream exited with code %s", rc)
            self._gui_call(_done)

        self.docker.stream_start(
            services=[service] if mode == "selected" else None,
            tail=tail_count,
            on_line=_on_line,
            on_exit=_on_exit,
        )

    def _docker_stream_pause(self):
        self.docker.log_paused = True
        self.docker.stream_stop()
        self._vars["docker_stream_state"].set("paused")

    def _docker_stream_resume(self):
        if not self.docker.log_busy:
            self._docker_stream_start()
            return
        self.docker.log_paused = False
        self._vars["docker_stream_state"].set("live")
        self._docker_flush_buffer_to_widget()

    def _docker_on_log_mode_changed(self, _event=None):
        if self.docker.log_busy:
            self._docker_stream_start(restart=True)

    def _docker_flush_buffer_to_widget(self):
        if not hasattr(self, "_docker_log_text"):
            return
        if not self._is_adv_tab_selected("DOC"):
            return
        if self.docker.log_paused:
            return

        tail = self.docker.get_new_log_entries()
        if not tail:
            return

        for ts, line in tail:
            clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
            self._docker_log_text.insert("end", f"{ts}  ", ("docker_ts",))
            if " | " in clean:
                svc, msg = clean.split(" | ", 1)
                svc = svc.strip()
                if svc:
                    self._docker_log_text.insert("end", f"{svc} | ", ("docker_svc",))
                self._docker_insert_pretty_log_message(msg)
            else:
                self._docker_insert_pretty_log_message(clean)
            self._docker_log_text.insert("end", "\n")

        self._docker_log_text.see("end")
        self._docker_trim_widget_lines()

    def _docker_insert_pretty_log_message(self, msg: str):
        text = (msg or "").rstrip()

        # Pretty-print JSON payloads when possible.
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
                pretty = json.dumps(parsed, indent=2, sort_keys=True)
                self._docker_log_text.insert("end", pretty, ("docker_json",))
                return
            except Exception:
                pass

        m = re.match(r"^((?:\d{4}-\d{2}-\d{2}[ T])?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\s+(.*)$", text, re.IGNORECASE)
        if m:
            inner_ts, level, rest = m.groups()
            self._docker_log_text.insert("end", f"{inner_ts} ", ("docker_ts",))
            lvl = level.upper()
            lvl_tag = "docker_lvl"
            if lvl in ("ERROR", "CRITICAL"):
                lvl_tag = "docker_err"
            elif lvl in ("WARN", "WARNING"):
                lvl_tag = "docker_warn"
            elif lvl == "INFO":
                lvl_tag = "docker_info"
            self._docker_log_text.insert("end", f"{lvl:<8}", (lvl_tag,))
            self._docker_log_text.insert("end", rest, ("docker_msg",))
            return

        upper = text.upper()
        tag = "docker_msg"
        if any(k in upper for k in ("ERROR", "EXCEPTION", "CRITICAL", "TRACEBACK", "FAILED")):
            tag = "docker_err"
        elif any(k in upper for k in ("WARN", "WARNING")):
            tag = "docker_warn"
        elif "INFO" in upper:
            tag = "docker_info"
        self._docker_log_text.insert("end", text, (tag,))

    def _docker_trim_widget_lines(self, max_lines: int = 800):
        if not hasattr(self, "_docker_log_text"):
            return
        lines = int(self._docker_log_text.index("end-1c").split(".")[0])
        if lines > max_lines:
            self._docker_log_text.delete("1.0", f"{lines - max_lines}.0")

    def _docker_clear_buffer_and_widget(self):
        self.docker.clear_log()
        if hasattr(self, "_docker_log_text"):
            self._docker_log_text.delete("1.0", "end")

    def _build_docker_tab(self, frame: ttk.Frame):
        """DOC tab: compose service status, logs, and service controls."""
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        def _ro_row(parent, row, col, label, key):
            sv = self._vars.get(key)
            if sv is None:
                sv = tk.StringVar(value="—")
                self._vars[key] = sv
            c0 = col * 3
            ttk.Label(parent, text=label).grid(row=row, column=c0, sticky="w", padx=5, pady=2)
            e = ttk.Entry(parent, textvariable=sv, state="readonly", width=24)
            e.grid(row=row, column=c0 + 1, sticky="ew", padx=5, pady=2)
            self._bind_copy_menu(e, sv)

        self._vars["docker_engine_status"] = tk.StringVar(value="Unknown")
        self._vars["docker_compose_dir"] = tk.StringVar(value=self.docker.compose_dir)
        self._vars["docker_services_summary"] = tk.StringVar(value="0/0")
        self._vars["docker_last_refresh"] = tk.StringVar(value="never")

        st_f = ttk.LabelFrame(frame, text="Status")
        st_f.grid(row=0, column=0, padx=4, pady=(4, 2), sticky="ew")
        for c in (1, 4):
            st_f.columnconfigure(c, weight=1)
        st_f.columnconfigure(6, weight=0)

        _ro_row(st_f, 0, 0, "Docker", "docker_engine_status")
        _ro_row(st_f, 0, 1, "Containers", "docker_services_summary")
        _ro_row(st_f, 1, 0, "Compose Dir", "docker_compose_dir")
        _ro_row(st_f, 1, 1, "Last Refresh", "docker_last_refresh")
        ttk.Button(st_f, text="Refresh", width=9, command=self._docker_refresh_status_async).grid(
            row=0, column=6, rowspan=2, padx=(8, 6), pady=2, sticky="nsew"
        )

        svc_f = ttk.LabelFrame(frame, text="Containers")
        svc_f.grid(row=1, column=0, padx=4, pady=(2, 2), sticky="ew")
        for c in (1, 3):
            svc_f.columnconfigure(c, weight=1)
        svc_f.rowconfigure(2, weight=1)

        self._vars["docker_service"] = tk.StringVar(value="")

        self._vars["docker_selected_state"] = tk.StringVar(value="—")
        self._vars["docker_selected_container"] = tk.StringVar(value="—")
        self._vars["docker_selected_command"] = tk.StringVar(value="—")
        self._vars["docker_selected_ports"] = tk.StringVar(value="—")
        ttk.Label(svc_f, text="Container").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        _cn = ttk.Entry(svc_f, textvariable=self._vars["docker_selected_container"], state="readonly")
        _cn.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_cn, self._vars["docker_selected_container"])
        ttk.Label(svc_f, text="State").grid(row=0, column=2, sticky="w", padx=5, pady=2)
        _st = ttk.Entry(svc_f, textvariable=self._vars["docker_selected_state"], state="readonly")
        _st.grid(row=0, column=3, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_st, self._vars["docker_selected_state"])
        ttk.Label(svc_f, text="Command").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        _cmd = ttk.Entry(svc_f, textvariable=self._vars["docker_selected_command"], state="readonly")
        _cmd.grid(row=1, column=1, columnspan=3, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_cmd, self._vars["docker_selected_command"])
        ttk.Label(svc_f, text="Ports").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        _ports = ttk.Entry(svc_f, textvariable=self._vars["docker_selected_ports"], state="readonly")
        _ports.grid(row=2, column=1, columnspan=3, sticky="ew", padx=5, pady=2)
        self._bind_copy_menu(_ports, self._vars["docker_selected_ports"])

        table_f = ttk.Frame(svc_f)
        table_f.grid(row=3, column=0, columnspan=4, sticky="nsew", padx=5, pady=(2, 4))
        table_f.columnconfigure(0, weight=1)
        table_f.rowconfigure(0, weight=1)

        self._docker_services_tree = ttk.Treeview(
            table_f,
            columns=("state", "container", "command", "ports"),
            show="headings",
            selectmode="extended",
            height=8,
        )
        self._docker_services_tree.heading("state", text="State")
        self._docker_services_tree.heading("container", text="Container")
        self._docker_services_tree.heading("command", text="Command")
        self._docker_services_tree.heading("ports", text="Ports")
        self._docker_services_tree.column("state", width=90, minwidth=80, anchor="w")
        self._docker_services_tree.column("container", width=240, minwidth=160, anchor="w")
        self._docker_services_tree.column("command", width=420, minwidth=240, anchor="w")
        self._docker_services_tree.column("ports", width=320, minwidth=180, anchor="w")

        ysb = ttk.Scrollbar(table_f, orient="vertical", command=self._docker_services_tree.yview)
        xsb = ttk.Scrollbar(table_f, orient="horizontal", command=self._docker_services_tree.xview)
        self._docker_services_tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self._docker_services_tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        self._docker_services_tree.bind("<<TreeviewSelect>>", self._docker_on_service_tree_select)

        ctl_f = ttk.LabelFrame(svc_f, text="Controls")
        ctl_f.grid(row=4, column=0, columnspan=4, sticky="ew", padx=5, pady=(0, 4))
        for i in range(5):
            ctl_f.columnconfigure(i, weight=1)

        self._vars["docker_action_scope"] = tk.StringVar(value="selected")
        self._vars["docker_up_force_recreate"] = tk.BooleanVar(value=False)

        scope_f = ttk.Frame(ctl_f)
        scope_f.grid(row=0, column=0, columnspan=2, sticky="w", padx=2, pady=(2, 2))
        ttk.Label(scope_f, text="Scope:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        rb_selected = ttk.Radiobutton(scope_f, text="Selected", value="selected", variable=self._vars["docker_action_scope"])
        rb_selected.grid(row=0, column=1, sticky="w", padx=(0, 4))
        rb_all = ttk.Radiobutton(scope_f, text="All", value="all", variable=self._vars["docker_action_scope"])
        rb_all.grid(row=0, column=2, sticky="w")

        up_opt = ttk.Checkbutton(
            ctl_f,
            text="up: --force-recreate",
            variable=self._vars["docker_up_force_recreate"],
        )
        up_opt.grid(row=0, column=2, columnspan=3, sticky="e", padx=2, pady=(2, 2))

        b_start = ttk.Button(ctl_f, text="Start", command=lambda: self._docker_run_from_controls("start"))
        b_start.grid(row=1, column=0, sticky="ew", padx=(0, 2), pady=(0, 2))
        b_stop = ttk.Button(ctl_f, text="Stop", command=lambda: self._docker_run_from_controls("stop"))
        b_stop.grid(row=1, column=1, sticky="ew", padx=2, pady=(0, 2))
        b_restart = ttk.Button(ctl_f, text="Restart", command=lambda: self._docker_run_from_controls("restart"))
        b_restart.grid(row=1, column=2, sticky="ew", padx=2, pady=(0, 2))
        b_up = ttk.Button(ctl_f, text="Up", command=lambda: self._docker_run_from_controls("up"))
        b_up.grid(row=1, column=3, sticky="ew", padx=2, pady=(0, 2))
        b_down = ttk.Button(ctl_f, text="Down", command=lambda: self._docker_run_from_controls("down"))
        b_down.grid(row=1, column=4, sticky="ew", padx=(2, 0), pady=(0, 2))

        self._vars["docker_cmd_preview"] = tk.StringVar(
            value="Hover over an action to preview the DockerManager command"
        )
        ttk.Label(ctl_f, text="Command:").grid(row=2, column=0, sticky="w", padx=(2, 4), pady=(2, 2))
        ttk.Label(
            ctl_f,
            textvariable=self._vars["docker_cmd_preview"],
            foreground="grey",
            font=("TkDefaultFont", 8),
        ).grid(row=2, column=1, columnspan=4, sticky="w", padx=(0, 2), pady=(2, 2))

        self._docker_action_widgets = [b_start, b_stop, b_restart, b_up, b_down]

        self._docker_bind_hover_preview(b_start, "start")
        self._docker_bind_hover_preview(b_stop, "stop")
        self._docker_bind_hover_preview(b_restart, "restart")
        self._docker_bind_hover_preview(b_up, "up")
        self._docker_bind_hover_preview(b_down, "down")

        log_f = ttk.LabelFrame(frame, text="Logs")
        log_f.grid(row=2, column=0, padx=4, pady=(2, 2), sticky="nsew")
        log_f.columnconfigure(0, weight=1)
        log_f.rowconfigure(1, weight=1)

        log_ctl = ttk.Frame(log_f)
        log_ctl.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        for i in range(6):
            log_ctl.columnconfigure(i, weight=1 if i == 0 else 0)

        self._vars["docker_log_mode"] = tk.StringVar(value="selected")
        mode_f = ttk.Frame(log_ctl)
        mode_f.grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Label(mode_f, text="Logs:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Radiobutton(
            mode_f,
            text="Selected",
            value="selected",
            variable=self._vars["docker_log_mode"],
            command=self._docker_on_log_mode_changed,
        ).grid(row=0, column=1, sticky="w", padx=(0, 4))
        ttk.Radiobutton(
            mode_f,
            text="All",
            value="all",
            variable=self._vars["docker_log_mode"],
            command=self._docker_on_log_mode_changed,
        ).grid(row=0, column=2, sticky="w")

        self._vars["docker_stream_state"] = tk.StringVar(value="paused")
        ttk.Label(
            log_ctl,
            textvariable=self._vars["docker_stream_state"],
            foreground="grey",
            font=("TkFixedFont", 8),
        ).grid(row=0, column=1, sticky="w", padx=(0, 8))

        ttk.Button(log_ctl, text="Stream", command=self._docker_stream_start).grid(
            row=0, column=2, sticky="ew", padx=(0, 4)
        )
        ttk.Button(log_ctl, text="Pause", command=self._docker_stream_pause).grid(
            row=0, column=3, sticky="ew", padx=(0, 4)
        )
        ttk.Button(log_ctl, text="Clear", command=self._docker_clear_buffer_and_widget).grid(
            row=0, column=4, sticky="ew"
        )

        self._docker_log_text = scrolledtext.ScrolledText(
            log_f,
            height=10,
            wrap="word",
            font=("TkFixedFont", 9),
            background="#f5f5f5",
            exportselection=False,
        )
        self._docker_log_text.tag_configure("docker_ts", foreground="#6b7280")
        self._docker_log_text.tag_configure("docker_svc", foreground="#1d4ed8")
        self._docker_log_text.tag_configure("docker_lvl", foreground="#334155")
        self._docker_log_text.tag_configure("docker_msg", foreground="#111827")
        self._docker_log_text.tag_configure("docker_json", foreground="#0f172a")
        self._docker_log_text.tag_configure("docker_info", foreground="#0f766e")
        self._docker_log_text.tag_configure("docker_warn", foreground="#b45309")
        self._docker_log_text.tag_configure("docker_err", foreground="#b91c1c")
        self._docker_log_text.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self._docker_log_text.bind(
            "<Key>",
            lambda e: None if (e.state & 0x4 and e.keysym in ("c", "C", "a", "A")) else "break",
        )
        self._bind_copy_menu(self._docker_log_text, allow_paste=False)

        self._add_copyable_note(
            frame,
            "Source: docker compose project at /opt/radiohound/docker",
            row=3,
            wraplength=420,
        )
        self.root.after(100, self._docker_refresh_status_async)


    # ------------------------------------------------------------------ #
    #  SOC helpers
    # ------------------------------------------------------------------ #

    def _soc_refresh(self):
        threading.Thread(
            target=self.mep.rfsoc.status,
            daemon=True,
        ).start()

    def _soc_apply(self, tlm: dict):
        if "soc_state" not in self._vars:
            return  # SOC tab not yet built; skip until user opens it
        self._vars["soc_state"].set(tlm.get("state", "—"))
        self._vars["soc_state_RX"].set(tlm.get("state_RX", "—"))
        self._vars["soc_state_TX"].set(tlm.get("state_TX", "—"))
        f_c_hz = self._safe_float(tlm.get("f_c_hz"), 0.0)
        f_if_hz = self._safe_float(tlm.get("f_if_hz"), 0.0)
        f_s_hz = self._safe_float(tlm.get("f_s"), 0.0)
        self._vars["soc_fc"].set(f"{f_c_hz/1e6:.3f}")
        self._vars["soc_fif"].set(f"{f_if_hz/1e6:.3f}")
        self._vars["soc_fs"].set(f"{f_s_hz/1e6:.3f}")
        self._vars["soc_pps"].set(str(tlm.get("pps_count", "—")))
        interval = tlm.get("pps_publish_interval", tlm.get("pps_publish_interval_s", "—"))
        self._vars["soc_pps_publish_interval"].set(str(interval) if interval is not None else "—")

        raw_channels = tlm.get("channels", [])
        if isinstance(raw_channels, str):
            channels = [ch.strip() for ch in raw_channels.split(",") if ch.strip()]
        elif isinstance(raw_channels, (list, tuple, set)):
            channels = [str(ch).strip() for ch in raw_channels if str(ch).strip()]
        else:
            channels = []

        channel_with_ports = [
            f"{ch} ({RECORDER_CHANNEL_PORTS.get(ch, '?')})"
            for ch in channels
        ]
        self._vars["soc_channels"].set(", ".join(channel_with_ports) if channel_with_ports else "—")
        # Manual Control checkboxes are staged user intent and intentionally do
        # not mirror live hardware state; Current Status above is the source of truth.

    def _soc_query_pll(self, converter: str, tile: int):
        if hasattr(self, "_soc_pll_text"):
            self._soc_pll_text.delete("1.0", "end")

        after_id = getattr(self, "_soc_pll_after_id", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._soc_pll_after_id = None
        self._soc_pll_pending = (str(converter).lower(), int(tile))

        try:
            self.mep.rfsoc.get_pll_config(converter, tile)
        except Exception as e:
            self._soc_pll_pending = None
            logging.error(f"SOC: PLL query failed ({converter} tile {tile}): {e}")
            return
        label = f"{str(converter).upper()} {tile}"
        self._soc_pll_after_id = self.root.after(
            2000,
            lambda c=str(converter).lower(), t=int(tile): self._soc_pll_query_timeout(c, t),
        )
        logging.info("SOC: requested PLL config for %s", label)

    def _soc_pll_query_timeout(self, converter: str, tile: int):
        pending = getattr(self, "_soc_pll_pending", None)
        self._soc_pll_after_id = None
        if pending != (converter, tile):
            return
        self._soc_pll_pending = None
        if hasattr(self, "_soc_pll_text"):
            self._soc_pll_text.delete("1.0", "end")
            self._soc_pll_text.insert("end", "Blank reply, not enabled in bitstream")
        logging.info("SOC: no PLL reply for %s %s", converter.upper(), tile)

    def _soc_apply_pll_config(self, data: dict):
        if not isinstance(data, dict):
            return
        # Listener is registered globally, but SOC widgets are lazy-built.
        if not hasattr(self, "_soc_pll_text"):
            return
        converter = str(data.get("converter_type", "")).lower()
        try:
            tile = int(data.get("tile"))
        except (TypeError, ValueError):
            return
        pending = getattr(self, "_soc_pll_pending", None)
        if pending != (converter, tile):
            return
        after_id = getattr(self, "_soc_pll_after_id", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._soc_pll_after_id = None
        self._soc_pll_pending = None
        pretty = json.dumps(data, indent=2, sort_keys=True)
        self._soc_pll_text.delete("1.0", "end")
        self._soc_pll_text.insert("end", pretty)

    def _soc_start_stream(self):
        mode = self._vars["soc_start_mode"].get()
        if mode == "pps":
            self.mep.rfsoc.capture_next_pps()
            logging.info("SOC: capture_next_pps sent — will start on next GPS PPS pulse")
        else:
            self.mep.rfsoc.capture_now()
            logging.info("SOC: capture sent — streaming started immediately")

    def _soc_stop_stream(self):
        self.mep.rfsoc.reset()
        logging.info("SOC: reset sent — UDP stream stopped")

    def _soc_set_if_test(self):
        try:
            if_mhz = float(self._vars["soc_if_test"].get().strip())
        except ValueError:
            logging.error("SOC: invalid NCO frequency value")
            return
        orchestrator = self.capture_orchestrator.get_status()
        rx = orchestrator.get("rx") if isinstance(orchestrator.get("rx"), dict) else {}
        sweep_active = rx.get("state") in {"starting", "running"}
        recorder_status = self.mep.recorder.get_status()
        recorder_running = recorder_status.get("state") in {"starting", "recording", "running"}
        if sweep_active or recorder_running:
            logging.warning("SOC: setting NCO frequency during active capture/sweep can disrupt capture")
        self.mep.rfsoc.set_if(if_mhz)
        logging.info(f"SOC: set freq_IF {if_mhz:.3f} MHz sent")

    def _soc_set_freq_metadata(self):
        try:
            freq_mhz = float(self._vars["soc_freq_metadata"].get().strip())
        except ValueError:
            logging.error("SOC: invalid frequency metadata value")
            return
        freq_hz = freq_mhz * 1e6
        self.mep.rfsoc.set_frequency_metadata(freq_hz)
        logging.info(f"SOC: set freq_metadata {freq_hz:.0f} Hz sent")

    def _soc_set_pps_publish_interval(self):
        try:
            raw_value = self._vars["soc_pps_publish_interval_set"].get()
            interval_s = int(raw_value.strip()) if isinstance(raw_value, str) else int(raw_value)
        except (TypeError, ValueError):
            logging.error("SOC: invalid PPS publish interval (must be integer seconds)")
            return
        if interval_s < 0:
            logging.error("SOC: PPS publish interval must be >= 0 seconds")
            return
        self.mep.rfsoc.set_pps_publish_interval(interval_s)
        logging.info("SOC: set_pps_publish_interval %d sent", interval_s)

    def _soc_set_channels(self):
        selected = [ch for ch in ("A", "B", "C", "D") if self._vars[f"soc_ch_{ch}"].get()]
        if not selected:
            logging.warning("SOC: no channels selected — at least one channel required")
            return
        channel_str = ",".join(selected)
        self.mep.rfsoc.set_channel(channel_str)
        logging.info(f"SOC: set channel {channel_str} sent — restart UDP stream to apply")

    # ------------------------------------------------------------------ #
    #  TX helpers
    # ------------------------------------------------------------------ #

    def _tx_apply(self, tlm: dict):
        if "tx_st_transmitting" not in self._vars:
            return  # TX tab not yet built
        if not isinstance(tlm, dict):
            # Never leave a stale transmit claim on screen when the payload is unusable.
            self._vars["tx_st_transmitting"].set("Unknown")
            return

        ch = tlm.get("tx_channels") or []
        raw_tx_state = tlm.get("state_TX")
        if raw_tx_state == "active":
            names = ",".join(str(c) for c in ch)
            self._vars["tx_st_transmitting"].set(
                f"Transmitting on {names}" if names else "Transmitting"
            )
        elif tlm.get("state") != "offline" and raw_tx_state is not None:
            self._vars["tx_st_transmitting"].set("Not transmitting")
        else:
            self._vars["tx_st_transmitting"].set("Unknown")

        self._vars["tx_st_channels"].set(str(ch) if ch else "-")

        cf = tlm.get("tx_center_freq")
        self._vars["tx_st_center_freq"].set(f"{cf:.3f}" if cf is not None else "-")

        of = tlm.get("tx_offset_freq")
        self._vars["tx_st_offset_freq"].set(f"{of:.3f}" if of is not None else "-")

        amp = tlm.get("tx_amplitude_bins")
        self._vars["tx_st_amplitude"].set(str(amp) if amp is not None else "-")

    def _tx_start_update_click(self):
        """Apply the staged TX settings and begin (or update) transmission."""
        try:
            center_mhz = float(self._vars["tx_center_freq"].get().strip())
            offset_mhz = float(self._vars["tx_offset_freq"].get().strip())
            amplitude = int(self._vars["tx_amplitude_bins"].get())
        except (ValueError, tk.TclError):
            logging.error("TX: invalid staged value; start/update aborted")
            return
        channel = self._vars["tx_channel"].get().strip()
        tuner_enabled, adc_if_mhz, injection = self._parse_tuner_params()
        self._vars["tx_st_transmitting"].set("Requesting start...")
        self.capture_orchestrator.start_tx(
            channel=channel,
            center_freq_mhz=center_mhz,
            offset_freq_mhz=offset_mhz,
            amplitude_bins=amplitude,
            external_tuner_enabled=tuner_enabled,
            adc_if_mhz=adc_if_mhz,
            injection=injection,
            callback=lambda response: self._gui_call(self._handle_workflow_response, "TX", response),
        )

    def _tx_stop_click(self):
        """Disable all TX output."""
        self._vars["tx_st_transmitting"].set("Requesting stop...")
        self.capture_orchestrator.stop_tx(
            callback=lambda response: self._gui_call(self._handle_workflow_response, "TX", response)
        )

    def _handle_workflow_response(self, signal_path: str, response: dict):
        if response.get("success"):
            logging.info("%s workflow request completed", signal_path)
            return
        error = response.get("error") or "service request failed"
        logging.error("%s workflow request failed: %s", signal_path, error)
        if signal_path == "RX":
            self._status_var.set(f"RX failed: {error}")
        elif "tx_st_transmitting" in self._vars:
            self._vars["tx_st_transmitting"].set(f"Failed: {error}")

    # ------------------------------------------------------------------ #
    #  TUN helpers
    # ------------------------------------------------------------------ #

    def _tun_refresh(self):
        if "tun_state" not in self._vars:
            return  # TUN tab not yet built
        status = self.mep.tuner.get_status()

        if not status:
            self._vars["tun_state"].set("—")
            self._vars["tun_name"].set("—")
            text = "no status received"
        else:
            self._vars["tun_state"].set(str(status.get("state", "—")))
            name_val = status.get("name", status.get("backend", "—"))
            self._vars["tun_name"].set(str(name_val) if name_val else "—")

            freq_val = self._safe_float(status.get("frequency_mhz"))
            if freq_val is not None and not self._vars["tun_set_freq"].get().strip():
                self._vars["tun_set_freq"].set(str(freq_val))

            pwr_val = self._safe_float(status.get("power_dbm"))
            if pwr_val is not None and not self._vars["tun_set_power"].get().strip():
                self._vars["tun_set_power"].set(str(pwr_val))

            if status and status.get("lock_supported"):
                lock_status = status.get("lock_status")
                if isinstance(lock_status, dict) and lock_status:
                    locked = all(bool(value) for value in lock_status.values())
                    detail = ", ".join(
                        f"{key}={'LOCK' if bool(value) else 'UNLOCK'}"
                        for key, value in lock_status.items()
                    )
                    self._vars["tun_lock_status"].set(
                        f"{'Locked' if locked else 'Unlocked'} ({detail})"
                    )
                else:
                    self._vars["tun_lock_status"].set("Unavailable")
            else:
                self._vars["tun_lock_status"].set("Unsupported")

            lines = []
            for k, v in (status or {}).items():
                if k == "info":
                    continue
                if isinstance(v, dict):
                    lines.append(f"{k}:")
                    for sk, sv in v.items():
                        if sk == "info":
                            continue
                        lines.append(f"  {sk}: {sv}")
                else:
                    lines.append(f"{k}: {v}")
            info = (status or {}).get("info")
            if info:
                lines.append("--- info ---")
                lines.append(str(info).replace("\\r\\n", "\n").replace("\r\n", "\n"))
            text = "\n".join(lines)

        if text != self._tun_status_signature:
            yview = self._tun_status_text.yview()
            self._tun_status_text.configure(state="normal")
            self._tun_status_text.delete("1.0", "end")
            self._tun_status_text.insert("end", text)
            self._tun_status_text.configure(state="disabled")
            if yview:
                self._tun_status_text.yview_moveto(yview[0])
            self._tun_status_signature = text
            logging.info("TUN: status text updated")

    def _tun_handle_response(self, data: dict):
        if "tun_set_freq" not in self._vars:
            return  # TUN tab not yet built
        task = data.get("task_name", "")
        value = data.get("value")
        if value is None:
            return
        if task == "get_frequency":
            self._vars["tun_set_freq"].set(str(value))
            logging.info(f"TUN: freq = {value} MHz")
        elif task == "get_power":
            self._vars["tun_set_power"].set(str(value))
            logging.info(f"TUN: power = {value} dBm")
        elif task == "get_lock_status":
            if isinstance(value, dict):
                locks = [bool(v) for v in value.values() if isinstance(v, bool)]
                if locks:
                    state = "Locked" if all(locks) else "Unlocked"
                    detail = ", ".join(
                        f"{key}={'LOCK' if bool(val) else 'UNLOCK'}"
                        for key, val in value.items()
                    )
                    self._vars["tun_lock_status"].set(f"{state} ({detail})")
                else:
                    self._vars["tun_lock_status"].set(str(value))
            else:
                self._vars["tun_lock_status"].set(str(value))
            logging.info(f"TUN: lock status = {self._vars['tun_lock_status'].get()}")

    def _tun_init(self):
        self.mep.tuner.initialize()
        logging.info("TUN: initialize sent")

    def _tun_discover(self):
        self.mep.tuner.discover()
        logging.info("TUN: discover sent")

    def _tun_set_freq(self):
        try:
            freq = float(self._vars["tun_set_freq"].get())
        except ValueError:
            logging.error("TUN: invalid frequency value")
            return
        self.mep.tuner.set_frequency(freq)
        logging.info(f"TUN: set_frequency {freq:.3f} MHz sent")

    def _tun_get_freq(self):
        self.mep.tuner.get_frequency()
        logging.info("TUN: get_frequency sent")

    def _tun_set_power(self):
        try:
            pwr = float(self._vars["tun_set_power"].get())
        except ValueError:
            logging.error("TUN: invalid power value")
            return
        self.mep.tuner.set_power(pwr)
        logging.info(f"TUN: set_power {pwr:.1f} dBm sent")

    def _tun_get_power(self):
        self.mep.tuner.get_power()
        logging.info("TUN: get_power sent")

    def _tun_check_lock(self):
        self.mep.tuner.get_lock_status()
        logging.info("TUN: get_lock_status sent")

    def _tun_send_status(self):
        self.mep.tuner.status()
        logging.info("TUN: status command sent")

    def _tun_update_capability_buttons(self):
        capabilities = self.mep.tuner.get_status().get("capabilities", {})
        controls = [
            (getattr(self, "_tuner_power_widgets", []), capabilities.get("power", False)),
            (getattr(self, "_tuner_lock_widgets", []), capabilities.get("lock_status", False)),
        ]
        for widgets, supported in controls:
            for widget in widgets:
                try:
                    widget.configure(state="normal" if supported else "disabled")
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    #  REC helpers
    # ------------------------------------------------------------------ #

    def _on_rec_sample_rate_change(self, *_):
        if "rec_config_source" not in self._vars or getattr(self, "_rec_trace_busy", False):
            return
        self._rec_pending_overrides.clear()
        self._rec_load_preset()

    def _rec_sample_rate_mhz(self) -> int:
        return int(float(self._vars["sample_rate_mhz"].get()))

    def _rec_collect_draft(self) -> dict:
        """Collect widget values for CaptureOrchestrator validation."""
        return {
            "batch_size": self._vars["sg_batch_size"].get(),
            "max_packet_size": self._vars["sg_max_packet_size"].get(),
            "chunk_size": self._vars["sg_chunk_size"].get(),
            "batch_capacity": self._vars["sg_batch_capacity"].get(),
            "buffer_size": self._vars["sg_buffer_size"].get(),
            "worker_thread_number": self._vars["sg_worker_threads"].get(),
            "nperseg": self._vars["sg_nperseg"].get(),
            "nfft": self._vars["sg_nfft"].get(),
            "noverlap": self._vars["sg_noverlap"].get(),
            "window": self._vars["sg_window"].get(),
            "reduce_op": self._vars["sg_reduce_op"].get(),
            "num_spectra_per_chunk": self._vars["sg_num_spectra_per_chunk"].get(),
            "num_spectra_per_output": self._vars["sg_spectra_per_output"].get(),
            "snr_db_min": self._vars["sg_snr_min"].get(),
            "snr_db_max": self._vars["sg_snr_max"].get(),
            "cmap": self._vars["sg_cmap"].get(),
            "dpi": self._vars["sg_dpi"].get(),
            "figsize": self._vars["sg_figsize"].get(),
            "compute": self._vars["sg_compute"].get(),
            "mqtt": self._vars["sg_mqtt"].get(),
            "output": self._vars["sg_output"].get(),
            "digital_rf": self._vars["sg_digital_rf"].get(),
            "metadata": self._vars["sg_metadata"].get(),
        }

    def _rec_set_draft_values(self, values: dict):
        self._rec_trace_busy = True
        try:
            field_map = {
                "sg_batch_size": "batch_size",
                "sg_max_packet_size": "max_packet_size",
                "sg_chunk_size": "chunk_size",
                "sg_batch_capacity": "batch_capacity",
                "sg_buffer_size": "buffer_size",
                "sg_worker_threads": "worker_thread_number",
                "sg_nperseg": "nperseg",
                "sg_nfft": "nfft",
                "sg_noverlap": "noverlap",
                "sg_window": "window",
                "sg_reduce_op": "reduce_op",
                "sg_num_spectra_per_chunk": "num_spectra_per_chunk",
                "sg_spectra_per_output": "num_spectra_per_output",
                "sg_snr_min": "snr_db_min",
                "sg_snr_max": "snr_db_max",
                "sg_cmap": "cmap",
                "sg_dpi": "dpi",
            }
            for widget_key, value_key in field_map.items():
                self._vars[widget_key].set(str(values[value_key]))
            self._vars["sg_figsize"].set(
                ",".join(f"{float(value):g}" for value in values["figsize"])
            )
            self._vars["sg_compute"].set(bool(values["compute"]))
            self._vars["sg_mqtt"].set(bool(values["mqtt"]))
            self._vars["sg_output"].set(bool(values["output"]))
            self._vars["sg_digital_rf"].set(bool(values["digital_rf"]))
            self._vars["sg_metadata"].set(bool(values["metadata"]))
        finally:
            self._rec_trace_busy = False

    def _rec_clear_draft_values(self):
        self._rec_trace_busy = True
        try:
            for key in (
                "sg_batch_size", "sg_max_packet_size", "sg_chunk_size",
                "sg_batch_capacity", "sg_buffer_size", "sg_worker_threads",
                "sg_nperseg", "sg_nfft", "sg_noverlap", "sg_window", "sg_reduce_op",
                "sg_num_spectra_per_chunk", "sg_spectra_per_output", "sg_snr_min",
                "sg_snr_max", "sg_cmap", "sg_dpi", "sg_figsize",
            ):
                self._vars[key].set("")
            self._vars["sg_compute"].set(False)
            self._vars["sg_mqtt"].set(False)
            self._vars["sg_output"].set(False)
            self._vars["sg_digital_rf"].set(False)
            self._vars["sg_metadata"].set(False)
        finally:
            self._rec_trace_busy = False

    def _rec_render_model(self, model: dict, load_values: bool = False):
        preset_path = model.get("preset_path", "—")
        if not model.get("available"):
            self._vars["rec_config_source"].set(f"Not Found: {preset_path}")
            self._vars["rec_draft_error"].set("")
            for key in (
                "calc_freq_formula", "calc_hop_formula", "calc_segments",
                    "calc_cadence_formula", "calc_resamplers", "calc_waterfall_formula",
                    "calc_throughput_formula",
            ):
                self._vars[key].set("—")
            self._rec_stage_button.configure(state="disabled")
            return

        self._vars["rec_config_source"].set(preset_path)
        if model.get("draft_valid") is False:
            self._vars["rec_draft_error"].set(
                f"Fix input: {model.get('draft_error', 'invalid value')}"
            )
            self._rec_stage_button.configure(state="disabled")
            return

        self._vars["rec_draft_error"].set("")
        self._rec_stage_button.configure(state="normal")
        if load_values:
            self._rec_set_draft_values(model["values"])

        metrics = model["metrics"]
        self._vars["calc_freq_formula"].set(
            "{frequency_bins:,} bins | {frequency_resolution_hz:,.3f} Hz/bin".format(
                **metrics
            )
        )
        self._vars["calc_hop_formula"].set(
            "{fft_hop_samples:,} samples | {hop_us:,.3f} us".format(
                hop_us=metrics["fft_hop_time_s"] * 1e6, **metrics
            )
        )
        self._vars["calc_segments"].set(
            f"{metrics['segments_per_row']:,} STFT segments/row"
        )
        self._vars["calc_cadence_formula"].set(
            "samples/row: {samples_per_row:,}\n"
            "scan: {scan_time_s:.6g} s/row\n"
            "rate: {spectrum_rate_hz:,.6g} rows/s".format(**metrics)
        )
        batch_size = int(model["values"]["batch_size"])
        chunk_size = int(model["values"]["chunk_size"])
        if batch_size > 0 and chunk_size % batch_size == 0:
            samples_per_packet = chunk_size // batch_size
            self._vars["calc_throughput_formula"].set(
                f"Throughput relation: {batch_size:,} packets/batch × "
                f"{samples_per_packet:,} samples/packet = {chunk_size:,} samples/chunk"
            )
        else:
            self._vars["calc_throughput_formula"].set(
                "Throughput relation: current batch size and chunk size do not divide exactly"
            )
        stages = model.get("enabled_resamplers", [])
        stage_text = ", ".join(
            f"{stage['name']} {stage['up']}/{stage['down']}" for stage in stages
        ) or "none"
        self._vars["calc_resamplers"].set(
            f"Configured chain: {stage_text}\n"
            f"packet.num_samples -> chunk after chain: {metrics['effective_chunk_size']:,}"
        )
        self._vars["calc_waterfall_formula"].set(
            "{waterfall_duration_s:,.6g} s | {waterfall_rows:,} rows x "
            "{frequency_bins:,} bins".format(**metrics)
        )

    def _rec_request_presets(self):
        self.capture_orchestrator.list_recorder_presets(
            callback=lambda response: self._gui_call(
                self._rec_handle_presets_response, response
            )
        )

    def _rec_handle_presets_response(self, response: dict):
        status_data = response.get("status_data") if isinstance(response, dict) else None
        sample_rates = status_data.get("sample_rates") if isinstance(status_data, dict) else None
        try:
            values = tuple(str(int(rate)) for rate in sample_rates)
            valid_sample_rates = bool(values) and all(int(rate) > 0 for rate in values)
        except (TypeError, ValueError):
            values = ()
            valid_sample_rates = False
        if not isinstance(response, dict) or not response.get("success") or not valid_sample_rates:
            if "REC" not in self._tabs_built:
                logging.warning(
                    "REC: %s",
                    response.get("error") if isinstance(response, dict) else "Recorder presets unavailable",
                )
                return
            model = {
                "available": False,
                "preset_path": "—",
                "error": response.get("error") if isinstance(response, dict) else "Recorder presets unavailable",
            }
            if not model["error"]:
                model["error"] = "Recorder presets unavailable"
            self._rec_clear_draft_values()
            self._rec_render_model(model, load_values=True)
            logging.warning("REC: %s", model["error"])
            return

        self._rec_presets_loaded = True
        current = self._vars["sample_rate_mhz"].get()
        self._sample_rate_combo.configure(values=values)
        self._rec_trace_busy = True
        try:
            self._vars["sample_rate_mhz"].set(current if current in values else values[0])
        finally:
            self._rec_trace_busy = False
        if "REC" in self._tabs_built:
            self._rec_load_preset()

    @staticmethod
    def _rec_response_model(response: dict) -> dict:
        model = response.get("status_data") if isinstance(response, dict) else None
        if (
            isinstance(response, dict)
            and response.get("success")
            and isinstance(model, dict)
            and isinstance(model.get("available"), bool)
            and (
                not model["available"]
                or (
                    isinstance(model.get("values"), dict)
                    and isinstance(model.get("metrics"), dict)
                )
            )
        ):
            return model
        return {
            "available": False,
            "preset_path": "—",
            "error": (
                response.get("error") or "Recorder settings unavailable"
                if isinstance(response, dict)
                else "Recorder settings unavailable"
            ),
        }

    def _rec_load_preset(self):
        self._rec_preview_request_seq += 1
        request_seq = self._rec_preview_request_seq
        self.capture_orchestrator.preview_recorder_settings(
            self._rec_sample_rate_mhz(),
            callback=lambda response: self._gui_call(
                self._rec_handle_load_response, request_seq, response
            ),
        )

    def _rec_handle_load_response(self, request_seq: int, response: dict):
        if request_seq != self._rec_preview_request_seq:
            return
        model = self._rec_response_model(response)
        if not model.get("available"):
            self._rec_clear_draft_values()
            logging.warning("REC: %s", model.get("error"))
        self._rec_render_model(model, load_values=True)

    def _rec_preview_draft(self, *_):
        if getattr(self, "_rec_trace_busy", False):
            return
        self._rec_preview_request_seq += 1
        request_seq = self._rec_preview_request_seq
        self.capture_orchestrator.preview_recorder_settings(
            self._rec_sample_rate_mhz(),
            self._rec_collect_draft(),
            callback=lambda response: self._gui_call(
                self._rec_handle_preview_response, request_seq, response
            ),
        )

    def _rec_handle_preview_response(self, request_seq: int, response: dict):
        if request_seq != self._rec_preview_request_seq:
            return
        self._rec_render_model(self._rec_response_model(response))

    def _stage_rec_settings(self):
        self._rec_preview_request_seq += 1
        request_seq = self._rec_preview_request_seq
        self._rec_stage_button.configure(state="disabled")
        self.capture_orchestrator.preview_recorder_settings(
            self._rec_sample_rate_mhz(),
            self._rec_collect_draft(),
            callback=lambda response: self._gui_call(
                self._rec_handle_stage_response, request_seq, response
            ),
        )

    def _rec_handle_stage_response(self, request_seq: int, response: dict):
        if request_seq != self._rec_preview_request_seq:
            return
        model = self._rec_response_model(response)
        if (
            not model.get("available")
            or not model.get("draft_valid", False)
            or not isinstance(model.get("overrides"), dict)
        ):
            self._rec_render_model(model)
            messagebox.showerror(
                "REC Settings",
                model.get("draft_error") or model.get("error") or "Invalid settings",
            )
            return
        self._rec_pending_overrides = dict(model["overrides"])
        self._rec_render_model(model)
        logging.info("REC: settings will apply on next recorder start")

    def _rec_reset_config(self):
        self._rec_pending_overrides.clear()
        self._rec_load_preset()
        logging.info("REC: staged overrides cleared; selected preset restored")

    def _rec_status_ui_update(self, data: dict):
        """Update REC tab widgets from recorder status (only called after tab is built)."""
        if "rec_status" not in self._vars:
            return  # REC tab not yet built
        self._vars["rec_status"].set(data.get("state", "—"))

    # ------------------------------------------------------------------ #
    #  Tuner trace
    # ------------------------------------------------------------------ #

    def _on_tuner_change(self, *_):
        tuner_enabled = self._vars["tuner_selection"].get() != "Disabled"
        state = "normal" if tuner_enabled else "disabled"
        for if_entry in self._if_entries:
            if_entry.configure(state=state)
        for injection_combo in self._injection_combos:
            injection_combo.configure(state="readonly" if tuner_enabled else "disabled")
        self._update_synth_lo()

    def _update_synth_lo(self, *_):
        if self._vars["tuner_selection"].get() == "Disabled":
            self._vars["synth_lo"].set("—")
            return
        try:
            rf_mhz = float(self._synth_lo_rf_source_mhz())
            if_mhz = float(self._vars["adc_if_mhz"].get())
            mode   = self._vars["injection_mode"].get()
            lo_mhz = rf_mhz + if_mhz if str(mode).lower() == "high" else rf_mhz - if_mhz
            self._vars["synth_lo"].set(f"{lo_mhz:.3f}")
        except ValueError:
            self._vars["synth_lo"].set("—")

    def _synth_lo_rf_source_mhz(self) -> str:
        """Frequency feeding the Synth LO preview: TX's center freq when the TX
        tab is active, RX's Start field otherwise (the field is shared/synced,
        but the two tabs plan for different legs of the one physical oscillator).
        """
        if hasattr(self, "_top_notebook") and "tx_center_freq" in self._vars:
            try:
                current = self._top_notebook.tab(self._top_notebook.select(), "text")
            except tk.TclError:
                current = "RX"
            if current == "TX":
                return self._vars["tx_center_freq"].get()
        return self._vars["freq_start"].get()

    def _toggle_advanced(self):
        if self._adv_frame.winfo_viewable():
            self._adv_frame.grid_remove()
            self._adv_btn_text.set("Show Advanced Options \u25b6")
        else:
            self._ensure_tab_built(self._get_current_tab_text())
            self._adv_frame.grid()
            self._adv_btn_text.set("Hide Advanced Options \u25c0")
        # Tk freezes the toplevel at its last actual size once mapped/resized;
        # releasing the geometry lets it recompute to fit current content.
        self.root.update_idletasks()
        self.root.geometry("")

    # ------------------------------------------------------------------ #
    #  Logging
    # ------------------------------------------------------------------ #

    def _setup_logging(self):
        handler = _TextHandler(self._log_text)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                              datefmt="%H:%M:%S")
        )
        # Root logger stays permissive (DEBUG) so records of every level reach the
        # handlers; the GUI panel's own threshold is what the user controls via the
        # Log "Level" combo. This keeps verbosity a presentation-layer decision.
        handler.setLevel(logging.INFO)
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)
        self._text_log_handler = handler

    def _apply_log_level(self):
        """Set the GUI log panel's verbosity from the Level combo (handler only)."""
        level = self._vars["log_level"].get()
        self._text_log_handler.setLevel(getattr(logging, level, logging.INFO))

    def _pump_text_log(self):
        """Drain queued log records onto Tk widgets on the main thread."""
        handler = getattr(self, "_text_log_handler", None)
        if handler is not None:
            try:
                handler.flush_pending()
            except Exception:
                pass
        self._mqtt_flush_buffer_to_widget()
        try:
            self.root.after(50, self._pump_text_log)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    #  Parameter parsing
    # ------------------------------------------------------------------ #

    def _parse_tuner_params(self) -> tuple:
        """Return (enabled, adc_if_mhz, injection) from the shared tuner controls."""
        tuner_enabled = self._vars["tuner_selection"].get() != "Disabled"
        adc_if_s = self._vars["adc_if_mhz"].get().strip()
        adc_if_mhz = float(adc_if_s) if (adc_if_s and tuner_enabled) else None
        injection = self._vars["injection_mode"].get().lower()
        return tuner_enabled, adc_if_mhz, injection

    def _parse_single_params(self) -> dict:
        freq_start = float(self._vars["freq_start"].get())
        channel    = self._vars["channel"].get()
        tuner_enabled, adc_if_mhz, injection = self._parse_tuner_params()

        capture_name_s = self._vars["capture_name"].get().strip()
        capture_name   = capture_name_s if capture_name_s else None

        sample_rate_mhz = int(self._vars["sample_rate_mhz"].get())
        dwell_enabled   = self._vars["dwell_enabled"].get()
        dwell_raw       = self._vars["dwell"].get().strip()

        try:
            dwell_s = float(dwell_raw) if (dwell_enabled and dwell_raw) else None
        except ValueError:
            raise ValueError(f"Invalid dwell value: {dwell_raw!r}")

        if dwell_s is not None and dwell_s <= 0:
            dwell_s = None

        return {
            "freq_start":       freq_start,
            "channel":          channel,
            "tuner_enabled":    tuner_enabled,
            "adc_if_mhz":       adc_if_mhz,
            "capture_name":     capture_name,
            "sample_rate_mhz":  sample_rate_mhz,
            "injection":        injection,
            "dwell":            dwell_s,
        }

    def _parse_sweep_params(self) -> dict:
        freq_start = float(self._vars["freq_start"].get())
        freq_end_s = self._vars["freq_end"].get().strip()
        freq_end   = float(freq_end_s) if freq_end_s else float("nan")
        step       = float(self._vars["step"].get())
        dwell      = float(self._vars["dwell"].get())

        channel    = self._vars["channel"].get()
        tuner_enabled, adc_if_mhz, injection = self._parse_tuner_params()

        capture_name_s = self._vars["capture_name"].get().strip()
        capture_name   = capture_name_s if capture_name_s else None

        sample_rate_mhz = int(self._vars["sample_rate_mhz"].get())

        return {
            "freq_start":       freq_start,
            "freq_end":         freq_end,
            "step":             step,
            "dwell":            dwell,
            "channel":          channel,
            "tuner_enabled":    tuner_enabled,
            "adc_if_mhz":       adc_if_mhz,
            "capture_name":     capture_name,
            "sample_rate_mhz":  sample_rate_mhz,
            "injection":        injection,
        }

    # ------------------------------------------------------------------ #
    #  Button handlers
    # ------------------------------------------------------------------ #

    def _orchestrator_rx_settings(self, params: dict, *, sweep: bool) -> dict:
        tuner_enabled = params["tuner_enabled"]
        acquisition = {
            "mode": "sweep" if sweep else "single",
            "rf_frequency_hz": int(params["freq_start"] * 1e6),
        }
        if sweep:
            acquisition["sweep"] = {
                "end_frequency_hz": int(params["freq_end"] * 1e6),
                "step_hz": int(params["step"] * 1e6),
                "dwell_s": params["dwell"],
            }
        settings = {
            "capture": {"name": params["capture_name"]},
            "acquisition": acquisition,
            "receive": {
                "rfsoc_channel": params["channel"],
                "adc_sample_rate_mhz": params["sample_rate_mhz"],
                "conjugate_policy": self._vars["conjugate_policy"].get() or CONJUGATE_POLICY_DEFAULT,
                "external_tuner": {
                    "enabled": tuner_enabled,
                    "adc_if_mhz": params["adc_if_mhz"] if tuner_enabled else None,
                    "injection": params["injection"] if tuner_enabled else None,
                },
            },
            "recorder": {"overrides": dict(self._rec_pending_overrides)},
        }
        return settings

    # ------------------------------------------------------------------ #
    #  Button handlers
    # ------------------------------------------------------------------ #

    def _set_rx_sweep_enabled(self, enabled: bool):
        self._vars["end_enabled"].set(enabled)
        self._vars["step_enabled"].set(enabled)
        state = "normal" if enabled else "disabled"
        self._rx_end_entry.config(state=state)
        self._rx_step_entry.config(state=state)
        self._set_rx_dwell_enabled(enabled)

    def _toggle_rx_end(self):
        self._set_rx_sweep_enabled(self._vars["end_enabled"].get())

    def _toggle_rx_step(self):
        self._set_rx_sweep_enabled(self._vars["step_enabled"].get())

    def _set_rx_dwell_enabled(self, enabled: bool):
        self._vars["dwell_enabled"].set(enabled)
        self._rx_dwell_entry.config(state="normal" if enabled else "disabled")

    def _toggle_rx_dwell(self):
        enabled = self._vars["dwell_enabled"].get()
        if not enabled:
            self._set_rx_sweep_enabled(False)
        else:
            self._set_rx_dwell_enabled(True)

    def _start(self):
        if self._vars["end_enabled"].get() and self._vars["step_enabled"].get():
            self._start_sweep()
        else:
            self._start_single()

    def _start_sweep(self):
        try:
            params = self._parse_sweep_params()
            settings = self._orchestrator_rx_settings(params, sweep=True)
        except (TypeError, ValueError, OverflowError) as e:
            logging.error(f"Parameter error: {e}")
            return
        self._status_var.set("Requesting RX sweep...")
        self.capture_orchestrator.start_rx(
            settings=settings,
            callback=lambda response: self._gui_call(self._handle_workflow_response, "RX", response),
        )

    def _start_single(self):
        try:
            params = self._parse_single_params()
            settings = self._orchestrator_rx_settings(params, sweep=False)
        except (TypeError, ValueError, OverflowError) as e:
            logging.error(f"Parameter error: {e}")
            return
        self._status_var.set("Requesting RX capture...")
        self.capture_orchestrator.start_rx(
            settings=settings,
            dwell_s=params["dwell"],
            callback=lambda response: self._gui_call(self._handle_workflow_response, "RX", response),
        )

    def _stop_all(self):
        self._status_var.set("Requesting stop...")
        self.capture_orchestrator.abort(
            callback=lambda response: self._gui_call(self._handle_workflow_response, "RX", response)
        )

    # ------------------------------------------------------------------ #
    #  Housekeeping polling (Jetson health only)
    # ------------------------------------------------------------------ #

    def _schedule_housekeeping(self):
        pass

    def _poll_housekeeping(self):
        pass


# ===== ENTRY POINT ===== #

def main():
    parser = argparse.ArgumentParser(description="MEP Control GUI")
    parser.add_argument("--mqtt_host", type=str, default=MEPClient.DEFAULT_HOST,
                        help=f"MQTT broker host (default: {MEPClient.DEFAULT_HOST})")
    parser.add_argument("--mqtt_port", type=int, default=MEPClient.DEFAULT_PORT,
                        help=f"MQTT broker port (default: {MEPClient.DEFAULT_PORT})")
    args = parser.parse_args()

    root = tk.Tk()
    print("Loading MEP Control App...", flush=True)
    app  = MEPGui(root, mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port)
    print("  Initialization complete — starting event loop.", flush=True)

    close_state = {"ran": False}

    def _on_close():
        close_state["ran"] = True
        logging.info("Window closed — cleaning up")
        app._gui_queue_closed = True
        handler = getattr(app, "_text_log_handler", None)
        if handler is not None:
            try:
                logging.getLogger().removeHandler(handler)
            except Exception:
                pass
            try:
                handler.close()
            except Exception:
                pass
        try:
            if getattr(app, "tx", None) is not None:
                app.tx.stop()
        except Exception as e:
            logging.debug(f"Exception stopping TX during cleanup: {e}")
        try:
            app._spec_is_active = False
            app._spec_stop_render_loop()
        except Exception as e:
            logging.debug(f"Exception stopping SPEC during cleanup: {e}")
        try:
            if getattr(app, "docker", None) is not None and app.docker.log_busy:
                app.docker.stream_stop()
        except Exception as e:
            logging.debug(f"Exception stopping Docker log stream during cleanup: {e}")
        try:
            if getattr(app, "service_manager", None) is not None and app.service_manager.log_busy:
                app.service_manager.stream_stop()
        except Exception as e:
            logging.debug(f"Exception stopping service journal stream during cleanup: {e}")
        try:
            if getattr(app, "bus", None) is not None:
                app.bus.disconnect()
        except Exception as e:
            logging.debug(f"Exception during cleanup: {e}")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    # Programmatic "Minimize/Restore" to wake up XQuartz on first render.
    # 200ms lets the event loop process the initial draw before kickstarting.
    def kickstart():
        root.withdraw()
        root.deiconify()
        logging.info("X11 render kickstart complete")

    root.after(200, kickstart)

    app._mainloop_started = True
    try:
        root.mainloop()
    finally:
        # Last-resort TX-off in case the loop exits without WM_DELETE_WINDOW
        # (exception, signal). Idempotent with the _on_close path above.
        try:
            bus = getattr(app, "bus", None)
            if (
                not close_state["ran"]
                and getattr(app, "tx", None) is not None
                and bus is not None
                and bus.is_connected()
            ):
                app.tx.stop()
        except Exception as e:
            logging.debug(f"Exception stopping TX during final cleanup: {e}")
        if not close_state["ran"]:
            try:
                if getattr(app, "docker", None) is not None and app.docker.log_busy:
                    app.docker.stream_stop()
            except Exception as e:
                logging.debug(f"Exception stopping Docker log stream during final cleanup: {e}")
            try:
                if getattr(app, "service_manager", None) is not None and app.service_manager.log_busy:
                    app.service_manager.stream_stop()
            except Exception as e:
                logging.debug(f"Exception stopping service journal stream during final cleanup: {e}")
            try:
                if getattr(app, "bus", None) is not None:
                    app.bus.disconnect()
            except Exception as e:
                logging.debug(f"Exception disconnecting MQTT during final cleanup: {e}")


if __name__ == "__main__":
    main()
