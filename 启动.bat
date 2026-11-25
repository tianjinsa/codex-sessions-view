@echo off
setlocal
cd /d "%~dp0"
rem 优先使用当前环境的 pythonw；Windows Store Python 也可正常运行。
where pythonw >nul 2>nul
if errorlevel 1 (
    echo [Codex Session Ledger] Python 3 with PyQt6 is required.
    echo Run: python -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "Codex Session Ledger" pythonw main.py %*
endlocal
