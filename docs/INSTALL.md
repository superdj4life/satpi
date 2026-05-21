# Install satpi on a Raspberry Pi

This guide covers everything from hardware selection to a running satpi installation.
It starts with what you need, shows how to prepare a Raspberry Pi using Raspberry Pi Imager
on Windows or macOS, and walks through the full installation and configuration.

Raspberry Pi recommends [Raspberry Pi Imager][rpi-imager] as the standard way to write
Raspberry Pi OS to a microSD card. The Imager can preconfigure hostname, user account,
Wi-Fi, and SSH for a headless setup
(see the [official getting-started guide][rpi-getting-started]).

## 1. What you need

- a Raspberry Pi 4 or Raspberry Pi 5 (the latter with a cooling fan)
- a suitable power supply for your Raspberry Pi
- a microSD card
- a computer with Windows or macOS (to set up the SD card initially)
- an internet connection
- an RTL-SDR compatible USB receiver
- an antenna suitable for weather satellite reception (a QFH antenna gives the best results; a V-dipole works fine for testing)
- optionally an LNA and Bias-T — with the Raspberry Pi installed close to the antenna in a weatherproof box, short coax cables often make an LNA unnecessary

## 2. Download Raspberry Pi Imager

On your Windows PC or Mac:

1. Open the [Raspberry Pi software page][rpi-software].
2. Download **Raspberry Pi Imager** for your system.
3. Install and launch it.

## 3. Install Raspberry Pi OS Lite 64-bit

1. Insert the microSD card into your computer.
2. Start **Raspberry Pi Imager**.
3. Click **Choose Device** and select your Raspberry Pi model.
4. Click **Choose OS** and select **Raspberry Pi OS Lite (64-bit)**. This is a headless install — no desktop GUI needed.
5. Click **Choose Storage** and select your microSD card.
6. Click **Next**.

## 4. Configure the image before writing

When Raspberry Pi Imager asks whether you want to apply OS customisation settings, choose **Edit Settings**.

Set at least the following:

### General

- hostname — for example: `satpi`
- username — for example: `andreas`
- password
- Wi-Fi SSID and password
- Wi-Fi country

### Services

- enable **SSH**

Save the settings and continue. Raspberry Pi Imager will preconfigure hostname, user account,
network, and SSH during imaging — ideal for a headless setup.

## 5. Write the card and boot the Raspberry Pi

1. Click **Write** in Raspberry Pi Imager.
2. Wait until writing has finished.
3. Remove the card from your computer.
4. Insert the card into the Raspberry Pi.
5. Connect the following:
   - power
   - network cable, or rely on Wi-Fi if configured
   - the RTL-SDR can be connected later — it is not required for the first boot
6. Wait 1 to 2 minutes for the first boot.

## 6. Connect via SSH

Open a terminal on your computer.

- **macOS**: use the **Terminal** app
- **Windows**: use **PowerShell** or **PuTTY**

```bash
ssh YOUR_USER@HOSTNAME.local
```

Replace `YOUR_USER` and `HOSTNAME` with the values you configured in Raspberry Pi Imager.

## 7. Clone the satpi repository

On the Raspberry Pi, update the system and clone the repository:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/HorvathAndreas/satpi.git
```

Or as a single copy-paste line:

```bash
sudo apt update && sudo apt install -y git && git clone https://github.com/HorvathAndreas/satpi.git
```

Then run the base installer:

```bash
cd ~/satpi
scripts/install_base.sh
```

## 8. Configure satpi

Copy the example configuration and open it for editing:

```bash
cd ~/satpi
cp config/config.example.ini config/config.ini
nano config/config.ini
```

Save and exit nano with `Ctrl+O`, `Enter`, `Ctrl+X`.

### TLE data source

satpi downloads orbital data (TLE) once a day to predict satellite passes.
Two sources are supported — choose the one that works for your network.

**Option A: Celestrak** (no account needed)

First test if Celestrak is reachable from your Raspberry Pi:

```bash
curl --max-time 10 "https://celestrak.org/NORAD/elements/gp.php?CATNR=59051&FORMAT=json"
```

If you see JSON output, set this in `config/config.ini`:

```ini
[network]
tle_url    = https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=json
tle_format = GP_JSON
api_key    =
```

If the command times out, your IP is blocked by Celestrak.
You can request unblocking by e-mail: **TS.Kelso@celestrak.org**
(mention that it is a residential satellite reception system, not a scraper).
Use Option B in the meantime.

**Option B: N2YO** (free account required)

Register at [n2yo.com](https://www.n2yo.com/login/register.php) and copy your API key.
Then set this in `config/config.ini`:

```ini
[network]
tle_url    = https://api.n2yo.com/rest/v1/satellite/tle/
tle_format = JSON
api_key    = YOUR-N2YO-API-KEY
```

---

Configure rclone for cloud-storage upload:

```bash
rclone config
```

Send a test mail to confirm that msmtp is working
(replace `you@example.com` with your address):

```bash
printf "Subject: satpi test\n\nTest mail.\n" | /usr/bin/msmtp you@example.com
```

## 9. Run the initial pipeline

```bash
python3 bin/update_tle.py
python3 bin/predict_passes.py
python3 bin/schedule_passes.py
python3 bin/generate_refresh_units.py
```

Or as a single copy-paste line:

```bash
python3 bin/update_tle.py && \
python3 bin/predict_passes.py && \
python3 bin/schedule_passes.py && \
python3 bin/generate_refresh_units.py
```

## 10. Verify scheduled timers

```bash
systemctl list-timers --all | grep satpi
```

You should see one timer per upcoming pass.

## 11. Wait for results

Once everything is running, reception results will arrive in your mailbox
with links to the decoded weather images. Have fun!

---

[rpi-imager]: https://www.raspberrypi.com/software/
[rpi-getting-started]: https://www.raspberrypi.com/documentation/computers/getting-started.html
[rpi-software]: https://www.raspberrypi.com/software/operating-systems/
