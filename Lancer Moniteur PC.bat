@echo off
setlocal
cd /d "%~dp0"
title Moniteur PC
set "VENV=%~dp0.venv"

rem --- 1) Environnement Python local (cree une seule fois, dans ce dossier) ---
if exist "%VENV%\Scripts\pythonw.exe" goto deps
echo Premier lancement : creation de l'environnement Python...
py -3.11 -m venv "%VENV%" >nul 2>&1
if exist "%VENV%\Scripts\pythonw.exe" goto deps
py -3 -m venv "%VENV%" >nul 2>&1
if exist "%VENV%\Scripts\pythonw.exe" goto deps
python -m venv "%VENV%" >nul 2>&1
if exist "%VENV%\Scripts\pythonw.exe" goto deps
echo.
echo ERREUR : Python 3.10+ introuvable. Installe-le depuis python.org puis relance.
pause
exit /b 1

rem --- 2) Dependances (psutil, anthropic) ---
:deps
"%VENV%\Scripts\python.exe" -c "import psutil, anthropic" >nul 2>&1
if not errorlevel 1 goto config
echo Installation des dependances (une seule fois, ~1 min)...
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo ERREUR : installation des dependances impossible. Verifie ta connexion Internet.
  pause
  exit /b 1
)

rem --- 3) Fichier de config (a remplir soi-meme pour la cle API) ---
:config
if not exist "config.json" copy "config.exemple.json" "config.json" >nul

rem --- 4) Lancement sans fenetre console ---
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0app.py"
exit /b 0
