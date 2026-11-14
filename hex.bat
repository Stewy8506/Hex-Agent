@echo off
setlocal
set "REPO_PYTHON=%~dp0.venv\Scripts\python.exe"
set "HEX_HOME_PYTHON=%USERPROFILE%\.hex\env\Scripts\python.exe"

if exist "%REPO_PYTHON%" (
    "%REPO_PYTHON%" -m harness_core.cli %*
    exit /b %ERRORLEVEL%
)

if exist "%HEX_HOME_PYTHON%" (
    "%HEX_HOME_PYTHON%" -m harness_core.cli %*
    exit /b %ERRORLEVEL%
)

python -m harness_core.cli %*
exit /b %ERRORLEVEL%
