# Deploy latest build (dist/智能网关.exe) to production directory and restart.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy_gw.ps1
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$distExe = Join-Path $project 'dist\智能网关.exe'
$distEnv = Join-Path $project 'dist\.env'
$deployDir = 'D:\智能网关'
$targetExe = Join-Path $deployDir '智能网关.exe'

if (-not (Test-Path $distExe)) { Write-Output "[X] $distExe not found - build first"; exit 1 }

# 1. Kill processes running from the deploy dir (wildcard avoids CJK literals)
Write-Output '[1/5] Killing running gateway...'
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like 'D:\智能网关*' -and $_.Name -eq '智能网关.exe' } |
    ForEach-Object { Write-Output ("  kill PID {0}" -f $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# 2. Backup current exe
if (Test-Path $targetExe) {
    $bak = Join-Path $deployDir ("智能网关.exe.bak_" + (Get-Date -Format 'yyyyMMdd_HHmm'))
    Move-Item $targetExe $bak -Force
    Write-Output "[2/5] Backed up old exe -> $bak"
}

# 3. Copy new exe + .env
Write-Output '[3/5] Copying new exe + .env...'
Copy-Item $distExe $targetExe -Force
if (Test-Path $distEnv) { Copy-Item $distEnv (Join-Path $deployDir '.env') -Force }

# 4. Start
Write-Output '[4/5] Starting gateway...'
Start-Process -FilePath $targetExe -WorkingDirectory $deployDir

# 5. Verify port 8765
$ok = $false
foreach ($i in 1..15) {
    Start-Sleep -Seconds 2
    $conn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($conn) { $ok = $true; break }
}
if ($ok) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($conn.OwningProcess)"
    Write-Output ("[5/5] OK: port 8765 listening, PID {0}, path {1}" -f $proc.ProcessId, $proc.ExecutablePath)
} else {
    Write-Output '[X] Port 8765 not listening after 30s - check D:\智能网关\logs\gateway.log'
    exit 1
}