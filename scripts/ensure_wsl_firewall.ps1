param(
    [int]$Port = 8100,
    [string]$RuleName = "ashare-system-v2-wsl-8100"
)

$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if (-not $existing) {
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -Profile Any | Out-Null
    Write-Host "Created firewall rule: $RuleName (TCP/$Port)"
} else {
    Write-Host "Firewall rule already exists: $RuleName"
}

Get-NetFirewallRule -DisplayName $RuleName | Select-Object DisplayName,Enabled,Direction,Action | Format-List
