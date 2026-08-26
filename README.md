<div align="center">
  <a href="./assets/icons/icon.png">
    <img src="./assets/icons/icon.png" alt="Logo" width="120" height="120">
  </a>
  <h3 align="center">ZenStream Orchestrator</h3>
  <p align="center">
    A backend application server for <a href="https://github.com/Rystal-Team/zenstream-orchestrator">ZenStream</a>.
    <br />
    <br />  
    <a href="https://github.com/Rystal-Team/zenstream-orchestrator/issues">Submit Issues</a>
    · 
    <a href="https://github.com/Rystal-Team/zenstream-orchestrator/releases">Releases</a>
  </p>
</div>

<div align="center">

[![GitHub Forks](https://img.shields.io/github/forks/Rystal-Team/zenstream-orchestrator.svg?style=for-the-badge)](https://github.com/Rystal-Team/zenstream-orchestrator)
[![GitHub Stars](https://img.shields.io/github/stars/Rystal-Team/zenstream-orchestrator.svg?style=for-the-badge)](https://github.com/Rystal-Team/zenstream-orchestrator)
[![License](https://img.shields.io/github/license/Rystal-Team/zenstream-orchestrator.svg?style=for-the-badge)](https://github.com/Rystal-Team/zenstream-orchestrator/blob/main/LICENSE)
[![Github Watchers](https://img.shields.io/github/watchers/Rystal-Team/zenstream-orchestrator.svg?style=for-the-badge)](https://github.com/Rystal-Team/zenstream-orchestrator)

</div>

## 🚀 Introduction

ZenStream Orchestrator is the backend for ZenStream. It manages accounts,
libraries, metadata, playback, and Syncplay.

## 🪟 Windows native launcher

Windows 10/11 x64 users should use the **ZenStream Orchestrator** launcher when
they need immediate media-library filesystem events. Docker Desktop bind mounts
do not reliably forward Windows changes into a Linux container, while the native
launcher lets Watchdog observe the Windows paths directly.

Download either the installer or portable executable from the GitHub release.
Both contain the backend source, a dedicated Python environment, administrator
dashboard, FFmpeg, and FFprobe;
Python, Node.js, Docker, and host media tools are not required. The launcher:

- starts the API and dashboard together at `http://127.0.0.1:9088`;
- stores configuration at
  `%APPDATA%\zenstream-orchestrator-launcher\launcher-config.json`, protects
  `SECRET_KEY` with Windows secure storage, and prepares the versioned launcher
  venv beneath `%APPDATA%\zenstream-orchestrator-launcher\runtime`;
- stores SQLite and generated caches beneath
  `%LOCALAPPDATA%\ZenStream Orchestrator\metadata` by default;
- shows live backend output and opens the persistent rotating log directory;
- stores launcher stdout, stderr, and lifecycle output on demand as five
  rotating 10 MiB `zenstream-launcher-*.ndjson` segments (50 MiB total),
  without retaining the complete history in memory;
- remains in the notification area when its window is closed; and
- stops Orchestrator cleanly only when **Quit** is selected.

The default binding is local-only. Set `ORCHESTRATOR_HOST` to `0.0.0.0` only
when LAN clients need access, then configure Windows Firewall and the network
accordingly. Installer and portable builds share the same per-user settings;
moving a portable executable after enabling **Start with Windows** requires
registering that setting again.

## 📦 Docker installation

Docker Compose uses two separate persistent mounts:

- `LIBRARY_PATH` is your media directory, mounted read-only in the container at
  `/media`.
- `METADATA_PATH` holds the SQLite database, rotating logs, and generated
  metadata, artwork, people, and trickplay caches. It is mounted at
  `/app/sqlite`; native runs use the configured path directly.

These are host bind mounts, so you can use an existing directory on the host;
no Docker named volume is required. Docker calls this a volume mount, but the
media itself stays in the directory you choose. Keep the metadata directory
separate from the media library and include it in your backups.

The image includes `ffmpeg` and `ffprobe`, so host media tools are not required.
`FFMPEG_PATH` and `FFPROBE_PATH` are optional overrides for custom builds.

### Windows

1. Install and start Docker Desktop, with the drive containing your media shared
   with Docker Desktop.
2. Copy `.env.example` to `.env` and set a long, random `SECRET_KEY`.
3. Set paths using forward slashes. For example:

   ```dotenv
   LIBRARY_PATH=C:/Users/Alice/Videos
   METADATA_PATH=C:/Users/Alice/ZenStream/metadata
   ```

   The library directory must already exist. Create the metadata directory if
   you prefer not to let Docker Compose create it.
4. Start the service:

   ```powershell
   docker compose up -d --build
   ```
5. Open `http://localhost:9088/web/`, sign in with the administrator credentials
   printed in the first container startup logs, and add a physical library with
   directory `/media` (or a subdirectory such as `/media/Movies`). Do not enter
   the Windows host path in the dashboard.

   Native Watchdog events work when the mounted storage forwards inotify. A
   Docker Desktop Windows-host bind mounted at `/media` does not reliably
   forward those events into the Linux container, so the existing periodic
   library scan job is the safety net. Move the media into Linux-host storage
   if immediate events through Docker are required.

### Linux

1. Install Docker Engine with the Docker Compose plugin, then copy
   `.env.example` to `.env` and set a long, random `SECRET_KEY`.
2. Set absolute paths owned by the account running Docker. For example:

   ```dotenv
   LIBRARY_PATH=/srv/media
   METADATA_PATH=/var/lib/zenstream/metadata
   ```

   The container reads the library only; ensure its files are readable by the
   container. The metadata path must be writable.
3. Start the service and configure `/media` as the library directory in the
   dashboard:

   ```sh
   docker compose up -d --build
   ```

Use `docker compose logs -f orchestrator` to retrieve the one-time bootstrap
administrator credentials or diagnose a mount permission issue. Stop the
service with `docker compose down`; this does not delete either host directory.

### Native installation

When running outside Docker, no mount is needed: add the host filesystem path
directly in the administrator dashboard. Install dependencies with
`pip install -r requirements.txt`, then run `python orchestrator/init.py`.

### Building the Windows launcher

From a Windows x64 PowerShell prompt with Node.js and Python 3.14 available:

```powershell
./scripts/build-windows.ps1 -PythonExecutable python
```

The build verifies and stages the pinned FFmpeg binaries, exports the dashboard,
stages the backend source with its isolated Python environment, runs launcher
tests, serializes packaging to prevent overlapping release writers, validates
the packaged Electron archive, and writes the
NSIS installer, portable executable, and `SHA256SUMS.txt` beneath
`launcher/release/`. Generated FFmpeg binaries, Python build directories, and
release outputs are intentionally untracked.

Database schema changes are managed with Alembic. The server automatically runs
`alembic upgrade head` during startup; to apply migrations manually, run:

```sh
alembic -c alembic.ini upgrade head
```

## 🖥️ For Developers

When the public `zenstream` web client is deployed separately from the
administrator dashboard, set the launcher's **Public web URL** setting to the
public web origin (for example `http://localhost:9086`). Invite links then
target `/register` on that origin; they never target the dashboard's `/web`
routes. The configured public web origin is automatically allowed to call the
API; use `CORS_ORIGINS` for additional browser origins.

Please see [`DEVELOPER`](/DEVELOPER.md) for more information.

## 📜 License

Distributed under the GPL-3.0 license. See [`LICENSE`](/LICENSE) for more information.
<br>
<br>
<br>

<div align="center">
	<p><small>Copyright © 2026 <a href="https://rystal.net">Rystal</a>. All rights reserved.</small></p>
</div>
