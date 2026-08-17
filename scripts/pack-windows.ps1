$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$launcherRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "launcher"))
$buildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot ".build"))
$releaseRoot = [IO.Path]::GetFullPath((Join-Path $launcherRoot "release"))
$packageRoot = [IO.Path]::GetFullPath((Join-Path $buildRoot ("windows-package-{0}" -f [Guid]::NewGuid().ToString("N"))))
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

    New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null

    Push-Location $launcherRoot
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "Launcher build failed." }

        $electronBuilder = Join-Path $launcherRoot "node_modules\.bin\electron-builder.cmd"
        & $electronBuilder --win nsis portable --x64 "--config.directories.output=$packageRoot"
        if ($LASTEXITCODE -ne 0) { throw "Windows installer packaging failed." }

        $archivePath = Join-Path $packageRoot "win-unpacked\resources\app.asar"
        & node (Join-Path $projectRoot "scripts\validate-electron-package.mjs") $archivePath
        if ($LASTEXITCODE -ne 0) { throw "Packaged Electron archive validation failed." }

        New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
        Get-ChildItem -LiteralPath $releaseRoot -File -Filter "*.exe" -ErrorAction SilentlyContinue |
            Remove-Item -Force
        Remove-Item -LiteralPath (Join-Path $releaseRoot "SHA256SUMS.txt") -Force -ErrorAction SilentlyContinue

        $artifacts = @(Get-ChildItem -LiteralPath $packageRoot -File -Filter "*.exe")
        if ($artifacts.Count -eq 0) {
            throw "Electron Builder did not produce any Windows artifacts."
        }
        foreach ($artifact in $artifacts) {
            Copy-Item -LiteralPath $artifact.FullName -Destination (Join-Path $releaseRoot $artifact.Name) -Force
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($lockStream) { $lockStream.Dispose() }
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $packageRoot) {
        try {
            Remove-Item -LiteralPath $packageRoot -Recurse -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "Could not remove temporary packaging directory '$packageRoot': $($_.Exception.Message)"
        }
    }
}
