$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$launcherRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "launcher"))
$buildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot ".build"))
$releaseRoot = [IO.Path]::GetFullPath((Join-Path $launcherRoot "release"))
$lockPath = Join-Path $buildRoot "windows-package.lock"

if (-not $releaseRoot.StartsWith($launcherRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to replace a release path outside the launcher directory."
}

New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
$lockStream = $null
try {
    try {
        $lockStream = [IO.File]::Open(
            $lockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    }
    catch {
        throw "Another ZenStream Windows packaging process is already running. Wait for it to finish before rebuilding."
    }

    if (Test-Path -LiteralPath $releaseRoot) {
        Remove-Item -LiteralPath $releaseRoot -Recurse -Force
    }

    Push-Location $launcherRoot
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "Launcher build failed." }

        $electronBuilder = Join-Path $launcherRoot "node_modules\.bin\electron-builder.cmd"
        & $electronBuilder --win nsis portable --x64
        if ($LASTEXITCODE -ne 0) { throw "Windows installer packaging failed." }

        & node (Join-Path $projectRoot "scripts\validate-electron-package.mjs")
        if ($LASTEXITCODE -ne 0) { throw "Packaged Electron archive validation failed." }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($lockStream) { $lockStream.Dispose() }
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
