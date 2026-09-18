@echo off
REM ===================================================================
REM  Ledger - build a standalone Windows application
REM
REM  Run this from the project root, inside your virtual environment.
REM  Output: dist\Ledger\  -- copy that whole folder to any machine.
REM ===================================================================

echo.
echo Ledger - building standalone application
echo.

REM PyInstaller and tzdata are build-time requirements. tzdata must be
REM installed here even though it is a Windows-conditional dependency,
REM because the spec bundles its data files.
python -m pip install --quiet --upgrade pyinstaller tzdata
if errorlevel 1 goto :failed

REM A stale build\ directory is the most common cause of a build that
REM succeeds but ships the previous version's files.
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

python -m PyInstaller Ledger.spec --noconfirm
if errorlevel 1 goto :failed

echo.
echo Build complete: dist\Ledger\Ledger.exe
echo.
echo Smoke test - this must print paths and find no problems:
echo.
dist\Ledger\Ledger.exe check
echo.
echo If that looked right, copy the whole dist\Ledger folder to the
echo target machine. Put your PDFs in a "pdfs" subfolder, or set
echo LEDGER_PDF_ROOT in a .env file beside Ledger.exe.
echo.
goto :end

:failed
echo.
echo BUILD FAILED - see the output above.
exit /b 1

:end
