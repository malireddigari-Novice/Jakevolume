@echo off
REM Jakevolume watchdog — starts main.py and auto-restarts on crash during
REM trading hours. Checks every 5 minutes until 15:15 local time, then exits.

cd /d "C:\Users\malir\Projects\Python\Jakevolume"
set SAMPLE_MODE=false
REM Force UTF-8 for all Python I/O so log lines with non-cp1252 glyphs (e.g. the
REM "->" arrow) don't raise UnicodeEncodeError on the Windows console/log stream.
set PYTHONUTF8=1

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
REM Fix (2) — opening-window event-time flow can fire in production. Conservative:
REM inherits the chain-led live-leadership gate + Gold gate + demand-story filter, so it
REM will not fire directional entries into two-sided opening flow.
set OPENING_SCAN_PRODUCTION_ENABLED=true

REM ── Flow-leadership reversal exit ─────────────────────────────────────────
REM While a position is open, watch the OPPOSITE side of the watched strikes; when it
REM takes control (concentrated opposite volume) exit the trade and re-watch. Guarded
REM against the earlier churn ("penny flips"): requires same-side fading + opposite
REM leadership + opposite-premium EXPANSION (+5% off streak low) + VWAP price confirmation
REM (both confirm layers default-on). Auto-flip into the opposite trade stays OFF.
set FLOW_REVERSAL_ENABLED=true

REM ── Candidate-generation V2R corrections (2026-07-19) — enabled for paper validation ──
REM Fixes the "clean single-strike winner missed" class (GOOGL 370P / NVDA 200C / META 635C):
REM   BACKFILL   — seed a newly-watched contract's history from real OPRA bars, not delta=0
REM                (stops events vanishing when a strike rotates into the window).
REM   PERSISTENT — keep session-active strikes subscribed as spot moves (widens the fetch).
REM   VOLUME_LEADER — standalone single-strike entry when one strike is economically exceptional.
REM   HIST_VALUE_NORMALIZED — compare historical value within the same DTE bucket, not across expiries.
REM   ACTIVATION_FASTPATH — an exceptional COMPLETED-bar event fires now, not after 1-3 bars.
REM   ECONOMIC_LEADERSHIP — $-weighted flow (built; currently used by VOLUME_LEADER only).
REM NOTE: PREMIUM_DISCOVERY_GATE_ENABLED is deliberately NOT set — PDS stays shadow-only until
REM its history coverage is broadened (per the review's point 4); enabling it now would only add
REM inconsistent blocks. Flip it here if you want it on despite that.
set BACKFILL_NEW_CONTRACT_BARS=true
set PERSISTENT_UNIVERSE_ENABLED=true
set VOLUME_LEADER_ENABLED=true
set HIST_VALUE_NORMALIZED_ENABLED=true
set ACTIVATION_FASTPATH_ENABLED=true
set ECONOMIC_LEADERSHIP_ENABLED=true

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
