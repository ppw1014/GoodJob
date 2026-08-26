[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Revision,
    [switch]$TestFailAfterBackup,
    [switch]$TestFailAfterActivation
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$SkillName = "goodjob-career-review"
$InstallMarkerName = ".goodjob-install.json"
$InstallMarkerSchema = "goodjob-skill-install-v1"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string[]]$GitArguments
    )

    $output = @(& git.exe -C $Repository @GitArguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine).Trim()
}

function Test-PathEntry {
    param([Parameter(Mandatory = $true)][string]$Path)

    return $null -ne (Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue)
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-PathEntry $Path)) {
        return $false
    }

    $entry = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    return 0 -ne ([int]$entry.Attributes -band [int][IO.FileAttributes]::ReparsePoint)
}

function Assert-NoReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (Test-ReparsePoint $Path) {
        throw "$Label must not be a reparse point: $Path"
    }
}

function Assert-ProtectedInstallPaths {
    param(
        [Parameter(Mandatory = $true)][string]$AgentsRoot,
        [Parameter(Mandatory = $true)][string]$SkillRoot,
        [Parameter(Mandatory = $true)][string]$ActivationRoot,
        [Parameter(Mandatory = $true)][string]$BackupParent,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$Target
    )

    Assert-NoReparsePoint $AgentsRoot ".agents"
    Assert-NoReparsePoint $SkillRoot ".agents\\skills"
    Assert-NoReparsePoint $ActivationRoot ".agents\\skill-staging"
    Assert-NoReparsePoint $BackupParent ".agents\\skill-backups"
    Assert-NoReparsePoint $BackupRoot "GoodJob backup root"
    Assert-NoReparsePoint $Target "GoodJob skill target"
}

function Get-SkillContentDigest {
    param([Parameter(Mandatory = $true)][string]$SkillPath)

    $root = [IO.Path]::GetFullPath($SkillPath).TrimEnd([char]92)
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "Skill directory is missing: $root"
    }

    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $records = New-Object 'System.Collections.Generic.List[string]'
    $pending.Push($root)

    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        Assert-NoReparsePoint $directory "Skill content directory"

        foreach ($entryPath in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $attributes = [IO.File]::GetAttributes($entryPath)
            if (0 -ne ([int]$attributes -band [int][IO.FileAttributes]::ReparsePoint)) {
                throw "Skill content must not contain a reparse point: $entryPath"
            }

            if (0 -ne ([int]$attributes -band [int][IO.FileAttributes]::Directory)) {
                $pending.Push($entryPath)
                continue
            }

            $relativePath = $entryPath.Substring($root.Length).TrimStart([char[]]@([char]92, [char]47))
            if ([string]::Equals($relativePath, $InstallMarkerName, [StringComparison]::OrdinalIgnoreCase)) {
                continue
            }

            $normalizedPath = $relativePath.Replace([char]92, [char]47).ToLowerInvariant()
            $fileHash = (Get-FileHash -LiteralPath $entryPath -Algorithm SHA256).Hash.ToLowerInvariant()
            $records.Add("${normalizedPath}:$fileHash")
        }
    }

    $payload = [Text.Encoding]::UTF8.GetBytes((@($records | Sort-Object) -join "`n"))
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha256.ComputeHash($payload))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-ManagedInstallation {
    param([Parameter(Mandatory = $true)][string]$InstallPath)

    $markerPath = Join-Path $InstallPath $InstallMarkerName
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        return $null
    }
    Assert-NoReparsePoint $markerPath "Install marker"

    try {
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }

    if ($marker.schema -ne $InstallMarkerSchema -or
        $marker.commit -notmatch '^[0-9a-fA-F]{40}$') {
        return $null
    }

    $contentHash = $null
    if ($null -ne $marker.content_sha256 -and
        $marker.content_sha256 -match '^[0-9a-fA-F]{64}$') {
        $contentHash = $marker.content_sha256.ToLowerInvariant()
    }

    return [PSCustomObject]@{
        Commit = $marker.commit.ToLowerInvariant()
        ContentHash = $contentHash
    }
}

function Assert-RequiredReleaseFiles {
    param([Parameter(Mandatory = $true)][string]$SkillPath)

    $requiredFiles = @(
        "SKILL.md",
        "agents\\openai.yaml",
        "runtime\\scripts\\launch_broker.py",
        "runtime\\scripts\\session.py",
        "runtime\\src\\goodjob\\dashboard_assets\\__init__.py",
        "runtime\\src\\goodjob\\dashboard_assets\\dashboard.css",
        "runtime\\src\\goodjob\\dashboard_assets\\dashboard.js"
    )

    foreach ($relativePath in $requiredFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $SkillPath $relativePath) -PathType Leaf)) {
            throw "Missing required release file: $relativePath"
        }

        Assert-NoReparsePoint (Join-Path $SkillPath $relativePath) "Required release file"
    }
}

function Write-InstallMarker {
    param(
        [Parameter(Mandatory = $true)][string]$InstallPath,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$ContentHash
    )

    $marker = [ordered]@{
        schema = $InstallMarkerSchema
        commit = $Commit
        content_sha256 = $ContentHash
    } | ConvertTo-Json -Compress
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $InstallPath $InstallMarkerName),
        $marker + [Environment]::NewLine,
        $encoding
    )
}

function Remove-DirectoryIfPresent {
    param(
        [AllowNull()][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($null -ne $Path -and (Test-PathEntry $Path)) {
        Assert-NoReparsePoint $Path $Label
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This installer requires native Windows."
}

[void](Get-Command git.exe -ErrorAction Stop)

$repository = (Resolve-Path -LiteralPath $RepositoryPath -ErrorAction Stop).Path
$topLevel = Invoke-Git $repository @("rev-parse", "--show-toplevel")
if (-not [string]::Equals(
    [IO.Path]::GetFullPath($repository).TrimEnd('\\'),
    [IO.Path]::GetFullPath($topLevel).TrimEnd('\\'),
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "RepositoryPath must be the checked-out repository root."
}

$commit = $null
if ($Revision -match '^[0-9a-fA-F]{40}$') {
    $commit = Invoke-Git $repository @("rev-parse", "--verify", "$Revision^{commit}")
    if (-not [string]::Equals($commit, $Revision, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Revision must resolve to its supplied full commit SHA."
    }
}
else {
    try {
        $tagRef = "refs/tags/$Revision"
        $null = Invoke-Git $repository @("check-ref-format", "--allow-onelevel", $tagRef)
        $null = Invoke-Git $repository @("show-ref", "--verify", "--quiet", $tagRef)
        $commit = Invoke-Git $repository @("rev-parse", "--verify", "$tagRef^{commit}")
    }
    catch {
        throw "Revision must be a full commit SHA or an exact tag."
    }
}

if ($commit -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Revision must be a full commit SHA or an exact tag."
}
$commit = $commit.ToLowerInvariant()

$userProfile = [Environment]::GetEnvironmentVariable("USERPROFILE")
if ([string]::IsNullOrWhiteSpace($userProfile)) {
    throw "USERPROFILE is required for a user-level Codex installation."
}

$agentsRoot = Join-Path $userProfile ".agents"
$skillRoot = Join-Path $agentsRoot "skills"
$activationRoot = Join-Path $agentsRoot "skill-staging"
$target = Join-Path $skillRoot $SkillName
$legacyTarget = Join-Path $userProfile ".codex\\skills\\$SkillName"
$backupParent = Join-Path $agentsRoot "skill-backups"
$backupRoot = Join-Path $backupParent $SkillName

Assert-ProtectedInstallPaths $agentsRoot $skillRoot $activationRoot $backupParent $backupRoot $target

if (Test-PathEntry $legacyTarget) {
    throw (
        "Legacy Codex skill conflict at $legacyTarget. Back up or remove it manually, " +
        "then retry; this installer will not migrate, delete, or overwrite that location."
    )
}

$installed = $null
if (Test-PathEntry $target) {
    $existing = Get-Item -LiteralPath $target -Force
    if (-not $existing.PSIsContainer) {
        throw "Existing installation is not a directory: $target"
    }

    $installed = Get-ManagedInstallation $target
    if ($null -eq $installed) {
        throw "Existing installation is not managed by this installer: $target"
    }

}

[void][IO.Directory]::CreateDirectory($skillRoot)
[void][IO.Directory]::CreateDirectory($activationRoot)
[void][IO.Directory]::CreateDirectory($backupRoot)
Assert-ProtectedInstallPaths $agentsRoot $skillRoot $activationRoot $backupParent $backupRoot $target

$targetVolume = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($target))
$activationVolume = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($activationRoot))
$backupVolume = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($backupRoot))
if (-not [string]::Equals($targetVolume, $activationVolume, [StringComparison]::OrdinalIgnoreCase) -or
    -not [string]::Equals($targetVolume, $backupVolume, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Skill target, staging root, and backup root must be on the same volume."
}

$stagingRoot = Join-Path ([IO.Path]::GetTempPath()) ("goodjob-skill-stage-" + [Guid]::NewGuid().ToString("N"))
$candidate = Join-Path $activationRoot ("$SkillName.pending-" + [Guid]::NewGuid().ToString("N"))
$backup = $null
$oldMoved = $false

try {
    [void][IO.Directory]::CreateDirectory($stagingRoot)
    $archivePath = Join-Path $stagingRoot "release.zip"
    $extractionRoot = Join-Path $stagingRoot "extracted"
    Invoke-Git $repository @(
        "archive",
        "--format=zip",
        "--output=$archivePath",
        "--prefix=$SkillName/",
        "${commit}:.agents/skills/$SkillName"
    ) | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $extractionRoot)

    $stagedSkill = Join-Path $extractionRoot $SkillName
    Assert-RequiredReleaseFiles $stagedSkill

    [void][IO.Directory]::CreateDirectory($candidate)
    foreach ($entry in Get-ChildItem -LiteralPath $stagedSkill -Force) {
        Copy-Item -LiteralPath $entry.FullName -Destination $candidate -Recurse -Force
    }
    $candidateHash = Get-SkillContentDigest $candidate
    Write-InstallMarker $candidate $commit $candidateHash
    Assert-RequiredReleaseFiles $candidate

    if ($null -ne $installed -and
        $installed.Commit -eq $commit -and
        $installed.ContentHash -eq $candidateHash) {
        $targetIsCurrent = $false
        try {
            Assert-RequiredReleaseFiles $target
            if ((Get-SkillContentDigest $target) -eq $candidateHash) {
                $targetIsCurrent = $true
            }
        }
        catch {
            # A managed but damaged installation is replaced from the fixed Git release below.
        }

        if ($targetIsCurrent) {
            Remove-DirectoryIfPresent $candidate "Pending skill candidate"
            $candidate = $null
            Write-Output "GoodJob skill $commit is already installed."
            return
        }
    }

    if (Test-PathEntry $target) {
        $backup = Join-Path $backupRoot (
            "$($commit.Substring(0, 12))-$([Guid]::NewGuid().ToString('N'))"
        )
        [IO.Directory]::Move($target, $backup)
        $oldMoved = $true

        if ($TestFailAfterBackup) {
            throw "Injected activation failure after backup."
        }
    }

    [IO.Directory]::Move($candidate, $target)
    if ($TestFailAfterActivation) {
        throw "Injected activation failure after target switch."
    }

    $candidate = $null
    Write-Output "Installed GoodJob skill $commit to $target"
}
catch {
    $failure = $_
    $cleanupFailure = $null
    try {
        Remove-DirectoryIfPresent $candidate "Pending skill candidate"

        if ($oldMoved -and (Test-PathEntry $target)) {
            Remove-DirectoryIfPresent $target "Activated skill target"
        }

        if ($oldMoved -and -not (Test-PathEntry $target) -and $null -ne $backup) {
            Assert-ProtectedInstallPaths $agentsRoot $skillRoot $activationRoot $backupParent $backupRoot $target
            [IO.Directory]::Move($backup, $target)
            $backup = $null
        }
    }
    catch {
        $cleanupFailure = $_
    }

    if ($null -ne $cleanupFailure) {
        throw (
            "Activation failed and automatic rollback also failed. " +
            "Restore $backup to $target manually. Original error: $($failure.Exception.Message). " +
            "Rollback error: $($cleanupFailure.Exception.Message)"
        )
    }

    if ($oldMoved -and -not (Test-PathEntry $target)) {
        throw (
            "Activation failed and the prior installation was not restored. " +
            "Restore $backup to $target manually. Original error: $($failure.Exception.Message)"
        )
    }

    throw $failure
}
finally {
    Remove-DirectoryIfPresent $stagingRoot "Temporary release staging"
}
