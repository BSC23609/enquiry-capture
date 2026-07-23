@echo off
REM ===================================================================
REM  push.bat  -  initialise the repo and push to GitHub
REM
REM  Usage:  double-click, or run from a command prompt in this folder.
REM  Safe to run repeatedly - after the first push it just commits and
REM  pushes whatever has changed.
REM ===================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ================================================
echo   Enquiry Capture - push to GitHub
echo ================================================
echo.

REM ---------- 1. is git installed? ----------
where git >nul 2>nul
if errorlevel 1 (
    echo [X] Git is not installed, or not on PATH.
    echo.
    echo     Install it from https://git-scm.com/download/win
    echo     Accept all the defaults. Then close this window,
    echo     open a NEW one, and run push.bat again.
    echo.
    pause
    exit /b 1
)
echo [OK] Git found

REM ---------- 2. the package marker must exist ----------
if not exist "app\__init__.py" (
    echo [..] Creating app\__init__.py
    type nul > "app\__init__.py"
)
echo [OK] app\__init__.py present

REM ---------- 3. .gitignore must exist BEFORE any git add ----------
if not exist ".gitignore" (
    echo [X] .gitignore is missing. Refusing to continue.
    echo     Without it, your .env secrets could be committed.
    echo.
    pause
    exit /b 1
)
echo [OK] .gitignore present

REM ---------- 4. init if needed ----------
if not exist ".git" (
    echo [..] Initialising repository
    git init -q
    git branch -M main
) else (
    echo [OK] Repository already initialised
)

REM ---------- 5. remote ----------
git remote get-url origin >nul 2>nul
if errorlevel 1 (
    echo.
    echo No remote configured yet.
    echo.
    echo Create a PRIVATE repo on github.com first, then paste its URL below.
    echo Example:  https://github.com/your-org/enquiry-capture.git
    echo.
    set /p REPOURL="Repository URL: "
    if "!REPOURL!"=="" (
        echo [X] No URL given. Aborting.
        pause
        exit /b 1
    )
    git remote add origin "!REPOURL!"
    echo [OK] Remote added
) else (
    for /f "delims=" %%u in ('git remote get-url origin') do set EXISTING=%%u
    echo [OK] Remote: !EXISTING!
)

REM ---------- 6. stage ----------
echo.
echo [..] Staging files
git add -A

REM ---------- 7. SAFETY: refuse to commit .env ----------
git diff --cached --name-only > "%TEMP%\ec_staged.txt"
findstr /X /C:".env" "%TEMP%\ec_staged.txt" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo ============================================================
    echo  [X] STOPPED - .env is staged for commit.
    echo.
    echo  That file holds your Azure client secret, Anthropic key
    echo  and database password. It must never reach GitHub.
    echo.
    echo  Fixing it now with:  git rm --cached .env
    echo ============================================================
    git rm --cached .env -q
    echo.
    echo  [OK] Removed from staging. Verify .gitignore contains a
    echo       line reading exactly:  .env
    echo.
    pause
)
del "%TEMP%\ec_staged.txt" >nul 2>nul

REM ---------- 8. show what is going up ----------
echo.
echo ------------------------------------------------
echo  Files to be committed:
echo ------------------------------------------------
git diff --cached --name-only
echo ------------------------------------------------
echo.
echo  Check the list above. If you see .env anywhere,
echo  press Ctrl+C now and tell someone.
echo.
pause

REM ---------- 9. commit ----------
set /p MSG="Commit message (Enter for default): "
if "!MSG!"=="" set MSG=Enquiry capture: Graph + Claude + Postgres

git diff --cached --quiet
if not errorlevel 1 (
    echo [OK] Nothing changed - skipping commit
) else (
    git commit -q -m "!MSG!"
    echo [OK] Committed
)

REM ---------- 10. push ----------
echo.
echo [..] Pushing to GitHub
echo      A browser or credential prompt may appear - sign in there.
echo.
git push -u origin main
if errorlevel 1 (
    echo.
    echo [X] Push failed. The usual causes:
    echo.
    echo     - Repo URL is wrong, or the repo does not exist yet
    echo     - Not signed in. Install GitHub CLI ^(cli.github.com^)
    echo       and run:  gh auth login
    echo     - Remote has commits you do not have. Run:
    echo         git pull --rebase origin main
    echo       then run push.bat again
    echo.
    pause
    exit /b 1
)

echo.
echo ================================================
echo   [OK] Pushed successfully
echo ================================================
echo.
echo  Next, on github.com:
echo.
echo   1. Settings - Secrets and variables - Actions
echo      Add these SECRETS:
echo        MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET,
echo        ANTHROPIC_API_KEY, DATABASE_URL
echo.
echo      Add these VARIABLES:
echo        MAILBOX, INTERNAL_DOMAINS,
echo        LOOKBACK_DAYS, MAX_MESSAGES_PER_RUN
echo.
echo   2. Actions tab - Sync enquiries - Run workflow
echo.
echo  Full details are in SETUP-GITHUB.md
echo.
pause
endlocal
