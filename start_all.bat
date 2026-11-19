@echo off
setlocal
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe run_all.py
) else (
  python run_all.py
)
endlocal
