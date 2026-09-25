@echo off
rem ============================================================================
rem  affiliate-ai resident Threads worker launcher (T4.3)
rem
rem  Role: start (and let Task Scheduler restart) the RESIDENT Threads worker.
rem  It is NOT a posting cron. The worker decides every time from real state
rem  (approval, posting gap, window, queue) and posts at most one per cycle.
rem
rem  NOTE: this file is parsed by cmd.exe using the OEM code page, so it must
rem  stay ASCII-only (same rule as run_operations_task.cmd).
rem
rem  Usage:
rem      run_threads_worker_task.cmd observe   (default)
rem      run_threads_worker_task.cmd operate
rem      run_threads_worker_task.cmd publish
rem
rem  Profiles (each includes the previous one):
rem      observe  --collect-insights --sync-approvals    (reads + local records)
rem      operate  + --send-approval-digests              (approval digest email)
rem      publish  + --auto-publish                        (automatic posting)
rem
rem  SAFETY: the publish profile still posts NOTHING while
rem  automatic_publication.enabled is false in threads_operations_policy.json.
rem  The flag alone is never enough.
rem
rem  Overlap: a second worker exits immediately (DB lock, exit code 4), so a
rem  periodic trigger only "recovers" a dead worker; it never runs two.
rem
rem  Exit codes: 64 unknown profile, 65 cannot enter the project directory,
rem  66 log directory unavailable. Worker codes: 0 ok, 4 already running,
rem  5 lost the lock.
rem
rem  Optional environment overrides:
rem      AFFILIATE_AI_UV       absolute path to uv.exe (default: uv from PATH)
rem      AFFILIATE_AI_LOG_DIR  log destination (default: D:\Logs\affiliate-ai)
rem
rem  No secrets live here. Credentials are resolved in-process through the
rem  project's existing .env loading path.
rem ============================================================================
setlocal EnableExtensions

set "PROFILE=%~1"
if "%PROFILE%"=="" set "PROFILE=observe"
set "FLAGS="
if /I "%PROFILE%"=="observe" set "FLAGS=--collect-insights --sync-approvals"
if /I "%PROFILE%"=="operate" set "FLAGS=--collect-insights --sync-approvals --send-approval-digests"
if /I "%PROFILE%"=="publish" set "FLAGS=--collect-insights --sync-approvals --send-approval-digests --auto-publish"
if "%FLAGS%"=="" (
    echo unknown profile: %PROFILE% ^(expected observe, operate or publish^) 1>&2
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

rem Separate log from the C8 daily/weekly pipeline.
"%UV%" run python scripts\run_threads_worker.py --resident %FLAGS% >> "%LOG_DIR%\threads-worker.log" 2>&1

exit /b %ERRORLEVEL%
