@echo off
chcp 65001 >nul
title 币安合约交易机器人 - 一键启动
cd /d "%~dp0"

echo ============================================
echo   币安 U 本位合约量化交易 - 一键启动
echo ============================================
echo.

echo [1/3] 启动交易机器人 bot.py ...
start "Bot" /min python "%~dp0bot.py"

echo [2/3] 等待机器人初始化（约 8 秒）...
timeout /t 8 /nobreak >nul

echo [3/3] 启动可视化看板，自动打开浏览器 ...
echo.
echo 看板地址：http://localhost:8501
echo 关闭此窗口不会影响机器人和看板运行。
echo 如需停止，请在看板内点击"停止程序"。
echo.
python -m streamlit run "%~dp0dashboard.py" --server.headless true --server.port 8501

pause
