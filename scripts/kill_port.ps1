$conn = Get-NetTCPConnection -LocalPort 8100 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    $pid = $conn.OwningProcess
    Write-Host "Port 8100 is used by PID: $pid"
    $process = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if ($process) {
        Write-Host "Process name: $($process.Name)"
        Write-Host "Stopping process..."
        Stop-Process -Id $pid -Force
        Write-Host "Done."
    }
} else {
    Write-Host "Port 8100 is free"
}
