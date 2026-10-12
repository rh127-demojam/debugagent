<#
.SYNOPSIS
  Refresh Slack credentials for the mcp-slack-server deployment in ANY target
  OpenShift/Kubernetes environment - the same portable Apply/Export pattern
  as refresh-nbk-creds.ps1.

.DESCRIPTION
  mcp-slack-server never performs an interactive login itself - it just reads
  SLACK_XOXC_TOKEN / SLACK_XOXD_TOKEN from its own environment (a Secret) at
  startup. Those values are always produced elsewhere, by a human extracting
  them from an authenticated browser session on any machine - that machine
  does NOT need to be anywhere near the cluster the pod runs in.

  STANDALONE: this single file has no dependency on any other script in this
  repo. The CDP-based browser token extraction (originally a separate
  scripts/extract-slack-tokens.ps1) is embedded below as the
  Get-SlackTokensViaCdp function. Copy just this one .ps1 file to any Windows
  machine with PowerShell 5.1+ and a Chrome/Edge install and it works on its
  own - no git checkout, no sibling scripts/ folder required. (The original
  scripts/extract-slack-tokens.ps1 file still exists separately in this repo
  and is still used by setup.ps1's Cursor-side Slack MCP setup flow - this
  file is an independent, self-contained copy of that same logic, not a
  replacement for it.)

  This script automates the full refresh cycle end-to-end:

    1. (Optional) Launches a real browser via Chrome DevTools Protocol to
       pull a fresh xoxc/xoxd pair out of a real logged-in Slack session on
       THIS machine. -SkipExtract reuses whatever is already in a .env file
       next to this script (or one directory up, to match this repo's
       layout) instead (SLACK_MCP_XOXC_TOKEN / SLACK_MCP_XOXD_TOKEN) -
       useful when those were extracted moments ago for the Cursor-side
       Slack MCP and are still fresh. -ExtractOnly runs just the extraction
       step standalone (prints the tokens, does not touch any cluster) -
       the direct equivalent of running the old extract-slack-tokens.ps1 by
       itself.
    2. Renders a Kubernetes/OpenShift Secret manifest from those two values,
       then either:
         -Mode Apply  (default) - applies it directly to the target
                        namespace/context and restarts the deployment.
                        Requires `oc`/`kubectl` + network access FROM THIS
                        MACHINE to the target cluster's API server.
         -Mode Export - writes a ready-to-apply Secret YAML file to disk
                        instead, for hand-off via GitOps, sealed-secrets, a
                        jump host, or any other out-of-band delivery path
                        (this is also what you want when the only path to
                        the cluster is through a bastion host - scp the
                        output file up, then `oc apply -f` it there).

  SECURITY: this script never prints the extracted token values to the
  console in the default (Apply/Export) flow - every stream from the
  embedded extraction function is captured, not displayed, and the function
  itself is called with -Silent so it withholds its own raw token print
  lines too. The one exception is -ExtractOnly, which is explicitly a
  diagnostic/standalone mode and prints the tokens on purpose, exactly like
  the original extract-slack-tokens.ps1 did when run directly.

.PARAMETER Mode
  "Apply" (default, push directly to the target cluster) or "Export" (write
  a Secret YAML file for out-of-band delivery). Ignored if -ExtractOnly.

.PARAMETER Namespace
  Target namespace holding mcp-slack-server. Default: debug-agent-mcp

.PARAMETER SecretName
  Target Secret name. Default: mcp-slack-creds

.PARAMETER DeploymentName
  Deployment to roll-restart after an Apply. Default: mcp-slack-server

.PARAMETER KubeContext
  Optional oc/kubectl context name, if you have multiple clusters configured
  locally and need to target a specific one.

.PARAMETER WorkspaceUrl
  Slack workspace URL to navigate to for extraction. Default:
  https://app.slack.com

.PARAMETER ExtractTimeout
  Max seconds to wait for the browser login during extraction. Default: 300

.PARAMETER Browser
  Browser to use for extraction: auto, chrome, edge. Default: auto

.PARAMETER Port
  CDP remote-debugging port to launch the browser on. Default: 9334 (will
  probe up to 9 ports above this if it's busy).

.PARAMETER Incognito
  Launch the extraction browser in InPrivate (Edge) or Incognito (Chrome).
  Enterprise SSO (e.g. Red Hat) often fails in private windows; use only
  when a clean session is required. Ignored if -ReuseSession/-ProfileDir is
  set (durable profiles take precedence).

.PARAMETER ReuseSession
  Keep a durable browser profile under %LOCALAPPDATA% so the next
  extraction can skip SSO. Default is off: each run uses a throwaway,
  auto-deleted profile.

.PARAMETER ProfileDir
  Explicit directory for the CDP user-data-dir. Implies session reuse
  (the profile is kept, not deleted, after extraction) when set.

.PARAMETER OutputPath
  Export-mode output file path. Default: .\scripts\out\slack-mcp-creds-secret.yaml

.PARAMETER SkipExtract
  Skip the browser-based extraction and reuse SLACK_MCP_XOXC_TOKEN /
  SLACK_MCP_XOXD_TOKEN already present in a .env file next to this script
  (or its parent directory, to match this repo's scripts/ layout).

.PARAMETER NoRestart
  In Apply mode, update the Secret but skip the deployment rollout restart
  (useful if you want to batch multiple changes before restarting).

.PARAMETER ExtractOnly
  Run just the browser-based token extraction and print the result - no
  Secret is rendered, no cluster is touched. Direct standalone equivalent
  of running extract-slack-tokens.ps1 by itself. Tokens ARE printed to the
  console in this mode (by design, for interactive/manual use).

.EXAMPLE
  # Fresh browser extraction, push straight to a reachable cluster
  .\refresh-slack-creds.ps1 -Mode Apply -KubeContext new-env

.EXAMPLE
  # Reuse tokens already in .env, produce a YAML bundle for hand-off (e.g.
  # scp to a bastion host that has the only path to the cluster)
  .\refresh-slack-creds.ps1 -Mode Export -SkipExtract -OutputPath .\scripts\out\slack-mcp-creds-secret.yaml

.EXAMPLE
  # Just extract and print tokens - no cluster interaction at all
  .\refresh-slack-creds.ps1 -ExtractOnly
#>

param(
    [ValidateSet("Apply", "Export")]
    [string]$Mode = "Apply",

    [string]$Namespace = "debug-agent-mcp",
    [string]$SecretName = "mcp-slack-creds",
    [string]$DeploymentName = "mcp-slack-server",
    [string]$KubeContext = "",

    [string]$WorkspaceUrl = "https://app.slack.com",
    [int]$ExtractTimeout = 300,
    [string]$Browser = "auto",
    [int]$Port = 9334,
    [switch]$Incognito,
    [switch]$ReuseSession,
    [string]$ProfileDir = "",

    [string]$OutputPath = ".\scripts\out\slack-mcp-creds-secret.yaml",

    [switch]$SkipExtract,
    [switch]$NoRestart,
    [switch]$ExtractOnly
)

$ErrorActionPreference = "Stop"

function Fail($msg) {
    Write-Error $msg
    exit 1
}

# =============================================================================
# Embedded Slack token extraction (originally scripts/extract-slack-tokens.ps1)
# =============================================================================
# Launches Chrome/Edge with remote debugging, navigates to Slack, waits for
# the user to be logged in, then extracts the xoxd cookie and xoxc token.
# Mirrors the approach used by notebooklm-mcp-cli's `nlm login`.

function Write-Status($symbol, $color, $msg) {
    Write-Host "  [$symbol] " -ForegroundColor $color -NoNewline
    Write-Host $msg
}

function Find-BrowserPath {
    param([string]$Preferred = "auto")

    $pf = "C:\Program Files"
    $pf86 = "C:\Program Files (x86)"
    $local = Join-Path $env:USERPROFILE "AppData\Local"

    # Edge paths
    if ($Preferred -eq "auto" -or $Preferred -eq "edge") {
        $p = Join-Path $pf86 "Microsoft\Edge\Application\msedge.exe"
        if (Test-Path $p) { return $p }
        $p = Join-Path $pf "Microsoft\Edge\Application\msedge.exe"
        if (Test-Path $p) { return $p }
        $p = Join-Path $local "Microsoft\Edge\Application\msedge.exe"
        if (Test-Path $p) { return $p }
    }

    # Chrome paths
    if ($Preferred -eq "auto" -or $Preferred -eq "chrome") {
        $p = Join-Path $pf "Google\Chrome\Application\chrome.exe"
        if (Test-Path $p) { return $p }
        $p = Join-Path $pf86 "Google\Chrome\Application\chrome.exe"
        if (Test-Path $p) { return $p }
        $p = Join-Path $local "Google\Chrome\Application\chrome.exe"
        if (Test-Path $p) { return $p }
    }

    return $null
}

function Find-AvailablePort {
    param([int]$StartFrom = 9334, [int]$MaxAttempts = 10)

    for ($i = 0; $i -lt $MaxAttempts; $i++) {
        $testPort = $StartFrom + $i
        try {
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $testPort)
            $listener.Start()
            $listener.Stop()
            return $testPort
        } catch {
            continue
        }
    }
    return $null
}

function Clear-ExtractProfile {
    param([string]$Dir)
    if ($script:keepProfile) { return }
    if ($Dir -and (Test-Path $Dir)) {
        Remove-Item -Path $Dir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Send-CdpCommand {
    param(
        [System.Net.WebSockets.ClientWebSocket]$Ws,
        [string]$Method,
        [hashtable]$Params = @{},
        [int]$TimeoutMs = 10000
    )

    $id = [System.Threading.Interlocked]::Increment([ref]$script:cdpCommandId)
    $msg = @{ id = $id; method = $Method; params = $Params } | ConvertTo-Json -Depth 10 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($msg)
    $segment = [System.ArraySegment[byte]]::new($bytes)

    $cts = [System.Threading.CancellationTokenSource]::new($TimeoutMs)
    $Ws.SendAsync($segment, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $cts.Token).GetAwaiter().GetResult() | Out-Null

    # Read response
    $buffer = [byte[]]::new(1048576)  # 1MB buffer
    $result = [System.Text.StringBuilder]::new()
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)

    while ([DateTime]::UtcNow -lt $deadline) {
        $cts2 = [System.Threading.CancellationTokenSource]::new([Math]::Max(1000, ($deadline - [DateTime]::UtcNow).TotalMilliseconds))
        try {
            $seg = [System.ArraySegment[byte]]::new($buffer)
            $recv = $Ws.ReceiveAsync($seg, $cts2.Token).GetAwaiter().GetResult()
            $chunk = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $recv.Count)
            $result.Append($chunk) | Out-Null

            if ($recv.EndOfMessage) {
                $json = $result.ToString()
                $parsed = $json | ConvertFrom-Json
                if ($parsed.id -eq $id) {
                    return $parsed.result
                }
                # Not our response (could be an event), keep reading
                $result.Clear() | Out-Null
            }
        } catch [System.OperationCanceledException] {
            break
        } finally {
            $cts2.Dispose()
        }
    }
    return $null
}

function Get-XoxcCandidatesFromWs {
    param([System.Net.WebSockets.ClientWebSocket]$Ws)

    $named = @(
        @{ src = "boot_data"; expr = '(() => { try { return window.boot_data?.api_token || ""; } catch(e) { return ""; } })()' }
        @{ src = "TS.boot_data"; expr = '(() => { try { return window.TS?.boot_data?.api_token || ""; } catch(e) { return ""; } })()' }
        @{ src = "slackDebug"; expr = '(() => { try { return window.slackDebug?.api_token || ""; } catch(e) { return ""; } })()' }
        @{ src = "localConfig_v2_team"; expr = '(() => { try { var m = (location.pathname || "").match(/\/client\/([A-Z0-9]+)/); var teamId = m ? m[1] : ""; var cfg = JSON.parse(localStorage.getItem("localConfig_v2") || "{}"); var team = (cfg.teams && teamId) ? cfg.teams[teamId] : null; var t = (team && (team.token || team.api_token)) || ""; return (typeof t === "string" && t.indexOf("xoxc-") === 0) ? t : ""; } catch(e) { return ""; } })()' }
        @{ src = "localConfig_v2"; expr = '(() => { try { var raw = localStorage.getItem("localConfig_v2") || ""; var m = raw.match(/"api_token":"(xoxc-[^"]+)"/); return m ? m[1] : ""; } catch(e) { return ""; } })()' }
        @{ src = "fetch_sniffer"; expr = '(() => { try { return window.__slackXoxcCapture || ""; } catch(e) { return ""; } })()' }
        @{ src = "html_api_token"; expr = '(() => { try { var html = document.documentElement ? document.documentElement.innerHTML : ""; var m = html.match(/"api_token":"(xoxc-[^"]+)"/); return m ? m[1] : ""; } catch(e) { return ""; } })()' }
        @{ src = "html_token"; expr = '(() => { try { var html = document.documentElement ? document.documentElement.innerHTML : ""; var m = html.match(/"token":"(xoxc-[^"]+)"/); return m ? m[1] : ""; } catch(e) { return ""; } })()' }
        @{ src = "script_api_token"; expr = '(() => { try { var texts = Array.prototype.map.call(document.scripts, function(s) { return s.textContent || ""; }).join("\n"); var m = texts.match(/"api_token":"(xoxc-[^"]+)"/); return m ? m[1] : ""; } catch(e) { return ""; } })()' }
    )

    $found = @()
    $seen = @{}
    foreach ($item in $named) {
        $evalResult = Send-CdpCommand -Ws $Ws -Method "Runtime.evaluate" -Params @{
            expression = $item.expr
            returnByValue = $true
        }
        $val = $null
        if ($evalResult -and $evalResult.result) { $val = $evalResult.result.value }
        if ($val -match "^xoxc-" -and -not $seen.ContainsKey($val)) {
            $seen[$val] = $true
            $found += @{ src = $item.src; value = $val }
        }
    }

    $lsResult = Send-CdpCommand -Ws $Ws -Method "Runtime.evaluate" -Params @{
        expression = '(() => { try { var out = []; for (var i = 0; i < localStorage.length; i++) { var v = localStorage.getItem(localStorage.key(i)) || ""; var re = /xoxc-[a-zA-Z0-9-]+/g; var m; while ((m = re.exec(v)) !== null) { out.push(m[0]); } } return out.join("\n"); } catch(e) { return ""; } })()'
        returnByValue = $true
    }
    if ($lsResult -and $lsResult.result -and $lsResult.result.value) {
        foreach ($line in ($lsResult.result.value -split "`n")) {
            if ($line -match "^xoxc-" -and -not $seen.ContainsKey($line.Trim())) {
                $seen[$line.Trim()] = $true
                $found += @{ src = "localStorage"; value = $line.Trim() }
            }
        }
    }

    $teamsResult = Send-CdpCommand -Ws $Ws -Method "Runtime.evaluate" -Params @{
        expression = '(() => { try { var cfg = JSON.parse(localStorage.getItem("localConfig_v2") || "{}"); var out = []; var teams = (cfg && cfg.teams) ? cfg.teams : {}; for (var tid in teams) { if (!Object.prototype.hasOwnProperty.call(teams, tid)) continue; var t = teams[tid]; var tok = (t && (t.token || t.api_token)) || ""; if (typeof tok === "string" && tok.indexOf("xoxc-") === 0) out.push(tok); } return out.join("\n"); } catch(e) { return ""; } })()'
        returnByValue = $true
    }
    if ($teamsResult -and $teamsResult.result -and $teamsResult.result.value) {
        foreach ($line in ($teamsResult.result.value -split "`n")) {
            if ($line -match "^xoxc-" -and -not $seen.ContainsKey($line.Trim())) {
                $seen[$line.Trim()] = $true
                $found += @{ src = "localConfig_v2_teams"; value = $line.Trim() }
            }
        }
    }
    return $found
}

function Get-SlackTokensViaCdp {
    <#
    .SYNOPSIS
      Full CDP-based Slack xoxc/xoxd extraction. Returns a PSCustomObject
      { Success, Xoxc, Xoxd, Validated, Error } instead of exiting the
      process, so it's safe to call in-process from the main flow below.
    .PARAMETER Silent
      Suppress the raw "SLACK_XOXC=.../SLACK_XOXD=..." print lines (the
      caller still gets the values via the returned object). Used when this
      function is called as part of the Apply/Export refresh flow, where
      the caller also captures *>&1 so none of this function's other
      diagnostic chatter reaches the console either. Omit -Silent to get
      the original extract-slack-tokens.ps1 standalone/interactive UX
      (used by -ExtractOnly below).
    #>
    param(
        [string]$WorkspaceUrl = "https://app.slack.com",
        [int]$Timeout = 300,
        [int]$Port = 9334,
        [string]$Browser = "auto",
        [switch]$Incognito,
        [switch]$ReuseSession,
        [string]$ProfileDir = "",
        [switch]$Silent
    )

    Write-Host ""
    Write-Host "  Slack Token Extraction (via CDP)" -ForegroundColor Cyan
    Write-Host "  =================================" -ForegroundColor DarkCyan
    Write-Host ""

    $script:cdpCommandId = 0
    $script:keepProfile = $false
    $script:authValidated = $false
    $script:lastAuthError = $null

    # Step 1: Find browser
    $browserPath = Find-BrowserPath -Preferred $Browser
    if (-not $browserPath) {
        Write-Status "X" Red "No supported browser found (Chrome or Edge required)"
        return [PSCustomObject]@{ Success = $false; Xoxc = $null; Xoxd = $null; Validated = $false; Error = "no browser found" }
    }
    $browserName = if ($browserPath -match "msedge") { "Microsoft Edge" } else { "Google Chrome" }
    Write-Status "OK" Green "Found $browserName at $browserPath"

    # Step 2: Find available port
    $cdpPort = Find-AvailablePort -StartFrom $Port
    if (-not $cdpPort) {
        Write-Status "X" Red "No available ports in range $Port-$($Port+9)"
        return [PSCustomObject]@{ Success = $false; Xoxc = $null; Xoxd = $null; Validated = $false; Error = "no available CDP port" }
    }

    # Step 3: Profile dir - throwaway by default. Durable only if opted in.
    if ($ProfileDir) {
        $profileDir = $ProfileDir
        $script:keepProfile = $true
    } elseif ($ReuseSession) {
        $localApp = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE "AppData\Local" }
        $profileDir = Join-Path $localApp "debugagent-hub\slack-cdp-profile"
        $script:keepProfile = $true
    } else {
        $profileDir = Join-Path $env:TEMP "slack-cdp-profile-$([guid]::NewGuid().ToString('N').Substring(0,8))"
    }
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
    if ($script:keepProfile) {
        Write-Status "-" DarkGray "Reusing browser profile at $profileDir"
    }

    # Generic app.slack.com landing often hits marketing; /client/ still uses the same login.
    # Custom workspace URLs are left unchanged so SSO redirects stay as the caller specified.
    $navigateUrl = $WorkspaceUrl
    if ($WorkspaceUrl -match '^https://app\.slack\.com/?$') {
        $navigateUrl = "https://app.slack.com/client/"
    }

    # Step 4: Launch browser
    $incognitoFlag = $null
    if ($Incognito -and $script:keepProfile) {
        Write-Status "!" Yellow "Ignoring -Incognito because a reusable profile was requested"
    } elseif ($Incognito) {
        $incognitoFlag = if ($browserPath -match "msedge") { "--inprivate" } else { "--incognito" }
    }
    $modeLabel = if ($incognitoFlag) { " ($incognitoFlag)" } else { "" }
    Write-Status "-" DarkGray "Launching browser on port $cdpPort$modeLabel..."

    $browserArgs = @(
        "--remote-debugging-port=$cdpPort",
        "--user-data-dir=$profileDir",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps"
    )
    if ($incognitoFlag) { $browserArgs += $incognitoFlag }
    $browserArgs += $navigateUrl

    $browserProc = Start-Process -FilePath $browserPath -ArgumentList $browserArgs -PassThru

    # Wait for CDP to become available
    $cdpReady = $false
    $cdpBase = "http://127.0.0.1:$cdpPort"

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $pages = Invoke-RestMethod -Uri "$cdpBase/json" -TimeoutSec 2 -ErrorAction Stop
            if ($pages -and $pages.Count -gt 0) {
                $cdpReady = $true
                break
            }
        } catch {
            continue
        }
    }

    if (-not $cdpReady) {
        Write-Status "X" Red "Browser did not start CDP in time"
        if ($browserProc -and -not $browserProc.HasExited) { $browserProc.Kill() }
        Clear-ExtractProfile -Dir $profileDir
        return [PSCustomObject]@{ Success = $false; Xoxc = $null; Xoxd = $null; Validated = $false; Error = "CDP did not become ready" }
    }

    Write-Status "OK" Green "Browser ready with CDP on port $cdpPort"

    # Bring the extract window to the foreground so SSO is visible
    try {
        if (-not ("SlackExtractWin" -as [type])) {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class SlackExtractWin {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@ -ErrorAction SilentlyContinue
        }
        if ($browserProc.MainWindowHandle -ne [IntPtr]::Zero) {
            [SlackExtractWin]::ShowWindow($browserProc.MainWindowHandle, 9) | Out-Null
            [SlackExtractWin]::SetForegroundWindow($browserProc.MainWindowHandle) | Out-Null
        }
    } catch {}

    Write-Host ""
    Write-Host "  A browser window has opened to Slack$(if ($incognitoFlag) { ' (private/incognito)' } else { '' })." -ForegroundColor Yellow
    Write-Host "  Sign in, then click into any channel so the client fully loads." -ForegroundColor Yellow
    Write-Host "  Waiting up to $Timeout seconds for login..." -ForegroundColor DarkGray
    Write-Host ""

    # Step 5: Wait for Slack to be logged in
    $slackLoggedIn = $false
    $startTime = [DateTime]::UtcNow
    $lastMsg = 0

    function Get-SafePageSummary($pages) {
        $parts = @()
        foreach ($p in @($pages | Where-Object { $_.type -eq "page" })) {
            try {
                $u = [Uri]$p.url
                $parts += "$($u.Host)$($u.AbsolutePath)"
            } catch {
                $parts += "(unparseable)"
            }
        }
        return ($parts -join " | ")
    }

    while (([DateTime]::UtcNow - $startTime).TotalSeconds -lt $Timeout) {
        Start-Sleep -Seconds 2

        try {
            $pages = Invoke-RestMethod -Uri "$cdpBase/json" -TimeoutSec 5 -ErrorAction Stop
            $slackPage = $pages | Where-Object {
                $_.url -match "slack\.com" -and $_.url -notmatch "accounts\.google" -and $_.type -eq "page" -and
                $_.url -notmatch "get-started|/signin|workspace-signin|/pricing|/create|/download|gantry/auth"
            } | Select-Object -First 1

            if (-not $slackPage) { continue }

            # Real workspace UI is /client/<orgOrTeamId>/... (T=team, E=enterprise grid)
            # Do not treat the bare /client/ landing or sign-in redirects as logged-in.
            if ($slackPage.url -match "slack\.com/client/[TE][A-Z0-9]+") {
                $slackLoggedIn = $true
                break
            }

            $looksLikeWorkspace = $slackPage.url -match "\.slack\.com/" -and $slackPage.url -notmatch "/signin|workspace-signin"
            if ($looksLikeWorkspace -and $slackPage.webSocketDebuggerUrl) {
                $wsUrl = $slackPage.webSocketDebuggerUrl -replace "://localhost:", "://127.0.0.1:"
                $ws = [System.Net.WebSockets.ClientWebSocket]::new()
                $cts = [System.Threading.CancellationTokenSource]::new(5000)
                try {
                    $ws.ConnectAsync([Uri]$wsUrl, $cts.Token).GetAwaiter().GetResult() | Out-Null
                    $checkResult = Send-CdpCommand -Ws $ws -Method "Runtime.evaluate" -Params @{
                        expression = '(() => { try { var t = (window.boot_data && window.boot_data.api_token) || (window.TS && window.TS.boot_data && window.TS.boot_data.api_token) || ""; return typeof t === "string" && t.indexOf("xoxc-") === 0; } catch(e) { return false; } })()'
                        returnByValue = $true
                    }
                    try {
                        $ws.CloseOutputAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "", [System.Threading.CancellationToken]::None).GetAwaiter().GetResult() | Out-Null
                    } catch {}
                    try { $ws.Dispose() } catch {}
                    if ($checkResult -and $checkResult.result -and $checkResult.result.value -eq $true) {
                        $slackLoggedIn = $true
                        break
                    }
                } catch {
                    try { $ws.Dispose() } catch {}
                }
            }
        } catch {}

        $elapsed = [int]([DateTime]::UtcNow - $startTime).TotalSeconds
        if ($elapsed - $lastMsg -ge 30) {
            $lastMsg = $elapsed
            $summary = ""
            try { $summary = Get-SafePageSummary (Invoke-RestMethod -Uri "$cdpBase/json" -TimeoutSec 3) } catch {}
            Write-Host "  Still waiting for sign-in... ($elapsed`s elapsed)  pages: $summary" -ForegroundColor DarkGray
        }
    }

    if (-not $slackLoggedIn) {
        Write-Status "X" Red "Login timeout after $Timeout seconds"
        if ($browserProc -and -not $browserProc.HasExited) { $browserProc.Kill() }
        Clear-ExtractProfile -Dir $profileDir
        return [PSCustomObject]@{ Success = $false; Xoxc = $null; Xoxd = $null; Validated = $false; Error = "login timeout" }
    }

    Write-Status "OK" Green "Slack login detected"

    # Inject a fetch/XHR sniffer so we can capture the live xoxc the client sends
    try {
        $pagesNow = Invoke-RestMethod -Uri "$cdpBase/json" -TimeoutSec 5
        $clientPage = $pagesNow | Where-Object { $_.url -match "slack\.com/client" -and $_.webSocketDebuggerUrl } | Select-Object -First 1
        if ($clientPage) {
            $sniffWsUrl = $clientPage.webSocketDebuggerUrl -replace "://localhost:", "://127.0.0.1:"
            $sniffWs = [System.Net.WebSockets.ClientWebSocket]::new()
            $sniffCts = [System.Threading.CancellationTokenSource]::new(8000)
            $sniffWs.ConnectAsync([Uri]$sniffWsUrl, $sniffCts.Token).GetAwaiter().GetResult() | Out-Null
            Send-CdpCommand -Ws $sniffWs -Method "Runtime.evaluate" -Params @{
                expression = @'
(() => {
  if (window.__slackXoxcCaptureInstalled) return true;
  window.__slackXoxcCapture = "";
  window.__slackXoxcCaptureInstalled = true;
  const grab = (v) => {
    if (!v) return;
    const m = String(v).match(/xoxc-[a-zA-Z0-9-]+/);
    if (m) window.__slackXoxcCapture = m[0];
  };
  const origFetch = window.fetch;
  window.fetch = function(input, init) {
    try {
      if (init && init.headers) {
        const h = init.headers;
        if (typeof Headers !== "undefined" && h instanceof Headers) grab(h.get("Authorization"));
        else grab(h.Authorization || h.authorization);
      }
      if (input && typeof Request !== "undefined" && input instanceof Request) grab(input.headers.get("Authorization"));
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };
  const origSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function(k, v) {
    try { if (String(k).toLowerCase() === "authorization") grab(v); } catch (e) {}
    return origSet.apply(this, arguments);
  };
  return true;
})()
'@
                returnByValue = $true
            } | Out-Null
            try { $sniffWs.CloseOutputAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "", [System.Threading.CancellationToken]::None).GetAwaiter().GetResult() | Out-Null } catch {}
            try { $sniffWs.Dispose() } catch {}
            Write-Host "  Installed live xoxc sniffer; waiting for Slack API calls..." -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "  Could not install xoxc sniffer: $_" -ForegroundColor DarkGray
    }

    # Give Slack time for HttpOnly cookies + boot_data / API calls to settle
    Start-Sleep -Seconds 12

    # Step 6: Extract tokens via CDP
    Write-Host "  Extracting tokens..." -ForegroundColor DarkGray

    $xoxcToken = $null
    $xoxdToken = $null

    try {
        $pages = Invoke-RestMethod -Uri "$cdpBase/json" -TimeoutSec 5
        $slackPage = $pages | Where-Object {
            $_.url -match "slack\.com/client/[TE][A-Z0-9]+" -and $_.type -eq "page" -and $_.webSocketDebuggerUrl
        } | Select-Object -First 1
        if (-not $slackPage) {
            $slackPage = $pages | Where-Object {
                $_.url -match "slack\.com" -and $_.type -eq "page" -and $_.url -notmatch "accounts\.google"
            } | Select-Object -First 1
        }

        if (-not $slackPage) {
            Write-Status "X" Red "Could not find Slack page in browser tabs"
            if ($browserProc -and -not $browserProc.HasExited) { $browserProc.Kill() }
            Clear-ExtractProfile -Dir $profileDir
            return [PSCustomObject]@{ Success = $false; Xoxc = $null; Xoxd = $null; Validated = $false; Error = "slack page not found" }
        }

        $wsUrl = $slackPage.webSocketDebuggerUrl -replace "://localhost:", "://127.0.0.1:"

        $ws = [System.Net.WebSockets.ClientWebSocket]::new()
        $cts = [System.Threading.CancellationTokenSource]::new(10000)
        $ws.ConnectAsync([Uri]$wsUrl, $cts.Token).GetAwaiter().GetResult() | Out-Null

        Write-Host "  Page URL: $($slackPage.url)" -ForegroundColor DarkGray

        # Enable Network domain so cookie APIs see HttpOnly cookies.
        # Do not Page.reload here: that aborts this CDP WebSocket.
        Send-CdpCommand -Ws $ws -Method "Network.enable" | Out-Null

        $xoxcCandidates = @()
        $xoxcCandidates += @(Get-XoxcCandidatesFromWs -Ws $ws)

        $allTargets = Invoke-RestMethod -Uri "$cdpBase/json" -TimeoutSec 5
        $slackTargets = @($allTargets | Where-Object {
            $_.webSocketDebuggerUrl -and $_.url -match "slack\.com" -and $_.url -notmatch "accounts\.google"
        })
        Write-Host "  Scanning $($slackTargets.Count) Slack CDP target(s) for xoxc..." -ForegroundColor DarkGray
        foreach ($target in $slackTargets) {
            $targetWsUrl = $target.webSocketDebuggerUrl -replace "://localhost:", "://127.0.0.1:"
            if ($targetWsUrl -eq $wsUrl) { continue }
            $tws = $null
            try {
                $tws = [System.Net.WebSockets.ClientWebSocket]::new()
                $tcts = [System.Threading.CancellationTokenSource]::new(8000)
                $tws.ConnectAsync([Uri]$targetWsUrl, $tcts.Token).GetAwaiter().GetResult() | Out-Null
                $xoxcCandidates += @(Get-XoxcCandidatesFromWs -Ws $tws)
                try { $tws.CloseOutputAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "", [System.Threading.CancellationToken]::None).GetAwaiter().GetResult() | Out-Null } catch {}
            } catch {
            } finally {
                if ($tws) { try { $tws.Dispose() } catch {} }
            }
        }

        $workspaceHost = $null
        try { $workspaceHost = ([Uri]$WorkspaceUrl).Host } catch { }
        $pageHost = $null
        try { $pageHost = ([Uri]$slackPage.url).Host } catch { }
        $cookieUrls = @(
            "https://slack.com",
            "https://app.slack.com",
            "https://edgeapi.slack.com",
            $WorkspaceUrl,
            $navigateUrl
        )
        if ($workspaceHost) { $cookieUrls += "https://$workspaceHost" }
        if ($pageHost) { $cookieUrls += "https://$pageHost" }
        $cookieUrls = $cookieUrls | Where-Object { $_ } | Select-Object -Unique

        $allCookies = @()
        $allCookieResult = Send-CdpCommand -Ws $ws -Method "Network.getAllCookies"
        if ($allCookieResult -and $allCookieResult.cookies) {
            $allCookies = @($allCookieResult.cookies)
        }
        if ($allCookies.Count -eq 0) {
            $cookieResult = Send-CdpCommand -Ws $ws -Method "Network.getCookies" -Params @{
                urls = @($cookieUrls)
            }
            if ($cookieResult -and $cookieResult.cookies) {
                $allCookies = @($cookieResult.cookies)
            }
        }

        $slackCookies = @($allCookies | Where-Object { $_.domain -match "slack\.com$" })
        $slackCookieDiag = @($slackCookies | ForEach-Object { "$($_.name)@$($_.domain)" })
        Write-Host "  Slack cookies: $($slackCookieDiag -join ', ')" -ForegroundColor DarkGray

        $slackXoxd = @()
        $slackDs = $null
        foreach ($cookie in $slackCookies) {
            if ($cookie.name -eq "d" -and $cookie.value -match "^xoxd-") {
                $slackXoxd += $cookie.value
            }
            if ($cookie.name -eq "token" -and $cookie.value -match "^xoxc-") {
                $xoxcCandidates += @{ src = "token-cookie"; value = $cookie.value }
            }
            if ($cookie.name -eq "d-s" -and $cookie.value) {
                $slackDs = $cookie.value
            }
        }

        $xoxcSources = @($xoxcCandidates | ForEach-Object { $_.src } | Select-Object -Unique)
        if ($slackXoxd.Count -gt 0) { $xoxdToken = $slackXoxd[0] }
        if ($xoxcCandidates.Count -gt 0) { $xoxcToken = $xoxcCandidates[0].value }

        Write-Host "  xoxc candidates: $($xoxcCandidates.Count) ($($xoxcSources -join ', '))  xoxd: $($slackXoxd.Count)  d-s: $([bool]$slackDs)" -ForegroundColor DarkGray

        if (-not $xoxdToken) {
            $cookieEval = Send-CdpCommand -Ws $ws -Method "Runtime.evaluate" -Params @{
                expression = "document.cookie"
                returnByValue = $true
            }
            if ($cookieEval -and $cookieEval.result -and $cookieEval.result.value) {
                $cookieStr = $cookieEval.result.value
                if ($cookieStr -match "(?:^|;\s*)d=(xoxd-[^;]+)") {
                    $xoxdToken = $Matches[1]
                }
            }
        }

        try {
            $ws.CloseOutputAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "", [System.Threading.CancellationToken]::None).GetAwaiter().GetResult() | Out-Null
        } catch {}
        try { $ws.Dispose() } catch {}

        function Test-SlackPair($xoxc, $xoxd, $ds) {
            if (-not $xoxc -or -not $xoxd) { return $false }
            $cookie = "d=$xoxd"
            if ($ds) { $cookie = "$cookie; d-s=$ds" }
            # Windows PowerShell 5.1 Invoke-RestMethod silently drops the Cookie header
            # (restricted header). curl.exe sends it. Without d=, Slack always returns
            # invalid_auth even for a valid xoxc/xoxd pair.
            $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
            if ($curl) {
                try {
                    $out = & curl.exe -sS -X POST "https://slack.com/api/auth.test" `
                        -H "Authorization: Bearer $xoxc" `
                        -H "Cookie: $cookie" `
                        -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" `
                        -H "Accept: application/json" `
                        --max-time 15 2>&1
                    $text = ($out | Out-String).Trim()
                    $resp = $text | ConvertFrom-Json
                    if ([bool]$resp.ok) { return $true }
                    $script:lastAuthError = if ($resp.error) { [string]$resp.error } else { "auth.test ok=false" }
                    return $false
                } catch {
                    $script:lastAuthError = $_.Exception.Message
                    return $false
                }
            }
            try {
                $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
                $nc = New-Object System.Net.Cookie
                $nc.Name = "d"
                $nc.Value = $xoxd
                $nc.Domain = ".slack.com"
                $nc.Path = "/"
                $session.Cookies.Add($nc)
                if ($ds) {
                    $ncs = New-Object System.Net.Cookie
                    $ncs.Name = "d-s"
                    $ncs.Value = $ds
                    $ncs.Domain = ".slack.com"
                    $ncs.Path = "/"
                    $session.Cookies.Add($ncs)
                }
                $resp = Invoke-RestMethod -Uri "https://slack.com/api/auth.test" -WebSession $session -Method Post -TimeoutSec 15 -Headers @{
                    Authorization = "Bearer $xoxc"
                    "User-Agent"  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    Accept        = "application/json"
                } -ErrorAction Stop
                if ([bool]$resp.ok) { return $true }
                $script:lastAuthError = [string]$resp.error
                return $false
            } catch {
                $script:lastAuthError = $_.Exception.Message
                return $false
            }
        }

        $script:authValidated = $false
        $tried = 0
        foreach ($cand in $xoxcCandidates) {
            foreach ($d in $slackXoxd) {
                $dVariants = @($d)
                try {
                    $decoded = [uri]::UnescapeDataString($d)
                    if ($decoded -ne $d) { $dVariants += $decoded }
                } catch {}
                foreach ($dv in $dVariants) {
                    $tried++
                    if (Test-SlackPair $cand.value $dv $null) {
                        $xoxcToken = $cand.value
                        $xoxdToken = $dv
                        $script:authValidated = $true
                        Write-Status "OK" Green "auth.test passed ($($cand.src)+d)"
                        break
                    }
                    if ($slackDs -and (Test-SlackPair $cand.value $dv $slackDs)) {
                        $xoxcToken = $cand.value
                        $xoxdToken = $dv
                        $script:authValidated = $true
                        Write-Status "OK" Green "auth.test passed ($($cand.src)+d+d-s)"
                        break
                    }
                }
                if ($script:authValidated) { break }
            }
            if ($script:authValidated) { break }
        }
        if (-not $script:authValidated) {
            $errHint = if ($script:lastAuthError) { " last_error=$script:lastAuthError" } else { "" }
            Write-Status "!" Yellow "auth.test failed after $tried pair attempt(s)$errHint"
        }

    } catch {
        Write-Status "!" Yellow "CDP error: $_"
    }

    # Step 7: Close browser and clean up
    if ($browserProc -and -not $browserProc.HasExited) {
        $browserProc.Kill()
        Start-Sleep -Seconds 1
    }
    Clear-ExtractProfile -Dir $profileDir

    # Step 8: Report results
    Write-Host ""

    if ($xoxcToken -and $xoxdToken) {
        if ($script:authValidated) {
            Write-Status "OK" Green "Both tokens extracted and auth.test confirmed"
        } else {
            $errHint = if ($script:lastAuthError) { " ($script:lastAuthError)" } else { "" }
            Write-Status "!" Yellow "Tokens extracted; auth.test did not confirm$errHint. Saving pair anyway; MCP is the real check."
        }
        if (-not $Silent) {
            Write-Host ""
            Write-Host "SLACK_XOXC=$xoxcToken"
            Write-Host "SLACK_XOXD=$xoxdToken"
        }
        return [PSCustomObject]@{ Success = $true; Xoxc = $xoxcToken; Xoxd = $xoxdToken; Validated = [bool]$script:authValidated; Error = $null }
    } elseif ($xoxdToken -and -not $xoxcToken) {
        Write-Status "!" Yellow "Got xoxd cookie but could not extract xoxc token from page"
        Write-Host "  The xoxc token may not be available yet. Try:"  -ForegroundColor DarkGray
        Write-Host "  1. Open a channel in Slack before the extraction" -ForegroundColor DarkGray
        Write-Host "  2. Or manually get the xoxc token from DevTools console:" -ForegroundColor DarkGray
        Write-Host "     window.boot_data.api_token" -ForegroundColor DarkGray
        if (-not $Silent) { Write-Host ""; Write-Host "SLACK_XOXD=$xoxdToken" }
        return [PSCustomObject]@{ Success = $false; Xoxc = $null; Xoxd = $xoxdToken; Validated = $false; Error = "xoxc not found" }
    } elseif ($xoxcToken -and -not $xoxdToken) {
        Write-Status "!" Yellow "Got xoxc token but could not extract xoxd cookie"
        if (-not $Silent) { Write-Host ""; Write-Host "SLACK_XOXC=$xoxcToken" }
        return [PSCustomObject]@{ Success = $false; Xoxc = $xoxcToken; Xoxd = $null; Validated = $false; Error = "xoxd not found" }
    } else {
        Write-Status "X" Red "Could not extract either token"
        Write-Host "  Possible causes:" -ForegroundColor DarkGray
        Write-Host "  - Slack page did not fully load" -ForegroundColor DarkGray
        Write-Host "  - Login was not completed" -ForegroundColor DarkGray
        Write-Host "  - Slack updated their page structure" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "  Fallback: extract tokens manually from DevTools" -ForegroundColor Yellow
        return [PSCustomObject]@{ Success = $false; Xoxc = $null; Xoxd = $null; Validated = $false; Error = "no tokens found" }
    }
}

# =============================================================================
# -ExtractOnly: standalone diagnostic mode, no Secret, no cluster touched
# =============================================================================
if ($ExtractOnly) {
    $extractParams = @{
        WorkspaceUrl = $WorkspaceUrl
        Timeout      = $ExtractTimeout
        Port         = $Port
        Browser      = $Browser
    }
    if ($Incognito) { $extractParams["Incognito"] = $true }
    if ($ReuseSession) { $extractParams["ReuseSession"] = $true }
    if ($ProfileDir) { $extractParams["ProfileDir"] = $ProfileDir }

    $result = Get-SlackTokensViaCdp @extractParams
    if ($result.Success) { exit 0 } else { exit 1 }
}

# =============================================================================
# Main refresh flow: obtain tokens -> render Secret -> Export or Apply
# =============================================================================

function Resolve-EnvPath {
    # Prefer a .env next to this script (fully standalone placement on a new
    # machine), fall back to one directory up (this repo's scripts/ layout).
    $candidates = @(
        (Join-Path $PSScriptRoot ".env"),
        (Join-Path (Split-Path -Parent $PSScriptRoot) ".env")
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $candidates[0]
}

$xoxc = $null
$xoxd = $null

# 1. Obtain tokens -------------------------------------------------------
if ($SkipExtract) {
    Write-Host "==> -SkipExtract set: reading existing tokens from .env" -ForegroundColor Yellow
    $envPath = Resolve-EnvPath
    if (-not (Test-Path $envPath)) { Fail ".env not found at $envPath" }
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^SLACK_MCP_XOXC_TOKEN=(.+)$') { $xoxc = $Matches[1].Trim().Trim('"').Trim("'") }
        if ($line -match '^SLACK_MCP_XOXD_TOKEN=(.+)$') { $xoxd = $Matches[1].Trim().Trim('"').Trim("'") }
    }
    if (-not $xoxc -or -not $xoxd) {
        Fail "SLACK_MCP_XOXC_TOKEN / SLACK_MCP_XOXD_TOKEN not both found in $envPath"
    }
    Write-Host "==> Loaded existing tokens from .env (values not shown)" -ForegroundColor Green
}
else {
    Write-Host "==> Launching browser-based Slack token extraction (up to $ExtractTimeout s)..." -ForegroundColor Cyan
    Write-Host "    A browser window will open - sign in and click into a channel." -ForegroundColor DarkGray
    Write-Host "    Progress output is captured, not displayed, so no token fragments reach this console." -ForegroundColor DarkGray

    $extractParams = @{
        WorkspaceUrl = $WorkspaceUrl
        Timeout      = $ExtractTimeout
        Port         = $Port
        Browser      = $Browser
        Silent       = $true
    }
    if ($Incognito) { $extractParams["Incognito"] = $true }
    if ($ReuseSession) { $extractParams["ReuseSession"] = $true }
    if ($ProfileDir) { $extractParams["ProfileDir"] = $ProfileDir }

    # *>&1 merges ALL streams (including Write-Host's Information stream)
    # into the success stream so the result object can be pulled out of it -
    # and, crucially, so none of this call's diagnostic output (or the raw
    # token lines the function would otherwise print) reaches this host.
    # -Silent above also makes the function withhold the raw token lines
    # even if something upstream changes how this capture works.
    $extractStreams = Get-SlackTokensViaCdp @extractParams *>&1
    $extractResult = $extractStreams | Where-Object {
        $_ -is [System.Management.Automation.PSCustomObject] -and ($_.PSObject.Properties.Name -contains "Success")
    } | Select-Object -Last 1

    if (-not $extractResult -or -not $extractResult.Success -or -not $extractResult.Xoxc -or -not $extractResult.Xoxd) {
        $errHint = if ($extractResult -and $extractResult.Error) { " ($($extractResult.Error))" } else { "" }
        Fail "Token extraction failed$errHint. Re-run this script with -ExtractOnly to see full diagnostic output, or pass -SkipExtract to reuse existing .env tokens."
    }
    $xoxc = $extractResult.Xoxc
    $xoxd = $extractResult.Xoxd
    Write-Host "==> Tokens extracted (xoxc: yes, xoxd: yes). Values held in memory only." -ForegroundColor Green
}

# 2. Render Secret YAML (plain stringData - no file encoding needed) -----
function Build-SlackSecretYaml {
    param([string]$Namespace, [string]$SecretName, [string]$Xoxc, [string]$Xoxd)
    return @"
apiVersion: v1
kind: Secret
metadata:
  name: $SecretName
  namespace: $Namespace
type: Opaque
stringData:
  SLACK_XOXC_TOKEN: "$Xoxc"
  SLACK_XOXD_TOKEN: "$Xoxd"
"@
}

Write-Host "==> Rendering Secret manifest for $Namespace/$SecretName..." -ForegroundColor Cyan
$secretYaml = Build-SlackSecretYaml -Namespace $Namespace -SecretName $SecretName -Xoxc $xoxc -Xoxd $xoxd

if ($Mode -eq "Export") {
    $outputDir = Split-Path -Parent $OutputPath
    if ($outputDir -and -not (Test-Path $outputDir)) {
        New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
    }
    $secretYaml | Out-File -FilePath $OutputPath -Encoding ascii
    Write-Host "==> Wrote Secret manifest to $OutputPath" -ForegroundColor Green
    Write-Host "    Contains live session tokens - never commit (already .gitignore'd)." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Next steps (out-of-band delivery to the target environment):" -ForegroundColor Green
    Write-Host "  1. Hand this file off via your target environment's normal path"
    Write-Host "     (scp to a bastion/jump host, GitOps commit, sealed-secrets seal + commit, etc.)"
    Write-Host "  2. Apply it and restart the deployment there, e.g.:"
    Write-Host "     oc apply -f $(Split-Path -Leaf $OutputPath)"
    Write-Host "     oc -n $Namespace rollout restart deploy/$DeploymentName"
    Write-Host "  3. Verify with a REAL search_messages/whoami call - not just a health check."
    exit 0
}

# 3. Apply mode: push directly + restart ----------------------------------
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
Write-Host "    Verify with a real search_messages/whoami call against the target environment, not just /health." -ForegroundColor Yellow
