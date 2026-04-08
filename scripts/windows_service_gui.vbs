Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)
guiScript = scriptDir & "\windows_service_gui.py"
venvPythonw = projectDir & "\.venv\Scripts\pythonw.exe"

If fso.FileExists(venvPythonw) Then
    shell.Run """" & venvPythonw & """ """ & guiScript & """", 0, False
Else
    shell.Run "pyw -3 """ & guiScript & """", 0, False
End If
