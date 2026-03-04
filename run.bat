@echo off
echo Starting AI assistant...

:: go to this folder
cd /d "%~dp0"

:: activate your virtual environment
call venv\Scripts\activate

:: run your Flask app
python app.py

:: keep window open so you can see errors
pause