$ErrorActionPreference = "Stop"

function Fail-Package([string]$Message) {
    @{ ok = $false; error = @{ code = "INVALID_PACKAGE"; message = $Message } } | ConvertTo-Json -Compress
    exit 7
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillDir = Split-Path -Parent $ScriptDir
$ManifestPath = Join-Path $SkillDir "manifest.json"
if (-not (Test-Path $ManifestPath -PathType Leaf)) { Fail-Package "manifest.json is missing" }
try {
    $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
} catch {
    Fail-Package "manifest.json is invalid"
}
$SkillVersion = [string]$Manifest.version
$MinVersion = [string]$Manifest.cli.min_version
if (-not $SkillVersion) { Fail-Package "manifest version is empty" }
if (-not $MinVersion) { Fail-Package "manifest cli.min_version is empty" }
$CliHome = if ($env:ZHIHU_CLI_HOME) { $env:ZHIHU_CLI_HOME } else { Join-Path $env:LOCALAPPDATA "ZhihuCLI" }
$Binary = Join-Path $CliHome "current\zhihu-cli.exe"

# System.Version does not support SemVer prerelease values such as 0.2.0-beta.1.
# Use the same comparison rules as setup.ps1 to enforce the minimum CLI version.
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

function Test-Ready {
    if (-not (Test-Path $Binary -PathType Leaf)) { return $false }
    try {
        $CurrentVersion = [string]((& $Binary version | ConvertFrom-Json).version)
        return (Compare-SemVer $CurrentVersion $MinVersion) -ge 0
    } catch {
        return $false
    }
}

if ($args.Count -gt 0 -and $args[0] -eq "status") {
    if (Test-Ready) {
        & $Binary status --skill-version $SkillVersion --min-cli-version $MinVersion
        exit $LASTEXITCODE
    } else {
        @{ ok = $true; installed = $false; skill = @{ current_version = $SkillVersion; min_cli_version = $MinVersion }; auth = @{ configured = $false }; update_check = @{ status = "not_applicable" }; next_action = "request_install_consent" } | ConvertTo-Json -Compress -Depth 4
    }
    exit 0
}

if ($args.Count -gt 0 -and $args[0] -eq "setup") {
    & (Join-Path $ScriptDir "setup.ps1") @($args | Select-Object -Skip 1)
    exit $LASTEXITCODE
}

if (-not (Test-Ready)) {
    @{ ok = $false; error = @{ code = "CLI_NOT_INSTALLED"; message = "Run scripts/setup.ps1 after the user approves installation" } } | ConvertTo-Json -Compress
    exit 7
}

& $Binary @args
exit $LASTEXITCODE
