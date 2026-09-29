# Install or upgrade snagentic on Windows without WinGet, signing or admin rights.
#
#   irm https://github.com/caabayfe/snagentic/releases/latest/download/install.ps1 | iex
#
# The archive is verified against the release SHA256SUMS, extracted per user and added to
# the user PATH. Settings are the same environment variables as install.sh:
# SNAGENTIC_VERSION, SNAGENTIC_REPO, SNAGENTIC_DOWNLOAD_URL (URL or local directory),
# SNAGENTIC_HOME (default %LOCALAPPDATA%\Programs\snagentic), SNAGENTIC_NO_COPILOT=1.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Fail([string]$Message) { throw "snagentic install: $Message" }

$repo = if ($env:SNAGENTIC_REPO) { $env:SNAGENTIC_REPO } else { 'caabayfe/snagentic' }
$version = if ($env:SNAGENTIC_VERSION) { $env:SNAGENTIC_VERSION.TrimStart('v') } else { 'latest' }
$appDir = if ($env:SNAGENTIC_HOME) { $env:SNAGENTIC_HOME } else { Join-Path $env:LOCALAPPDATA 'Programs\snagentic' }
$target = if ($env:SNAGENTIC_TARGET) { $env:SNAGENTIC_TARGET } else { 'windows-x64' }

if ($env:SNAGENTIC_DOWNLOAD_URL) { $base = $env:SNAGENTIC_DOWNLOAD_URL.TrimEnd('/', '\') }
elseif ($version -eq 'latest') { $base = "https://github.com/$repo/releases/latest/download" }
else { $base = "https://github.com/$repo/releases/download/v$version" }

function Get-ReleaseFile([string]$Name, [string]$Destination) {
    if (Test-Path -LiteralPath $base -PathType Container) {
        Copy-Item -LiteralPath (Join-Path $base $Name) -Destination $Destination
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri "$base/$Name" -OutFile $Destination
    }
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("snagentic-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $work | Out-Null
try {
    Get-ReleaseFile 'SHA256SUMS' (Join-Path $work 'SHA256SUMS')
    $pattern = "^([0-9a-f]{64})  (snagentic-\S+-$([regex]::Escape($target))\.zip)$"
    $match = Get-Content (Join-Path $work 'SHA256SUMS') |
        ForEach-Object { [regex]::Match($_.TrimEnd("`r"), $pattern) } |
        Where-Object Success | Select-Object -First 1
    if (-not $match) { Fail "no $target archive in the release" }
    $expected = $match.Groups[1].Value
    $archive = $match.Groups[2].Value

    Write-Host "Downloading $archive"
    $zip = Join-Path $work $archive
    Get-ReleaseFile $archive $zip
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { Fail "checksum mismatch for $archive" }

    $extract = Join-Path $work 'extract'
    Expand-Archive -LiteralPath $zip -DestinationPath $extract
    $bundle = Join-Path $extract 'snagentic'
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'snagentic.exe'))) {
        Fail "$archive does not contain snagentic\snagentic.exe"
    }
    Get-ChildItem -LiteralPath $bundle -Recurse -File | Unblock-File

    if ((Test-Path -LiteralPath $appDir) -and -not (Test-Path -LiteralPath (Join-Path $appDir 'snagentic.exe'))) {
        Fail "$appDir exists and is not a snagentic installation"
    }
    $parent = Split-Path -Parent $appDir
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (Test-Path -LiteralPath $appDir) { Remove-Item -LiteralPath $appDir -Recurse -Force }
    Move-Item -LiteralPath $bundle -Destination $appDir
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$entries = @($userPath -split ';' | Where-Object { $_ })
if ($entries -notcontains $appDir) {
    [Environment]::SetEnvironmentVariable('Path', (($entries + $appDir) -join ';'), 'User')
    Write-Host "Added $appDir to your user PATH (open a new terminal to use it)"
}
if (($env:Path -split ';') -notcontains $appDir) { $env:Path = "$appDir;$env:Path" }

$exe = Join-Path $appDir 'snagentic.exe'
& $exe --version
if ($LASTEXITCODE -ne 0) { Fail 'snagentic --version failed' }
if ($env:SNAGENTIC_NO_COPILOT -ne '1') {
    & $exe copilot install | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'snagentic copilot install failed' }
    Write-Host 'Copilot CLI extension and plugin installed'
}
Write-Host 'Next: snagentic auth login -i <instance>   (and optionally: snagentic ui install)'
