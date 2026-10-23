# Clawpaw

面向 OpenClaw 和其他本地 agent 的 **Windows + Edge 自动化 Python 包**，包含可复用的 agent skill。

网页通过自己的 Playwright/CDP 后端读取元素并操作；桌面软件通过 Windows UI Automation
定位控件。优先后台执行，必要时再使用前台窗口、Win32 输入和屏幕截图。

## 能做什么

- **Edge 网页**：复用经用户授权的已登录会话，读取元素、点击、填写、按键、等待、截图。
- **Windows 软件**：枚举窗口、读取 UIA 控件、后台调用按钮、填写输入框、切换复选框。
- **前台控制**：激活窗口并验证结果、鼠标键盘输入、物理像素坐标转换。
- **截图与图片交付**：桌面截图、网页截图、近期图片下载查找和稳定保存。
- **适合 agent**：单次 `call`、常驻 `serve`、JSON-RPC 2.0 / JSONL、工具参数 schema、批量动作。

浏览器控制由 Clawpaw 自己提供，不依赖 OpenClaw browser 插件。本项目是通用工具后端和
skill，尚未绑定某个 OpenClaw 版本的原生插件接口，也不是 MCP server。

## 安装

要求 Windows 10/11、Python 3.10+。桌面控制必须运行在目标用户已解锁的交互桌面会话。

```powershell
git clone https://github.com/wuchenwl/clawpaw.git
cd clawpaw
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[browser,uia]"
& .\.venv\Scripts\clawpaw.exe --version
```

仅需基础截图和鼠标键盘时可省略 extras：`pip install -e .`。浏览器功能使用已安装的
Microsoft Edge，无需额外执行 `playwright install`。项目当前版本为 0.2.5。

## 快速开始

```powershell
$Clawpaw = (Resolve-Path '.\.venv\Scripts\clawpaw.exe').Path
& $Clawpaw call windows.list_windows
& $Clawpaw describe windows.run_actions
& $Clawpaw call browser.discover
```

复杂参数保存为 UTF-8 JSON 文件，避免 Windows PowerShell 5.1 的原生程序引号问题：

```powershell
$paramsPath = Join-Path $env:TEMP 'clawpaw-shot.json'
'{"max_width":1280,"format":"png"}' | Set-Content -LiteralPath $paramsPath -Encoding UTF8
& $Clawpaw call windows.screenshot --params-file $paramsPath
```

`call` 输出一份 JSON-RPC 响应并退出：成功退出码 0，错误退出码 1。`describe METHOD`
只返回指定方法的 schema，不启动桌面或浏览器。`clawpaw tools` 列出全部 schema。

### Edge 浏览器

在目标 Edge 打开 `edge://inspect/#remote-debugging`，按当前 Edge 版本支持的方式启用远程
调试。然后执行 `browser.attach_existing`，并允许这次由 Clawpaw 发起的连接。
该设置页流程适用于支持它的 Edge 版本（144+）；企业策略可能限制调试。

```powershell
& $Clawpaw call browser.discover
& $Clawpaw call browser.attach_existing
```

`discover` 只读取默认 Edge profile 的调试元数据和检查端口；它不读取 cookies、不启用
调试，也不证明连接已获批准。非默认 profile 可传 `user_data_dir`。调试接口只接受本机
回环地址，不要求固定 9222。已有明确授权时，可用 `approve_window` 自动批准本次新连接，
其身份与提示校验见 [浏览器参考](references/browser.md)。仓库不包含任何用户的预授权。

独立 `call` 会断开连接，下一次可能再次需要许可；多步骤任务使用 `browser.run_actions`
或保持一个 `serve` / Python `Browser` 会话。关闭客户端不会关闭用户浏览器或标签页。

`browser.launch` 可启动独立持久 profile，保存在 `%LOCALAPPDATA%\Clawpaw\Edge`，首次需要
用户登录。它不会继承普通 Edge 的登录状态。连接配置保存在
`%LOCALAPPDATA%\Clawpaw\browser.json`，可用 `CLAWPAW_BROWSER_ENDPOINT` 覆盖。

### Python API

```python
from clawpaw import Desktop

with Desktop() as desktop:
    print(desktop.list_windows())
    shot = desktop.screenshot(max_width=1280)
    print(shot["path"], shot["region"])
```

```python
from clawpaw import Browser

# 使用自己已经开启、批准的本机调试地址。
with Browser(endpoint="http://127.0.0.1:9222") as browser:
    print(browser.tabs())
    tab = browser.open("https://example.com")["tab"]
    print(browser.snapshot(tab))
```

两种会话都须在创建它们的线程使用。窗口/网页目标应来自最新的实际观察；不要复用其它
调用的 UIA `element_id`，也不要把标签页顺序当作 tab ID。

### 常驻与批量执行

```powershell
& $Clawpaw serve
```

stdin 每行一个 JSON-RPC 请求，stdout 每行一个响应；使用普通管道而非 PTY。
保持 stdin 开启以复用会话，EOF 正常退出。通知（无 id）不返回响应。

```json
{"jsonrpc":"2.0","id":1,"method":"tools.list"}
{"jsonrpc":"2.0","id":2,"method":"windows.list_windows"}
{"jsonrpc":"2.0","id":3,"method":"windows.screenshot","params":{"max_width":1280}}
```

批量操作使用 `windows.run_actions` / `browser.run_actions`，不使用 JSON-RPC batch 数组。
全部参数签名先检查，执行遇错停止，返回 `completed_actions`；之前的动作不自动回滚。
Windows 后台批量显式设置 `background_only:true`，会预先拒绝鼠标、键盘、激活和焦点动作。
需要前台展示时显式关闭该选项，并验证激活和截图内容。

## 安装 agent skill

将以下内容复制到所用 agent 的技能目录中，以 `clawpaw` 为文件夹名：

```text
clawpaw/
  SKILL.md
  references/
  scripts/find_download.py
```

以使用默认用户技能目录的 OpenClaw 为例，可从仓库根目录执行：

```powershell
$skillDir = Join-Path $env:USERPROFILE '.openclaw\skills\clawpaw'
New-Item -ItemType Directory -Path $skillDir -Force | Out-Null
Copy-Item -LiteralPath '.\SKILL.md' -Destination $skillDir
Copy-Item -LiteralPath '.\references' -Destination $skillDir -Recurse -Force
Copy-Item -LiteralPath '.\scripts' -Destination $skillDir -Recurse -Force
```

已有自定义技能时先自行保留其配置；不要覆盖个人授权备注。wheel 也携带这些文件，位置为
当前 Python 安装数据目录下的 `share/clawpaw`。安装 skill 不会安装 Python 运行环境，需先
完成包安装，并让 agent 解析出真实的可执行文件绝对路径。

skill 的描述支持自动发现；是否自动加载仍取决于 agent 的技能配置。详见
[SKILL.md](SKILL.md)、[Windows 参考](references/windows.md)和[媒体参考](references/media.md)。

## 限制与权限

- 只控制你拥有操作权限的桌面和浏览器；工具可用不表示获得额外授权。登录、验证码和
  其它需要用户接手的步骤由用户完成，不自动提权。
- UIA 支持取决于应用。自绘界面可能只有少量控件，provider 也可能卡住；调用方应设置
  进程总超时。后台语义操作不会暗中退回坐标点击，但软件自身可能改变焦点。
- `windows.screenshot(window=...)` 是可见桌面的矩形裁切，会包含遮挡物，不是离屏捕获。
  二维码等具体内容应在截图后检查；截图不是网页原图。
- Windows 前台激活先尝试原生调用，失败时有限尝试输入线程队列恢复，核验前台 HWND。
  不模拟 Alt 键、不移动鼠标、不修改全局前台锁定设置；安全桌面和权限差异仍可能阻止它。
- 浏览器元素接口限主文档，不含跨域 iframe、扩展、工具栏与下载管理器。目前不支持
  indexed/scoped selector，也没有直接下载事件保存接口。
- 下载助手检查 Downloads 和 Playwright artifacts，可能返回其它任务的图片候选。
  不自动认定最新图片属于当前任务；发送前确认目标、文件及当前渠道回执。
- 不要把调试端点暴露到公网；截图、浏览器 profile、会话日志和下载文件属于运行数据。

## 开发与测试

```powershell
& .\.venv\Scripts\python.exe -m pip install -e ".[dev,browser,uia]"
& .\.venv\Scripts\python.exe -m pytest
& .\.venv\Scripts\python.exe -m ruff check src tests scripts
& .\.venv\Scripts\python.exe -m pip wheel . --no-deps --wheel-dir dist
```

默认测试使用假后端，不移动真实鼠标或控制用户浏览器。可选 Edge 集成测试使用隔离的无头
profile 和本机页面；前台激活测试会短暂创建并切换两个测试窗口，请在允许的桌面会话运行：

```powershell
$env:CLAWPAW_TEST_EDGE = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
& .\.venv\Scripts\python.exe -m pytest tests/test_browser_integration.py
$env:CLAWPAW_TEST_ACTIVATION = '1'
& .\.venv\Scripts\python.exe -m pytest tests/test_activation_integration.py
```

## License

[MIT](LICENSE)。
