# Kiosk Audio Gateway

Local audio gateway for a Seeed ReSpeaker USB 4 Mic Array.

The gateway gates microphone audio before the browser sends it to OpenAI Realtime:

```text
ReSpeaker audio + direction/is_voice
-> front-zone gate
-> FIFO
-> virtual mic "Kiosk Customer Mic"
-> MonshyDisplay browser call
```

## Current calibration

First kiosk measurement showed the customer/front direction around:

```text
direction: 180-185
is_voice: 1
```

The MVP starts with:

```text
FRONT_CENTER_DEG=180
FRONT_HALF_WINDOW_DEG=35
OPEN_STABLE_MS=400
CLOSE_STABLE_MS=250
```

That means audio opens only when voice is detected in roughly `145-215` degrees for about `400 ms`.

## Install on Ubuntu kiosk

From this directory:

```bash
chmod +x install.sh
```

```bash
./install.sh
```

Unplug and replug the ReSpeaker once after install.

Check the config:

```bash
nano ~/.config/kiosk-audio-gateway/config.env
```

If `arecord -l` shows a different card/device, update:

```text
ALSA_DEVICE=plughw:1,0
```

## Start service

```bash
systemctl --user --now enable kiosk-audio-gateway.service
```

Open the local UI on the kiosk:

```text
http://localhost:8765
```

The UI shows:

- gate open/closed
- current ReSpeaker `direction`
- current `is_voice`
- whether the direction is inside the configured front window
- audio capture status and `arecord` errors
- live diagnostics
- editable calibration values

Most calibration changes apply immediately:

- `FRONT_CENTER_DEG`
- `FRONT_HALF_WINDOW_DEG`
- `OPEN_STABLE_MS`
- `CLOSE_STABLE_MS`

Audio plumbing fields require a service restart after saving:

- `ALSA_DEVICE`
- `ARECORD_PERIOD_SIZE`
- `ARECORD_BUFFER_SIZE`
- `RATE`
- `CHANNELS`
- `FRAME_MS`
- `FIFO_PATH`
- `RESPEAKER_REPO`
- `POLL_MS`
- `UI_HOST`
- `UI_PORT`

Restart after changing those:

```bash
systemctl --user restart kiosk-audio-gateway.service
```

Logs are still available for deeper troubleshooting:

```bash
journalctl --user -u kiosk-audio-gateway.service -f
```

### Capture logs during a live voice call

On the NUC, in **one** terminal, start a rolling capture (stop with `Ctrl+C` after the call):

```bash
journalctl --user -u kiosk-audio-gateway.service -f --no-hostname -o short-precise | tee ~/kiosk-gateway-$(date +%Y%m%d-%H%M%S).log
```

Then start the MonshyDisplay call and speak as you normally would. The file under `$HOME` is what you attach or grep later.

Optional: extra backlog hint in each `gate_status` line (helps spot `arecord` underruns):

```bash
nano ~/.config/kiosk-audio-gateway/config.env
```

Add:

```text
KIOSK_GATEWAY_DEBUG=1
```

Then:

```bash
systemctl --user restart kiosk-audio-gateway.service
```

Useful JSON events in the journal:

- `gate_transition` — gate opened or closed (direction / `is_voice` / `in_front` at that moment).
- `fifo_status` — FIFO write errors appear or clear (PipeWire reader not keeping up, alignment issues).
- `gate_status` — once per second heartbeat (same fields as the local UI).

Expected log events:

```json
{"event": "gateway_started", "...": "..."}
{"event": "gate_status", "gate_open": false, "direction": 180.0, "is_voice": true}
{"event": "gate_status", "gate_open": true, "direction": 181.0, "is_voice": true}
```

## Verify virtual microphone

```bash
pactl list sources short | grep kiosk_customer_mic
```

Expected:

```text
kiosk_customer_mic ...
```

Record from the virtual mic:

```bash
parecord --device=kiosk_customer_mic --rate=16000 --channels=1 --format=s16le /tmp/kiosk_customer_mic.raw
```

Speak from the front for a few seconds, then stop with `CTRL+C`.

Convert to WAV for playback:

```bash
sox -r 16000 -e signed -b 16 -c 1 /tmp/kiosk_customer_mic.raw /tmp/kiosk_customer_mic.wav
```

If `sox` is missing:

```bash
sudo apt install -y sox
```

Play:

```bash
aplay /tmp/kiosk_customer_mic.wav
```

## Manual run without service

Load the virtual mic manually:

```bash
FIFO_PATH="$XDG_RUNTIME_DIR/kiosk_customer_mic.fifo"
```

```bash
test -p "$FIFO_PATH" || mkfifo "$FIFO_PATH"
```

```bash
pactl load-module module-pipe-source \
  file="$FIFO_PATH" \
  source_name=kiosk_customer_mic \
  source_properties=device.description="Kiosk Customer Mic" \
  format=s16le rate=16000 channels=1 channel_map=mono
```

Run gateway:

```bash
FIFO_PATH="$FIFO_PATH" ./gateway.py --config ./config.example.env
```

## Calibrate another station

Run the ReSpeaker direction test:

```bash
cd ~/Downloads/usb_4_mic_array
```

```bash
python3 - <<'PY'
from tuning import Tuning
import usb.core
import time

dev = usb.core.find(idVendor=0x2886, idProduct=0x0018)
if dev is None:
    raise SystemExit("ReSpeaker nicht gefunden")

mic = Tuning(dev)

while True:
    print({
        "direction": mic.direction,
        "is_voice": mic.is_voice()
    }, flush=True)
    time.sleep(0.2)
PY
```

Measure:

- Person in front of kiosk
- Person left side
- Person right side
- Store/background noise only

Set `FRONT_CENTER_DEG` to the stable value seen when speaking from the customer position.

Example:

```text
front values: 180, 181, 183, 185
FRONT_CENTER_DEG=180
FRONT_HALF_WINDOW_DEG=35
```

If too much side speech passes through, reduce `FRONT_HALF_WINDOW_DEG` to `25`.
If real customers are clipped, increase it to `40`.

You can also do this from the UI at:

```text
http://localhost:8765
```

## Troubleshooting

### ReSpeaker not found

```bash
lsusb | grep -i respeaker
```

Expected USB ID:

```text
2886:0018
```

### Audio card changed

```bash
arecord -l
```

Update:

```bash
nano ~/.config/kiosk-audio-gateway/config.env
```

### Permission denied for USB control

Run installer again, unplug/replug ReSpeaker:

```bash
./install.sh
```

### Gateway starts but gate never opens

Check live ReSpeaker values:

```bash
journalctl --user -u kiosk-audio-gateway.service -f
```

If `direction` is `null` or `telemetry_error` is set, the ReSpeaker control path is broken.

If `direction` is valid but outside the front window, recalibrate `FRONT_CENTER_DEG`.

### UI is reachable but audio capture fails

The service keeps running and retries capture automatically. Open:

```text
http://localhost:8765
```

Check `audio_error` in Diagnostics. For ReSpeaker UAC1 on Ubuntu 24.04, keep explicit buffering disabled unless field tests prove it is needed:

```text
ARECORD_PERIOD_SIZE=0
ARECORD_BUFFER_SIZE=0
```

If the device card changed, update `ALSA_DEVICE` in the UI or in:

```bash
nano ~/.config/kiosk-audio-gateway/config.env
```

Then restart:

```bash
systemctl --user restart kiosk-audio-gateway.service
```

### Browser still uses wrong mic

MonshyDisplay should prefer a device whose label contains:

```text
Kiosk Customer Mic
```

If labels are hidden, grant microphone permission once and reload the display page.
