"""Explicit opt-in approval of a fresh Edge CDP prompt, without global input."""

import threading
import time
import unicodedata
from pathlib import Path

from .desktop import Desktop, integer

PROMPT_TITLES = {"是否允许远程调试?", "是否允许远程调试？", "Allow remote debugging?"}


def prompt_windows(windows, pid):
    return [w for w in windows if w["pid"] == pid and w["title"] in PROMPT_TITLES]


def approve_prompt(desktop, window, stopped):
    tree = desktop.inspect(window, max_depth=10, max_nodes=200)
    if tree["truncated"]:
        raise RuntimeError("Edge consent tree is incomplete; inspect the prompt before retrying")
    names = [n["name"] for n in tree["nodes"]]
    if not any(
        "Microsoft Edge" in name and ("外部应用" in name or "external app" in name.lower())
        for name in names
    ):
        raise RuntimeError("Unrecognized Edge consent content; no button was invoked")
    buttons = [
        n
        for n in tree["nodes"]
        if n["control_type"] == "ButtonControl" and n["name"] in {"允许", "Allow"}
    ]
    if len(buttons) != 1:
        raise RuntimeError("Edge consent button is missing or ambiguous; no button was invoked")
    if stopped.is_set():
        return None
    return desktop.background_click(
        window, {"name": buttons[0]["name"], "control_type": "ButtonControl"}
    )


class EdgeConsent:
    """Scoped to one explicit Edge HWND and one connection attempt; never persisted."""

    def __init__(self, window, timeout_ms):
        self.window = integer(window, "approve_window", 1)
        self.timeout_ms = timeout_ms
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.worker = None
        self.result = {"requested": True, "approved": False, "window": window}
        self.error = None

    def __enter__(self):
        self.worker = threading.Thread(target=self._watch, daemon=True)
        self.worker.start()
        if not self.ready.wait(5):
            self.stopped.set()
            raise RuntimeError(
                "Edge consent preflight did not complete; connection was not started"
            )
        if self.error is not None:
            self.stopped.set()
            raise RuntimeError(self.error)
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.worker.join(timeout=2)

    def _watch(self):
        try:
            import uiautomation as uia

            with uia.UIAutomationInitializerInThread(), Desktop() as desktop:
                windows = desktop.list_windows()
                target = next((w for w in windows if w["handle"] == self.window), None)
                title = (
                    ""
                    if target is None
                    else "".join(c for c in target["title"] if unicodedata.category(c) != "Cf")
                )
                if target is None or "Microsoft Edge" not in title:
                    raise ValueError(
                        "approve_window must identify the selected Microsoft Edge window"
                    )
                if Path(desktop._native.process_path(target["pid"])).name.lower() != "msedge.exe":
                    raise ValueError("approve_window process is not Microsoft Edge")
                pid = target["pid"]
                if prompt_windows(windows, pid):
                    raise RuntimeError(
                        "An Edge debugging prompt already exists. Inspect its source and clear "
                        "the stale request before starting another connection; none was approved."
                    )
                self.ready.set()
                deadline = time.monotonic() + self.timeout_ms / 1000
                while not self.stopped.is_set() and time.monotonic() < deadline:
                    windows = desktop.list_windows()
                    if not any(w["handle"] == self.window and w["pid"] == pid for w in windows):
                        raise RuntimeError("Selected Edge window changed during connection")
                    candidates = prompt_windows(windows, pid)
                    if len(candidates) > 1:
                        raise RuntimeError("Multiple Edge connection prompts; none was approved")
                    if candidates:
                        result = approve_prompt(desktop, candidates[0]["handle"], self.stopped)
                        if result is not None:
                            self.result.update(approved=True, dialog=candidates[0]["handle"])
                        return
                    self.stopped.wait(0.15)
        except Exception as error:  # noqa: BLE001 -- report worker/provider errors to caller
            self.error = str(error)
            self.result["error"] = self.error
        finally:
            self.ready.set()
