@echo off
REM ============================================================================
REM start-edge-debug.cmd —— 用一个「带调试端口」的 Edge 打开佳明国区
REM ============================================================================
REM 为什么需要这个：Chrome/Edge 的 --remote-debugging-port 只在**进程全新启动**时
REM 才生效。你日常那个 Edge 已经开着，它永远不会开端口 —— 而 fetch.py 接管的就是
REM 这个端口。所以第一次（以及每次它报「拿不到调试端口」时）跑一下本脚本。
REM
REM [warn] 安全提醒：调试端口对本机任意进程**无鉴权**。用完关掉这个窗口，
REM    不要把它做成开机常驻，不要在共享/公司机器上用。
REM ============================================================================

setlocal

set "PORT=%GARMIN_CDP_PORT%"
if "%PORT%"=="" set "PORT=9222"

set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" (
    echo [x] 没找到 msedge.exe。请装 Edge，或手动改本脚本里的 EDGE 路径。
    exit /b 1
)

echo [i] 即将用调试端口 %PORT% 启动 Edge。
echo [i] 已开着的 Edge 窗口需要先全部关闭，否则新实例会并入旧进程、端口不生效。
echo.

start "" "%EDGE%" --remote-debugging-port=%PORT% https://connect.garmin.cn/app/home

echo [i] 已启动。等页面加载完并确认你已登录，再运行：
echo       python scripts/fetch.py --cdp --probe
echo.
echo [i] 抓完记得关掉这个 Edge 窗口（调试端口不要常开）。
endlocal
