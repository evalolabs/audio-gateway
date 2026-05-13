#!/usr/bin/env python3
"""Directional ReSpeaker gateway for kiosk browser audio.

Reads mono PCM from the ReSpeaker, checks the array DOA/VAD telemetry, and writes
either the original audio frame or silence to a FIFO that backs a virtual mic.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = os.path.expandvars(os.path.expanduser(value))


def _env_str(name: str, default: str) -> str:
    return os.path.expandvars(os.path.expanduser(os.environ.get(name, default)))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _angular_error_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


CONFIG_FIELDS: dict[str, dict[str, Any]] = {
    "ALSA_DEVICE": {"type": "str", "default": "plughw:1,0", "restart": True},
    "RATE": {"type": "int", "default": 16000, "restart": True},
    "CHANNELS": {"type": "int", "default": 1, "restart": True},
    "FRAME_MS": {"type": "int", "default": 20, "restart": True},
    "ARECORD_PERIOD_SIZE": {"type": "int", "default": 0, "restart": True},
    "ARECORD_BUFFER_SIZE": {"type": "int", "default": 0, "restart": True},
    "AUDIO_RETRY_SECONDS": {"type": "float", "default": 2.0, "restart": False},
    "FIFO_PATH": {"type": "str", "default": f"/run/user/{os.getuid()}/kiosk_customer_mic.fifo", "restart": True},
    "RESPEAKER_REPO": {"type": "str", "default": "~/Downloads/usb_4_mic_array", "restart": True},
    "FRONT_CENTER_DEG": {"type": "float", "default": 180.0, "restart": False},
    "FRONT_HALF_WINDOW_DEG": {"type": "float", "default": 35.0, "restart": False},
    "OPEN_STABLE_MS": {"type": "int", "default": 400, "restart": False},
    "CLOSE_STABLE_MS": {"type": "int", "default": 250, "restart": False},
    # When gate is open and direction stays in the front window but ReSpeaker is_voice
    # blinks false briefly, wait this long before closing (reduces choppy mic).
    "IN_FRONT_VOICE_DROP_MS": {"type": "int", "default": 800, "restart": False},
    "POLL_MS": {"type": "int", "default": 50, "restart": True},
    "UI_HOST": {"type": "str", "default": "127.0.0.1", "restart": True},
    "UI_PORT": {"type": "int", "default": 8765, "restart": True},
}


def _coerce_config_value(name: str, value: Any) -> Any:
    spec = CONFIG_FIELDS[name]
    typ = spec["type"]
    if typ == "int":
        return int(value)
    if typ == "float":
        return float(value)
    return str(value)


def _build_settings() -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for name, spec in CONFIG_FIELDS.items():
        default = spec["default"]
        if spec["type"] == "int":
            settings[name] = _env_int(name, int(default))
        elif spec["type"] == "float":
            settings[name] = _env_float(name, float(default))
        else:
            settings[name] = _env_str(name, str(default))
    return settings


class GatewayState:
    def __init__(self, config_path: Path, settings: dict[str, Any]) -> None:
        self.config_path = config_path
        self._settings = dict(settings)
        self._status: dict[str, Any] = {
            "gateway_started": False,
            "gate_open": False,
            "direction": None,
            "is_voice": False,
            "in_front": False,
            "angular_error_deg": None,
            "telemetry_error": None,
            "audio_error": None,
            "audio_capture_active": False,
            "fifo_error": None,
            "open_counter_ms": 0,
            "close_counter_ms": 0,
            "alsa_device": settings.get("ALSA_DEVICE"),
            "fifo_path": settings.get("FIFO_PATH"),
            "last_update_ts": None,
        }
        self._last_config_save: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get_setting(self, name: str) -> Any:
        with self._lock:
            return self._settings[name]

    def update_status(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)
            self._status["last_update_ts"] = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": dict(self._status),
                "config": dict(self._settings),
                "config_meta": {
                    name: {"restart_required": bool(spec["restart"]), "type": spec["type"]}
                    for name, spec in CONFIG_FIELDS.items()
                },
                "last_config_save": dict(self._last_config_save),
            }

    def save_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        restart_required: list[str] = []
        with self._lock:
            for name, raw_value in updates.items():
                if name not in CONFIG_FIELDS:
                    continue
                try:
                    value = _coerce_config_value(name, raw_value)
                except (TypeError, ValueError):
                    continue
                if self._settings.get(name) != value:
                    changed[name] = value
                    self._settings[name] = value
                    if CONFIG_FIELDS[name]["restart"]:
                        restart_required.append(name)

            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                "# Kiosk audio gateway configuration.",
                "# Managed by the local UI; restart the service after changing fields marked restart_required.",
                "",
            ]
            for name in CONFIG_FIELDS:
                value = self._settings[name]
                lines.append(f"{name}={value}")
            self.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._last_config_save = {
                "saved_at": time.time(),
                "changed": changed,
                "restart_required": restart_required,
            }
            return dict(self._last_config_save)


def _ui_html() -> bytes:
    return b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kiosk Customer Mic</title>
  <style>
    :root { color-scheme: dark light; font-family: system-ui, sans-serif; }
    body { margin: 0; padding: 24px; background: #111827; color: #f9fafb; }
    main { max-width: 980px; margin: 0 auto; }
    h1 { margin: 0 0 6px; }
    p { color: #cbd5e1; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
    .card { background: #1f2937; border: 1px solid #374151; border-radius: 14px; padding: 16px; }
    .value { font-size: 34px; font-weight: 750; margin-top: 8px; }
    .ok { color: #86efac; }
    .bad { color: #fca5a5; }
    label { display: block; margin: 12px 0 5px; color: #cbd5e1; }
    input { width: 100%; box-sizing: border-box; padding: 10px; border-radius: 10px; border: 1px solid #4b5563; background: #111827; color: #f9fafb; }
    button { margin-top: 16px; padding: 11px 16px; border: 0; border-radius: 10px; background: #2563eb; color: white; font-weight: 700; cursor: pointer; }
    code, pre { background: #0f172a; border-radius: 8px; padding: 2px 5px; }
    .hint { font-size: 14px; color: #94a3b8; }
  </style>
</head>
<body>
<main>
  <h1>Kiosk Customer Mic</h1>
  <p>Local ReSpeaker gateway status and calibration UI.</p>

  <section class="grid">
    <div class="card"><div>Gate</div><div id="gate" class="value">...</div></div>
    <div class="card"><div>Direction</div><div id="direction" class="value">...</div></div>
    <div class="card"><div>Voice</div><div id="voice" class="value">...</div></div>
    <div class="card"><div>In front window</div><div id="front" class="value">...</div></div>
  </section>

  <section class="card" style="margin-top:14px">
    <h2>Calibration</h2>
    <p class="hint">Current front measurement was around 180-185 degrees. Adjust only if the customer position reads differently.</p>
    <form id="configForm">
      <label>Front center degrees</label>
      <input name="FRONT_CENTER_DEG" type="number" step="1">
      <label>Front half window degrees</label>
      <input name="FRONT_HALF_WINDOW_DEG" type="number" step="1">
      <label>Open stable ms</label>
      <input name="OPEN_STABLE_MS" type="number" step="10">
      <label>Close stable ms</label>
      <input name="CLOSE_STABLE_MS" type="number" step="10">
      <label>In-front voice drop hold ms (longer close delay while still in beam)</label>
      <input name="IN_FRONT_VOICE_DROP_MS" type="number" step="50">
      <label>ALSA device (restart required)</label>
      <input name="ALSA_DEVICE" type="text">
      <label>arecord period size (restart required)</label>
      <input name="ARECORD_PERIOD_SIZE" type="number" step="1">
      <label>arecord buffer size (restart required)</label>
      <input name="ARECORD_BUFFER_SIZE" type="number" step="1">
      <button type="submit">Save config</button>
    </form>
    <p id="saveResult" class="hint"></p>
  </section>

  <section class="card" style="margin-top:14px">
    <h2>Diagnostics</h2>
    <pre id="diag">{}</pre>
  </section>
</main>
<script>
let loadedConfig = false;
async function refresh() {
  const res = await fetch('/api/status');
  const data = await res.json();
  const s = data.status || {};
  const c = data.config || {};
  document.getElementById('gate').textContent = s.gate_open ? 'OPEN' : 'CLOSED';
  document.getElementById('gate').className = 'value ' + (s.gate_open ? 'ok' : 'bad');
  document.getElementById('direction').textContent = s.direction == null ? '-' : Math.round(s.direction) + ' deg';
  document.getElementById('voice').textContent = s.is_voice ? 'YES' : 'NO';
  document.getElementById('voice').className = 'value ' + (s.is_voice ? 'ok' : 'bad');
  document.getElementById('front').textContent = s.in_front ? 'YES' : 'NO';
  document.getElementById('front').className = 'value ' + (s.in_front ? 'ok' : 'bad');
  document.getElementById('diag').textContent = JSON.stringify(data, null, 2);
  if (!loadedConfig) {
    for (const el of document.querySelectorAll('#configForm input')) {
      if (c[el.name] !== undefined) el.value = c[el.name];
    }
    loadedConfig = true;
  }
}
document.getElementById('configForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const updates = {};
  for (const el of event.currentTarget.querySelectorAll('input')) updates[el.name] = el.value;
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(updates)
  });
  const data = await res.json();
  const restart = (data.restart_required || []).length ? ' Restart service for: ' + data.restart_required.join(', ') : '';
  document.getElementById('saveResult').textContent = 'Saved.' + restart;
  loadedConfig = false;
  await refresh();
});
refresh();
setInterval(refresh, 500);
</script>
</body>
</html>"""


def _start_ui_server(state: GatewayState, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/api/status" or self.path == "/api/config":
                self._send_json(state.snapshot())
                return
            body = _ui_html()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/api/config":
                self._send_json({"error": "not found"}, 404)
                return
            try:
                length = int(self.headers.get("content-length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
                result = state.save_config(payload)
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": html.escape(str(exc))}, 400)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="gateway-ui", daemon=True)
    thread.start()
    return server


@dataclass
class Telemetry:
    direction: Optional[float] = None
    is_voice: bool = False
    last_error: str = ""
    last_update_ts: float = 0.0


@dataclass
class Gate:
    open_counter_ms: int = 0
    close_counter_ms: int = 0
    is_open: bool = False


class ReSpeakerTelemetryPoller:
    def __init__(self, repo: str, poll_ms: int) -> None:
        self.repo = Path(repo).expanduser()
        self.poll_ms = max(50, poll_ms)
        self.telemetry = Telemetry()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name="respeaker-telemetry", daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Telemetry:
        with self._lock:
            return Telemetry(
                direction=self.telemetry.direction,
                is_voice=self.telemetry.is_voice,
                last_error=self.telemetry.last_error,
                last_update_ts=self.telemetry.last_update_ts,
            )

    def _set(self, *, direction: Optional[float], is_voice: bool, last_error: str = "") -> None:
        with self._lock:
            self.telemetry.direction = direction
            self.telemetry.is_voice = bool(is_voice)
            self.telemetry.last_error = last_error
            self.telemetry.last_update_ts = time.time()

    def _run(self) -> None:
        try:
            sys.path.insert(0, str(self.repo))
            import usb.core  # type: ignore
            from tuning import Tuning  # type: ignore
        except Exception as exc:
            self._set(direction=None, is_voice=False, last_error=str(exc))
            return

        mic: Optional[Any] = None
        while not self._stop.is_set():
            if mic is None:
                try:
                    dev = usb.core.find(idVendor=0x2886, idProduct=0x0018)
                    if dev is None:
                        raise RuntimeError("ReSpeaker 2886:0018 not found")
                    mic = Tuning(dev)
                except Exception as exc:
                    self._set(direction=None, is_voice=False, last_error=str(exc))
                    self._stop.wait(max(1.0, self.poll_ms / 1000.0))
                    continue

            try:
                direction = float(mic.direction)
                is_voice = bool(mic.is_voice())
                self._set(direction=direction, is_voice=is_voice)
            except Exception as exc:
                self._set(direction=None, is_voice=False, last_error=str(exc))
                mic = None
                self._stop.wait(max(0.5, self.poll_ms / 1000.0))
                continue
            self._stop.wait(self.poll_ms / 1000.0)


def _ensure_fifo(path: Path) -> None:
    if path.exists():
        if not path.is_fifo():
            raise RuntimeError(f"{path} exists but is not a FIFO")
        return
    os.mkfifo(path)


def _open_fifo_nonblocking(path: Path) -> int:
    # O_RDWR prevents open() from blocking when PipeWire has not started reading
    # yet. O_NONBLOCK prevents service stop/restart from hanging on fifo writes.
    return os.open(path, os.O_RDWR | os.O_NONBLOCK)


def _try_enlarge_pipe(fd: int, target_bytes: int) -> Optional[int]:
    """Linux only: grow the pipe buffer so PipeWire/WebRTC bursts stall less often."""
    if target_bytes <= 0:
        return None
    try:
        import fcntl

        if not hasattr(fcntl, "F_SETPIPE_SZ"):
            return None
        return int(fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, target_bytes))
    except OSError:
        return None


def _write_fifo_frame(
    fd: int,
    frame: bytes,
    *,
    write_timeout_s: float = 0.25,
    pad_timeout_s: float = 0.12,
) -> Optional[str]:
    """Write one full PCM frame. Retries on EAGAIN so we rarely drop audio.

    A non-blocking FIFO can return BlockingIOError when PipeWire is briefly behind.
    Abandoning a partial frame would shift byte alignment for the virtual mic reader.
    """
    deadline = time.monotonic() + max(0.05, write_timeout_s)
    total = 0
    while total < len(frame):
        try:
            written = os.write(fd, frame[total:])
            if written <= 0:
                return "fifo write returned 0 bytes"
            total += written
        except BlockingIOError:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.001)
        except OSError as exc:
            return str(exc)
    if total >= len(frame):
        return None
    pad = b"\x00" * (len(frame) - total)
    pad_deadline = time.monotonic() + max(0.05, pad_timeout_s)
    pt = 0
    while pt < len(pad):
        try:
            w = os.write(fd, pad[pt:])
            if w <= 0:
                return "fifo buffer full; incomplete frame"
            pt += w
        except BlockingIOError:
            if time.monotonic() >= pad_deadline:
                return "fifo buffer full; incomplete frame"
            time.sleep(0.001)
        except OSError as exc:
            return str(exc)
    return None


def _start_arecord(
    device: str,
    rate: int,
    channels: int,
    *,
    period_size: int,
    buffer_size: int,
) -> subprocess.Popen[bytes]:
    cmd = [
        "arecord",
        "-D",
        device,
        "-f",
        "S16_LE",
        "-r",
        str(rate),
        "-c",
        str(channels),
        "-t",
        "raw",
        "-q",
        "-",
    ]
    if period_size > 0:
        cmd.extend(["--period-size", str(period_size)])
    if buffer_size > 0:
        cmd.extend(["--buffer-size", str(buffer_size)])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    if proc.stdout is not None:
        os.set_blocking(proc.stdout.fileno(), False)
    if proc.stderr is not None:
        os.set_blocking(proc.stderr.fileno(), False)
    return proc


def _read_nonblocking(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes
    while remaining > 0:
        try:
            chunk = os.read(fd, remaining)
        except BlockingIOError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_arecord_stderr(rec: subprocess.Popen[bytes]) -> str:
    if rec.stderr is None:
        return ""
    return _read_nonblocking(rec.stderr.fileno(), 64 * 1024).decode("utf-8", errors="replace")


def _update_gate(
    gate: Gate,
    telemetry: Telemetry,
    *,
    frame_ms: int,
    front_center_deg: float,
    front_half_window_deg: float,
    open_stable_ms: int,
    close_stable_ms: int,
    in_front_voice_drop_ms: int,
) -> bool:
    in_front = (
        telemetry.direction is not None
        and _angular_error_deg(telemetry.direction, front_center_deg) <= front_half_window_deg
    )
    should_open = bool(telemetry.is_voice and in_front)
    if should_open:
        gate.open_counter_ms += frame_ms
        gate.close_counter_ms = 0
        if gate.open_counter_ms >= open_stable_ms:
            gate.is_open = True
    else:
        close_need_ms = close_stable_ms
        if gate.is_open and in_front and not telemetry.is_voice:
            close_need_ms = max(close_stable_ms, in_front_voice_drop_ms)
        gate.close_counter_ms += frame_ms
        gate.open_counter_ms = 0
        if gate.close_counter_ms >= close_need_ms:
            gate.is_open = False
    return gate.is_open


def main() -> int:
    default_config = Path(__file__).with_name("config.env")
    parser = argparse.ArgumentParser(description="ReSpeaker directional kiosk audio gateway")
    parser.add_argument("--config", default=str(default_config), help="Path to config.env")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    _load_env_file(config_path)
    settings = _build_settings()
    state = GatewayState(config_path, settings)

    rate = int(settings["RATE"])
    channels = int(settings["CHANNELS"])
    frame_ms = int(settings["FRAME_MS"])
    bytes_per_frame = rate * frame_ms // 1000 * channels * 2
    alsa_device = str(settings["ALSA_DEVICE"])
    period_size = int(settings["ARECORD_PERIOD_SIZE"])
    buffer_size = int(settings["ARECORD_BUFFER_SIZE"])
    fifo_path = Path(str(settings["FIFO_PATH"]))
    respeaker_repo = str(settings["RESPEAKER_REPO"])
    poll_ms = int(settings["POLL_MS"])
    audio_retry_seconds = float(settings["AUDIO_RETRY_SECONDS"])
    ui_host = str(settings["UI_HOST"])
    ui_port = int(settings["UI_PORT"])
    fifo_write_timeout_s = _env_float("FIFO_WRITE_TIMEOUT_S", 0.6)
    fifo_pad_timeout_s = _env_float("FIFO_PAD_TIMEOUT_S", 0.15)
    fifo_pipe_size_bytes = _env_int("FIFO_PIPE_SIZE_BYTES", 1048576)

    running = True

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    _ensure_fifo(fifo_path)
    poller = ReSpeakerTelemetryPoller(respeaker_repo, poll_ms)
    poller.start()
    ui_server = _start_ui_server(state, ui_host, ui_port)

    rec: Optional[subprocess.Popen[bytes]] = None
    next_audio_retry_ts = 0.0
    gate = Gate()
    last_log_ts = 0.0
    prev_gate_open: Optional[bool] = None
    prev_fifo_error: Optional[str] = None
    silence = b"\x00" * bytes_per_frame
    capture_buffer = bytearray()
    debug_extra = os.environ.get("KIOSK_GATEWAY_DEBUG", "").strip().lower() in ("1", "true", "yes")

    fifo_fd: Optional[int] = None
    try:
        fifo_fd = _open_fifo_nonblocking(fifo_path)
        fifo_pipe_applied = _try_enlarge_pipe(fifo_fd, fifo_pipe_size_bytes)
        print(
            json.dumps(
                {
                    "event": "gateway_started",
                    "alsa_device": alsa_device,
                    "fifo_path": str(fifo_path),
                    "rate": rate,
                    "frame_ms": frame_ms,
                    "ui_url": f"http://{ui_host}:{ui_port}",
                    "fifo_pipe_size_requested": fifo_pipe_size_bytes,
                    "fifo_pipe_size_applied": fifo_pipe_applied,
                    "fifo_write_timeout_s": fifo_write_timeout_s,
                    "fifo_pad_timeout_s": fifo_pad_timeout_s,
                }
            ),
            flush=True,
        )
        state.update_status(gateway_started=True, alsa_device=alsa_device, fifo_path=str(fifo_path))
        state.update_status(fifo_error=None)
        while running:
            now = time.time()
            if rec is None and now >= next_audio_retry_ts:
                try:
                    rec = _start_arecord(
                        alsa_device,
                        rate,
                        channels,
                        period_size=period_size,
                        buffer_size=buffer_size,
                    )
                    capture_buffer.clear()
                    state.update_status(audio_capture_active=True, audio_error=None)
                except Exception as exc:
                    state.update_status(audio_capture_active=False, audio_error=str(exc))
                    next_audio_retry_ts = now + audio_retry_seconds

            frame = silence
            if rec is not None:
                try:
                    if rec.stdout is None:
                        raise RuntimeError("arecord stdout missing")

                    exit_code = rec.poll()
                    if exit_code is not None:
                        err = _read_arecord_stderr(rec)
                        detail = f"arecord exited with status {exit_code}: {err}".strip()
                        raise RuntimeError(detail)

                    capture_buffer.extend(_read_nonblocking(rec.stdout.fileno(), bytes_per_frame * 8))
                    if len(capture_buffer) >= bytes_per_frame:
                        frame = bytes(capture_buffer[:bytes_per_frame])
                        del capture_buffer[:bytes_per_frame]
                    state.update_status(audio_capture_active=True, audio_error=None)
                except Exception as exc:
                    state.update_status(audio_capture_active=False, audio_error=str(exc))
                    rec.terminate()
                    try:
                        rec.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        rec.kill()
                    rec = None
                    capture_buffer.clear()
                    next_audio_retry_ts = now + audio_retry_seconds

            telemetry = poller.snapshot()
            front_center_deg = float(state.get_setting("FRONT_CENTER_DEG"))
            front_half_window_deg = float(state.get_setting("FRONT_HALF_WINDOW_DEG"))
            open_stable_ms = int(state.get_setting("OPEN_STABLE_MS"))
            close_stable_ms = int(state.get_setting("CLOSE_STABLE_MS"))
            in_front_voice_drop_ms = int(state.get_setting("IN_FRONT_VOICE_DROP_MS"))
            is_open = _update_gate(
                gate,
                telemetry,
                frame_ms=frame_ms,
                front_center_deg=front_center_deg,
                front_half_window_deg=front_half_window_deg,
                open_stable_ms=open_stable_ms,
                close_stable_ms=close_stable_ms,
                in_front_voice_drop_ms=in_front_voice_drop_ms,
            )
            fifo_error = _write_fifo_frame(
                fifo_fd,
                frame if is_open else silence,
                write_timeout_s=fifo_write_timeout_s,
                pad_timeout_s=fifo_pad_timeout_s,
            )
            state.update_status(fifo_error=fifo_error)
            if fifo_error != prev_fifo_error:
                print(
                    json.dumps(
                        {
                            "event": "fifo_status",
                            "fifo_error": fifo_error,
                            "gate_open": is_open,
                        }
                    ),
                    flush=True,
                )
                prev_fifo_error = fifo_error
            angular_error = (
                _angular_error_deg(telemetry.direction, front_center_deg)
                if telemetry.direction is not None
                else None
            )
            in_front = angular_error is not None and angular_error <= front_half_window_deg
            state.update_status(
                gate_open=is_open,
                direction=telemetry.direction,
                is_voice=telemetry.is_voice,
                in_front=in_front,
                angular_error_deg=angular_error,
                telemetry_error=telemetry.last_error or None,
                open_counter_ms=gate.open_counter_ms,
                close_counter_ms=gate.close_counter_ms,
            )

            if prev_gate_open is not None and prev_gate_open != is_open:
                print(
                    json.dumps(
                        {
                            "event": "gate_transition",
                            "gate_open": is_open,
                            "direction": telemetry.direction,
                            "is_voice": telemetry.is_voice,
                            "in_front": in_front,
                            "angular_error_deg": angular_error,
                            "open_counter_ms": gate.open_counter_ms,
                            "close_counter_ms": gate.close_counter_ms,
                        }
                    ),
                    flush=True,
                )
            prev_gate_open = is_open

            now = time.time()
            if now - last_log_ts >= 1.0:
                last_log_ts = now
                current_status = state.snapshot()["status"]
                status_payload: dict[str, Any] = {
                    "event": "gate_status",
                    "gate_open": is_open,
                    "direction": telemetry.direction,
                    "is_voice": telemetry.is_voice,
                    "in_front": in_front,
                    "telemetry_error": telemetry.last_error or None,
                    "audio_error": current_status.get("audio_error"),
                    "audio_capture_active": current_status.get("audio_capture_active"),
                    "fifo_error": current_status.get("fifo_error"),
                    "open_counter_ms": gate.open_counter_ms,
                    "close_counter_ms": gate.close_counter_ms,
                }
                if debug_extra:
                    status_payload["capture_backlog_frames"] = len(capture_buffer) // max(1, bytes_per_frame)
                print(json.dumps(status_payload), flush=True)
            time.sleep(frame_ms / 1000.0)
    finally:
        state.update_status(gateway_started=False)
        ui_server.shutdown()
        poller.stop()
        if fifo_fd is not None:
            os.close(fifo_fd)
        if rec is not None:
            rec.terminate()
            try:
                rec.wait(timeout=2)
            except subprocess.TimeoutExpired:
                rec.kill()

    print(json.dumps({"event": "gateway_stopped"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
