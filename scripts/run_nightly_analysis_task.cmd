@echo off
rem ============================================================================
rem  affiliate-ai nightly analysis launcher (C10-2)
rem
rem  Scheduled once a day, separate from the resident Threads worker. Registering
rem  the scheduled task is a human decision (see nightly_analysis_policy.json and
rem  scripts/run_nightly_analysis.py --section schedule for the exact plan).
rem
rem  NOTE: ASCII only (cmd.exe parses this file with the OEM code page).
rem
rem  Exit codes: 65 cannot enter the project directory, 66 log directory
rem  unavailable; otherwise the exit code of scripts/run_nightly_analysis.py.
rem
rem  Optional environment overrides:
rem      AFFILIATE_AI_UV       absolute path to uv.exe (default: uv from PATH)
rem      AFFILIATE_AI_LOG_DIR  log destination (default: D:\Logs\affiliate-ai)
rem
rem  The batch reads local and cached data only and writes only its own local
rem  tables (nightly_analysis_runs, content_discovery_candidates).
rem ============================================================================
setlocal EnableExtensions

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

"%UV%" run python scripts\run_nightly_analysis.py --execute --trigger scheduler >> "%LOG_DIR%\nightly-analysis.log" 2>&1

exit /b %ERRORLEVEL%
