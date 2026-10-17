@echo off
title TeamTrau AutoEdit Desktop Studio
cd /d "%~dp0"

echo ========================================================
echo   TeamTrau AutoEdit - Desktop Studio Launcher
echo ========================================================
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py gui %*
) else (
    where uv >nul 2>nul
    if %errorlevel% equ 0 (
        uv run python main.py gui %*
    ) else (
        where python >nul 2>nul
        if %errorlevel% equ 0 (
            echo [WARNING] .venv not found. Running with system python...
            python main.py gui %*
        ) else (
            echo [ERROR] Python environment not detected.
            echo Please install Python 3.11+ or initialize .venv.
            pause
            exit /b 1
        )
    )
)

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Application exited with error code %errorlevel%.
    pause
)
