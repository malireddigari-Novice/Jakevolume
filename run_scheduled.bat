@echo off
REM Jakevolume watchdog — starts main.py and auto-restarts on crash during
REM trading hours. Checks every 5 minutes until 15:15 local time, then exits.

cd /d "C:\Users\malir\Projects\Python\Jakevolume"
set SAMPLE_MODE=false

REM ── Gold-only production mode — ALL layers active ─────────────────────────
REM Full Gold pipeline: structural + value/contract-low gate, deferred directional-
REM intent validation (P2 wired), opposite-side veto, event-time capture/eligibility,
REM and breakout/breakdown continuation. Everything non-Gold is research-only.
REM Remove any line to disable that layer; remove all to revert to pre-Gold behavior.
set GOLD_ONLY_PRODUCTION_MODE=true
set INTENT_VALIDATION_ENABLED=true
set OPPOSITE_SIDE_VETO_ENABLED=true
set EVENT_TIME_ELIGIBILITY_ENABLED=true
set BREAKOUT_BREAKDOWN_ENABLED=true

set LOG=jakevolume_scheduled.log
set TRADE_END_HOUR=15
set TRADE_END_MIN=15

REM --- Log housekeeping: rotate if over 10 MB, keeping one .old backup ---
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA GTR 10485760 move /Y "%LOG%" "%LOG%.old" >nul

echo [%DATE% %TIME%] ===== Jakevolume watchdog started ===== >> %LOG%

:WATCHDOG_LOOP

REM --- Check if within trading hours ---
set "T=%TIME: =0%"
set /a CURR_H=1%T:~0,2% - 100
set /a CURR_M=1%T:~3,2% - 100
set /a CURR_MINS=CURR_H * 60 + CURR_M
set /a END_MINS=TRADE_END_HOUR * 60 + TRADE_END_MIN

if %CURR_MINS% GEQ %END_MINS% (
    echo [%DATE% %TIME%] Outside trading hours ^(past %TRADE_END_HOUR%:%TRADE_END_MIN%^). Watchdog exiting. >> %LOG%
    goto :EOF
)

echo [%DATE% %TIME%] Launching main.py ... >> %LOG%
"C:\Python314\python.exe" main.py >> %LOG% 2>&1
set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% EQU 0 (
    echo [%DATE% %TIME%] main.py exited cleanly ^(code 0^). Watchdog done. >> %LOG%
    goto :EOF
)

REM --- main.py crashed ---
echo [%DATE% %TIME%] main.py crashed ^(exit code %EXIT_CODE%^). >> %LOG%

REM Re-check trading hours after the crash
set "T=%TIME: =0%"
set /a CURR_H=1%T:~0,2% - 100
set /a CURR_M=1%T:~3,2% - 100
set /a CURR_MINS=CURR_H * 60 + CURR_M

if %CURR_MINS% GEQ %END_MINS% (
    echo [%DATE% %TIME%] Outside trading hours after crash. Not restarting. >> %LOG%
    goto :EOF
)

echo [%DATE% %TIME%] Still in trading hours. Retrying in 5 minutes ... >> %LOG%
timeout /t 300 /nobreak > nul
goto WATCHDOG_LOOP
