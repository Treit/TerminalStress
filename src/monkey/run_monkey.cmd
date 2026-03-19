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
start "MonkeyTester" conhost.exe cmd /k "cd /d %~dp0\.. && python -m monkey.runner %*"
