@echo off
chcp 65001 >nul
echo =========================================
echo   Настройка Subtitle Improver
echo =========================================
echo.

REM Проверка наличия Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python не найден!
    echo    Установите Python: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo ✅ Python найден
echo.

REM Установка зависимостей
echo 📦 Устанавливаю зависимости...
pip install -r requirements.txt
if errorlevel 1 (
    echo ❌ Ошибка установки зависимостей
    pause
    exit /b 1
)
echo ✅ Зависимости установлены
echo.

REM Проверка ffmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo ⚠️  ffmpeg не найден!
    echo    Скачайте: https://www.gyan.dev/ffmpeg/builds/
    echo    Инструкция: SETUP_WINDOWS.md
    echo.
) else (
    echo ✅ ffmpeg найден
    echo.
)

REM Создание .env файла
if exist .env (
    echo ✅ Файл .env уже существует
) else (
    if exist env.example (
        copy env.example .env >nul
        echo ✅ Создан файл .env из env.example
        echo.
        echo ⚠️  ВАЖНО: Отредактируйте .env файл!
        echo    Замените "your_api_key_here" на ваш настоящий API ключ
        echo.
        choice /C YN /M "Открыть .env в блокноте сейчас"
        if errorlevel 2 goto skip_edit
        if errorlevel 1 notepad .env
:skip_edit
    ) else (
        echo ❌ Файл env.example не найден!
    )
)

echo.
echo =========================================
echo   Настройка завершена!
echo =========================================
echo.
echo Следующий шаг:
echo   python subtitle_improver.py ваше_видео.mp4
echo.
echo Документация:
echo   START_HERE.txt - быстрый старт
echo   SETUP_WINDOWS.md - подробная инструкция
echo.
pause

