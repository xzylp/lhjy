param(
    [string]$ProjectDir = "D:\Coding\lhjy\ashare-system-v2"
)

$desktop = [Environment]::GetFolderPath("Desktop")
$vbsPath = Join-Path $ProjectDir "scripts\windows_service_gui.vbs"
$iconPath = Join-Path $ProjectDir "assets\icons\candlestick-chart.ico"
$shell = New-Object -ComObject WScript.Shell

if (-not (Test-Path $vbsPath)) {
    throw "GUI launcher not found: $vbsPath"
}

function Set-GuiShortcut {
    param(
        [string]$ShortcutPath
    )

    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $shortcut.Arguments = '"' + $vbsPath + '"'
    $shortcut.WorkingDirectory = $ProjectDir
    $shortcut.Description = "ashare-system-v2 control panel"
    if (Test-Path $iconPath) {
        $shortcut.IconLocation = $iconPath
    }
    $shortcut.Save()
    Write-Host "Updated shortcut: $ShortcutPath"
}

$updated = $false
$needles = @(
    "windows_service_gui.cmd",
    "windows_service_gui.py",
    "ashare-system-v2",
    "windows_service_gui.vbs"
)

Get-ChildItem -Path $desktop -Filter "*.lnk" -ErrorAction SilentlyContinue | ForEach-Object {
    $current = $shell.CreateShortcut($_.FullName)
    $text = (($current.TargetPath + " " + $current.Arguments + " " + $current.WorkingDirectory) | Out-String).ToLowerInvariant()
    foreach ($needle in $needles) {
        if ($text.Contains($needle.ToLowerInvariant())) {
            Set-GuiShortcut -ShortcutPath $_.FullName
            $updated = $true
            break
        }
    }
}

if (-not $updated) {
    $newShortcut = Join-Path $desktop "ashare-system-v2 Console.lnk"
    Set-GuiShortcut -ShortcutPath $newShortcut
}
