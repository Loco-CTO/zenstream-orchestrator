$ErrorActionPreference = "Stop"

python -m pip install --disable-pip-version-check watchdog uvicorn python-dotenv
if ($LASTEXITCODE -ne 0) { throw "Could not install Windows runtime test dependencies." }

$env:PYTHONPATH = "orchestrator"
python -m unittest orchestrator.test.windows_watchdog_test orchestrator.test.launcher_test
if ($LASTEXITCODE -ne 0) { throw "Windows runtime regression tests failed." }
