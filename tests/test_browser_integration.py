"""Optional end-to-end test on isolated, headless Edge and a local fixture page."""

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from clawpaw import Browser
from clawpaw.browser import discover_edge


@pytest.mark.skipif(
    not os.environ.get("CLAWPAW_TEST_EDGE"), reason="Set CLAWPAW_TEST_EDGE for isolated Edge test"
)
def test_browser_real_elements_unicode_duplicate_refusal_and_cli_reconnect(tmp_path):
    pytest.importorskip("playwright.sync_api")
    html = """<!doctype html><html lang="zh"><meta charset="utf-8"><body>
      <h1>浏览器控制测试</h1><label for="message">消息</label><input id="message">
      <div id="prompt-textarea" role="textbox" contenteditable="true"></div>
      <button onclick="document.querySelector('#result').textContent =
        document.querySelector('#message').value">提交</button><p id="result"></p>
      <button>重复</button><button>重复</button>
      <button id="later" style="display:none">准备完成</button>
      <button onclick="setTimeout(() => document.querySelector('#later').style.display='block',100)">
        开始等待</button></body></html>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    profile = tmp_path / "profile"
    endpoint = None
    process = subprocess.Popen(
        [
            os.environ["CLAWPAW_TEST_EDGE"],
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={tmp_path / 'profile'}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                discovered = discover_edge(str(profile))
                if discovered["available"]:
                    endpoint = discovered["endpoint"]
                    break
            except OSError:
                discovered = {"available": False}
            if time.monotonic() > deadline:
                pytest.fail("Isolated headless Edge CDP failed to start")
            time.sleep(0.1)
        with Browser(endpoint=endpoint, config_path=tmp_path / "browser.json") as browser:
            browser.attach_existing(user_data_dir=str(profile))
            tab = browser.open(f"http://127.0.0.1:{server.server_port}")["tab"]
            snapshot = browser.snapshot(tab)
            assert "浏览器控制测试" in snapshot["tree"]
            assert any(e["id"] == "prompt-textarea" for e in snapshot["elements"])
            browser.fill(tab, {"label": "消息"}, "秋天奶茶👋")
            browser.fill(tab, {"id": "prompt-textarea"}, "中文编辑器👋")
            browser.press(tab, {"label": "消息"}, "End")
            browser.click(tab, {"role": "button", "name": "提交"})
            assert "秋天奶茶👋" in browser.snapshot(tab)["tree"]
            with pytest.raises(Exception, match="strict mode|ambiguous"):
                browser.click(tab, {"role": "button", "name": "重复"}, timeout_ms=1000)
            browser.click(tab, {"role": "button", "name": "开始等待"})
            browser.wait(tab, {"role": "button", "name": "准备完成"}, timeout_ms=2000)
            shot = browser.screenshot(tab, path=str(tmp_path / "page.png"))
            assert Path(shot["path"]).read_bytes().startswith(b"\x89PNG")
        # Batch attaches once and preserves ordering across multiple page actions.
        with Browser(endpoint=endpoint, config_path=tmp_path / "browser.json") as browser:
            batch = browser.run_actions(
                [
                    {
                        "method": "fill",
                        "params": {
                            "tab": tab,
                            "selector": {"label": "消息"},
                            "text": "批量后台读取",
                        },
                    },
                    {
                        "method": "click",
                        "params": {"tab": tab, "selector": {"role": "button", "name": "提交"}},
                    },
                    {"method": "snapshot", "params": {"tab": tab}},
                ],
                user_data_dir=str(profile),
            )
            assert batch["completed_actions"] == 3
            assert "批量后台读取" in batch["results"][2]["tree"]
        # Separate CLI process reconnects to the same tab after the first client closes.
        params = tmp_path / "params.json"
        params.write_text(json.dumps({"tab": tab}), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "clawpaw",
                "call",
                "browser.snapshot",
                "--params-file",
                str(params),
            ],
            env={**os.environ, "CLAWPAW_BROWSER_ENDPOINT": endpoint},
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "批量后台读取" in json.loads(result.stdout)["result"]["tree"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        # Only close the isolated test browser, never the user's existing Edge.
        from playwright.sync_api import Error, sync_playwright

        # Edge may drop the transport while processing Browser.close.
        if endpoint is not None:
            with suppress(Error), sync_playwright() as playwright:
                isolated = playwright.chromium.connect_over_cdp(endpoint, timeout=3000)
                isolated.new_browser_cdp_session().send("Browser.close")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
