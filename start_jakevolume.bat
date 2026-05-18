@echo off
:: Jakevolume daily launcher
:: Called by Windows Task Scheduler at 7:58 AM CST on market days.

cd /d "C:\Users\malir\Projects\Python\Jakevolume"

:: Activate virtual environment if one exists in the project folder
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

echo [%DATE% %TIME%] Jakevolume starting >> startup.log 2>&1
python main.py >> startup.log 2>&1
echo [%DATE% %TIME%] Jakevolume exited with code %ERRORLEVEL% >> startup.log 2>&1
