@echo off
set "BASE=%~dp0"
if exist "%BASE%.venv\Scripts\pythonw.exe" (
  "%BASE%.venv\Scripts\pythonw.exe" "%BASE%discordllm_gui.py"
) else (
  pythonw.exe "%BASE%discordllm_gui.py"
)
