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

ZenStream Orchestrator is a robust application server that extends the functionality of [ZenStream](https://github.com/Rystal-Team/zenstream-orchestrator) by providing additional features and services to enhance users' experience.

## 📦 Installation

1. Clone the repository:
   ```sh
   git clone https://github.com/Rystal-Team/zenstream-orchestrator.git
   ```
2. Navigate to the project directory:
   ```sh
   cd zenstream-orchestrator
   ```
3. Install dependencies:
   ```sh
   pip install -r requirements.txt
   ```

Database schema changes are managed with Alembic. The server automatically runs
`alembic upgrade head` during startup; to apply migrations manually, run:

```sh
alembic -c alembic.ini upgrade head
```

## 🛠️ Usage

1. Set up the environment variables:
   ```sh
   export SECRET_KEY=your_secret_key
   ```
2. Run the application:
   ```sh
   cd orchestrator
   python init.py
   ```

## 🖥️ For Developers

Please see [`DEVELOPER`](/DEVELOPER.md) for more information.

## 📜 License

Distributed under the GPL-3.0 license. See [`LICENSE`](/LICENSE) for more information.
<br>
<br>
<br>

<div align="center">
	<p><small>Copyright © 2024 <a href="https://rystal.net">Rystal</a>. All rights reserved.</small></p>
</div>
