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

## Configuration

Copy `.env.example` to `.env` and configure `SECRET_KEY`, `LIBRARY_PATH`, and `METADATA_PATH`. Use `ORCHESTRATOR_HOST` and `ORCHESTRATOR_PORT` to change the bind address or port. See `.env.example` for the complete list of settings.

## Development

Requires Python 3.14 and FFmpeg/FFprobe. Create and activate a virtual environment, install the dependencies, and run:

```sh
python -m venv .venv
python -m pip install -r requirements.txt
python orchestrator/init.py
```

## Deployment

### Docker

Set `SECRET_KEY`, `LIBRARY_PATH`, and `METADATA_PATH` in `.env`, then run:

```sh
docker compose up -d --build
```

The administrator dashboard is available at `http://localhost:9088/web/` by default.

### Native

Use the Development setup to run the Orchestrator directly on the host.

## Windows launcher

The Windows x64 launcher starts the Orchestrator and administrator dashboard. Download the installer or portable executable from [GitHub Releases](https://github.com/Loco-CTO/zenstream-orchestrator/releases).

Use the launcher's Configuration tab to set the server host and port, public web URL, metadata path, and secret key. The dashboard opens at `http://127.0.0.1:9088/web/` by default.

To build the launcher from Windows with Node.js 24 and Python 3.14:

```powershell
./scripts/build-windows.ps1 -PythonExecutable python
```

The installer, portable executable, and `SHA256SUMS.txt` are written to `launcher/release/`.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
