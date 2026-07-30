@echo off
setlocal

rem Always run from the directory containing this script.
cd /d "%~dp0"

set "NCATBOT_EXE=%CD%\.venv\Scripts\ncatbot.exe"

if not exist "%NCATBOT_EXE%" (
    echo [ERROR] NcatBot virtual environment was not found.
    echo Expected: %NCATBOT_EXE%
    echo Please create the .venv environment and install NcatBot first.
    pause
    exit /b 1
)

if not exist "config.yaml" (
    echo [ERROR] config.yaml was not found in the project root.
    echo Copy config.example.yaml to config.yaml and complete the configuration first.
    pause
    exit /b 1
)

echo Starting NcatBot...
"%NCATBOT_EXE%" run
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo NcatBot stopped with exit code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
