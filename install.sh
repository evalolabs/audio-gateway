#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HOME/kiosk-audio-gateway}"
CONFIG_DIR="$HOME/.config/kiosk-audio-gateway"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="kiosk-audio-gateway.service"
RESPEAKER_REPO="${RESPEAKER_REPO:-$HOME/Downloads/usb_4_mic_array}"

echo "Installing packages..."
sudo apt update
sudo apt install -y \
  alsa-utils git python3-usb pulseaudio-utils \
  pipewire pipewire-pulse wireplumber

echo "Installing gateway files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$SERVICE_DIR"
cp "$SCRIPT_DIR/gateway.py" "$INSTALL_DIR/gateway.py"
cp "$SCRIPT_DIR/config.example.env" "$INSTALL_DIR/config.example.env"
chmod +x "$INSTALL_DIR/gateway.py"

if [ ! -f "$CONFIG_DIR/config.env" ]; then
  cp "$SCRIPT_DIR/config.example.env" "$CONFIG_DIR/config.env"
  echo "Created $CONFIG_DIR/config.env"
else
  echo "Keeping existing $CONFIG_DIR/config.env"
fi

echo "Installing ReSpeaker control tools..."
mkdir -p "$(dirname "$RESPEAKER_REPO")"
if [ ! -d "$RESPEAKER_REPO/.git" ]; then
  git clone https://github.com/respeaker/usb_4_mic_array.git "$RESPEAKER_REPO"
else
  git -C "$RESPEAKER_REPO" pull --ff-only || true
fi
sed -i 's/\.tostring()/\.tobytes()/g' "$RESPEAKER_REPO/tuning.py"

echo "Installing udev rule for ReSpeaker 2886:0018..."
sudo tee /etc/udev/rules.d/99-respeaker.rules >/dev/null <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="0018", MODE="0666"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "Installing systemd user service..."
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$SCRIPT_DIR/$SERVICE_NAME" > "$SERVICE_DIR/$SERVICE_NAME"
systemctl --user daemon-reload

echo
echo "Install complete."
echo
echo "Next:"
echo "  1. Unplug and replug the ReSpeaker once for the udev rule."
echo "  2. Check $CONFIG_DIR/config.env and adjust ALSA_DEVICE if needed."
echo "  3. Start the gateway:"
echo "       systemctl --user --now enable $SERVICE_NAME"
echo "  4. Watch logs:"
echo "       journalctl --user -u $SERVICE_NAME -f"
