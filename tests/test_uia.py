import sys
from types import SimpleNamespace

import pytest

from clawpaw import Desktop


class Pattern:
    IsReadOnly = False
    ToggleState = 0

    def __init__(self):
        self.calls = []
        self.success = True

    def Invoke(self, waitTime):
        self.calls.append(("invoke", waitTime))
        return self.success

    def SetValue(self, value, waitTime):
        self.calls.append(("value", value, waitTime))
        return self.success

    def Toggle(self, waitTime):
        self.calls.append(("toggle", waitTime))
        if self.success:
            self.ToggleState = 1 - self.ToggleState
        return self.success


class Control:
    def __init__(self, name, children=()):
        self.Name = name
        self.AutomationId = name
        self.NativeWindowHandle = 0
        self.ControlTypeName = "ButtonControl"
        self.IsEnabled = True
        self.IsOffscreen = False
        self.BoundingRectangle = SimpleNamespace(left=0, top=0, right=100, bottom=20)
        self.children = children
        self.pattern = Pattern()
        self.focused = False

    def GetChildren(self):
        return self.children

    def GetPattern(self, _):
        return self.pattern

    def SetFocus(self):
        self.focused = True
        return True


@pytest.fixture
def uia_desktop(monkeypatch):
    target = Control("发送")
    root = Control("窗口", [target])
    native = SimpleNamespace(window_rect=lambda _: {})
    monkeypatch.setattr("clawpaw.desktop.Native", lambda: native)
    monkeypatch.setitem(
        sys.modules,
        "uiautomation",
        SimpleNamespace(
            ControlFromHandle=lambda _: root,
            PatternId=SimpleNamespace(InvokePattern=1, ValuePattern=2, TogglePattern=3),
        ),
    )
    with Desktop() as desktop:
        yield desktop, root, target


def test_semantic_actions_resolve_fresh_without_cached_ids(uia_desktop):
    desktop, _root, target = uia_desktop
    desktop.invoke(123, {"name": "发送"})
    desktop.close()  # A subsequent call needs no cached inspect ID.
    desktop.set_value(123, {"automation_id": "发送"}, "中文👋")
    desktop.focus(123, {"name": "发送"})
    assert target.pattern.calls == [("invoke", 0), ("value", "中文👋", 0)]
    assert target.focused


def test_ambiguous_and_truncated_tree_never_act(uia_desktop):
    desktop, root, target = uia_desktop
    root.children.append(Control("发送"))
    with pytest.raises(ValueError, match="ambiguous"):
        desktop.invoke(123, {"name": "发送"})
    root.children.pop()
    target.children = [Control("nested")]
    with pytest.raises(ValueError, match="truncated"):
        desktop.invoke(123, {"name": "发送"}, max_depth=1)
    assert not target.pattern.calls


@pytest.mark.parametrize("reason", ["disabled", "offscreen", "read_only", "unsupported", "failed"])
def test_uia_rejections_do_not_claim_success(uia_desktop, reason):
    desktop, _, target = uia_desktop
    if reason == "disabled":
        target.IsEnabled = False
    elif reason == "offscreen":
        target.IsOffscreen = True
    elif reason == "read_only":
        target.pattern.IsReadOnly = True
    elif reason == "unsupported":
        target.pattern = None
    else:
        target.pattern.success = False
    with pytest.raises((ValueError, RuntimeError)):
        desktop.set_value(123, {"name": "发送"}, "test")


def test_missing_selector_and_batch_uia(uia_desktop):
    desktop, _, target = uia_desktop
    with pytest.raises(ValueError, match="No matching"):
        desktop.invoke(123, {"name": "missing"})
    with pytest.raises(ValueError, match="selector requires"):
        desktop.invoke(123, {})
    result = desktop.run_actions(
        [
            {"method": "invoke", "params": {"window": 123, "selector": {"name": "发送"}}},
        ],
        screenshot_after=False,
    )
    assert result["completed_actions"] == 1
    assert target.pattern.calls == [("invoke", 0)]


def test_background_batch_never_uses_global_input_or_focus(uia_desktop):
    # The fake native backend has no mouse, keyboard, or activate methods.
    desktop, _, target = uia_desktop
    result = desktop.run_actions(
        [
            {"method": "background_click", "params": {"window": 123, "selector": {"name": "发送"}}},
            {
                "method": "set_value",
                "params": {"window": 123, "selector": {"name": "发送"}, "text": "后台"},
            },
        ],
        background_only=True,
        screenshot_after=False,
    )
    assert result["completed_actions"] == 2
    assert result["results"][0]["input_mode"] == "uia_invoke"
    assert target.pattern.calls == [("invoke", 0), ("value", "后台", 0)]
    assert not target.focused


@pytest.mark.parametrize(
    "method", ["activate", "move", "click", "type_text", "press_keys", "scroll", "focus"]
)
def test_background_batch_rejects_entire_plan_before_acting(uia_desktop, method):
    desktop, _, target = uia_desktop
    with pytest.raises(ValueError, match="background_only rejects"):
        desktop.run_actions(
            [
                {
                    "method": "background_click",
                    "params": {"window": 123, "selector": {"name": "发送"}},
                },
                {"method": "windows." + method},
            ],
            background_only=True,
            screenshot_after=False,
        )
    assert not target.pattern.calls
    assert not target.focused


def test_background_click_unsupported_never_falls_back(uia_desktop):
    desktop, _, target = uia_desktop
    target.pattern = None
    with pytest.raises(ValueError, match="InvokePattern"):
        desktop.background_click(123, {"name": "发送"})
    assert not target.focused


def test_toggle_is_idempotent_and_verified(uia_desktop):
    desktop, _, target = uia_desktop
    selector = {"name": "发送"}
    assert desktop.set_toggle(123, selector, True)["changed"]
    assert not desktop.set_toggle(123, selector, True)["changed"]
    assert desktop.set_toggle(123, selector, False)["changed"]
    assert target.pattern.calls == [("toggle", 0), ("toggle", 0)]
    assert not target.focused


def test_indeterminate_and_unconfirmed_toggle_never_retry(uia_desktop, monkeypatch):
    desktop, _, target = uia_desktop
    target.pattern.ToggleState = 2
    with pytest.raises(ValueError, match="indeterminate"):
        desktop.set_toggle(123, {"name": "发送"}, True)
    assert not target.pattern.calls
    target.pattern.ToggleState = 0
    calls = []
    monkeypatch.setattr(target.pattern, "Toggle", lambda waitTime: calls.append(waitTime) or True)
    with pytest.raises(RuntimeError, match="not confirmed"):
        desktop.set_toggle(123, {"name": "发送"}, True)
    assert calls == [0]


def test_inspect_exposes_dialog_handle_for_narrow_search(uia_desktop, monkeypatch):
    desktop, root, target = uia_desktop
    target.NativeWindowHandle = 456
    inspected = desktop.inspect(123)
    assert inspected["nodes"][1]["native_window_handle"] == 456
    # A complex app has two identically named buttons; a dialog HWND scopes one.
    root.children.append(Control("发送"))
    import uiautomation as uia

    monkeypatch.setattr(uia, "ControlFromHandle", lambda hwnd: target if hwnd == 456 else root)
    desktop.background_click(456, {"name": "发送"})
    assert target.pattern.calls == [("invoke", 0)]
    assert not root.children[1].pattern.calls
