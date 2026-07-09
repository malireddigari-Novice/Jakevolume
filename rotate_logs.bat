@echo off
REM ============================================================================
REM Jakevolume standalone log housekeeping.
REM
REM Rotates the launcher/redirect "sidecar" logs that the .bat watchdog and
REM Start-Process redirects produce. Each log over MAXBYTES is moved to a single
REM .old backup (current + .old caps each at ~2x the threshold).
REM
REM NOTE: jakevolume.log is intentionally NOT handled here — Python's
REM RotatingFileHandler already manages it (10 MB x 5 backups). This script only
REM covers the launcher-owned logs Python cannot rotate itself.
REM
REM Belt-and-suspenders usage: register as a daily Scheduled Task, ideally at a
REM time the bot is NOT running (e.g. before the 08:10 start), since a log held
REM open for append cannot be moved and is simply skipped until next run:
REM   schtasks /Create /TN "Jakevolume log rotate" /TR "C:\Users\malir\Projects\Python\Jakevolume\rotate_logs.bat" /SC DAILY /ST 07:00 /F
REM ============================================================================

cd /d "C:\Users\malir\Projects\Python\Jakevolume"

REM Rotate any sidecar log over this many bytes (10 MB).
set MAXBYTES=10485760

for %%L in (jakevolume_scheduled.log jakevolume_startup.log jakevolume_err.log jakevolume_startup.err) do (
    if exist "%%L" for %%A in ("%%L") do if %%~zA GTR %MAXBYTES% (
        move /Y "%%L" "%%L.old" >nul 2>&1 && echo [%DATE% %TIME%] rotated %%L ^(%%~zA bytes^) -^> %%L.old
    )
)
