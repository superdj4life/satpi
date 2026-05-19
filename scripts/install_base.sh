#!/usr/bin/env bash
# satpi base installation script
# Author: Andreas Horvath
# Project: Autonomous, config-driven satellite reception pipeline for Raspberry Pi

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SATPI_DIR="${REPO_DIR}"
CONFIG_DIR="${SATPI_DIR}/config"
CONFIG_EXAMPLE="${CONFIG_DIR}/config.example.ini"
CONFIG_LOCAL="${CONFIG_DIR}/config.ini"

# ---------- helpers ----------

section() {
    echo
    echo "============================================================"
    echo "$1"
    echo "============================================================"
    echo
}

info() { echo "[INFO] $*"; }
warn() { echo "[WARN] $*"; }

press_enter() {
    echo
    read -r -p "Press Enter to continue..."
    echo
}

# ---------- single-shot sudo: ask once, keep the timestamp alive ----------

SUDO_KEEPALIVE_PID=""
SUDOERS_TEMP=""

sudo_keepalive_start() {
    sudo -v
    ( while true; do
          sudo -n true 2>/dev/null
          sleep 50
          kill -0 "$$" 2>/dev/null || exit
      done ) &
    SUDO_KEEPALIVE_PID=$!
}

cleanup() {
    if [[ -n "$SUDO_KEEPALIVE_PID" ]]; then
        kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    fi
    if [[ -n "$SUDOERS_TEMP" && -f "$SUDOERS_TEMP" ]]; then
        rm -f "$SUDOERS_TEMP"
    fi
}
trap cleanup EXIT

# ---------- intro ----------

section "SATPI BASE INSTALLATION FOR RASPBERRY PI 4 / 5"

cat <<'EOF'
This script prepares a Raspberry Pi for satpi.

Phases (grouped for fewer interruptions):
  1. APT      — system upgrade + all required packages in one batch
  2. SYSTEM   — locale, sudoers, udev/modprobe rules, boot config, journald
  3. SERVICES — install/enable all systemd units in a single daemon-reload
                (CPU governor, WiFi power-save off, fake-hwclock,
                 disable headless-unneeded services)
  4. USER     — directory layout, group membership, config patching
  5. DB       — initialize the reception database (inside the satpi tree)
  6. BUILD    — SatDump headless (longest step, deferred to the end)
  7. VERIFY   — sanity-check installed tools

You will be asked for your sudo password ONCE at the start.

Run this on Raspberry Pi OS Lite 64-bit. Use tmux for the SatDump build.
EOF

press_enter

sudo_keepalive_start

# ============================================================
# PHASE 1 — APT
# ============================================================

section "PHASE 1 — APT: UPDATE + INSTALL ALL PACKAGES"

# Suppress msmtp AppArmor debconf prompt before install.
echo 'msmtp msmtp/apparmor boolean false' | sudo debconf-set-selections

sudo apt update
sudo apt full-upgrade -y

sudo apt install -y \
  git cmake build-essential pkg-config curl wget jq \
  python3 python3-skyfield python3-numpy python3-pip python3-venv \
  python3-openai python3-reportlab \
  sqlite3 \
  rtl-sdr librtlsdr-dev \
  ffmpeg \
  libfftw3-dev libvolk-dev libzstd-dev \
  libpng-dev libjpeg-dev libtiff-dev \
  libcurl4-openssl-dev libnng-dev libsqlite3-dev \
  libglfw3-dev libjemalloc-dev libusb-1.0-0-dev libdbus-1-dev \
  rclone msmtp rsync iw \
  fake-hwclock

press_enter

# ============================================================
# PHASE 2 — SYSTEM CONFIG FILES
# ============================================================

section "PHASE 2 — SYSTEM CONFIG (locale, sudoers, udev, modprobe, boot, journald)"

# ---- locale ----
info "Configuring locale en_GB.UTF-8"
sudo sed -i 's/^# *en_GB.UTF-8 UTF-8/en_GB.UTF-8 UTF-8/' /etc/locale.gen
sudo locale-gen
sudo update-locale LANG=en_GB.UTF-8

sudo tee /etc/environment >/dev/null <<'EOF'
LANG=en_GB.UTF-8
LC_ALL=en_GB.UTF-8
EOF

sudo sed -i 's/^AcceptEnv LANG LC_/#AcceptEnv LANG LC_/g' /etc/ssh/sshd_config || true

# ---- sudoers: passwordless systemctl ----
info "Adding passwordless systemctl sudoers rule for ${USER}"
SUDOERS_TEMP="$(mktemp)"
cat > "$SUDOERS_TEMP" <<EOF
# satpi: Allow passwordless systemctl for satellite pass scheduling
${USER} ALL=(root) NOPASSWD: /bin/systemctl
EOF

if sudo visudo -c -f "$SUDOERS_TEMP" >/dev/null 2>&1; then
    sudo install -o root -g root -m 0440 "$SUDOERS_TEMP" /etc/sudoers.d/satpi-systemctl
    info "sudoers rule installed: ${USER} ALL=(root) NOPASSWD: /bin/systemctl"
else
    warn "sudoers validation failed. Add manually with visudo:"
    warn "  ${USER} ALL=(root) NOPASSWD: /bin/systemctl"
fi
rm -f "$SUDOERS_TEMP"
SUDOERS_TEMP=""

# ---- modprobe: block DVB-T drivers ----
info "Blacklisting DVB-T drivers (dvb_usb_rtl28xxu / rtl2830 / rtl2832)"
sudo tee /etc/modprobe.d/blacklist-rtl2832.conf >/dev/null <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

# ---- udev: RTL-SDR USB access ----
info "Installing RTL-SDR udev rules"
sudo tee /etc/udev/rules.d/99-rtlsdr.rules >/dev/null <<'EOF'
# RTL-SDR dongles (various manufacturer IDs)
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0666"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", MODE="0666"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="0129", MODE="0666"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="16c0", ATTRS{idProduct}=="0296", MODE="0666"
EOF

# One udev reload covers both modprobe blacklist + rtl-sdr rules.
sudo udevadm control --reload-rules
sudo udevadm trigger

# ---- boot config: USB max current (Pi 4 only) ----
PI_CONFIG_TXT=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "$candidate" ]]; then
        PI_CONFIG_TXT="$candidate"
        break
    fi
done

if [[ -z "$PI_CONFIG_TXT" ]]; then
    warn "Neither /boot/firmware/config.txt nor /boot/config.txt found; skipping USB power config."
elif grep -qE '^[[:space:]]*usb_max_current_enable[[:space:]]*=' "$PI_CONFIG_TXT"; then
    info "usb_max_current_enable already set in ${PI_CONFIG_TXT}; skipping."
else
    info "Adding usb_max_current_enable=1 to ${PI_CONFIG_TXT} (reboot required)"
    sudo tee -a "$PI_CONFIG_TXT" >/dev/null <<'EOT_USBCFG'

# satpi: enable 1.2 A USB current (Pi 4) so RTL-SDR dongles don't
# disconnect under load. No effect on Pi 5.
usb_max_current_enable=1
EOT_USBCFG
fi

# ---- journald: persistent journal with size cap ----
info "Configuring persistent systemd journal (cap 200 MB, retain 2 weeks)"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/satpi.conf >/dev/null <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemKeepFree=1G
MaxRetentionSec=2week
EOF

sudo mkdir -p "/var/log/journal/$(cat /etc/machine-id)"
sudo systemd-tmpfiles --create --prefix /var/log/journal

press_enter

# ============================================================
# PHASE 3 — SYSTEMD UNITS
# ============================================================

section "PHASE 3 — SYSTEMD UNITS (install, then daemon-reload once)"

# ---- CPU governor: performance ----
sudo tee /etc/systemd/system/cpu-performance.service >/dev/null <<'EOF'
[Unit]
Description=Set CPU governor to performance
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance | tee "$f"; done'

[Install]
WantedBy=multi-user.target
EOF

# ---- WiFi power save off (Pi 4 / Pi 5 brcmfmac hang workaround) ----
sudo tee /etc/systemd/system/wifi-powersave-off.service >/dev/null <<'EOF'
[Unit]
Description=Disable WiFi power save on wlan0 (brcmfmac hang workaround)
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/iw dev wlan0 set power_save off
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# Single daemon-reload after all unit-file changes.
sudo systemctl daemon-reload

# Enable + start newly installed services.
sudo systemctl enable --now cpu-performance.service
sudo systemctl enable --now wifi-powersave-off.service || \
    warn "wifi-powersave-off failed to start (wlan0 may not exist on this Pi)."

# Disable services not needed for headless operation.
for unit in ModemManager.service getty@tty1.service; do
    sudo systemctl disable --now "$unit" 2>/dev/null || true
done
sudo systemctl mask serial-getty@ttyAMA10.service 2>/dev/null || true

# fake-hwclock: enable whichever unit-name variant Pi OS ships.
if systemctl list-unit-files fake-hwclock-load.service >/dev/null 2>&1; then
    sudo systemctl enable --now fake-hwclock-load.service \
                                fake-hwclock-save.service \
                                fake-hwclock-save.timer || true
elif systemctl list-unit-files fake-hwclock.service >/dev/null 2>&1; then
    sudo systemctl enable --now fake-hwclock || true
fi
sudo fake-hwclock save 2>/dev/null || true

# Restart journald to pick up the persistent-storage config written in phase 2.
sudo systemctl restart systemd-journald

CURRENT_PS="$(iw dev wlan0 get power_save 2>/dev/null | sed 's/^.*: //' || true)"
info "wlan0 power_save now: ${CURRENT_PS:-unknown}"
info "fake-hwclock saved time: $(cat /etc/fake-hwclock.data 2>/dev/null || echo unknown)"

press_enter

# ============================================================
# PHASE 4 — USER, DIRECTORIES, CONFIG
# ============================================================

section "PHASE 4 — USER, DIRECTORIES, CONFIG"

# ---- group membership for RTL-SDR ----
sudo usermod -a -G plugdev "$USER"
info "Added ${USER} to plugdev group (re-login required for it to take effect)."

# ---- /usr/local/src (for SatDump build in phase 6) ----
sudo mkdir -p /usr/local/src
sudo chown -R "$USER:$USER" /usr/local/src

# ---- satpi directory tree (created with the running user, NOT root) ----
mkdir -p "${SATPI_DIR}"/{bin,config,docs,logs,results,scripts,systemd}
mkdir -p "${SATPI_DIR}/results"/{captures,passes,tle}
mkdir -p "${SATPI_DIR}/systemd/generated"

# ---- config.ini from example ----
if [[ -f "$CONFIG_LOCAL" ]]; then
    warn "config.ini already exists — not overwriting."
elif [[ -f "$CONFIG_EXAMPLE" ]]; then
    cp "$CONFIG_EXAMPLE" "$CONFIG_LOCAL"
    info "Created ${CONFIG_LOCAL} from config.example.ini"
else
    warn "config.example.ini not found at ${CONFIG_EXAMPLE} — skipping config copy."
fi

# ---- patch hardcoded /home/pi paths in config.ini ----
# Earlier versions of config.example.ini hardcoded paths like
# '/home/pi/satpi/...' or relative 'pi/satpi/...'. On systems whose user
# is NOT 'pi', that caused init_reception_db.py to create a stray
# ~/pi/satpi tree (the database ended up in ~/pi/satpi/results/database).
# Rewrite any such lines to point at the real SATPI_DIR before phase 5.
if [[ -f "$CONFIG_LOCAL" ]]; then
    if grep -Eq '(^|[^A-Za-z0-9_/])pi/satpi|/home/pi/satpi' "$CONFIG_LOCAL"; then
        info "Patching config.ini: replacing pi/satpi references with ${SATPI_DIR}"
        cp "$CONFIG_LOCAL" "${CONFIG_LOCAL}.bak"
        sed -i \
            -e "s|/home/pi/satpi|${SATPI_DIR}|g" \
            -e "s|^pi/satpi|${SATPI_DIR}|g" \
            -e "s|= *pi/satpi|= ${SATPI_DIR}|g" \
            -e "s|=pi/satpi|=${SATPI_DIR}|g" \
            "$CONFIG_LOCAL"
        info "Backup written to ${CONFIG_LOCAL}.bak"
    fi
fi

# ---- warn about stray ~/pi from previous broken runs ----
if [[ -d "${HOME}/pi" ]]; then
    warn "Found stray ${HOME}/pi (from a previous install with the path bug)."
    warn "Inspect it and remove it manually once you've confirmed it has no data you need:"
    warn "  ls -la ${HOME}/pi"
    warn "  rm -rf ${HOME}/pi   # only after you've checked"
fi

press_enter

# ============================================================
# PHASE 5 — DATABASE INIT
# ============================================================

section "PHASE 5 — INITIALIZE RECEPTION DATABASE"

# Run from SATPI_DIR so any relative paths in init_reception_db.py resolve
# inside the satpi tree.
cd "${SATPI_DIR}"
python3 bin/init_reception_db.py

press_enter

# ============================================================
# PHASE 6 — SATDUMP BUILD (longest step, deferred to the end)
# ============================================================

section "PHASE 6 — BUILD SATDUMP 1.2.2 (HEADLESS)"

cat <<'EOF'
SatDump is required for satpi.

This step will:
  - delete any previous /usr/local/src/SatDump tree (to avoid corruption
    from interrupted earlier clones)
  - clone SatDump from upstream
  - check out stable version 1.2.2
  - build the headless version
  - install it to /usr/bin/satdump

The build runs in this shell. If your SSH connection is unstable, abort
and re-run this script inside tmux so the build survives drops.
EOF

press_enter

cd /usr/local/src
if [[ -d /usr/local/src/SatDump ]]; then
    info "Removing existing /usr/local/src/SatDump to start clean..."
    sudo rm -rf /usr/local/src/SatDump
fi

git clone https://github.com/SatDump/SatDump.git
cd SatDump
sudo chown -R "$USER:$USER" .
git fetch --all --tags
git checkout 1.2.2

rm -rf build
mkdir build
cd build

cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/usr \
  -DSATDUMP_BUILD_UI=OFF \
  -DSATDUMP_BUILD_GUI=OFF \
  -DSATDUMP_BUILD_TESTS=OFF \
  -DCMAKE_C_FLAGS="-O3 -march=native -pipe" \
  -DCMAKE_CXX_FLAGS="-O3 -march=native -pipe" \
  -DCMAKE_EXE_LINKER_FLAGS="-s"

cmake --build . -j "$(nproc)"
sudo cmake --install .

# Disable SatDump's TLE auto-updates so satpi controls the TLE refresh.
sudo sed -i '/tle_update_interval/,/^[[:space:]]*},/{s/"value": "[^"]*"/"value": "Never"/}' \
    /usr/share/satdump/satdump_cfg.json

info "SatDump installed."

press_enter

# ============================================================
# PHASE 7 — VERIFICATION
# ============================================================

section "PHASE 7 — VERIFY INSTALLED TOOLS"

ok=0
missing=0
for cmd in python3 git curl jq rclone msmtp cmake satdump; do
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "[OK]      $cmd -> $(command -v "$cmd")"
        ok=$((ok + 1))
    else
        echo "[MISSING] $cmd"
        missing=$((missing + 1))
    fi
done

echo
info "Found $ok of $((ok + missing)) expected tools."

# ============================================================
# MANUAL STEPS
# ============================================================

section "REQUIRED MANUAL STEPS"

cat <<EOF
Manual steps still required:

1. Review and edit your config:
     nano "${CONFIG_LOCAL}"

2. Configure rclone:
     rclone config

3. Configure msmtp:
     nano ~/.msmtprc

4. Test mail setup:
     printf "Subject: satpi test\\n\\nTest mail.\\n" | /usr/bin/msmtp you@example.com

5. Run the main workflow manually:
     cd "${SATPI_DIR}"
     python3 bin/update_tle.py
     python3 bin/predict_passes.py
     python3 bin/schedule_passes.py

6. Generate refresh units:
     cd "${SATPI_DIR}"
     python3 bin/generate_refresh_units.py

7. Reboot to apply: USB max current, plugdev group membership, locale,
   journald persistence, SSH AcceptEnv change.
EOF

section "BASE INSTALLATION COMPLETE"

info "satpi base setup finished."
info "Repository: ${REPO_DIR}"
info "satpi dir:  ${SATPI_DIR}"
