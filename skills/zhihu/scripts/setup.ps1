$ErrorActionPreference = "Stop"

function Fail-Json([string]$Code, [string]$Message) {
    @{ ok = $false; error = @{ code = $Code; message = $Message } } | ConvertTo-Json -Compress
    exit 1
}

function Compare-SemVer([string]$A, [string]$B) {
    $Pattern = '^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$'
    if ($A -notmatch $Pattern) { throw "invalid version: $A" }
    $AMatch = [regex]::Match($A, $Pattern)
    if ($B -notmatch $Pattern) { throw "invalid version: $B" }
    $BMatch = [regex]::Match($B, $Pattern)
    foreach ($Index in 1..3) {
        $ANumber = [int64]$AMatch.Groups[$Index].Value
        $BNumber = [int64]$BMatch.Groups[$Index].Value
        if ($ANumber -gt $BNumber) { return 1 }
        if ($ANumber -lt $BNumber) { return -1 }
    }
    $APre = $AMatch.Groups[4].Value
    $BPre = $BMatch.Groups[4].Value
    if ($APre -eq $BPre) { return 0 }
    if (-not $APre) { return 1 }
    if (-not $BPre) { return -1 }

    # Compare SemVer prerelease dot segments; numeric segments sort below non-numeric ones.
    $AParts = $APre -split '\.'
    $BParts = $BPre -split '\.'
    $PartCount = [Math]::Min($AParts.Count, $BParts.Count)
    for ($PartIndex = 0; $PartIndex -lt $PartCount; $PartIndex++) {
        $AIdentifier = $AParts[$PartIndex]
        $BIdentifier = $BParts[$PartIndex]
        if ($AIdentifier -ceq $BIdentifier) { continue }

        $ANumeric = $AIdentifier -match '^\d+$'
        $BNumeric = $BIdentifier -match '^\d+$'
        if ($ANumeric -and $BNumeric) {
            $ANumber = [int64]$AIdentifier
            $BNumber = [int64]$BIdentifier
            if ($ANumber -gt $BNumber) { return 1 }
            if ($ANumber -lt $BNumber) { return -1 }
            continue
        }
        if ($ANumeric) { return -1 }
        if ($BNumeric) { return 1 }
        return [Math]::Sign([string]::CompareOrdinal($AIdentifier, $BIdentifier))
    }
    if ($AParts.Count -gt $BParts.Count) { return 1 }
    if ($AParts.Count -lt $BParts.Count) { return -1 }
    return 0
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillDir = Split-Path -Parent $ScriptDir
$LocalManifestPath = Join-Path $SkillDir "manifest.json"
if (-not (Test-Path $LocalManifestPath -PathType Leaf)) { Fail-Json "INVALID_PACKAGE" "manifest.json is missing" }
try {
    $LocalManifest = Get-Content $LocalManifestPath -Raw | ConvertFrom-Json
} catch {
    Fail-Json "INVALID_PACKAGE" "manifest.json is invalid"
}
$MinVersion = [string]$LocalManifest.cli.min_version
$UpdateManifestUrl = [string]$LocalManifest.cli.update_manifest_url
if (-not $MinVersion) { Fail-Json "INVALID_PACKAGE" "manifest cli.min_version is empty" }
try { [void](Compare-SemVer $MinVersion $MinVersion) } catch { Fail-Json "INVALID_PACKAGE" "manifest cli.min_version is invalid" }
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -notin @("AMD64", "x86")) {
    Fail-Json "UNSUPPORTED_PLATFORM" "This internal build supports Windows x64 only"
}

$Platform = "windows-amd64"
$CliHome = if ($env:ZHIHU_CLI_HOME) { $env:ZHIHU_CLI_HOME } else { Join-Path $env:LOCALAPPDATA "ZhihuCLI" }
$CurrentDir = Join-Path $CliHome "current"
$Current = Join-Path $CurrentDir "zhihu-cli.exe"

# setup only installs or repairs; upgrade maintains an existing compatible CLI.
$CurrentVersion = $null
try { if (Test-Path $Current -PathType Leaf) { $CurrentVersion = [string]((& $Current version | ConvertFrom-Json).version) } } catch {}
if ($CurrentVersion -and (Compare-SemVer $CurrentVersion $MinVersion) -ge 0) {
    $AuthConfigured = $false
    try { $AuthConfigured = [bool]((& $Current auth status 2>$null | ConvertFrom-Json).source) } catch {}
    @{ ok = $true; installed = $false; reused_cli = $true; installed_cli_version = $CurrentVersion; binary_path = [IO.Path]::GetFullPath($Current); auth_configured = $AuthConfigured; next_action = $(if ($AuthConfigured) { "ready" } else { "request_access_secret" }) } | ConvertTo-Json -Compress
    exit 0
}

if (-not $UpdateManifestUrl) { Fail-Json "UPDATE_NOT_CONFIGURED" "CLI download manifest is not configured" }
try { $ManifestUri = [Uri]$UpdateManifestUrl } catch { Fail-Json "UPDATE_INTEGRITY_FAILED" "CLI download manifest URL is invalid" }
$AllowTestHttp = $env:ZHIHU_CLI_ALLOW_INSECURE_TEST_URL -eq "1" -and $ManifestUri.Scheme -eq "http" -and $ManifestUri.Host -in @("127.0.0.1", "localhost")
if (($ManifestUri.Scheme -ne "https" -and -not $AllowTestHttp) -or $ManifestUri.UserInfo) {
    Fail-Json "UPDATE_INTEGRITY_FAILED" "CLI download manifest must use HTTPS"
}

$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("zhihu-cli-setup-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempRoot | Out-Null
try {
    $RemoteManifestPath = Join-Path $TempRoot "manifest.json"
    [Console]::Error.WriteLine("Reading the official zhihu-cli release manifest...")
    Invoke-WebRequest -UseBasicParsing -MaximumRedirection 0 -Uri $ManifestUri -OutFile $RemoteManifestPath
    if ((Get-Item $RemoteManifestPath).Length -gt 1MB) { Fail-Json "UPDATE_INTEGRITY_FAILED" "CLI release manifest is too large" }
    try { $RemoteManifest = Get-Content $RemoteManifestPath -Raw | ConvertFrom-Json } catch { Fail-Json "UPSTREAM_ERROR" "CLI release manifest is invalid" }
    if ([int]$RemoteManifest.schema_version -ne 1) { Fail-Json "UPSTREAM_ERROR" "CLI release manifest schema is unsupported" }

    $LatestVersion = [string]$RemoteManifest.cli.latest_version
    try { if ((Compare-SemVer $LatestVersion $MinVersion) -lt 0) { Fail-Json "INCOMPATIBLE_VERSION" "Latest CLI does not satisfy this Skill" } } catch { Fail-Json "UPSTREAM_ERROR" "CLI release version is invalid" }
    $Artifact = $RemoteManifest.cli.artifacts.$Platform
    if (-not $Artifact) { Fail-Json "UPSTREAM_ERROR" "CLI release manifest lacks the current platform" }
    try { $ArtifactUri = [Uri][string]$Artifact.url } catch { Fail-Json "UPSTREAM_ERROR" "CLI artifact URL is invalid" }
    if ($ArtifactUri.Scheme -ne $ManifestUri.Scheme -or $ArtifactUri.Authority -ne $ManifestUri.Authority -or $ArtifactUri.UserInfo) {
        Fail-Json "UPDATE_INTEGRITY_FAILED" "CLI artifact host differs from manifest host"
    }
    $ExpectedSha = [string]$Artifact.sha256
    $ExpectedSize = [int64]$Artifact.size
    if ($ExpectedSha -cnotmatch '^[0-9a-f]{64}$' -or $ExpectedSize -le 0 -or $ExpectedSize -gt 128MB) {
        Fail-Json "UPSTREAM_ERROR" "CLI artifact integrity fields are invalid"
    }

    $Archive = Join-Path $TempRoot "zhihu-cli.zip"
    [Console]::Error.WriteLine("Downloading zhihu-cli $LatestVersion for $Platform...")
    Invoke-WebRequest -UseBasicParsing -MaximumRedirection 0 -Uri $ArtifactUri -OutFile $Archive
    if ((Get-Item $Archive).Length -ne $ExpectedSize) { Fail-Json "UPDATE_INTEGRITY_FAILED" "CLI artifact size does not match manifest" }
    if ((Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLowerInvariant() -ne $ExpectedSha) { Fail-Json "CHECKSUM_MISMATCH" "CLI artifact SHA-256 does not match manifest" }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $Entry = $Zip.Entries | Where-Object { $_.Name -eq "zhihu-cli.exe" } | Select-Object -First 1
        if (-not $Entry -or $Entry.Length -gt 128MB) { Fail-Json "UPDATE_INTEGRITY_FAILED" "CLI archive does not contain a valid zhihu-cli.exe" }
        $Staged = Join-Path $TempRoot "zhihu-cli.exe"
        $Input = $Entry.Open()
        $Output = [IO.File]::Open($Staged, [IO.FileMode]::CreateNew)
        try { $Input.CopyTo($Output) } finally { $Output.Dispose(); $Input.Dispose() }
    } finally {
        $Zip.Dispose()
    }
    try { $StagedVersion = [string]((& $Staged version | ConvertFrom-Json).version) } catch { Fail-Json "BINARY_INVALID" "Downloaded CLI cannot run" }
    if ($StagedVersion -ne $LatestVersion) { Fail-Json "BINARY_INVALID" "Downloaded CLI version does not match manifest" }

    $VersionDir = Join-Path $CliHome "versions\$LatestVersion"
    $Dest = Join-Path $VersionDir "zhihu-cli.exe"
    New-Item -ItemType Directory -Force -Path $VersionDir, $CurrentDir | Out-Null
    Copy-Item -Force $Staged $Dest
    Copy-Item -Force $Dest $Current

    $AuthConfigured = $false
    try { $AuthConfigured = [bool]((& $Current auth status 2>$null | ConvertFrom-Json).source) } catch {}
    $Absolute = [IO.Path]::GetFullPath($Current)
    [Console]::Error.WriteLine("[OK] zhihu-cli $LatestVersion is ready at $Absolute")
    @{ ok = $true; installed = $true; downloaded_cli_version = $LatestVersion; binary_path = $Absolute; auth_configured = $AuthConfigured; next_action = $(if ($AuthConfigured) { "ready" } else { "request_access_secret" }) } | ConvertTo-Json -Compress
} catch {
    if ($_.Exception.Message -match '^Response status code does not indicate success') {
        Fail-Json "NETWORK_ERROR" "Unable to download CLI release files"
    }
    throw
} finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $TempRoot
}
