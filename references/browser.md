# Clawpaw 自己的浏览器控制

后端是 Python Playwright + Chromium CDP，直接控制 Microsoft Edge，不调用 OpenClaw browser 插件。当前接口操作主文档；跨域 iframe、浏览器工具栏、扩展弹窗及下载管理器不在本版元素接口范围内。

网页 click/fill/press 在浏览器内操作元素，不移动系统鼠标或调用 Windows 激活，用户可继续使用其它窗口。连接后避免混用 windows.activate/type_text/press_keys 来操作网页，也不要为网页截图切到前台。用户正在操作同一标签页时，网页内容和 DOM 焦点仍可能受任务影响。多步骤优先复用持久连接，减少重连许可弹窗。

## 连接

用户要求复用已登录 Edge 时，先 `call browser.discover`。它只读取 Edge 的 `DevToolsActivePort` 调试元数据和检查该端口是否响应，不连接网页、不读取 cookies、不改变浏览器设置。

- `available:true`：在当前用户已授权的范围内，先用 `windows.list_windows` 找到当前 Edge HWND，优先 `browser.run_actions` 加 `approve_window`，一次连接内完成网页动作。仅需获取 tabs 时可用 `browser.attach_existing` 加 approve_window（exec 总超时 60 秒）。成功返回原会话 tabs，不启动新浏览器、不改变登录状态。
- `debugging_metadata_unavailable`：获得当前用户对开启调试的授权后，按下方流程，通过 Windows 控制在原 Edge 开启调试后再连接。支持此设置页的 Edge（144+）通常不需要手动提供端口，具体以安装版本及企业策略为准。默认目录为 `%LOCALAPPDATA%\Microsoft\Edge\User Data`，非默认安装可传 `user_data_dir`。
- `debugging_endpoint_unreachable`：可能是旧元数据或浏览器已关闭，检查所选 Edge 是否运行及调试开关。不能把它推断成未登录或权限等级不足。
- 已知调试地址也可以 `browser.attach`，参数 `{"endpoint":"http://127.0.0.1:9222"}`，或浏览器级 `ws://127.0.0.1:PORT/devtools/browser/ID`。只接受本机回环地址；不要求固定 9222。
- `browser.status` 检查已配置端点，不可用时提供 existing_edge 发现结果。WebSocket 的元数据和端口可响应仍不等于获得了浏览器批准，只有成功 attach 才确认连接。
- `browser.launch`（可选 `port`，默认 9222）启动 `%LOCALAPPDATA%\Clawpaw\Edge` 专用 profile。它与原来的 Edge 登录分开，首次使用需要用户自行登录；以后保留登录。调用退出不会关闭它。端口已提供 CDP 时 launch 拒绝重复启动，应明确 `attach`。
- `CLAWPAW_BROWSER_ENDPOINT` 可覆盖配置；用于隔离测试或不同本机浏览器。

成功连接设置保存在 `%LOCALAPPDATA%\Clawpaw\browser.json`。attach_existing 保存选定的 profile 路径，下一次连接重新发现调试端点，适应 Edge 重启后的端口变化。用户指定现有登录会话时先走此流程，按已有授权完成连接准备，不重复问新会话/旧会话二选一。

独立 call 会重连，Edge 可能再次要求批准；多步现有会话任务可以保持 `serve` 管道连接，复用一次已批准的连接。不要承诺批准一次永久免提示。

## 单次调用内连接、批准和执行

仅当当前用户已明确授权自动批准这次 Clawpaw 任务连接时，获取当前真实 Edge HWND 后，write 参数文件，再 `call browser.run_actions --params-file 'C:\absolute\browser-actions.json'`：

```json
{"approve_window":123456,"timeout_ms":30000,"actions":[{"method":"tabs"}]}
```

approve_window 不是网页 tab ID；必须来自当前 `windows.list_windows` 的 Edge 主窗口。它是明确的许可操作选项，工具会验证窗口标题及实际进程为 msedge.exe，在连接发起前准备后台 UIA 监听，及时点击该进程新出现且内容匹配的唯一调试许可。不会移动鼠标或激活窗口。已有许可弹窗、多个新提示、身份或内容不匹配时拒绝自动批准；也不启用调试开关。不要同时运行其它 CDP 连接请求，以免提示归属不清。此选项不会保存到配置或授权后续无关请求。

从返回的 `connection.tabs` / `results[0]` 选取网站的真实 tab。下一次批量读取页面和截图仍传 approve_window，新连接会再次及时批准，以下 tab 必须替换为实际观察到的值：

```json
{"approve_window":123456,"actions":[{"method":"snapshot","params":{"tab":"真实tab"}},{"method":"screenshot","params":{"tab":"真实tab"}}]}
```

批量动作可用 tabs/open/navigate/snapshot/click/fill/press/wait/screenshot，参数与独立方法相同。全部动作先检查签名，只连接一次；结果为 connection、results、completed_actions。动作失败停止，已完成动作不回滚。填写与提交只在当前快照已观察到的目标上执行；提交失败先检查结果，不整批重放。exec 总超时应覆盖连接及整批操作的耗时。

只需建立持久连接时可在 `serve` 调用 `browser.attach_existing`，参数 `{"approve_window":123456,"timeout_ms":30000}`，后续在同一活着的 serve 中读写。独立 call attach 成功后会断开，下一条独立 call snapshot 并不继承批准连接。Python API 同理，在一个 `with Browser()` 内 attach 和操作，不在每步结束 Browser。

## 连接失败恢复

`error.data` 保留 reason、timeout_ms、cause_type、cause 和本次 consent 结果。`connection_timeout` 不等于未开启调试。先核对批准是否及时；若模型分多条工具调用找按钮，极易晚于 30 秒。先停止旧请求，确认并处理本任务旧许可，再用上述单次自动批准流程重试一次。不随意点击已有、来源不明的许可，也不反复开关调试。

`consent.approved:true` 只说明本次按钮调用成功，连接实际错误仍要读 cause；端点已变时重新 discover。已有旧许可导致自动批准 preflight 拒绝时，确认它属于本任务已经退出的请求才取消旧请求；来源不明则附当前截图询问。其它连接失败按实际错误诊断，不能替换成要求用户手动开启的通用提示。

已安装 Edge 不需要 `playwright install` 下载浏览器。依赖缺失时使用项目虚拟环境安装 `clawpaw[browser]`（开发目录为 `pip install -e '.[browser]'`）。

## 经用户授权后：开启调试和批准 Clawpaw 连接

以下步骤涉及调试设置和连接许可，须有当前用户对此范围的明确授权。已有授权时可按其范围继续；复制、安装 skill 或工具可用不代表用户已授权。

1. 用 `windows.list_windows` 确认目标 Edge，检查窗口控件和当前截图。调用 `windows.open_url` 打开调试页，使用下面的真实参数格式，将 window 替换成当前 Edge HWND，写入 UTF-8 参数文件：

   ```json
   {"window":123456,"url":"edge://inspect/#remote-debugging","new_tab":true,"screenshot_after":true}
   ```

   然后 `call windows.open_url --params-file 'C:\absolute\params.json'`。此入口会短暂使用前台键盘，执行前说明占用；只在连接前需要打开调试页时使用，已有该页则读取控件而不重复导航。工具会先验证窗口激活，打开新标签页并返回截图；原任务标签页保留。读取截图验证页面实际内容，`navigation_requested` 不代表调试开关已启用。
2. 读取开关状态；优先 UIA `windows.set_toggle` 设置 checked=true，已经开启时保留现状。页面显示无法确认的提示或不可操作的策略限制时，附当前截图说明卡点。不要猜测坐标、反复切换开关，或修改无关设置来绕过限制。
3. 再次 `browser.discover`，发现有效地址后优先按上文调用 run_actions/attach_existing 并传 approve_window，批准由控制器在同次连接中后台完成。省略该参数的手动备用流程需要在连接仍运行时处理许可，不要等超时后才点击，也不要在顺序 JSONL 中将批准操作排在阻塞的连接后面。
4. 核对提示来自目标 Edge，并与刚由 Clawpaw 发起的连接相对应；若提示提供地址或客户端信息，也要核对。确认属于这次任务连接后，用 `windows.background_click` 调用“允许”按钮。无需激活 Edge 或移动真实鼠标。主窗口树过大或按钮重名时，从 inspect 的 parent_id 链找到此次许可弹窗的 native_window_handle，再对该 HWND inspect，确认提示内容和唯一按钮后调用；具体见 Windows reference。不能把所有新出现的许可弹窗一概批准；来源无法确认时，返回当前截图询问。
5. 读取原连接请求的实际结果，再用 `browser.tabs` / `browser.snapshot` 验证原来的任务页面可访问。看到批准按钮或完成点击不等于连接已经成功。

普通网页元素接口尚未连接时无法控制 Edge 的内部调试页或原生许可弹窗，使用 `windows.*` 的 UIA/截图操作。开启调试、批准连接、登录、验证码及系统提权属于不同操作，应分别按实际任务授权处理；不要从一个步骤推断其它步骤也已获授权。

## 元素操作

1. `browser.tabs` 返回 `tab`、URL、标题。复用目标网站已有标签页，记录 `tab`。
2. 必要时 `browser.open`（`url`）创建新标签页，或 `browser.navigate`（`tab`,`url`）导航。只支持 http/https URL。
3. `browser.snapshot`（`tab`）返回 accessibility `tree`（role/name）和可见 DOM `elements`（id、label、placeholder、test_id 等）。默认截断至 16000 字符和 100 个元素；按需调整 `max_chars`、`max_elements`。
4. 用实际快照中的元素定位：精确匹配 `{"role":"button","name":"提交"}`、`{"label":"消息"}`、`{"placeholder":"搜索"}`、`{"test_id":"send-button"}` 或 `{"id":"prompt-textarea"}`。id 必须来自当前页面，不能猜测。
5. `browser.click` 参数 `tab,selector`；`browser.fill` 增加 `text`（替换输入框或 contenteditable 全部内容）；`browser.press` 增加 `key`（如 `Enter`、`Control+A`）；`browser.wait` 增加 `state`（visible/hidden/attached/detached）。

操作目标必须唯一。重复元素会报错，不自动选择第一个。操作默认自动等待可见、可交互元素，通常无需固定 sleep。`timeout_ms` 为 1–30000；OpenClaw exec 总超时必须覆盖操作和连接时间。

同名目标先检查当前快照的 DOM id / test_id / label 是否能唯一定位。当前版本不支持 `nth`、索引、CSS、容器 scope 或快照引用编号，不得构造这些 selector；也没有 `browser.scroll`。确无唯一元素时可读目标 Edge 的 UIA 或一次当前截图，按 Windows reference 做短暂前台操作，并立即验证是否进入预期预览。重名不等于浏览器连接失败，不要重新开启调试、循环 Shift+Tab/Alt+Tab 或不断尝试旧坐标。

短任务用 `run_actions` 将已确认的输入、提交和结果快照放在一次连接内；较长的生成/导出任务优先保持同一个 serve 或 Python `with Browser()`。等待优先使用实际观察到的状态元素及 `browser.wait`；若长时间生成只能轮询，按页面预计耗时逐渐拉长间隔，只读取小快照，状态有变化时才补截图。每个阶段验证一次关键结果，不每次按键都截全页。

典型填写参数（保存到 JSON 文件再调用 `browser.fill`）：

```json
{"tab":"从 tabs 返回的真实 tab","selector":{"role":"textbox","name":"消息"},"text":"你好"}
```

动作后再次快照验证结果。定位失效时刷新一次快照，用当前目标重试；提交超时先检查是否已提交，不能盲目重发。

`browser.screenshot` 接受 `tab`、可选 `path`、`full_page`，返回 PNG 的绝对路径；它是页面截图，不能等同于下载到网站生成的原始图片。默认保存在调用工作目录的 captures 中。

用户要求把图发到聊天渠道时，按 [media.md](media.md) 发送并核对回执。网页中可见图像和已获取页面截图，都不代表渠道已经收到图片；页面截图应明确标注，不能称为海报原图。

关闭 Python Browser 或 `serve` 只断开客户端，保留用户的浏览器和标签页。持续运行的 Browser 必须在创建它的线程使用。
