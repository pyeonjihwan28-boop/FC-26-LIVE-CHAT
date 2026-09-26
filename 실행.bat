@echo off
chcp 65001 >nul
title FC 26 라이브 채팅
cd /d "%~dp0"

rem ── 처음 한 번: 가상환경 만들고 패키지 설치 ──
if not exist ".venv\Scripts\python.exe" (
    echo [설치] 처음 실행이라 가상환경을 만들고 패키지를 설치합니다. 몇 분 걸립니다...
    python -m venv .venv || py -3.12 -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [오류] Python 3.12 를 찾을 수 없습니다. https://www.python.org 에서 설치하세요.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    nvidia-smi >nul 2>&1 && ".venv\Scripts\python.exe" -m pip install -r requirements-gpu.txt
)

rem ── API 키 ──
if not exist ".env" (
    copy .env.example .env >nul
    echo [설정] .env 파일에 ANTHROPIC_API_KEY 를 넣고 저장한 뒤 이 창에서 아무 키나 누르세요.
    notepad .env
    pause
)

rem ── 실행 (옵션은 그대로 전달: 실행.bat --text 등) ──
".venv\Scripts\python.exe" -X utf8 main.py %*
if errorlevel 1 (
    echo.
    echo [종료] 오류로 멈췄습니다. 위 메시지를 확인하세요.
    pause
)
