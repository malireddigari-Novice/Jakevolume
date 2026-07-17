@echo off
cd /d "C:\Users\malir\Projects\Python\Jakevolume"
set SAMPLE_MODE=false
REM Force UTF-8 for all Python I/O so log lines with non-cp1252 glyphs (e.g. the
REM "->" arrow) don't raise UnicodeEncodeError on the Windows console/log stream.
set PYTHONUTF8=1
set LOG=jakevolume_startup.log

REM --- Log housekeeping: rotate if over 10 MB, keeping one .old backup ---
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA GTR 10485760 move /Y "%LOG%" "%LOG%.old" >nul

"C:\Python314\python.exe" main.py >> %LOG% 2>&1