# 图片附件发送与验收

用于用户要求发图，或 Windows 控制需要附当前截图的回复。只向当前任务授权的收件人
发送；仅诊断图片问题时先核对记录，不额外发送测试消息。

## 飞书 / OpenClaw

本地文件、图片工具预览和回复中的 `MEDIA:` 文本不证明渠道已经发送图片。
优先使用可以核对发送结果的显式附件接口。

1. 使用工具实际返回的绝对路径，确认图片存在、非空，且内容是用户需要的画面。截图
   可放在当前 OpenClaw 工作区 `captures`；不要为了发送放开全盘媒体访问限制。
2. 按当前 OpenClaw `message` schema 调用 `send`，`media` 为本地图片路径。收件人、
   账号、渠道来自可信的当前会话路由，不复制历史 ID。示意参数（替换所有占位值）：

   ```json
   {
     "action": "send",
     "channel": "feishu",
     "target": "当前会话的实际目标",
     "accountId": "当前会话的实际账号",
     "media": "C:\\workspace\\captures\\current.png",
     "message": "这是当前页面截图。"
   }
   ```

   `media` 必须在参数中，而不是写进 message 文本。实际 schema 或自动路由机制不同
   时以当前工具说明为准。
3. 检查发送回执。错误、仅排队或只有上传 key 不等于发送成功；`messageId` 等成功
   回执证明渠道接受消息，不证明用户客户端已经展示。结果不明时先查记录，避免重发。
4. 记录可读时，核对图片消息或实际附件；只有文本卡片或路径不算图片。流式卡片的
   兼容占位内容也不能单独证明客户端版本问题或图片已经上传。
5. 成功发送后按工具约定结束回复，不再用 `MEDIA:` 重发同一图。卡点说明和问题随
   截图发送，没有回执时如实报告失败阶段。

工具不可用时说明缺少附件发送能力；渠道支持时可备用独立一行 `MEDIA:<真实路径>`，
但仍需要验收。权限、上传、文件大小与客户端兼容问题按实际错误分别说明。

## 原图与截图

页面截图包含界面，网站原图是另一份资源。用户要求原图时从网站实际下载入口获取；
只有截图时明确说明，不把截图叫作原图。agent 的 read/image 预览不会向聊天渠道发送。

## 网页图片下载

确认目标图片后进入预览，从新快照里定位唯一下载按钮。同名图片先找 DOM id、test_id
等唯一标识，不默认选第一张。记录点击前时间，点击下载一次，再查文件，不反复下载。

当前没有 `browser.download` 或下载事件保存 API。CDP 下载可能保存在
`%TEMP%\playwright-artifacts-*\<UUID>`，没有扩展名，不一定在 Downloads。
先查这两个已知位置，不递归扫描整个 Temp；Downloads 为空不表示网站禁止下载。

随 skill 分发的 [../scripts/find_download.py](../scripts/find_download.py) 返回近期候选的
绝对路径、格式、尺寸、大小与时间。先解析当前 Python/skill 的实际位置，记录点击前时间：

```powershell
$Python = (Get-Command python -ErrorAction Stop).Source
$Finder = Join-Path $env:USERPROFILE '.openclaw\skills\clawpaw\scripts\find_download.py'
$since = (Get-Date).ToUniversalTime().ToString('o')
# 现在执行已观察到的下载按钮点击，再运行查找。
& $Python -X utf8 $Finder --since $since
```

使用其它 skill 目录或虚拟环境时替换为当前真实路径。不要在后续独立 exec 假设变量仍
存在；将时间与实际路径记录到任务状态。

脚本验证 PNG/JPEG/WebP 文件头与完整性，识别无扩展名图片。不自动认定候选来自本次任务；
多候选结合时间、尺寸和画面判断，尚未下载完成时等待再查。确认后显式保存到稳定位置：

```powershell
& $Python -X utf8 $Finder --source 'C:\download\SELECTED_UUID' --output 'C:\workspace\captures\image.png'
```

收到 `saved.path` 后再发送。扩展名须匹配格式；相同文件可复用，不同内容同名时拒绝覆盖。
保持连接的流程优先在断开前保存，已经断开则及时保存，避免临时文件被清理。

核对目标内容后按上述步骤发送一次，将文件路径与回执写入任务状态。恢复时已有文件且
没有回执，应续做当前用户授权的发送步骤，不重新生成和下载，也不用等待用户催问。
