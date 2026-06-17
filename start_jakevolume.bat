@echo off
cd /d "C:\Users\malir\Projects\Python\Jakevolume"
set SAMPLE_MODE=false
set LOG=jakevolume_startup.log

REM --- Log housekeeping: rotate if over 10 MB, keeping one .old backup ---
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA GTR 10485760 move /Y "%LOG%" "%LOG%.old" >nul

"C:\Python314\python.exe" main.py >> %LOG% 2>&1