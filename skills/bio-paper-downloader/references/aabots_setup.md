# AABots deployment guide

This document lists only what is needed **in addition to** the project installer for `--aabots`.

## Avoid duplicate installation

Run the normal project installer once:

```bash
./install.sh
```

After that, do **not** separately reinstall the base packages listed in `install.sh`. The installer already handles:

- Python virtualenv creation
- `pip install -r requirements.txt`
- Chrome or Chromium
- Xvfb
- poppler-utils / `pdftotext`
- procps
- Playwright Chromium + Playwright system dependencies
- `data/` and `data/tmp/`

`requirements.txt` already includes all Python packages used by `--aabots`:

- `cloudscraper`
- `curl_cffi`
- `playwright-stealth`
- `2captcha-python`

So on a new server, the normal sequence is:

```bash
chmod +x install.sh install_aabots.sh
./install.sh
./install_aabots.sh --check-only
```

If you want FlareSolverr, run the supplemental installer with:

```bash
./install_aabots.sh --flaresolverr
```

If Docker is not installed yet and you want the script to install it:

```bash
./install_aabots.sh --install-docker
```

Then only configure the optional services you actually plan to use. The manual steps below are for servers where you prefer not to let `install_aabots.sh` manage Docker.

## What each preset still needs after `install.sh`

| Method / preset | Extra setup after `install.sh` | Notes |
|---|---|---|
| `Default` | Nothing | Same behavior as current downloader. |
| `CloudScraper` | Nothing | Python package is in `requirements.txt`. |
| `curl_cffi` | Nothing | Python package is in `requirements.txt`. |
| `Quick` | Nothing | Uses `cloudscraper` + `curl_cffi`. |
| `Stealth` | Nothing | Uses Chrome/Xvfb/Playwright installed by `install.sh`. |
| `Browser` | FlareSolverr only if you want that second step | `Stealth` part is already covered. |
| `FlareSolverr` | Docker + FlareSolverr container | Not installed by `install.sh`. |
| `2Captcha` | `TWOCAPTCHA_API_KEY` environment variable | Python package is in `requirements.txt`. |
| `Full` / `All` | Docker for FlareSolverr + `TWOCAPTCHA_API_KEY` for 2Captcha | Other dependencies are covered by `install.sh`. |

## Optional: FlareSolverr setup

Only needed if you use one of these:

```bash
--aabots FlareSolverr
--aabots Default,FlareSolverr
--aabots Browser
--aabots Full
--aabots All
```

Install Docker if the server does not already have it. On Ubuntu/Debian:

```bash
sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

Then log out and log back in, or run:

```bash
newgrp docker
```

Start FlareSolverr:

```bash
docker run -d \
  --name=flaresolverr \
  -p 8191:8191 \
  -e LOG_LEVEL=info \
  --restart unless-stopped \
  ghcr.io/flaresolverr/flaresolverr:latest
```

Verify the service:

```bash
python3 - <<'PY'
import json, urllib.request
req = urllib.request.Request(
    'http://localhost:8191/v1',
    data=json.dumps({'cmd': 'sessions.list'}).encode(),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(req, timeout=5) as r:
    print(r.read().decode())
PY
```

A working service returns JSON containing `"status": "ok"` and a version.

### Docker permission notes

If `docker ps` reports permission denied against `/var/run/docker.sock`, check:

```bash
getent group docker
ls -la /var/run/docker.sock
```

The socket should normally be group-owned by `docker`:

```text
srw-rw---- 1 root docker ... /var/run/docker.sock
```

If Docker was installed through snap, the socket may remain `root:root`. Prefer the distro Docker package (`docker.io`) or official Docker Engine on production servers because it manages the `docker` group more predictably.

## Optional: 2Captcha setup

Only needed if you use one of these:

```bash
--aabots 2Captcha
--aabots Full
--aabots All
```

Set the API key in the environment used by the downloader:

```bash
export TWOCAPTCHA_API_KEY='your-api-key-here'
```

For persistent shell sessions, add it to `~/.bashrc` or your deployment environment file.

`config.yaml` already points to this env var:

```yaml
download:
  twocaptcha_api_key_env: "TWOCAPTCHA_API_KEY"
```

Do not put the raw API key into `config.yaml` unless this is a private, non-shared deployment.

## Usage examples

Default behavior, unchanged:

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py
python3 skills/bio-paper-downloader/scripts/paper_cli.py --aabots Default
```

Try FlareSolverr before the existing downloader pipeline:

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py --aabots Default,FlareSolverr
```

Fast HTTP-only attempts:

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py --aabots Quick
```

Full cascade:

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py --aabots Full
```

Single URL:

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py get \
  -u "https://www.biorxiv.org/content/10.1101/2025.01.01.123456" \
  --aabots Full
```

## Verification checklist

After `./install.sh`, verify Python-side aabots dependencies:

```bash
. venv/bin/activate
python3 - <<'PY'
import cloudscraper
from curl_cffi import requests
from twocaptcha import TwoCaptcha
print('aabots python deps OK')
PY
```

Verify CLI flags:

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py --help
python3 skills/bio-paper-downloader/scripts/paper_cli.py get --help
```

If using FlareSolverr, verify port 8191:

```bash
python3 - <<'PY'
import json, urllib.request
req = urllib.request.Request(
    'http://localhost:8191/v1',
    data=json.dumps({'cmd': 'sessions.list'}).encode(),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(req, timeout=5) as r:
    print(r.read().decode())
PY
```

If using 2Captcha, verify the key is visible to the process:

```bash
python3 - <<'PY'
import os
print('TWOCAPTCHA_API_KEY set:', bool(os.environ.get('TWOCAPTCHA_API_KEY')))
PY
```

## Troubleshooting

### `FlareSolverr not running`

Start the container and confirm port 8191 is reachable:

```bash
docker ps --filter name=flaresolverr
curl -s http://localhost:8191/v1
```

The `curl` call may return an error because the endpoint expects POST JSON, but it should connect. If it cannot connect, the service is not listening.

### Browser fallback fails on headless server

Do not reinstall packages manually first. Re-run the base installer if the server was not installed correctly:

```bash
./install.sh
```

Then try fallback level 2:

```bash
python3 skills/bio-paper-downloader/scripts/paper_cli.py --fallback-level 2 --aabots Browser
```

### `CloudScraper` / `curl_cffi` fail but browser methods work

This is expected for interactive Turnstile or higher Cloudflare protection. Use:

```bash
--aabots Browser
# or
--aabots Full
```