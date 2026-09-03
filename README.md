<div align="center">
  <a href="./assets/icons/icon.png">
    <img src="./assets/icons/icon.png" alt="Logo" width="120" height="120">
  </a>
  <h3 align="center">ZenStream Orchestrator</h3>
  <p align="center">
    A backend application server for <a href="https://github.com/Loco-CTO/zenstream-orchestrator">ZenStream</a>.
    <br />
    <br />
    <a href="https://github.com/Loco-CTO/zenstream-orchestrator/issues">Submit Issues</a>
    ·
    <a href="https://github.com/Loco-CTO/zenstream-orchestrator/releases">Releases</a>
  </p>
</div>

<div align="center">

[![GitHub Forks](https://img.shields.io/github/forks/Loco-CTO/zenstream-orchestrator.svg?style=for-the-badge)](https://github.com/Loco-CTO/zenstream-orchestrator)
[![GitHub Stars](https://img.shields.io/github/stars/Loco-CTO/zenstream-orchestrator.svg?style=for-the-badge)](https://github.com/Loco-CTO/zenstream-orchestrator)
[![License](https://img.shields.io/github/license/Loco-CTO/zenstream-orchestrator.svg?style=for-the-badge)](https://github.com/Loco-CTO/zenstream-orchestrator/blob/main/LICENSE)
[![Github Watchers](https://img.shields.io/github/watchers/Loco-CTO/zenstream-orchestrator.svg?style=for-the-badge)](https://github.com/Loco-CTO/zenstream-orchestrator)

</div>

## How it fits together

ZenStream has one Orchestrator backend and two clients:

- [Web client](https://github.com/Loco-CTO/zenstream)
- [Android client](https://github.com/Loco-CTO/zenstream-mobile)
- [Orchestrator](https://github.com/Loco-CTO/zenstream-orchestrator)

## Configuration

Copy `.env.example` to `.env`. The main settings are:

- `SECRET_KEY`: required secret used to protect sessions and credentials.
- `LIBRARY_PATH`: host media directory. Docker mounts it read-only at `/media`.
- `METADATA_PATH`: writable persistent directory for the database and caches. Docker mounts it at `/app/sqlite`.
- `ORCHESTRATOR_HOST` and `ORCHESTRATOR_PORT`: server bind address and port.
- `ZENSTREAM_PUBLIC_WEB_URL` and `CORS_ORIGINS`: optional settings for a separately hosted web client.

Do not commit `.env` files or credentials. See `.env.example` for the complete list of settings.

## Development

Requires Python 3.14 and FFmpeg/FFprobe. Create and activate a virtual environment, install the dependencies, and run:

```sh
python -m venv .venv
python -m pip install -r requirements.txt
python orchestrator/init.py
```

## Deployment

### Docker

Copy `.env.example` to `.env`. Set `SECRET_KEY`, an existing host `LIBRARY_PATH`, and a writable `METADATA_PATH`, then run:

```sh
docker compose up -d --build
```

Docker mounts media read-only at `/media` and metadata at `/app/sqlite`. Add `/media` (or a child directory) as the library path in the administrator dashboard. The dashboard is available at `http://localhost:9088/web/` by default. Use `docker compose logs -f orchestrator` to view startup output and `docker compose down` to stop the service without deleting the host directories.

### Native

Use the Development setup to run the Orchestrator directly on the host. Native libraries use their host filesystem paths.

## First run

1. Start the Orchestrator with Docker or the native Development setup.
2. Open the administrator dashboard at `/web/`.
3. Save the root administrator credentials printed during the first startup; they are shown only once.
4. Add a library. Use `/media` for Docker and the actual host path for a native run.
5. Point the web and mobile clients at the same Orchestrator URL.

## Windows launcher

The Windows x64 launcher starts the Orchestrator and administrator dashboard. Download the installer or portable executable from [GitHub Releases](https://github.com/Loco-CTO/zenstream-orchestrator/releases/latest).

Use the launcher's Configuration tab to set the server host and port, public web URL, metadata path, and secret key. The dashboard opens at `http://127.0.0.1:9088/web/` by default. Use the Logs tab when the backend needs attention.

To build the launcher from Windows with Node.js 24 and Python 3.14:

```powershell
./scripts/build-windows.ps1 -PythonExecutable python
```

The installer, portable executable, and `SHA256SUMS.txt` are written to `launcher/release/`.

## Checks

Run the Python tests from the repository root:

```sh
python -m unittest discover -s orchestrator/test -p '*_test.py'
python -m unittest discover -s tests -p 'test*.py'
```

Run the launcher tests from `launcher/`:

```sh
npm ci
npm test
```

## Troubleshooting

- If no media appears, confirm that `LIBRARY_PATH` exists and is readable, the Docker dashboard path is `/media`, and `METADATA_PATH` is writable.
- For web or CORS errors, check `ZENSTREAM_PUBLIC_WEB_URL`, `CORS_ORIGINS`, and the web client's `NEXT_PUBLIC_ZSO_URL`; rebuild the web container after changing its public URL.
- For Android connectivity, use `10.0.2.2:<port>` from an emulator or the host's LAN address from a physical device. Bind the Orchestrator to a reachable address and check the firewall.
- If the launcher does not start, inspect its Logs tab. After moving a portable executable, disable and re-enable Start with Windows.

## Releases

The [latest Orchestrator release](https://github.com/Loco-CTO/zenstream-orchestrator/releases/latest) includes the Windows x64 installer, portable executable, and `SHA256SUMS.txt`.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
