<#
.SYNOPSIS
  Refresh NotebookLM credentials for the nbk-mcp-server deployment in ANY
  target OpenShift/Kubernetes environment, without that environment ever
  needing browser/login access of its own.

.DESCRIPTION
  nbk-mcp-server never performs an interactive login itself - its
  entrypoint.sh only copies credential files (auth.json, cookies.json,
  metadata.json) out of a read-only Secret-backed volume at boot. Those
  files are always produced elsewhere, by a human running `nlm login` on
  any machine with a browser and network access to Google - that machine
  does NOT need to be anywhere near the cluster the pod runs in.

  This script automates that refresh cycle end-to-end:

    1. (Optional) Runs `nlm login` on THIS machine to mint a fresh Google
       session (opens a browser).
    2. Reads the resulting credential files from the local
       notebooklm-mcp-cli profile directory.
    3. Renders a Kubernetes/OpenShift Secret manifest from those files, then
       either:
         -Mode Apply  (default) - applies it directly to the target
                        namespace/context and restarts the deployment.
                        Requires `oc`/`kubectl` + network access FROM THIS
                        MACHINE to the target cluster's API server.
         -Mode Export - writes a ready-to-apply Secret YAML file to disk
                        instead, for hand-off via GitOps, sealed-secrets, a
                        jump host, or any other out-of-band delivery path.
                        Use this when you don't have direct cluster access
                        to the target environment either.

  IMPORTANT: `nlm login --check` (and the MCP server_info auth_status field)
  only performs a shallow homepage-fetch probe and can report success even
  when the real NotebookLM API rejects every query. After refreshing, always
  verify with a real notebook_query / notebook_list call against the target
  environment before trusting the fix.

.PARAMETER Mode
  "Apply" (default, push directly to the target cluster) or "Export" (write
  a Secret YAML file for out-of-band delivery).

.PARAMETER Namespace
  Target namespace holding nbk-mcp-server. Default: debug-agent-mcp

.PARAMETER SecretName
  Target Secret name. Default: nbk-mcp-creds

.PARAMETER DeploymentName
  Deployment to roll-restart after an Apply. Default: nbk-mcp-server

.PARAMETER KubeContext
  Optional oc/kubectl context name, if you have multiple clusters configured
  locally and need to target a specific one.

.PARAMETER ProfileDir
  Local notebooklm-mcp-cli credential store. Default: the NOTEBOOKLM_MCP_CLI_PATH
  environment variable if set (the CLI itself honors this override), else
  $env:USERPROFILE\.notebooklm-mcp-cli.

.PARAMETER ProfileName
  Profile name under ProfileDir\profiles\. Default: default

.PARAMETER OutputPath
  Export-mode output file path. Default: .\nbk-mcp-creds-secret.yaml

.PARAMETER SkipLogin
  Skip running `nlm login` and just package whatever credentials are already
  on disk (e.g. if you logged in moments ago for another purpose). Does NOT
  require the `nlm` CLI to be installed on this machine at all - only the
  credential files themselves need to be present (e.g. copied in from
  another machine's %USERPROFILE%\.notebooklm-mcp-cli).

.PARAMETER NoRestart
  In Apply mode, update the Secret but skip the deployment rollout restart
  (useful if you want to batch multiple changes before restarting).

.PARAMETER InstallCli
  Fallback only - if mcp-servers/notebooklm-portable/ (see
  scripts/build-nbk-portable-bundle.ps1) is NOT present next to this script,
  and `nlm` is missing (or below -CliVersion) on PATH, install/upgrade it
  automatically via `uv tool install` (preferred) or `python -m pip install`
  (fallback) before continuing. Off by default - this script will not alter
  a shared machine's Python environment unless you explicitly ask it to.
  Requires Python 3.9+ already on PATH; does not install Python itself.
  Not needed at all when the portable bundle is present - that needs no
  system Python/pip/uv and this flag is simply ignored in that case.

.PARAMETER CliVersion
  notebooklm-mcp-cli version to install/require. Default: 0.15.5 (versions
  before this had a login flow that could silently skip real interactive
  auth - see MCP-SETUP-LEARNINGS.md). Only used with -InstallCli, or to flag
  a mismatch warning if nlm is already present at a different version.

.EXAMPLE
  # Fresh login, push straight to a reachable new-environment cluster
  .\refresh-nbk-creds.ps1 -Mode Apply -Namespace debug-agent-mcp -KubeContext new-env

.EXAMPLE
  # Fresh login, produce a YAML bundle for an environment you don't have
  # direct cluster access to (hand off via GitOps / sealed-secrets / jump host)
  .\refresh-nbk-creds.ps1 -Mode Export -OutputPath .\out\nbk-mcp-creds-secret.yaml

.EXAMPLE
  # Already logged in a minute ago - just re-package + restart
  .\refresh-nbk-creds.ps1 -SkipLogin -KubeContext new-env

.EXAMPLE
  # Brand new machine, nlm not installed yet - let the script handle it
  .\refresh-nbk-creds.ps1 -InstallCli -Mode Export -OutputPath .\out\nbk-mcp-creds-secret.yaml

.NOTES
  ZERO-PREREQUISITE SHARING: run scripts\package-nbk-standalone.ps1 to produce
  one zip containing this script plus a fully self-contained portable
  notebooklm-mcp-cli bundle (its own embeddable Python + all dependencies).
  A colleague just unzips it and runs this script - no Python, pip, uv, or
  internet access for installation required on their machine at all. This
  script auto-detects that bundle (mcp-servers\notebooklm-portable\ or
  notebooklm-portable\ next to this file) and prefers it automatically.

  REQUIREMENTS, if you're sharing just this one .ps1 file WITHOUT the
  portable bundle above:
    - Default mode (no -SkipLogin): the `nlm` CLI (notebooklm-mcp-cli) MUST
      be installed on THIS machine, and this machine needs a real GUI/browser
      it can launch interactively - `nlm login` cannot complete on a headless
      server, a bare SSH session, or a bastion with no desktop session.
      Install with: python -m pip install notebooklm-mcp-cli==0.15.5 (no uv required -
      uv is just one of two optional ways to install it, pip works fine on
      its own) - or just pass -InstallCli and let this script do it.
    - -SkipLogin: does NOT need `nlm` installed at all on this machine - it
      only reads 3 existing JSON files (auth.json, cookies.json,
      metadata.json). Use this when credentials were already minted
      elsewhere and you just need to package/push them.
    - -Mode Apply additionally needs `oc` on PATH with access to the target
      cluster. -Mode Export needs neither `nlm` nor `oc` - it's pure
      PowerShell file I/O, for machines with no cluster access at all.
#>

param(
    [ValidateSet("Apply", "Export")]
    [string]$Mode = "Apply",

    [string]$Namespace = "debug-agent-mcp",
    [string]$SecretName = "nbk-mcp-creds",
    [string]$DeploymentName = "nbk-mcp-server",
    [string]$KubeContext = "",
    [string]$ProfileDir = $(if ($env:NOTEBOOKLM_MCP_CLI_PATH) { $env:NOTEBOOKLM_MCP_CLI_PATH } else { "$env:USERPROFILE\.notebooklm-mcp-cli" }),
    [string]$ProfileName = "default",
    [string]$OutputPath = ".\nbk-mcp-creds-secret.yaml",
    [switch]$SkipLogin,
    [switch]$NoRestart,
    [switch]$InstallCli,
    [string]$CliVersion = "0.15.5"
)

$ErrorActionPreference = "Stop"

# PowerShell 7.3+ defaults $PSNativeCommandUseErrorActionPreference to $true,
# which means ANY native command (nlm, oc, uv, python, pip, ...) that writes
# so much as one informational line to stderr gets treated as a SCRIPT-
# TERMINATING error here, because $ErrorActionPreference is "Stop" above -
# regardless of its actual exit code. Confirmed in practice: the Python
# Install Manager writes a harmless "successfully updated to X" notice to
# stderr on its first run and that alone crashed this script before pip ever
# got a chance to run, even though nothing had actually failed yet. This
# script already checks $LASTEXITCODE explicitly after every native command
# call, which is the correct and sufficient success/failure signal - so
# disable the PS 7.3+ auto-promotion globally rather than let incidental
# stderr chatter from any tool abort the whole run. Harmless no-op on
# Windows PowerShell 5.1, which has no such variable/behavior at all.
$PSNativeCommandUseErrorActionPreference = $false

function Fail($msg) {
    Write-Error $msg
    exit 1
}

# Separately from $PSNativeCommandUseErrorActionPreference above: merging a
# native command's stderr into the pipeline via '2>&1' wraps each stderr line
# as an ErrorRecord, and under $ErrorActionPreference = "Stop" that alone
# throws a terminating error - independent of exit code, and independent of
# the preference set above. This is the actual mechanism that turns a normal
# "pip build failed, here's why" message, or even just a harmless progress
# line a tool happens to write to stderr, into a script crash that only shows
# the FIRST stderr line and hides the real detail. Use this helper (which
# locally relaxes $ErrorActionPreference just for the duration of the call)
# any time output needs to be captured as text for display or pattern-
# matching; for installer calls where only $LASTEXITCODE matters, prefer no
# redirection at all (see Install-NlmCli below) so output streams live.
function Invoke-CaptureOutput {
    param([Parameter(Mandatory)][scriptblock]$ScriptBlock)
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        return (& $ScriptBlock 2>&1 | Out-String)
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

# Portable bundle detection: scripts/build-nbk-portable-bundle.ps1 produces a
# self-contained embeddable-Python + vendored notebooklm-mcp-cli bundle that
# needs ZERO system Python/pip/uv on the target machine at all (see that
# script's header for why). If present, prefer it over anything on PATH -
# it sidesteps every Windows Python environment quirk this script has hit in
# practice (missing pip/pip3, fake Store python.exe stubs, Python Install
# Manager self-update). Checks a few relative layouts so this resolves
# whether run from the real repo (scripts/ + mcp-servers/ at repo root) or
# from a standalone zip produced by scripts/package-nbk-standalone.ps1
# (script + notebooklm-portable/ as direct siblings, no mcp-servers/ prefix).
function Resolve-NlmCommand {
    # scripts/package-nbk-standalone.ps1 renames every .exe/.pyd/.dll inside
    # the portable bundle to .nbkbin before zipping - confirmed in practice
    # that emailing or Teams/SharePoint-uploading a zip containing a raw
    # python.exe + many .pyd/.dll files hangs or gets silently blocked by
    # Defender for Office 365 Safe Attachments / SharePoint DLP, on both
    # channels independently. Restore the real names here, transparently,
    # before looking for python.exe - same candidate paths as below so this
    # works whether unzipped from the standalone package or copied as-is
    # from the repo (where build-nbk-portable-bundle.ps1 never renames
    # anything, so this is a one-time no-op in that case).
    function Restore-PortableBundleIfRenamed {
        param([string]$PortableRoot)
        $renamedFiles = Get-ChildItem $PortableRoot -Recurse -File -Filter "*.nbkbin" -ErrorAction SilentlyContinue
        if ($renamedFiles.Count -eq 0) { return }
        Write-Host "==> First run of this standalone package: restoring $($renamedFiles.Count) renamed file(s) (.nbkbin -> real extension)..." -ForegroundColor Cyan
        foreach ($f in $renamedFiles) {
            $realName = $f.Name.Substring(0, $f.Name.Length - ".nbkbin".Length)
            try {
                Rename-Item -Path $f.FullName -NewName $realName -ErrorAction Stop
            } catch {
                Write-Warning "Could not restore $($f.FullName) -> $realName : $($_.Exception.Message)"
            }
        }
    }

    $candidates = @(
        (Join-Path $PSScriptRoot "..\mcp-servers\notebooklm-portable"),
        (Join-Path $PSScriptRoot "mcp-servers\notebooklm-portable"),
        (Join-Path $PSScriptRoot "notebooklm-portable")
    )
    foreach ($portableRoot in $candidates) {
        if (Test-Path $portableRoot) {
            Restore-PortableBundleIfRenamed -PortableRoot $portableRoot
            $exe = Join-Path $portableRoot "python\python.exe"
            if (Test-Path $exe) {
                return [PSCustomObject]@{
                    Mode   = "Portable"
                    Exe    = (Resolve-Path $exe).Path
                    Prefix = @("-m", "notebooklm_tools.cli.main")
                }
            }
        }
    }
    return [PSCustomObject]@{ Mode = "System"; Exe = "nlm"; Prefix = @() }
}

# Single call site for every nlm invocation below, so both the portable
# bundle and a system-installed nlm are driven identically. -Capture routes
# through Invoke-CaptureOutput (text result, no live echo); without it the
# command's streams print live and only $LASTEXITCODE matters (used for
# 'nlm login', which needs its real-time browser-flow prompts visible).
function Invoke-Nlm {
    param(
        [Parameter(Mandatory)]$NlmCmd,
        [Parameter(ValueFromRemainingArguments)][string[]]$NlmArgs,
        [switch]$Capture
    )
    $exe = $NlmCmd.Exe
    $fullArgs = @($NlmCmd.Prefix) + @($NlmArgs)
    if ($Capture) {
        return Invoke-CaptureOutput { & $exe @fullArgs }
    } else {
        & $exe @fullArgs
        return $null
    }
}

# 1. Fresh login (unless skipped) --------------------------------------------
if (-not $SkipLogin) {
    function Install-NlmCli {
        param([string]$Version)

        # 'pip'/'pip3' are NOT reliably separate executables on PATH on Windows
        # even when Python itself is fully installed and working (confirmed in
        # practice - a real machine had a working 'python' but neither 'pip'
        # nor 'pip3' as standalone commands). 'py' is the Windows Python
        # launcher, also common. 'python -m pip' / 'py -m pip' is pip's own
        # documented, more portable invocation - use that instead of ever
        # calling a standalone pip/pip3 executable directly.
        #
        # IMPORTANT: Get-Command alone is not enough to confirm Python is
        # real. Confirmed in practice, two distinct non-working stubs both
        # satisfy Get-Command on Windows and both need an actual invocation
        # to tell apart from a real install:
        #   1. The classic Microsoft Store 'App Execution Alias' stub under
        #      %LOCALAPPDATA%\Microsoft\WindowsApps\ - running it just prints
        #      "Python was not found; run without arguments to install from
        #      the Microsoft Store..." and fails outright.
        #   2. The newer official Python Install Manager ('pymanager') shim -
        #      its FIRST invocation on a machine can self-update and print
        #      "Python install manager was successfully updated to X" to
        #      stderr with no Python runtime actually installed/selected yet,
        #      then needs to be invoked a second time before it behaves like
        #      a normal 'python'.
        # Try each candidate up to twice (to get past a one-time pymanager
        # self-update) and only trust a real "Python X.Y" version string.
        $pythonCmd = $null
        $pymanagerSelfUpdated = $false
        foreach ($candidate in @("python", "py", "python3")) {
            if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
            for ($attempt = 1; $attempt -le 2; $attempt++) {
                $verOut = Invoke-CaptureOutput { & $candidate --version }
                if ($LASTEXITCODE -eq 0 -and $verOut -match "Python \d+\.\d+") {
                    $pythonCmd = $candidate
                    break
                }
                if ($verOut -match "install manager|Microsoft Store") {
                    $pymanagerSelfUpdated = $true
                    continue  # try this same candidate again - the self-update is usually one-time
                }
                break  # some other failure - no point retrying this candidate
            }
            if ($pythonCmd) { break }
        }
        if (-not $pythonCmd) {
            if ($pymanagerSelfUpdated) {
                Fail (@(
                    "Python's launcher just self-updated (or redirected to the Microsoft Store) and still has no"
                    "actual Python runtime installed/selected - this is a one-time first-run thing, not a failure"
                    "of this script. Fix it, then re-run this script with -InstallCli:"
                    "  1. Open a normal terminal and run 'python' (or 'py') by itself once."
                    "  2. If it opens the Microsoft Store, either install Python from there, or better, install"
                    "     real Python from https://python.org/downloads (check 'Add python.exe to PATH') and then"
                    "     disable the Store stub: Settings > Apps > Advanced app settings > App execution aliases >"
                    "     turn off 'python.exe' and 'python3.exe' so the real install takes priority on PATH."
                    "  3. Confirm it works: 'python --version' should print a real 'Python 3.x.y' line with no"
                    "     install-manager/Store text."
                ) -join "`n")
            }
            Fail (@(
                "No working Python found - 'python'/'python3'/'py' either aren't on PATH, or only resolve to a"
                "non-functional stub (Windows Store 'App Execution Alias', or an unconfigured launcher)."
                "Install real Python from https://python.org/downloads (check 'Add python.exe to PATH'), confirm"
                "with 'python --version' in a plain terminal, then re-run this script with -InstallCli."
            ) -join "`n")
        }

        $uvOk = $false
        $null = Invoke-CaptureOutput { & uv --version }
        if ($LASTEXITCODE -eq 0) { $uvOk = $true }

        # Try the normal install first; only fall back to --trusted-host (which
        # disables TLS cert validation for these two hosts specifically) if the
        # first attempt fails - some corporate TLS proxies break uv/pip's
        # default cert validation, but that should never be assumed by default.
        #
        # IMPORTANT: these calls are deliberately NOT redirected with '2>&1' -
        # a pip/uv build failure is expected to write real diagnostic detail to
        # stderr, and merging it into the pipeline here would wrap every line
        # as a terminating ErrorRecord (independent of $PSNativeCommandUseErrorActionPreference
        # above - see Invoke-CaptureOutput's comment) and abort the script
        # after showing only the first line. Leaving stderr unredirected lets
        # it print straight to the console in full, live, and $LASTEXITCODE
        # alone (already checked below) is all this script needs for success/
        # failure - it was never relying on captured text here.
        if ($uvOk) {
            Write-Host "  Using uv tool install..." -ForegroundColor DarkGray
            & uv tool install --force "notebooklm-mcp-cli==$Version"
            $installOk = ($LASTEXITCODE -eq 0)
            if (-not $installOk) {
                Write-Warning "uv install failed - retrying with --system-certs (corporate TLS proxy workaround)..."
                & uv tool install --force --system-certs "notebooklm-mcp-cli==$Version"
                $installOk = ($LASTEXITCODE -eq 0)
            }
        } else {
            Write-Host "  'uv' not found - using '$pythonCmd -m pip'..." -ForegroundColor DarkGray
            & $pythonCmd -m pip install --upgrade "notebooklm-mcp-cli==$Version"
            $installOk = ($LASTEXITCODE -eq 0)
            if (-not $installOk) {
                Write-Warning "pip install failed - retrying with --trusted-host (corporate TLS proxy workaround)..."
                & $pythonCmd -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --upgrade "notebooklm-mcp-cli==$Version"
                $installOk = ($LASTEXITCODE -eq 0)
            }
        }

        if (-not $installOk) {
            Fail "notebooklm-mcp-cli install failed via both the normal and TLS-workaround paths. Install it manually: $pythonCmd -m pip install notebooklm-mcp-cli==$Version"
        }

        # PATH may need a refresh in this same process after a fresh install
        # (pip/uv-installed console scripts land in a Scripts dir that was not
        # on PATH when this script started).
        if (-not (Get-Command nlm -ErrorAction SilentlyContinue)) {
            $candidateDirs = @(
                (Join-Path $env:APPDATA "Python\Scripts"),
                (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\Scripts"),
                (Join-Path $env:USERPROFILE ".local\bin")
            )
            foreach ($d in $candidateDirs) {
                if ((Test-Path $d) -and ($env:PATH -notlike "*$d*")) { $env:PATH = "$d;$env:PATH" }
            }
        }
        if (-not (Get-Command nlm -ErrorAction SilentlyContinue)) {
            Fail "notebooklm-mcp-cli install reported success, but 'nlm' still isn't on PATH in this session. Open a new terminal (so PATH picks up the newly installed Scripts folder) and re-run this script."
        }
        Write-Host "==> notebooklm-mcp-cli $Version installed." -ForegroundColor Green
    }

    $nlmCmd = Resolve-NlmCommand
    if ($nlmCmd.Mode -eq "Portable") {
        Write-Host "==> Using portable notebooklm-mcp-cli bundle at $($nlmCmd.Exe) - no system Python/pip/uv needed." -ForegroundColor Green
    } else {
        $systemNlm = Get-Command nlm -ErrorAction SilentlyContinue
        if (-not $systemNlm) {
            if ($InstallCli) {
                Write-Host "==> 'nlm' not found - installing notebooklm-mcp-cli==$CliVersion (-InstallCli was set)..." -ForegroundColor Cyan
                Install-NlmCli -Version $CliVersion
            } else {
                Fail (@(
                    "No portable bundle found (mcp-servers/notebooklm-portable/), and 'nlm' CLI not found on PATH either."
                    "Fastest fix: get mcp-servers/notebooklm-portable/ next to this script (see"
                    "scripts/build-nbk-portable-bundle.ps1 / scripts/package-nbk-standalone.ps1) - needs no install at all."
                    "Otherwise install with: python -m pip install notebooklm-mcp-cli==$CliVersion (no uv required) - or"
                    "re-run this script with -InstallCli to have it do that automatically."
                    "If this machine has no GUI/browser (headless server, bare SSH, bastion), 'nlm login' can never"
                    "work here at all regardless of install method - run this script WITHOUT -SkipLogin on a machine"
                    "that has a real browser, then either hand off the -Mode Export YAML, or copy this script plus"
                    "the resulting %USERPROFILE%\.notebooklm-mcp-cli folder to the target machine and re-run here"
                    "with -SkipLogin."
                ) -join "`n")
            }
        } else {
            $verOutput = Invoke-CaptureOutput { & nlm --version }
            if ($verOutput -notmatch [regex]::Escape($CliVersion)) {
                if ($InstallCli) {
                    Write-Host "==> 'nlm' found but not at $CliVersion - upgrading (-InstallCli was set)..." -ForegroundColor Cyan
                    Install-NlmCli -Version $CliVersion
                } else {
                    Write-Warning (@(
                        "'nlm' is installed but does not report version $CliVersion ($($verOutput.Trim())). Versions before"
                        "0.15.5 had a login flow that could silently skip real interactive auth while still reporting"
                        "'valid' from a shallow check - continuing anyway, but if the real backend check below fails,"
                        "re-run with -InstallCli to upgrade, or manually: python -m pip install --upgrade notebooklm-mcp-cli==$CliVersion"
                    ) -join " ")
                }
            }
        }
    }

    Write-Host "==> Running 'nlm login' to mint a fresh Google session (browser will open)..." -ForegroundColor Cyan
    Invoke-Nlm $nlmCmd login
    if ($LASTEXITCODE -ne 0) { Fail "nlm login failed (exit $LASTEXITCODE)" }

    Write-Host "==> Verifying with 'nlm login --check' (shallow check only)..." -ForegroundColor Cyan
    Invoke-Nlm $nlmCmd login --check
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "nlm login --check reported a problem. This check is shallow - continuing, but verify with a real query after deploying."
    }

    Write-Host "==> Confirming with a REAL backend call ('nlm list notebooks')..." -ForegroundColor Cyan
    $realCheck = Invoke-Nlm $nlmCmd -Capture list notebooks
    if ($LASTEXITCODE -ne 0 -or $realCheck -match "UNAUTHENTICATED|Authentication expired|[Ee]rror") {
        Write-Warning "'nlm login --check' passed but a real notebook list call failed - this session may be stale despite the shallow check. Run 'nlm login --clear' and try again before trusting the files below."
    } else {
        Write-Host "==> Real backend call succeeded - session is genuinely valid, not just shallow-valid." -ForegroundColor Green
    }
}
else {
    Write-Host "==> -SkipLogin set: using existing local credentials as-is" -ForegroundColor Yellow
}

# 2. Locate credential files --------------------------------------------------
$authJson = Join-Path $ProfileDir "auth.json"
$profilePath = Join-Path $ProfileDir "profiles\$ProfileName"
$cookiesJson = Join-Path $profilePath "cookies.json"
$metadataJson = Join-Path $profilePath "metadata.json"

# Storage-mode guard: notebooklm-mcp-cli >= ~0.12 supports an OS-keystore
# "protected" mode (see 'nlm auth storage --help') where the real secrets are
# encrypted to this specific Windows user/machine via Credential Manager -
# those can NEVER be exported as portable files for a remote cluster, and
# trying to read cookies.json for such a profile fails in a way that looks
# identical to "never logged in". Check the per-profile marker directly (no
# 'nlm' CLI call needed, so this still works under -SkipLogin on a machine
# without nlm installed).
$storageModeMarker = Join-Path $profilePath "storage-mode.json"
if (Test-Path $storageModeMarker) {
    try {
        $modeInfo = Get-Content $storageModeMarker -Raw | ConvertFrom-Json
        if ($modeInfo.mode -eq "protected") {
            Fail (@(
                "Profile '$ProfileName' is using OS-keystore 'protected' credential storage, which encrypts"
                "secrets to THIS Windows user/machine via Credential Manager - it cannot be exported as"
                "portable files for a remote cluster at all, no matter what this script does."
                "Switch it to portable file-mode storage first: nlm auth storage set file --profile $ProfileName"
                "(check current mode anytime with: nlm auth storage status --profile $ProfileName)"
                "then re-run this script."
            ) -join "`n")
        }
    } catch {
        Write-Warning "Could not parse $storageModeMarker - assuming file-mode storage and continuing."
    }
}

# notebooklm-mcp-cli only mirrors a root-level auth.json when its INTERNAL
# "configured default profile" setting happens to match a hardcoded
# baseline at the moment credentials are saved (see core/auth.py
# save_tokens_to_cache: "Writes root mirror only for file-mode configured
# default profile") - confirmed in practice this mirror can be silently
# skipped on a brand-new machine's very first 'nlm login', even though the
# profile itself (cookies.json + metadata.json) saved correctly and a REAL
# backend call against it succeeds. auth.json's schema is just
# {cookies, csrf_token, session_id, build_label, base_host, email} where
# "cookies" is byte-for-byte identical to the profile's own cookies.json
# and the rest come straight from metadata.json - confirmed by diffing a
# known-good auth.json against its own profile's files field-for-field. If
# the mirror is missing but the real profile files are present, synthesize
# it ourselves rather than fail on a CLI-internal quirk that doesn't
# reflect whether the login actually worked.
if ((-not (Test-Path $authJson)) -and (Test-Path $cookiesJson) -and (Test-Path $metadataJson)) {
    Write-Host "==> auth.json missing but profile '$ProfileName' has valid cookies.json/metadata.json - synthesizing auth.json from them (notebooklm-mcp-cli skipped its own root mirror, a known first-login quirk)..." -ForegroundColor Yellow
    try {
        # NOTE: deliberately NOT round-tripping cookies.json through
        # ConvertFrom-Json -> ConvertTo-Json. Windows PowerShell 5.1's
        # ConvertTo-Json has a known bug where a System.Object[] nested as a
        # value inside an [ordered]/Hashtable gets mis-serialized as
        # {"value": [...], "Count": N} instead of a plain JSON array
        # (confirmed by reproducing it directly). Splicing the original
        # cookies.json bytes straight into the output guarantees the
        # cookies array is byte-identical to the real profile data, and
        # ConvertTo-Json is only used on individual scalar strings below,
        # which is always safe.
        $cookiesRaw = (Get-Content $cookiesJson -Raw).Trim()
        if (-not $cookiesRaw.StartsWith("[")) {
            throw "cookies.json does not contain a JSON array as expected (got: $($cookiesRaw.Substring(0, [Math]::Min(40, $cookiesRaw.Length)))...)"
        }
        $metaData = Get-Content $metadataJson -Raw | ConvertFrom-Json
        $csrfToken = if ($metaData.csrf_token) { $metaData.csrf_token } else { "" }
        $sessionId = if ($metaData.session_id) { $metaData.session_id } else { "" }
        $buildLabel = if ($metaData.build_label) { $metaData.build_label } else { "" }
        $baseHost = if ($metaData.base_host) { $metaData.base_host } else { "" }
        $email = if ($metaData.email) { $metaData.email } else { "" }

        $authJsonText = "{`n" +
            "  `"cookies`": $cookiesRaw,`n" +
            "  `"csrf_token`": $($csrfToken | ConvertTo-Json -Compress),`n" +
            "  `"session_id`": $($sessionId | ConvertTo-Json -Compress),`n" +
            "  `"build_label`": $($buildLabel | ConvertTo-Json -Compress),`n" +
            "  `"base_host`": $($baseHost | ConvertTo-Json -Compress),`n" +
            "  `"email`": $($email | ConvertTo-Json -Compress)`n" +
            "}`n"

        # Validate what we built actually parses and round-trips the
        # cookie count before trusting it, rather than assuming.
        $validate = $authJsonText | ConvertFrom-Json
        if ($validate.cookies.Count -ne ($cookiesRaw | ConvertFrom-Json).Count) {
            throw "synthesized auth.json cookie count does not match cookies.json - aborting synthesis"
        }

        Set-Content -Path $authJson -Value $authJsonText -Encoding utf8 -NoNewline
        Write-Host "==> Synthesized auth.json successfully ($($validate.cookies.Count) cookies carried over verbatim)." -ForegroundColor Green
    } catch {
        Write-Warning "Could not synthesize auth.json from cookies.json/metadata.json: $($_.Exception.Message)"
    }
}

$missing = @(@($authJson, $cookiesJson, $metadataJson) | Where-Object { -not (Test-Path $_) })
if ($missing.Count -gt 0) {
    $diag = [System.Collections.Generic.List[string]]::new()
    $diag.Add("Expected credential file(s) not found:")
    foreach ($m in $missing) { $diag.Add("  - $m") }
    $diag.Add("")
    $diag.Add("Profile directory checked: $ProfileDir  (profile name: $ProfileName)")
    if (-not (Test-Path $ProfileDir)) {
        $diag.Add("That directory does not exist at all on this machine - 'nlm login' has never completed successfully here.")
    } elseif (Test-Path (Join-Path $ProfileDir "profiles")) {
        $existingProfiles = @(Get-ChildItem (Join-Path $ProfileDir "profiles") -Directory -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
        if ($existingProfiles.Count -gt 0) {
            $diag.Add("Profile(s) that DO exist here: $($existingProfiles -join ', ')")
            if ($existingProfiles -notcontains $ProfileName) {
                $diag.Add("'$ProfileName' is not one of them - pass -ProfileName <name> to match one above, or check which profile 'nlm login' actually created with: nlm login profile list")
            }
        } else {
            $diag.Add("No profiles exist under '$ProfileDir\profiles' at all.")
        }
    }
    if ($env:NOTEBOOKLM_MCP_CLI_PATH) {
        $diag.Add("NOTE: the NOTEBOOKLM_MCP_CLI_PATH environment variable is set to '$env:NOTEBOOKLM_MCP_CLI_PATH' and was used above - unset it to use the default location instead, or confirm this is where you expect credentials to live.")
    }
    $diag.Add("")
    $diag.Add("Checklist:")
    $diag.Add("  1. Is 'nlm' actually installed? Check: nlm --version  (install: python -m pip install notebooklm-mcp-cli==0.15.5, or re-run this script with -InstallCli)")
    $diag.Add("  2. Did 'nlm login' complete a REAL interactive browser sign-in? Re-run this script WITHOUT -SkipLogin.")
    $diag.Add("  3. Confirm with: nlm login profile list")
    $diag.Add("  4. If this machine has no GUI/browser (headless server, bare SSH, bastion), 'nlm login' can never work here - log in on a machine with a real browser instead, then copy the credentials over or use -Mode Export there and hand off the YAML.")
    Fail ($diag -join "`n")
}

try {
    $lastValidated = (Get-Content $metadataJson -Raw | ConvertFrom-Json).last_validated
    Write-Host "==> Credentials found. metadata.last_validated = $lastValidated" -ForegroundColor Green
}
catch {
    Write-Host "==> Credentials found (could not parse metadata.json for a timestamp, continuing anyway)" -ForegroundColor Yellow
}

# 3. Render Secret YAML natively in PowerShell (both modes) -------------------
# Deliberately does NOT shell out to 'oc create secret --dry-run' here: Export
# mode exists specifically for machines that have no cluster access, and may
# not even have the 'oc'/'kubectl' binary installed at all (as opposed to just
# lacking network access to the target cluster). Base64-encoding the 3 files
# ourselves keeps Export mode fully self-contained.
function Build-SecretYaml {
    param(
        [string]$Namespace,
        [string]$SecretName,
        [string]$AuthJsonPath,
        [string]$CookiesJsonPath,
        [string]$MetadataJsonPath
    )
    $authB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($AuthJsonPath))
    $cookiesB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($CookiesJsonPath))
    $metadataB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($MetadataJsonPath))
    return @"
apiVersion: v1
kind: Secret
metadata:
  name: $SecretName
  namespace: $Namespace
type: Opaque
data:
  auth.json: $authB64
  cookies.json: $cookiesB64
  metadata.json: $metadataB64
"@
}

Write-Host "==> Rendering Secret manifest for $Namespace/$SecretName (no cluster/CLI access required for this step)..." -ForegroundColor Cyan
$secretYaml = Build-SecretYaml -Namespace $Namespace -SecretName $SecretName `
    -AuthJsonPath $authJson -CookiesJsonPath $cookiesJson -MetadataJsonPath $metadataJson

if ($Mode -eq "Export") {
    $outputDir = Split-Path -Parent $OutputPath
    if ($outputDir -and -not (Test-Path $outputDir)) {
        New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
    }
    $secretYaml | Out-File -FilePath $OutputPath -Encoding ascii
    Write-Host "==> Wrote Secret manifest to $OutputPath" -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps (out-of-band delivery to the target environment):" -ForegroundColor Green
    Write-Host "  1. Hand this file off via your target environment's normal path"
    Write-Host "     (GitOps commit, 'oc apply -f' from a jump host with cluster access, sealed-secrets seal + commit, etc.)"
    Write-Host "  2. Once applied, restart the deployment there, e.g.:"
    Write-Host "     oc -n $Namespace rollout restart deploy/$DeploymentName"
    Write-Host "  3. Verify with a REAL notebook_query/notebook_list call - not just a health check."
    exit 0
}

# 4. Apply mode: push directly + restart --------------------------------------
if (-not (Get-Command oc -ErrorAction SilentlyContinue)) {
    Fail "'oc' was not found on PATH, so -Mode Apply can't push to a cluster from this machine.`nUse -Mode Export instead to write a Secret YAML file you can hand off (GitOps, sealed-secrets, a jump host with 'oc' installed, etc.)."
}

$ocArgs = @()
if ($KubeContext) { $ocArgs += @("--context", $KubeContext) }

Write-Host "==> Applying Secret to $Namespace/$SecretName (this machine needs API access to the target cluster)..." -ForegroundColor Cyan
$applyArgs = @("-n", $Namespace, "apply", "-f", "-") + $ocArgs
$secretYaml | & oc @applyArgs
if ($LASTEXITCODE -ne 0) { Fail "Failed to apply Secret. Check cluster connectivity, the active context/namespace, and RBAC permissions." }

if (-not $NoRestart) {
    Write-Host "==> Restarting deployment/$DeploymentName..." -ForegroundColor Cyan
    $restartArgs = @("-n", $Namespace, "rollout", "restart", "deploy/$DeploymentName") + $ocArgs
    & oc @restartArgs
    if ($LASTEXITCODE -ne 0) { Fail "Secret was applied, but rollout restart failed. Restart manually: oc -n $Namespace rollout restart deploy/$DeploymentName" }

    $statusArgs = @("-n", $Namespace, "rollout", "status", "deploy/$DeploymentName", "--timeout=120s") + $ocArgs
    & oc @statusArgs
}
else {
    Write-Host "==> -NoRestart set: Secret updated but deployment not restarted. It will not pick up new credentials until restarted." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==> Done. Credentials refreshed for $Namespace/$SecretName." -ForegroundColor Green
Write-Host "    IMPORTANT: verify with a real notebook_query/notebook_list call against the target environment." -ForegroundColor Yellow
Write-Host "    'nlm login --check' alone is a shallow probe and can report success on a dead session." -ForegroundColor Yellow
