@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Не найдено окружение .venv
    echo Создайте его: py -3.12 -m venv .venv
    echo Затем: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Панель: http://localhost:8501
".venv\Scripts\python.exe" -m streamlit run app.py
if errorlevel 1 pause
