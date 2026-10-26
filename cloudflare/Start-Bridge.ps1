param([switch]$InstallTask)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$credentialPath = Join-Path $PSScriptRoot '.deploy-credentials.json'
$cloudflared = Join-Path $env:LOCALAPPDATA 'Programs\cloudflared\cloudflared.exe'
$logDir = Join-Path $env:LOCALAPPDATA 'VideoRemakeWorkshop\logs'
$gatewayLog = Join-Path $logDir 'gateway.log'
$gatewayErr = Join-Path $logDir 'gateway-error.log'
$tunnelLog = Join-Path $logDir 'tunnel.log'
$tunnelErr = Join-Path $logDir 'tunnel-error.log'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Bridge supervisor started."
trap {
  $line = $_.InvocationInfo.ScriptLineNumber
  $trace = ($_.ScriptStackTrace -replace "`r?`n", ' | ')
  Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Unhandled error at line ${line}: $($_.Exception.Message) | $trace"
  break
}
function Test-TcpPort([int]$Port) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $connect = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
    if (!$connect.AsyncWaitHandle.WaitOne(1000)) { return $false }
    $client.EndConnect($connect)
    return $true
  } catch { return $false }
  finally { $client.Close() }
}
if (!(Test-Path $credentialPath) -or !(Test-Path $cloudflared)) { throw 'Deployment credentials or cloudflared are missing.' }
$credentials = Get-Content -LiteralPath $credentialPath -Raw | ConvertFrom-Json
if (!$credentials.gatewaySecret) { throw 'Gateway secret is missing from the ignored deployment credentials file.' }

function Set-WorkerSecret([string]$Name, [string]$Value) {
  $job = Start-Job -ArgumentList $Name, $Value, $projectRoot -ScriptBlock {
    param($SecretName, $SecretValue, $Root)
    Set-Location $Root
    $SecretValue | npx --yes wrangler@latest secret put $SecretName --config cloudflare/wrangler.jsonc --cwd $Root | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Wrangler exited with $LASTEXITCODE." }
  }
  if (!(Wait-Job -Id $job.Id -Timeout 90)) {
    Stop-Job -Id $job.Id -ErrorAction SilentlyContinue
    Remove-Job -Id $job.Id -Force -ErrorAction SilentlyContinue
    throw "Timed out updating Worker secret $Name."
  }
  $result = Receive-Job -Id $job.Id -ErrorAction SilentlyContinue
  $failed = $job.State -ne 'Completed'
  Remove-Job -Id $job.Id -Force -ErrorAction SilentlyContinue
  if ($failed) { throw "Failed to update Worker secret $Name." }
}

function Start-Gateway {
  $backendLog = Join-Path $logDir 'backend.log'
  $backendErr = Join-Path $logDir 'backend-error.log'
  if (Test-TcpPort 8003) {
    try {
      $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8003/api/health' -TimeoutSec 5
      if (!$health.ok) { throw 'Health returned ok=false.' }
    } catch {
      Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Port 8003 is occupied but unhealthy; leaving its process untouched."
      throw 'Port 8003 is occupied by an unhealthy backend; see backend service logs.'
    }
  } else {
    $python = [string]$credentials.pythonExecutable
    if (!$python -or !(Test-Path -LiteralPath $python)) {
      $python = ''
      $venvCandidates = @((Join-Path $projectRoot '.venv\Scripts\python.exe'), (Join-Path $projectRoot 'venv\Scripts\python.exe'))
      foreach ($candidate in $venvCandidates) { if (Test-Path -LiteralPath $candidate) { $python = $candidate; break } }
      if (!$python) {
        $python = (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
      }
    }
    if (!$python) { throw 'Python was not found; cannot start the existing backend entrypoint.' }
    $deps = & $python -c 'import fastapi, uvicorn' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'The selected Python environment is missing FastAPI or Uvicorn.' }
    $credentials.pythonExecutable = $python
    $credentials | ConvertTo-Json | Set-Content -LiteralPath $credentialPath -Encoding utf8
    $backendProc = Start-Process -FilePath $python -ArgumentList '-m uvicorn backend.app.main:app --host 127.0.0.1 --port 8003' -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $backendLog -RedirectStandardError $backendErr -PassThru
    $healthy = $false
    for ($i=0; $i -lt 30; $i++) {
      Start-Sleep -Seconds 2
      try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8003/api/health' -TimeoutSec 4
        if ($health.ok) { $healthy = $true; break }
      } catch {}
      if ($backendProc.HasExited) { break }
    }
    if (!$healthy) {
      Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Backend startup did not become healthy; see backend-error.log."
      throw 'Backend startup failed or timed out; tunnel not started.'
    }
  }
  $env:WORKSHOP_GATEWAY_SECRET = [string]$credentials.gatewaySecret
  $env:WORKSHOP_BACKEND_URL = 'http://127.0.0.1:8003'
  $args = '-m uvicorn cloudflare.gateway:app --host 127.0.0.1 --port 8004'
  $existing = Test-TcpPort 8004
  if (!$existing) {
    $gatewayPython = [string]$credentials.pythonExecutable
    if (!$gatewayPython -or !(Test-Path -LiteralPath $gatewayPython)) { $gatewayPython = (Get-Command python.exe).Source }
    $null = Start-Process -FilePath $gatewayPython -ArgumentList $args -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $gatewayLog -RedirectStandardError $gatewayErr -PassThru
    for ($i=0; $i -lt 30; $i++) {
      Start-Sleep -Seconds 1
      if (Test-TcpPort 8004) { break }
    }
  }
  $null = Invoke-WebRequest -Uri 'http://127.0.0.1:8004/api/health' -Headers @{'X-Workshop-Gateway-Secret'=[string]$credentials.gatewaySecret} -UseBasicParsing -TimeoutSec 6
}

if ($InstallTask) {
  $taskName = 'VideoRemakeWorkshop-Bridge'
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`""
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Starts the local protected gateway and Cloudflare quick tunnel after user logon.' -Force | Out-Null
  Start-ScheduledTask -TaskName $taskName
  return
}

try {
  Start-Gateway
  Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Local backend and gateway are healthy."
  $lastUrl = ''
  while ($true) {
  Remove-Item -LiteralPath $tunnelLog -Force -ErrorAction SilentlyContinue
  $proc = Start-Process -FilePath $cloudflared -ArgumentList @('tunnel','--no-autoupdate','--url','http://127.0.0.1:8004','--logfile',$tunnelLog,'--loglevel','info') -WindowStyle Hidden -PassThru
  $newUrl = ''
  $lastHealthProbe = Get-Date
  $healthFailures = 0
  while (!$proc.HasExited) {
    Start-Sleep -Seconds 3
    if (Test-Path $tunnelLog) {
      $match = Select-String -LiteralPath $tunnelLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -Last 1
      if ($match) {
        $url = [regex]::Match($match.Line, 'https://[a-z0-9-]+\.trycloudflare\.com').Value
        if ($url -and $url -ne $newUrl) {
          $newUrl = $url
        }
        if ($newUrl -and $newUrl -ne $lastUrl) {
          try {
            Set-WorkerSecret -Name 'BACKEND_URL' -Value $newUrl
            Set-WorkerSecret -Name 'BACKEND_SHARED_SECRET' -Value ([string]$credentials.gatewaySecret)
            $lastUrl = $newUrl
            Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Worker backend route refreshed."
          } catch {
            Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Worker route refresh failed: $($_.Exception.Message)"
          }
        }
        if (Select-String -LiteralPath $tunnelLog -Pattern 'Unauthorized: Tunnel not found' -Quiet) {
          Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Quick Tunnel was rejected by Cloudflare; restarting with a fresh hostname."
          Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
          break
        }
        if (((Get-Date) - $lastHealthProbe).TotalSeconds -ge 45) {
          $lastHealthProbe = Get-Date
          try {
            $check = Invoke-RestMethod -Uri "$newUrl/api/health" -Headers @{'X-Workshop-Gateway-Secret'=[string]$credentials.gatewaySecret} -TimeoutSec 8
            if (!$check.ok) { throw 'Gateway health did not return ok.' }
            $healthFailures = 0
          } catch {
            $healthFailures += 1
            Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Origin health probe failed ($healthFailures/3)."
            if ($healthFailures -ge 3) {
              Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Three origin health probes failed; restarting Quick Tunnel."
              Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
              break
            }
          }
        }
      }
    }
  }
  Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Tunnel exited; restarting."
    Start-Sleep -Seconds 4
  }
} catch {
  Add-Content -LiteralPath (Join-Path $logDir 'bridge-status.log') -Value "$(Get-Date -Format o) Fatal bridge error: $($_.Exception.Message)"
  throw
}
