@echo off
setlocal

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..

if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    "%PROJECT_DIR%\.venv\Scripts\python.exe" "%SCRIPT_DIR%windows_ops_proxy.py" %*
) else (
    py -3 "%SCRIPT_DIR%windows_ops_proxy.py" %*
)

exit /b %errorlevel%
