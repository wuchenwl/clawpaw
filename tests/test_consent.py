import sys
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from clawpaw.consent import EdgeConsent, approve_prompt


def node(name, kind="TextControl"):
    return {"name": name, "control_type": kind}


def dialog_tree():
    return {
        "truncated": False,
        "nodes": [
            node("是否允许远程调试?"),
            node("某个外部应用希望完全控制此 Microsoft Edge 会话以对其进行调试。"),
            node("允许", "ButtonControl"),
        ],
    }


@pytest.mark.parametrize("failure", ["truncated", "unrecognized", "ambiguous", "cancelled"])
def test_prompt_checks_and_cancellation_never_click(failure):
    tree, stopped, clicks = dialog_tree(), threading.Event(), []
    if failure == "truncated":
        tree["truncated"] = True
    elif failure == "unrecognized":
        tree["nodes"][1] = node("Other permission")
    elif failure == "ambiguous":
        tree["nodes"].append(node("Allow", "ButtonControl"))
    else:
        stopped.set()
    desktop = SimpleNamespace(
        inspect=lambda *_, **__: tree,
        background_click=lambda *args: clicks.append(args),
    )
    if failure == "cancelled":
        assert approve_prompt(desktop, 456, stopped) is None
    else:
        with pytest.raises(RuntimeError):
            approve_prompt(desktop, 456, stopped)
    assert clicks == []


@pytest.fixture
def consent_backend(monkeypatch):
    target = {"handle": 123, "pid": 100, "title": "测试 - Microsoft\u200b Edge"}
    prompt = {"handle": 456, "pid": 100, "title": "是否允许远程调试?"}
    state = {
        "windows": [target],
        "clicks": [],
        "path": "C:/Edge/msedge.exe",
        "clicked": threading.Event(),
    }

    class FakeDesktop:
        def __init__(self):
            self._native = SimpleNamespace(process_path=lambda _: state["path"])

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def list_windows(self):
            return list(state["windows"])

        def inspect(self, *_args, **_kwargs):
            return dialog_tree()

        def background_click(self, hwnd, selector):
            state["clicks"].append((hwnd, selector))
            state["clicked"].set()
            return {"invoked": True}

    monkeypatch.setattr("clawpaw.consent.Desktop", FakeDesktop)
    monkeypatch.setitem(
        sys.modules, "uiautomation", SimpleNamespace(UIAutomationInitializerInThread=nullcontext)
    )
    return state, target, prompt


def test_only_fresh_same_process_prompt_is_approved_in_worker(consent_backend):
    state, target, prompt = consent_backend
    with EdgeConsent(123, 2000) as consent:
        unrelated = {**prompt, "handle": 999, "pid": 999}
        state["windows"] = [target, unrelated, prompt]
        assert state["clicked"].wait(2)
    assert [hwnd for hwnd, _ in state["clicks"]] == [456]
    assert consent.result["approved"] is True


@pytest.mark.parametrize("reason", ["stale", "wrong_process", "wrong_window"])
def test_invalid_consent_scope_blocks_connection_start(consent_backend, reason):
    state, _, prompt = consent_backend
    if reason == "stale":
        state["windows"].append(prompt)
    elif reason == "wrong_process":
        state["path"] = "C:/Other/notepad.exe"
    else:
        state["windows"] = []
    with pytest.raises(RuntimeError), EdgeConsent(123, 2000):
        pytest.fail("Must not start connection")
    assert state["clicks"] == []


def test_multiple_fresh_prompts_are_rejected(consent_backend):
    state, target, prompt = consent_backend
    with EdgeConsent(123, 2000) as consent:
        state["windows"] = [target, prompt, {**prompt, "handle": 789}]
        consent.worker.join(2)
    assert "Multiple" in consent.error
    assert state["clicks"] == []


def test_finished_connection_stops_approval_watcher(consent_backend):
    state, target, prompt = consent_backend
    with EdgeConsent(123, 2000) as consent:
        pass
    state["windows"] = [target, prompt]
    assert not consent.worker.is_alive()
    assert state["clicks"] == []
