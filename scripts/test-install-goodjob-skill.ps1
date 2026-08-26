[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This test requires native Windows."
}

$installerPath = Join-Path $PSScriptRoot "install-goodjob-skill.ps1"

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )

    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

function Assert-FileText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Expected
    )

    Assert-True (Test-Path -LiteralPath $Path -PathType Leaf) "Missing file: $Path"
    Assert-True ([IO.File]::ReadAllText($Path) -eq $Expected) "Unexpected content in: $Path"
}

function Assert-Throws {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Pattern
    )

    $caught = $null
    try {
        & $Action
    }
    catch {
        $caught = $_
    }

    Assert-True ($null -ne $caught) "Expected an error matching: $Pattern"
    Assert-True ($caught.Exception.Message -match $Pattern) (
        "Unexpected error: " + $caught.Exception.Message
    )
}

function New-TestJunction {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Target
    )

    [void](New-Item -ItemType Junction -Path $Path -Target $Target -ErrorAction Stop)
}

function Remove-TestJunction {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (Test-Path -LiteralPath $Path) {
        [IO.Directory]::Delete($Path)
    }
}

function Invoke-TestGit {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $output = @(& git.exe -C $Repository @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Test Git command failed: $($output -join [Environment]::NewLine)"
    }
    return $output
}

function Set-ReleaseFiles {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][bool]$IncludeLegacy
    )

    $skill = Join-Path $Repository ".agents\\skills\\goodjob-career-review"
    $files = @{
        "SKILL.md" = "skill-$Version"
        "agents\\openai.yaml" = "agent-$Version"
        "runtime\\scripts\\launch_broker.py" = "launcher-$Version"
        "runtime\\scripts\\session.py" = "session-$Version"
        "runtime\\src\\goodjob\\dashboard_assets\\__init__.py" = "assets-$Version"
        "runtime\\src\\goodjob\\dashboard_assets\\dashboard.css" = "css-$Version"
        "runtime\\src\\goodjob\\dashboard_assets\\dashboard.js" = "js-$Version"
    }

    foreach ($relativePath in $files.Keys) {
        $path = Join-Path $skill $relativePath
        [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($path))
        [IO.File]::WriteAllText($path, $files[$relativePath])
    }

    $removedInV2 = Join-Path $skill "removed-in-v2.txt"
    if ($IncludeLegacy) {
        [IO.File]::WriteAllText($removedInV2, "legacy-$Version")
    }
    elseif (Test-Path -LiteralPath $removedInV2) {
        Remove-Item -LiteralPath $removedInV2 -Force
    }
}

function Commit-Release {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Message
    )

    Invoke-TestGit $Repository @("add", "-A") | Out-Null
    Invoke-TestGit $Repository @("commit", "-m", $Message) | Out-Null
    return (Invoke-TestGit $Repository @("rev-parse", "HEAD")).ToString().Trim()
}

$originalUserProfile = $env:USERPROFILE
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "GoodJob Installer Tests " + [Guid]::NewGuid().ToString("N")
)

try {
    $repositoryParent = $testRoot
    foreach ($index in 1..3) {
        $repositoryParent = Join-Path $repositoryParent "Long Repository Segment $index"
    }
    $repository = Join-Path $repositoryParent "Repository With Spaces"
    $testProfile = Join-Path $testRoot "User Profile With Spaces"
    [void][System.IO.Directory]::CreateDirectory($repository)
    [void][System.IO.Directory]::CreateDirectory($testProfile)
    $env:USERPROFILE = $testProfile

    Invoke-TestGit $repository @("init") | Out-Null
    Invoke-TestGit $repository @("config", "user.name", "GoodJob Installer Test") | Out-Null
    Invoke-TestGit $repository @("config", "user.email", "installer-test@goodjob.invalid") | Out-Null
    Invoke-TestGit $repository @("config", "core.longpaths", "true") | Out-Null

    Set-ReleaseFiles $repository "v1" $true
    $commitV1 = Commit-Release $repository "release v1"
    $target = Join-Path $testProfile ".agents\\skills\\goodjob-career-review"
    $backupRoot = Join-Path $testProfile ".agents\\skill-backups\\goodjob-career-review"
    $backupParent = Split-Path -Parent $backupRoot

    $agentsOutside = Join-Path $testRoot "Outside Agents Root"
    [void][System.IO.Directory]::CreateDirectory($agentsOutside)
    [IO.File]::WriteAllText((Join-Path $agentsOutside "sentinel.txt"), "do-not-touch")
    New-TestJunction (Join-Path $testProfile ".agents") $agentsOutside
    Assert-Throws {
        & $installerPath -RepositoryPath $repository -Revision $commitV1 | Out-Null
    } "reparse point"
    Assert-FileText (Join-Path $agentsOutside "sentinel.txt") "do-not-touch"
    Remove-TestJunction (Join-Path $testProfile ".agents")

    [void][System.IO.Directory]::CreateDirectory($target)
    [IO.File]::WriteAllText((Join-Path $target "manual.txt"), "do-not-overwrite")
    Assert-Throws {
        & $installerPath -RepositoryPath $repository -Revision $commitV1 | Out-Null
    } "not managed by this installer"
    Remove-Item -LiteralPath (Join-Path $testProfile ".agents") -Recurse -Force

    & $installerPath -RepositoryPath $repository -Revision $commitV1 | Out-Null
    Assert-FileText (Join-Path $target "SKILL.md") "skill-v1"
    Assert-True (Test-Path -LiteralPath (Join-Path $target "removed-in-v2.txt")) (
        "First install omitted a tracked file."
    )

    & $installerPath -RepositoryPath $repository -Revision $commitV1 | Out-Null
    $backups = @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force)
    Assert-True ($backups.Count -eq 0) "Same-version rerun created a backup."

    [IO.File]::WriteAllText((Join-Path $target "SKILL.md"), "tampered")
    Remove-Item -LiteralPath (
        Join-Path $target "runtime\\src\\goodjob\\dashboard_assets\\dashboard.css"
    ) -Force
    & $installerPath -RepositoryPath $repository -Revision $commitV1 | Out-Null
    Assert-FileText (Join-Path $target "SKILL.md") "skill-v1"
    Assert-FileText (
        Join-Path $target "runtime\\src\\goodjob\\dashboard_assets\\dashboard.css"
    ) "css-v1"
    $backups = @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force)
    Assert-True ($backups.Count -eq 1) "Damaged same-version rerun did not preserve one backup."

    $savedTarget = Join-Path $testRoot "Saved Managed Skill"
    [IO.Directory]::Move($target, $savedTarget)
    $targetOutside = Join-Path $testRoot "Outside Target"
    [void][System.IO.Directory]::CreateDirectory($targetOutside)
    New-TestJunction $target $targetOutside
    Assert-Throws {
        & $installerPath -RepositoryPath $repository -Revision $commitV1 | Out-Null
    } "reparse point"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $targetOutside ".goodjob-install.json"))) (
        "Target junction allowed an external write."
    )
    Remove-TestJunction $target
    [IO.Directory]::Move($savedTarget, $target)

    Set-ReleaseFiles $repository "v2" $false
    $commitV2 = Commit-Release $repository "release v2"
    Invoke-TestGit $repository @("tag", "release-v2", $commitV2) | Out-Null
    $savedBackupParent = Join-Path $testRoot "Saved Skill Backups"
    [IO.Directory]::Move($backupParent, $savedBackupParent)
    $backupOutside = Join-Path $testRoot "Outside Backup Root"
    [void][System.IO.Directory]::CreateDirectory($backupOutside)
    [IO.File]::WriteAllText((Join-Path $backupOutside "sentinel.txt"), "do-not-touch")
    New-TestJunction $backupParent $backupOutside
    Assert-Throws {
        & $installerPath -RepositoryPath $repository -Revision "release-v2" | Out-Null
    } "reparse point"
    Assert-FileText (Join-Path $backupOutside "sentinel.txt") "do-not-touch"
    Assert-FileText (Join-Path $target "SKILL.md") "skill-v1"
    Remove-TestJunction $backupParent
    [IO.Directory]::Move($savedBackupParent, $backupParent)

    & $installerPath -RepositoryPath $repository -Revision "release-v2" | Out-Null
    Assert-FileText (Join-Path $target "SKILL.md") "skill-v2"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $target "removed-in-v2.txt"))) (
        "Update retained a file deleted by the new release."
    )
    $backups = @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force)
    Assert-True ($backups.Count -eq 2) "Update did not retain the expected complete backups."
    Assert-True (@($backups | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName "removed-in-v2.txt")
    }).Count -ge 1) (
        "Update backup was incomplete."
    )

    Set-ReleaseFiles $repository "v3" $false
    $commitV3 = Commit-Release $repository "release v3"
    Assert-Throws {
        & $installerPath -RepositoryPath $repository -Revision $commitV3 -TestFailAfterBackup |
            Out-Null
    } "Injected activation failure"
    Assert-FileText (Join-Path $target "SKILL.md") "skill-v2"
    $backups = @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force)
    Assert-True ($backups.Count -eq 2) "Failed pre-activation rollback left an extra backup or mixed target."

    Assert-Throws {
        & $installerPath -RepositoryPath $repository -Revision $commitV3 -TestFailAfterActivation |
            Out-Null
    } "Injected activation failure after target switch"
    Assert-FileText (Join-Path $target "SKILL.md") "skill-v2"
    $backups = @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force)
    Assert-True ($backups.Count -eq 2) "Failed post-activation rollback left an extra backup or mixed target."

    $legacyTarget = Join-Path $testProfile ".codex\\skills\\goodjob-career-review"
    [void][System.IO.Directory]::CreateDirectory($legacyTarget)
    [IO.File]::WriteAllText((Join-Path $legacyTarget "sentinel.txt"), "do-not-touch")
    Assert-Throws {
        & $installerPath -RepositoryPath $repository -Revision $commitV3 | Out-Null
    } "Legacy Codex skill conflict"
    Assert-FileText (Join-Path $legacyTarget "sentinel.txt") "do-not-touch"
    Assert-FileText (Join-Path $target "SKILL.md") "skill-v2"
    Remove-Item -LiteralPath (Join-Path $testProfile ".codex") -Recurse -Force

    Assert-Throws {
        & $installerPath -RepositoryPath $repository -Revision "HEAD" | Out-Null
    } "full commit SHA or an exact tag"

    & $installerPath -RepositoryPath $repository -Revision $commitV1 | Out-Null
    Assert-FileText (Join-Path $target "SKILL.md") "skill-v1"
    Assert-True (Test-Path -LiteralPath (Join-Path $target "removed-in-v2.txt")) (
        "Installing an older fixed revision did not roll back the release."
    )

    Remove-Item -LiteralPath (
        Join-Path $repository ".agents\\skills\\goodjob-career-review\\runtime\\src\\goodjob\\dashboard_assets\\dashboard.css"
    ) -Force
    $incompleteCommit = Commit-Release $repository "incomplete release"
    Assert-Throws {
        & $installerPath -RepositoryPath $repository -Revision $incompleteCommit | Out-Null
    } "required release file"
    Assert-FileText (Join-Path $target "SKILL.md") "skill-v1"

    Write-Output "GoodJob PowerShell installer tests passed."
}
finally {
    $env:USERPROFILE = $originalUserProfile
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
