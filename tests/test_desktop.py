import base64
import ctypes
import io
from types import SimpleNamespace

import pytest

from clawpaw import Desktop
from clawpaw.desktop import BatchError, crop_region
from clawpaw.native import INPUT


class FakeNative:
    dpi_mode = "per_monitor_v2"

    def __init__(self):
        self.events = []
        self.positions = []

    def move(self, x, y):
        self.positions.append((x, y))

    def send(self, events):
        self.events.extend(events)

    def bounds(self):
        return {"left": -7, "top": 0, "width": 7, "height": 5}


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr("clawpaw.desktop.Native", FakeNative)
    with Desktop() as session:
        yield session


def test_negative_monitor_crop():
    bounds = {"left": -1920, "top": 0, "width": 3840, "height": 1080}
    assert crop_region({"left": -2000, "top": 10, "width": 300, "height": 100}, bounds) == {
        "left": -1920,
        "top": 10,
        "width": 220,
        "height": 100,
    }
    with pytest.raises(ValueError):
        crop_region({"left": 3000, "top": 0, "width": 100, "height": 100}, bounds)


def test_unicode_and_surrogate_pairs(desktop):
    desktop.type_text("中👋")
    events = desktop._native.events
    assert [e.ki.wScan for e in events[::2]] == [0x4E2D, 0xD83D, 0xDC4B]
    assert [e.ki.dwFlags for e in events] == [4, 6, 4, 6, 4, 6]


def test_hotkey_release_order_and_extended_flags(desktop):
    desktop.press_keys(["ctrl", "left"])
    assert [(e.ki.wVk, e.ki.dwFlags) for e in desktop._native.events] == [
        (0x11, 0),
        (0x25, 1),
        (0x25, 3),
        (0x11, 2),
    ]


def test_bad_button_does_not_move(desktop):
    with pytest.raises(ValueError):
        desktop.click(x=10, y=20, button="invalid")
    assert not desktop._native.positions


def test_partial_mouse_input_attempts_button_release(desktop, monkeypatch):
    calls = []

    def partial(events):
        calls.append([event.mi.dwFlags for event in events])
        if len(calls) == 1:
            raise OSError("partial input")

    monkeypatch.setattr(desktop._native, "send", partial)
    with pytest.raises(OSError):
        desktop.click(1, 2)
    assert calls == [[2, 4], [4]]


def test_batch_checks_all_method_signatures_first(desktop):
    with pytest.raises(TypeError):
        desktop.run_actions(
            [
                {"method": "click", "params": {"x": 10, "y": 20}},
                {"method": "type_text", "params": {"wrong_name": "test"}},
            ],
            screenshot_after=False,
        )
    assert not desktop._native.positions


def test_batch_stops_and_reports_completed_count(desktop):
    with pytest.raises(BatchError) as error:
        desktop.run_actions(
            [
                {"method": "move", "params": {"x": 10, "y": 20}},
                {"method": "press_keys", "params": {"keys": ["unsupported"]}},
                {"method": "move", "params": {"x": 30, "y": 40}},
            ],
            screenshot_after=False,
        )
    assert error.value.completed_actions == 1
    assert desktop._native.positions == [(10, 20)]


def test_capture_error_preserves_completed_count(desktop, monkeypatch):
    def broken_capture():
        raise OSError("capture failed")

    monkeypatch.setattr(desktop, "screenshot", broken_capture)
    with pytest.raises(BatchError) as error:
        desktop.run_actions([{"method": "wait", "params": {"seconds": 0}}])
    assert error.value.completed_actions == 1


def test_thread_affinity(desktop):
    desktop._thread = -1
    with pytest.raises(RuntimeError):
        desktop.move(1, 2)
    import threading

    desktop._thread = threading.get_ident()


def test_native_input_layout():
    # A missing hardware union member or pointer-sized extra-info breaks SendInput.
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(INPUT) == 40
    else:
        assert ctypes.sizeof(INPUT) == 28


def test_screenshot_resize_mapping_encoding_and_reuse(desktop, monkeypatch, tmp_path):
    import mss
    from PIL import Image

    captures = []

    class FakeCapture:
        def __init__(self):
            self.closed = False
            captures.append(self)

        def grab(self, region):
            size = region["width"], region["height"]
            return SimpleNamespace(size=size, bgra=b"\x00\x00\xff\xff" * (size[0] * size[1]))

        def close(self):
            self.closed = True

    monkeypatch.setattr(mss, "mss", FakeCapture)
    shot = desktop.screenshot(max_width=4, inline=True)
    assert "path" not in shot
    assert shot["region"]["left"] == -7
    assert shot["desktop_pixels_per_image_pixel"] == {"x": 7 / 4, "y": 5 / 3}
    with Image.open(io.BytesIO(base64.b64decode(shot["base64"]))) as image:
        assert image.size == (4, 3)
        assert image.getpixel((0, 0)) == (255, 0, 0)
    destination = tmp_path / "capture.jpg"
    second = desktop.screenshot(format="jpeg", path=str(destination))
    assert "base64" not in second
    assert destination.stat().st_size == second["bytes"]
    assert len(captures) == 1
    desktop.close()
    assert captures[0].closed


def test_open_url_verifies_activation_before_input_and_preserves_existing_tab(desktop, monkeypatch):
    trace = []
    monkeypatch.setattr(
        desktop, "list_windows", lambda: [{"handle": 123, "title": "Microsoft\u200b Edge"}]
    )
    monkeypatch.setattr(desktop, "activate", lambda window: trace.append(("activate", window)))
    monkeypatch.setattr(desktop, "press_keys", lambda keys: trace.append(("keys", keys)))
    monkeypatch.setattr(desktop, "type_text", lambda text: trace.append(("text", text)))
    monkeypatch.setattr(desktop, "wait", lambda seconds: None)
    monkeypatch.setattr(desktop, "screenshot", lambda **_: {"path": "fresh.png"})
    result = desktop.open_url(123, "edge://inspect/#remote-debugging")
    assert trace[0] == ("activate", 123)
    assert trace[1] == ("keys", ["ctrl", "t"])
    assert trace[2] == ("keys", ["ctrl", "l"])
    assert result["screenshot"]["path"] == "fresh.png"
    assert result["navigation_requested"]


def test_open_url_activation_failure_sends_no_keys(desktop, monkeypatch):
    monkeypatch.setattr(
        desktop, "list_windows", lambda: [{"handle": 123, "title": "Microsoft Edge"}]
    )

    def fail(window):
        raise OSError("foreground rejected")

    monkeypatch.setattr(desktop, "activate", fail)
    with pytest.raises(BatchError) as error:
        desktop.open_url(123, "https://example.com", screenshot_after=False)
    assert error.value.completed_actions == 0
    assert desktop._native.events == []


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "file:///C:/test", "https://example.com\nsecret"]
)
def test_open_url_rejects_bad_navigation_before_input(desktop, url):
    with pytest.raises(ValueError):
        desktop.open_url(123, url, screenshot_after=False)
    assert desktop._native.events == []


def test_open_url_rejects_other_apps(desktop, monkeypatch):
    monkeypatch.setattr(desktop, "list_windows", lambda: [{"handle": 123, "title": "Notepad"}])
    with pytest.raises(ValueError, match="browser window"):
        desktop.open_url(123, "https://example.com")
    assert desktop._native.events == []
