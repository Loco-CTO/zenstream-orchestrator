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

## 📦 Docker installation

Docker Compose uses two separate persistent mounts:

- `LIBRARY_PATH` is your media directory, mounted read-only in the container at
  `/media`.
- `METADATA_PATH` holds the SQLite database and generated metadata, artwork,
  people, and trickplay caches. It is mounted at `/app/sqlite`.

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

Database schema changes are managed with Alembic. The server automatically runs
`alembic upgrade head` during startup; to apply migrations manually, run:

```sh
alembic -c alembic.ini upgrade head
```

## 🖥️ For Developers

Please see [`DEVELOPER`](/DEVELOPER.md) for more information.

## 📜 License

Distributed under the GPL-3.0 license. See [`LICENSE`](/LICENSE) for more information.
<br>
<br>
<br>

<div align="center">
	<p><small>Copyright © 2026 <a href="https://rystal.net">Rystal</a>. All rights reserved.</small></p>
</div>
