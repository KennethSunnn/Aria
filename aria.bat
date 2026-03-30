@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

REM ===== ARIA Launch Script =====
REM Uses Tsinghua PyPI mirror to avoid timeouts
set "PIP_MIRROR=-i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

REM Install dependencies
if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" -m pip install %PIP_MIRROR% --default-timeout=300 --retries=10 -r "%~dp0requirements.txt" -q
) else (
    python -m pip install %PIP_MIRROR% --default-timeout=300 --retries=10 -r "%~dp0requirements.txt" -q 2>nul
)

REM Check Tesseract OCR (optional for screen_ocr)
where tesseract >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo [INFO] Tesseract OCR not detected. screen_ocr feature will be unavailable.
    echo Install from: https://github.com/UB-Mannheim/tesseract/wiki
    echo.
)

REM Start web service
if exist "%PYTHON_EXE%" (
    start "" "%PYTHON_EXE%" web_app.py
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        start "" python web_app.py
    ) else (
        powershell -NoProfile -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Python not found. Install Python or create .venv first.','ARIA launch failed')"
        exit /b 1
    )
)

REM Wait for service to start
timeout /t 2 /nobreak >nul
set "BASE_URL=http://127.0.0.1:5000"
set "HEALTH_URL=%BASE_URL%/api/check_api_key"
set /a RETRIES=40

:wait_ready
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri '%HEALTH_URL%' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>nul
if %errorlevel%==0 goto open_app

set /a RETRIES-=1
if %RETRIES% LEQ 0 goto open_anyway
timeout /t 1 /nobreak >nul
goto wait_ready

:open_app
start "" "%BASE_URL%/app"
goto done

:open_anyway
start "" "%BASE_URL%/app"

:done
endlocal
