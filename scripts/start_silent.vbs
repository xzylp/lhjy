Set objShell = CreateObject("WScript.Shell")
objShell.Run "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File ""D:\Coding\lhjy\ashare-system-v2\scripts\start_unattended.ps1""", 0, False
