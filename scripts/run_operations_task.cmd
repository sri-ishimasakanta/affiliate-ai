@echo off
rem ============================================================================
rem  affiliate-ai scheduled operations launcher (C8.6)
rem
rem  Why this file exists: putting the whole command into schtasks /TR nests
rem  double quotes, which breaks under both PowerShell and cmd.exe. Scheduling
rem  this launcher instead means /TR is only "<this file> <profile>", so no
rem  nested quoting can occur.
rem
rem  NOTE: this file is parsed by cmd.exe using the OEM code page, so it must
rem  stay ASCII-only. Non-ASCII comments corrupt batch parsing on a Japanese
rem  (CP932) console.
rem
rem  Usage:
rem      run_operations_task.cmd daily
rem      run_operations_task.cmd weekly
rem
rem  Exit codes: 64 unknown profile, 65 cannot enter the project directory,
rem  66 log directory unavailable. These sit above the usual child codes so a
rem  launcher fault is never confused with a pipeline result (a missing uv.exe,
rem  for example, propagates cmd's own 3 = ERROR_PATH_NOT_FOUND).
rem
rem  Optional environment overrides:
rem      AFFILIATE_AI_UV       absolute path to uv.exe (default: uv from PATH)
rem      AFFILIATE_AI_LOG_DIR  log destination (default: D:\Logs\affiliate-ai)
rem
rem  No secrets live here. Credentials are resolved in-process through the
rem  project's existing .env loading path. No business logic lives here either:
rem  the real work is delegated to scripts/run_operations.py.
rem ============================================================================
setlocal EnableExtensions

set "PROFILE=%~1"
if "%PROFILE%"=="" set "PROFILE=daily"
if /I not "%PROFILE%"=="daily" if /I not "%PROFILE%"=="weekly" (
    echo unknown profile: %PROFILE% ^(expected daily or weekly^) 1>&2
    exit /b 64
)

rem Work from the project root; this file lives in scripts\.
cd /d "%~dp0.." || (
    echo failed to change to the project directory 1>&2
    exit /b 65
)

set "LOG_DIR=%AFFILIATE_AI_LOG_DIR%"
if "%LOG_DIR%"=="" set "LOG_DIR=D:\Logs\affiliate-ai"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" 2>nul
if not exist "%LOG_DIR%" (
    echo log directory is not available: %LOG_DIR% 1>&2
    exit /b 66
)

set "UV=%AFFILIATE_AI_UV%"
if "%UV%"=="" set "UV=uv"

rem Append both stdout and stderr to the same log. No interactive shell needed.
"%UV%" run python scripts\run_operations.py --profile %PROFILE% --execute --trigger scheduler >> "%LOG_DIR%\operations-%PROFILE%.log" 2>&1

rem Propagate the child exit code as the scheduled task result.
exit /b %ERRORLEVEL%
