@echo off
cd /d "%~dp0"

if not exist venv (
    python -m venv venv
)

call venv\Scripts\activate
pip install flask resend python-dotenv

python app.py

pause