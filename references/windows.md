# Windows 软件的元素控制

Windows UI Automation（UIA）暴露按钮、输入框、菜单等控件的 name、AutomationId、ControlType 和矩形；具体能力取决于软件。Win32/WPF/WinUI 常能提供控件树，Electron 视 accessibility 实现而定，自绘画布及游戏可能只提供一个大面板。

先 `windows.list_windows` 找到目标 HWND，再 `windows.inspect`，参数例如 `{"window":123456,"max_depth":6,"max_nodes":500}`。根据实际控件组合 selector：

```json
{"window":123456,"selector":{"name":"保存","control_type":"ButtonControl"}}
```

- `windows.background_click`：后台按钮点击，通过 InvokePattern 实现，不调用 activate、SetFocus 或全局鼠标输入；不支持时明确报错，没有隐藏的坐标点击降级。`windows.invoke` 是同一语义操作。
- `windows.set_value`：增加 `text`，通过 ValuePattern 替换输入框的值；只读或不支持 ValuePattern 时会报错。不向当前焦点盲打。
- `windows.set_toggle`：增加 `checked:true/false`，通过 TogglePattern 设置复选框。已经符合目标状态时不切换；无法确定状态或未确认结果时停止，避免盲目重复切换。
- `windows.focus`：将键盘焦点给目标控件，会影响用户输入，后台流程避免使用。

这些接口每次重新查找控件，可以独立 `call`，无需旧 element_id。精确匹配、只接受唯一且启用的可见控件。搜索不完整时拒绝操作，增加 `max_depth`（最大 10）/`max_nodes`（最大 2000）后重新检查。默认语义搜索是 10/2000，复杂窗口可能仍达上限。UIA provider 可能卡住，本版未提供 provider 的硬超时，应由 exec 总超时限制进程。

`inspect` 节点提供 `native_window_handle`，0 表示该控件没有独立 HWND。复杂应用弹窗中的按钮重名或整体树截断时，沿 `parent_id` 找到所属弹窗的非零 HWND，用它作为 window 再 inspect 并执行后台点击；先验证弹窗内容与目标应用/本次任务一致。不要按按钮顺序选择第一个，或因矩形相同就把不同弹窗视为同一个。

Invoke/Value/TogglePattern 不支持时先检查可用的语义接口；确需前台输入时按 skill 的前台占用规则处理。不要用 PostMessage/WM_LBUTTON 模拟普遍可用的后台点击，Chromium 和自绘软件常不会正确处理。`windows.click(element_id=...)` 会移动真实鼠标到矩形中心，并不是 Invoke；ID 会在下一次成功 inspect 或会话关闭后失效，不能从前一次独立 call 复用。后台操作不依赖目标窗口在前台，但软件自身可能激活窗口；不能承诺对所有软件、最小化或锁屏状态可用。

坐标或键盘操作前 `windows.activate`，它只接受 `{"window":123456}`；不可混入 inspect 的 max_depth/max_nodes。成功验证真实前台 HWND 并返回 `foreground:true`；失败返回 `foreground_not_changed` 时，不向未知焦点输入。Windows 前台限制可能拒绝同权限进程，不能由此认定需要管理员权限。`-32602` 是参数错误。

激活入口先还原最小化窗口、尝试 SetForegroundWindow；没有切换成功时，控制器有限地尝试一次临时输入线程队列连接和 BringWindowToTop，再核验前台 HWND，并解除线程连接。此恢复不移动鼠标、不发送 Alt/Alt+Tab、不修改全局前台锁定设置、不提权。恢复后仍失败则返回实际前台和 recovery 诊断，agent 不必重写 Win32 脚本或反复调用同一激活。

用户要显示软件，或者截取被遮挡的二维码/特定页面时，可在既有任务范围内短暂切到前台；不用让用户先手动点任务栏。确认真实 HWND 后，用以下批量格式（替换窗口及实际输出路径），这里必须 `background_only:false`：

```json
{"actions":[{"method":"activate","params":{"window":123456}},{"method":"wait","params":{"seconds":0.15}}],"background_only":false,"screenshot_after":true,"screenshot_options":{"window":123456,"path":"C:\\实际工作区\\captures\\target-current.png"}}
```

读取返回图片，确认软件/二维码实际可见后才按 media reference 发图。run_actions 在 activate 失败时不会执行末尾截图；此时另取当前桌面截图，把具体激活错误和当前画面一起说明，再按需要请求用户协助。

激活失败后不要用 Alt+Tab 猜目标或继续发全局按键；先取当前截图，检查是否还有后台语义路径。坐标点击使用 `桌面坐标 = 截图区域原点 + 图片坐标 × desktop_pixels_per_image_pixel`，操作后验证实际页面变化；同一坐标无效后刷新证据，避免连续盲点。读取复杂应用时优先已确认的弹窗 HWND 和有限节点，再按需要扩展，而非每轮检查整个桌面。

`windows.run_actions` 支持顺序执行 activate/move/click/type_text/press_keys/scroll/wait/invoke/background_click/set_value/set_toggle/focus，可在末尾只截图一次。失败时 `completed_actions` 是已完成动作数，之前动作不回滚；失败动作也可能部分执行。

每个批量动作必须是 `{"method":"方法名","params":{...}}`；顶层只有 actions、screenshot_after、screenshot_options、background_only。窗口参数属于具体方法，不是 run_actions 的顶层参数。默认选用 `background_only:true`，整批在开始前只允许 wait/invoke/background_click/set_value/set_toggle，避免后半段意外抢焦点。该参数工具层默认 false 以兼容旧代码，agent 应显式开启。正确示例（替换 HWND 和当前观察到的控件后保存为参数文件）：

```json
{"actions":[{"method":"set_value","params":{"window":123456,"selector":{"automation_id":"SearchBox"},"text":"关键词"}},{"method":"background_click","params":{"window":123456,"selector":{"name":"搜索","control_type":"ButtonControl"}}}],"background_only":true,"screenshot_after":true,"screenshot_options":{"window":123456,"max_width":1280}}
```

浏览器连接后用 `browser.navigate/open` 后台打开地址。连接前确需前台打开调试页时才使用 `windows.open_url`，参数 window、url，默认 new_tab=true、screenshot_after=true。它会占用前台和键盘，仅接受明确识别的浏览器窗口与 http/https 或支持的调试页地址；焦点激活失败时停止，避免向其它应用输入。返回截图用于验证导航结果。

`-32602` 参数错误应先查看 error.data.input_schema 或 `clawpaw describe METHOD` 修正；不能仅因此判断 Windows 控制不可用并让用户接手。不存在的命令行标志也应按 `call --help` 修正，参数文件使用 `--params-file`。

`windows.screenshot` 可选 `window` 或 `region`（互斥），`max_width`、PNG/JPEG 等。根据 `region` 原点和 `desktop_pixels_per_image_pixel` 把缩小截图坐标换算回物理桌面像素；负坐标可能属于主屏左边的副屏。

`capture_source:visible_desktop` 表示真实屏幕像素，`window` 只决定裁切矩形，不激活窗口，也不捕获被遮挡软件的内部渲染层。指定窗口时返回 `requested_window` 和 `occlusion_possible:true`；即使窗口已在前台仍需检查遮挡/弹窗。截图出现其它软件时先按遮挡/位置排查，不能未经证据归因于 Chromium、QQ 版本或权限。

## 不确定状态与询问用户

Windows 控制中无法判断当前界面、目标控件或操作结果，或需要用户决定下一步时，先暂停会改变界面的操作，获取当前截图，再把截图与卡点说明/问题一起返回给当前用户。

1. 目标窗口明确且截图能够反映卡点时，调用 `windows.screenshot`，指定该窗口。目标不明、激活失败，或遮挡/弹窗需要桌面上下文时，截当前桌面；窗口截图失败后可尝试桌面截图。不要为了截图盲目切换焦点、点击或关闭弹窗。
2. 使用最后一次操作之后新获取的截图。优先一次清晰截图，可按需设置 `max_width`；不要仅用控件树或历史截图代替当前画面。
3. 将返回的绝对 `path` 作为图片附件或内嵌图片随回复展示，发送前读 [media.md](media.md)。飞书优先使用 `message` 显式发送附件并检查回执；其他支持媒体指令的渠道可独立一行写 `MEDIA:<实际绝对路径>`，不要放进代码块。应使用截图工具实际返回的路径，并按渠道的本地媒体路径规则保存截图。只在回复中写路径或声称“已截图”不满足返回截图的要求。
4. 在同一条回复中简要说明无法确定的地方，再提出具体问题。截图获取失败时说明实际错误；已获取但渠道无法展示时提供可用文件附件/链接并说明限制。不得伪造截图或声称用户已看到图片。

UIA 是可选依赖 `clawpaw[uia]`，使用前确认当前 Python 环境已安装。键盘输入作用于当前焦点，使用 Unicode SendInput，不占用剪贴板。不自动提权；锁屏、安全桌面及权限等级不同都可能限制操作。
