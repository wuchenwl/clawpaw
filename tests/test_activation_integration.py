"""Optional foreground and visible-pixel checks on disposable fixture windows."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from clawpaw import Desktop


@pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("CLAWPAW_TEST_ACTIVATION"),
    reason="Set CLAWPAW_TEST_ACTIVATION for disposable Windows activation test",
)
def test_real_window_activation_and_visible_capture(tmp_path):
    fixture = """
import sys, tkinter as tk
from pathlib import Path
root = tk.Tk()
root.title(sys.argv[1])
root.geometry('320x140+30+30')
root.configure(background=sys.argv[2])
tk.Label(root, text=sys.argv[1], background=sys.argv[2], foreground='white').pack(pady=45)
root.update()
Path(sys.argv[3]).write_text('ready')
root.after(20000, root.destroy)
root.mainloop()
"""
    processes = []
    titles = [f"Clawpaw fixture A {os.getpid()}", f"Clawpaw fixture B {os.getpid()}"]
    colors = ["#cc4422", "#2244cc"]
    try:
        for index, title in enumerate(titles):
            ready = tmp_path / f"ready-{index}"
            process = subprocess.Popen(
                [sys.executable, "-c", fixture, title, colors[index], str(ready)],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert process.poll() is None, "Fixture failed to start"
                assert time.monotonic() < deadline, "Fixture startup timed out"
                time.sleep(0.05)
        with Desktop() as desktop:
            windows = desktop.list_windows()
            targets = [next(w for w in windows if w["title"] == title) for title in titles]
            for target in reversed(targets):
                result = desktop.activate(target["handle"])
                assert result["foreground"]
                assert desktop._native.user.GetForegroundWindow() == target["handle"]
                print("Native activation:", result["activation_method"])
            desktop.activate(targets[1]["handle"])
            params = tmp_path / "activation.json"
            params.write_text(json.dumps({"window": targets[0]["handle"]}), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "clawpaw",
                    "call",
                    "windows.activate",
                    "--params-file",
                    str(params),
                ],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            response = json.loads(completed.stdout)
            print("Separate CLI activation:", response["result"]["activation_method"])
            assert desktop._native.user.GetForegroundWindow() == targets[0]["handle"]
            # Inject only the first API rejection; all recovery Win32 calls are real.
            desktop.activate(targets[1]["handle"])
            recover_fixture = """
import json, sys
from clawpaw import Desktop
with Desktop() as desktop:
    original = desktop._native.user.SetForegroundWindow
    calls = 0
    def reject_first(handle):
        global calls
        calls += 1
        return False if calls == 1 else original(handle)
    desktop._native.user.SetForegroundWindow = reject_first
    print(json.dumps(desktop.activate(int(sys.argv[1]))))
"""
            recovered = subprocess.run(
                [sys.executable, "-c", recover_fixture, str(targets[0]["handle"])],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert recovered.returncode == 0, recovered.stdout + recovered.stderr
            assert json.loads(recovered.stdout)["activation_method"] == "attached_input_queue"
            print("Injected rejection, real input-queue recovery: passed")
            assert desktop._native.user.GetForegroundWindow() == targets[0]["handle"]
            capture = desktop.screenshot(
                window=targets[0]["handle"], path=str(tmp_path / "fixture-a.png")
            )
            assert capture["capture_source"] == "visible_desktop"
            with Image.open(Path(capture["path"])) as image:
                dominant = max(
                    image.getcolors(image.width * image.height), key=lambda pair: pair[0]
                )
                assert dominant[1] == (204, 68, 34), "Capture is occluded by another window"
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
