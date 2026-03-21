@echo off
REM Launch the monkey tester in conhost.exe (legacy console) so it survives WT hangs.
REM All arguments are forwarded to the Python runner.
REM
REM Usage:
REM   run_monkey.cmd                          -- 5-minute run
REM   run_monkey.cmd --duration 3600          -- 1-hour run
REM   run_monkey.cmd --duration 0             -- run forever
REM   run_monkey.cmd --seed 99               -- reproduce a specific run

cd /d "%~dp0\.."

REM Use the Python 3.13 install that has all dependencies
set PYTHON=C:\Users\randy\AppData\Local\Programs\Python\Python313\python.exe
if not exist "%PYTHON%" set PYTHON=python

start "MonkeyTester" conhost.exe cmd /k "cd /d %~dp0\.. && "%PYTHON%" -m monkey.runner %*"
