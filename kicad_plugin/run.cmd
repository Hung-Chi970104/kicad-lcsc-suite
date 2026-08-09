@echo off
rem Launch the EasyAssembly app for KiCad's "exec" plugin runtime, on Windows.
rem
rem TRAP 1 — KiCad hands its own interpreter's environment down to exec
rem plugins. A venv Python started with KiCad's PYTHONHOME dies instantly with
rem "ModuleNotFoundError: No module named 'encodings'", before it can run a
rem line of ours, and KiCad reports nothing at all: the toolbar button simply
rem does nothing. Clearing these four is what lets the app bring its own
rem Python.
set PYTHONHOME=
set PYTHONPATH=
set PYTHONEXECUTABLE=
set PYTHONSTARTUP=

rem %~dp0 is this script's directory. install.ps1 copies this file into KiCad's
rem plugin directory alongside a plugin.json whose entrypoint names it, and
rem writes REPO_ROOT into repo_root.txt beside it, because Windows has no
rem symlink we can count on resolving.
set PLUGIN_DIR=%~dp0
if exist "%PLUGIN_DIR%repo_root.txt" (
    set /p REPO_ROOT=<"%PLUGIN_DIR%repo_root.txt"
) else (
    for %%I in ("%PLUGIN_DIR%..") do set REPO_ROOT=%%~fI
)

set VENV_PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe
set LOG_DIR=%LOCALAPPDATA%\EasyAssembly
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set LOG=%LOG_DIR%\plugin.log

if not exist "%VENV_PYTHON%" (
    echo --- %DATE% %TIME% --->>"%LOG%"
    echo No app virtualenv at %VENV_PYTHON%.>>"%LOG%"
    echo Run install.ps1 in %REPO_ROOT% to create it.>>"%LOG%"
    exit /b 1
)

set PYTHONPATH=%REPO_ROOT%
echo --- %DATE% %TIME% --- launching EasyAssembly>>"%LOG%"
"%VENV_PYTHON%" -m lcsc_suite %* >>"%LOG%" 2>&1
