#!/usr/bin/env python3
"""Directional ReSpeaker gateway for kiosk browser audio.

Reads mono PCM from the ReSpeaker, checks the array DOA/VAD telemetry, and writes
either the original audio frame or silence to a FIFO that backs a virtual mic.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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

            dev = usb.core.find(idVendor=0x2886, idProduct=0x0018)
            if dev is None:
                raise RuntimeError("ReSpeaker 2886:0018 not found")
            mic = Tuning(dev)
        except Exception as exc:
            self._set(direction=None, is_voice=False, last_error=str(exc))
            return

        while not self._stop.is_set():
            try:
                direction = float(mic.direction)
                is_voice = bool(mic.is_voice())
                self._set(direction=direction, is_voice=is_voice)
            except Exception as exc:
                self._set(direction=None, is_voice=False, last_error=str(exc))
            self._stop.wait(self.poll_ms / 1000.0)


def _ensure_fifo(path: Path) -> None:
    if path.exists():
        if not path.is_fifo():
            raise RuntimeError(f"{path} exists but is not a FIFO")
        return
    os.mkfifo(path)


def _start_arecord(device: str, rate: int, channels: int) -> subprocess.Popen[bytes]:
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
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)


def _update_gate(
    gate: Gate,
    telemetry: Telemetry,
    *,
    frame_ms: int,
    front_center_deg: float,
    front_half_window_deg: float,
    open_stable_ms: int,
    close_stable_ms: int,
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
        gate.close_counter_ms += frame_ms
        gate.open_counter_ms = 0
        if gate.close_counter_ms >= close_stable_ms:
            gate.is_open = False
    return gate.is_open


def main() -> int:
    default_config = Path(__file__).with_name("config.env")
    parser = argparse.ArgumentParser(description="ReSpeaker directional kiosk audio gateway")
    parser.add_argument("--config", default=str(default_config), help="Path to config.env")
    args = parser.parse_args()

    _load_env_file(Path(args.config).expanduser())

    rate = _env_int("RATE", 16000)
    channels = _env_int("CHANNELS", 1)
    frame_ms = _env_int("FRAME_MS", 20)
    bytes_per_frame = rate * frame_ms // 1000 * channels * 2
    alsa_device = _env_str("ALSA_DEVICE", "plughw:1,0")
    fifo_path = Path(_env_str("FIFO_PATH", f"/run/user/{os.getuid()}/kiosk_customer_mic.fifo"))
    respeaker_repo = _env_str("RESPEAKER_REPO", "~/Downloads/usb_4_mic_array")
    front_center_deg = _env_float("FRONT_CENTER_DEG", 180.0)
    front_half_window_deg = _env_float("FRONT_HALF_WINDOW_DEG", 35.0)
    open_stable_ms = _env_int("OPEN_STABLE_MS", 400)
    close_stable_ms = _env_int("CLOSE_STABLE_MS", 250)
    poll_ms = _env_int("POLL_MS", 50)

    running = True

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    _ensure_fifo(fifo_path)
    poller = ReSpeakerTelemetryPoller(respeaker_repo, poll_ms)
    poller.start()

    rec = _start_arecord(alsa_device, rate, channels)
    gate = Gate()
    last_log_ts = 0.0
    silence = b"\x00" * bytes_per_frame

    print(
        json.dumps(
            {
                "event": "gateway_started",
                "alsa_device": alsa_device,
                "fifo_path": str(fifo_path),
                "rate": rate,
                "frame_ms": frame_ms,
                "front_center_deg": front_center_deg,
                "front_half_window_deg": front_half_window_deg,
                "open_stable_ms": open_stable_ms,
                "close_stable_ms": close_stable_ms,
            }
        ),
        flush=True,
    )

    try:
        with fifo_path.open("wb", buffering=0) as fifo:
            while running:
                if rec.stdout is None:
                    raise RuntimeError("arecord stdout missing")
                frame = rec.stdout.read(bytes_per_frame)
                if len(frame) != bytes_per_frame:
                    err = rec.stderr.read().decode("utf-8", errors="replace") if rec.stderr else ""
                    raise RuntimeError(f"arecord ended unexpectedly: {err}".strip())

                telemetry = poller.snapshot()
                is_open = _update_gate(
                    gate,
                    telemetry,
                    frame_ms=frame_ms,
                    front_center_deg=front_center_deg,
                    front_half_window_deg=front_half_window_deg,
                    open_stable_ms=open_stable_ms,
                    close_stable_ms=close_stable_ms,
                )
                fifo.write(frame if is_open else silence)

                now = time.time()
                if now - last_log_ts >= 1.0:
                    last_log_ts = now
                    print(
                        json.dumps(
                            {
                                "event": "gate_status",
                                "gate_open": is_open,
                                "direction": telemetry.direction,
                                "is_voice": telemetry.is_voice,
                                "telemetry_error": telemetry.last_error or None,
                                "open_counter_ms": gate.open_counter_ms,
                                "close_counter_ms": gate.close_counter_ms,
                            }
                        ),
                        flush=True,
                    )
    finally:
        poller.stop()
        rec.terminate()
        try:
            rec.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rec.kill()

    print(json.dumps({"event": "gateway_stopped"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
