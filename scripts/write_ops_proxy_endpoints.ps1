param(
    [string]$ProjectDir = "D:\Coding\lhjy\ashare-system-v2",
    [int]$Port = 18791,
    [string]$BindHost = "0.0.0.0",
    [string]$TokenFile = ""
)

$stateDir = Join-Path $ProjectDir ".ashare_state"
$manifestPath = Join-Path $stateDir "ops_proxy_endpoints.json"

New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

$wslIps = @(
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -and
            $_.IPAddress -notlike '127.*' -and
            $_.IPAddress -notlike '169.254.*' -and
            ($_.InterfaceAlias -like 'vEthernet*' -or $_.InterfaceAlias -like '*WSL*')
        } |
        Select-Object InterfaceAlias,IPAddress
)

$lanIps = @(
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -and
            $_.IPAddress -notlike '127.*' -and
            $_.IPAddress -notlike '169.254.*' -and
            $_.InterfaceAlias -notlike 'vEthernet*' -and
            $_.InterfaceAlias -notlike '*WSL*'
        } |
        Select-Object InterfaceAlias,IPAddress
)

$candidateUrls = New-Object System.Collections.Generic.List[string]
$candidateUrls.Add("http://127.0.0.1:$Port")

foreach ($item in $wslIps) {
    $candidateUrls.Add("http://$($item.IPAddress):$Port")
}

foreach ($item in $lanIps) {
    $candidateUrls.Add("http://$($item.IPAddress):$Port")
}

$deduped = $candidateUrls | Where-Object { $_ } | Select-Object -Unique
$preferredUrl = if ($BindHost -eq "0.0.0.0") {
    if ($wslIps.Count -gt 0) { "http://$($wslIps[0].IPAddress):$Port" } else { "http://127.0.0.1:$Port" }
} else {
    "http://${BindHost}:$Port"
}

$manifest = [ordered]@{
    generated_at = (Get-Date).ToString("s")
    project_dir = $ProjectDir
    port = $Port
    bind_host = $BindHost
    preferred_wsl_url = $preferredUrl
    candidate_urls = @($deduped)
    token_file = $TokenFile
    wsl_adapter_ips = @($wslIps | ForEach-Object { $_.IPAddress })
    lan_ips = @($lanIps | ForEach-Object { $_.IPAddress })
}

$manifest | ConvertTo-Json -Depth 6 | Set-Content -Path $manifestPath -Encoding UTF8
Write-Host "Wrote ops proxy endpoint manifest: $manifestPath"
