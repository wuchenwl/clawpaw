"""Single-thread desktop session. Coordinates always use physical desktop pixels."""

import base64
import inspect as signatures
import io
import math
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .native import KEYS, Native, keyboard_input, mouse_input


def integer(value, name, minimum=None, maximum=None):
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} is outside the allowed range")
    return value


def crop_region(region, bounds):
    if not isinstance(region, dict) or set(region) != {"left", "top", "width", "height"}:
        raise ValueError("region requires left, top, width, height")
    for key, value in region.items():
        integer(value, key, 1 if key in ("width", "height") else None)
    left, top = max(region["left"], bounds["left"]), max(region["top"], bounds["top"])
    right = min(region["left"] + region["width"], bounds["left"] + bounds["width"])
    bottom = min(region["top"] + region["height"], bounds["top"] + bounds["height"])
    if right <= left or bottom <= top:
        raise ValueError("Capture region does not intersect the desktop.")
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


class BatchError(RuntimeError):
    def __init__(self, index, cause):
        super().__init__(f"Action {index} failed after {index} completed actions: {cause}")
        self.completed_actions = index
        details = getattr(cause, "data", {})
        self.data = {**(details if isinstance(details, dict) else {}), "completed_actions": index}


class BackgroundActionError(ValueError):
    def __init__(self, method):
        super().__init__(
            f"background_only rejects foreground action: {method}; "
            "use UIA semantic actions or browser element actions"
        )
        self.method = method


class Desktop:
    """Reuse a session on one thread; use a separate session for another thread."""

    def __init__(self):
        self._native = Native()
        self._thread = threading.get_ident()
        self._capture = None
        self._elements = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check_thread(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError("Use this Desktop on its creating thread.")

    def close(self):
        self._check_thread()
        if self._capture is not None:
            self._capture.close()
            self._capture = None
        self._elements.clear()

    def list_windows(self):
        self._check_thread()
        return self._native.list_windows()

    def activate(self, window):
        self._check_thread()
        details = self._native.activate(window) or {}
        return {"window": window, "foreground": True, **details}

    def open_url(self, window, url, new_tab=True, screenshot_after=True):
        """Navigate an explicitly selected browser window before CDP is available."""
        self._check_thread()
        integer(window, "window", 1)
        if not isinstance(new_tab, bool) or not isinstance(screenshot_after, bool):
            raise TypeError("new_tab and screenshot_after must be booleans")
        if not isinstance(url, str) or not url or len(url) > 10000:
            raise ValueError("url must be a nonempty string of at most 10000 characters")
        if any(ord(char) < 32 for char in url):
            raise ValueError("url must not contain control characters")
        parsed = urlparse(url)
        if url not in {
            "edge://inspect/#remote-debugging",
            "chrome://inspect/#remote-debugging",
        } and (parsed.scheme not in {"http", "https"} or not parsed.hostname):
            raise ValueError("Use an http/https URL or a supported browser remote-debugging page")
        target = next((item for item in self.list_windows() if item["handle"] == window), None)
        title = (
            ""
            if target is None
            else "".join(char for char in target["title"] if unicodedata.category(char) != "Cf")
        )
        if target is None or not any(
            name in title for name in ("Microsoft Edge", "Google Chrome", "Chromium", "Brave")
        ):
            raise ValueError(
                "Choose an explicitly identified browser window from windows.list_windows"
            )
        actions = [{"method": "activate", "params": {"window": window}}]
        if new_tab:
            actions.append({"method": "press_keys", "params": {"keys": ["ctrl", "t"]}})
            actions.append({"method": "wait", "params": {"seconds": 0.4}})
        actions.extend(
            [
                {"method": "press_keys", "params": {"keys": ["ctrl", "l"]}},
                {"method": "wait", "params": {"seconds": 0.15}},
                {"method": "type_text", "params": {"text": url}},
                {"method": "wait", "params": {"seconds": 0.2}},
                {"method": "press_keys", "params": {"keys": ["enter"]}},
                {"method": "wait", "params": {"seconds": 0.8}},
            ]
        )
        result = self.run_actions(
            actions,
            screenshot_after=screenshot_after,
            screenshot_options={"window": window, "max_width": 1280},
        )
        return {
            **result,
            "window": window,
            "url": url,
            "new_tab": new_tab,
            "navigation_requested": True,
        }

    def screenshot(
        self,
        window=None,
        region=None,
        max_width=None,
        format="png",
        quality=80,
        path=None,
        inline=False,
    ):
        """Capture visible desktop pixels, optionally crop/resize and encode once."""
        self._check_thread()
        if window is not None and region is not None:
            raise ValueError("Choose window or region.")
        if format not in ("png", "jpeg"):
            raise ValueError("format must be png or jpeg")
        integer(quality, "quality", 1, 95)
        if max_width is not None:
            integer(max_width, "max_width", 1, 32768)
        if not isinstance(inline, bool):
            raise TypeError("inline must be a boolean")
        if path is not None and (not isinstance(path, str) or not path):
            raise ValueError("path must be a nonempty string")
        bounds = self._native.bounds()
        if window is not None:
            region = self._native.window_rect(window)
            if self._native.user.IsIconic(window):
                raise ValueError("Activate a minimized window before capturing it.")
        rect = crop_region(bounds if region is None else region, bounds)
        from mss import mss
        from PIL import Image

        if self._capture is None:
            self._capture = mss()
        started = time.perf_counter()
        shot = self._capture.grab(rect)
        captured_at_ns = time.time_ns()
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if max_width is not None and image.width > max_width:
            height = max(1, round(image.height * max_width / image.width))
            image = image.resize((max_width, height), Image.Resampling.BILINEAR)
        buffer = io.BytesIO()
        options = {"compress_level": 3} if format == "png" else {"quality": quality}
        image.save(buffer, format=format.upper(), **options)
        encoded = buffer.getvalue()
        result = {
            "capture_source": "visible_desktop",
            "mime_type": f"image/{format}",
            "width": image.width,
            "height": image.height,
            "region": rect,
            "desktop_pixels_per_image_pixel": {
                "x": rect["width"] / image.width,
                "y": rect["height"] / image.height,
            },
            "captured_at_ns": captured_at_ns,
            "bytes": len(encoded),
            "dpi_mode": self._native.dpi_mode,
        }
        if window is not None:
            result.update(requested_window=window, occlusion_possible=True)
        if path is not None or not inline:
            destination = (
                Path(path).expanduser()
                if path
                else Path("captures") / f"{uuid.uuid4().hex}.{format}"
            )
            destination = destination.resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(encoded)
            result["path"] = str(destination)
        if inline:
            result["base64"] = base64.b64encode(encoded).decode("ascii")
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    def move(self, x, y):
        self._check_thread()
        integer(x, "x")
        integer(y, "y")
        self._native.move(x, y)
        return {"x": x, "y": y}

    def click(self, x=None, y=None, element_id=None, button="left", count=1):
        self._check_thread()
        if button not in ("left", "right", "middle"):
            raise ValueError("button must be left, right or middle")
        integer(count, "count", 1, 2)
        if element_id is not None:
            if x is not None or y is not None:
                raise ValueError("Choose element_id or x/y.")
            if not isinstance(element_id, str) or element_id not in self._elements:
                raise ValueError("Unknown element_id; inspect the window again.")
            control = self._elements[element_id]
            if control.IsOffscreen or not control.IsEnabled:
                raise ValueError("Element is offscreen or disabled.")
            rect = control.BoundingRectangle
            if rect.right <= rect.left or rect.bottom <= rect.top:
                raise ValueError("Element has no clickable rectangle.")
            x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        self.move(x, y)
        down, up = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}[button]
        try:
            self._native.send(
                [event for _ in range(count) for event in (mouse_input(down), mouse_input(up))]
            )
        except OSError:
            try:
                self._native.send([mouse_input(up)])
            except OSError:
                pass
            raise
        return {"x": x, "y": y, "button": button, "count": count}

    def type_text(self, text):
        self._check_thread()
        if not isinstance(text, str) or len(text) > 100000:
            raise ValueError("text must be a string of at most 100000 characters")
        units = text.encode("utf-16-le")
        # Send UTF-16 units, including surrogate pairs; never touch the clipboard.
        for start in range(0, len(units), 512):
            block = units[start : start + 512]
            events = []
            for offset in range(0, len(block), 2):
                scan = int.from_bytes(block[offset : offset + 2], "little")
                events.extend(
                    (keyboard_input(scan=scan, flags=4), keyboard_input(scan=scan, flags=6))
                )
            self._native.send(events)
        return {"characters": len(text)}

    def press_keys(self, keys):
        self._check_thread()
        if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
            raise ValueError("keys must be a list of 1 to 8 key names")
        if any(not isinstance(key, str) or key.lower() not in KEYS for key in keys):
            raise ValueError("Unsupported key name")
        codes = [KEYS[key.lower()] for key in keys]
        if len(set(codes)) != len(codes):
            raise ValueError("Duplicate keys are not allowed")
        extended = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}
        events = [keyboard_input(vk=code, flags=1 if code in extended else 0) for code in codes]
        events += [
            keyboard_input(vk=code, flags=3 if code in extended else 2) for code in reversed(codes)
        ]
        try:
            self._native.send(events)
        except OSError:
            # Best-effort release if Windows inserted only a prefix of the sequence.
            try:
                self._native.send(events[len(codes) :])
            except OSError:
                pass
            raise
        return {"keys": keys}

    def scroll(self, steps, horizontal=False):
        self._check_thread()
        integer(steps, "steps", -100, 100)
        if not isinstance(horizontal, bool):
            raise TypeError("horizontal must be a boolean")
        self._native.send([mouse_input(0x1000 if horizontal else 0x0800, steps * 120)])
        return {"steps": steps, "horizontal": horizontal}

    def wait(self, seconds=0.2):
        self._check_thread()
        if (
            not isinstance(seconds, (int, float))
            or isinstance(seconds, bool)
            or not math.isfinite(seconds)
            or not 0 <= seconds <= 60
        ):
            raise ValueError("seconds must be between 0 and 60")
        time.sleep(seconds)
        return {"seconds": seconds}

    def inspect(self, window, max_depth=4, max_nodes=200):
        self._check_thread()
        self._native.window_rect(window)
        integer(max_depth, "max_depth", 0, 10)
        integer(max_nodes, "max_nodes", 1, 2000)
        try:
            import uiautomation as uia
        except ImportError as error:
            raise RuntimeError('Install UIA support: pip install "clawpaw[uia]"') from error
        root = uia.ControlFromHandle(window)
        generation = uuid.uuid4().hex[:12]
        elements, nodes = {}, []
        stack = [(root, 0, None)]
        truncated = False
        while stack and len(nodes) < max_nodes:
            control, depth, parent_id = stack.pop()
            rect = control.BoundingRectangle
            element_id = f"{generation}:{len(nodes)}"
            elements[element_id] = control
            nodes.append(
                {
                    "element_id": element_id,
                    "parent_id": parent_id,
                    "depth": depth,
                    "name": control.Name,
                    "control_type": control.ControlTypeName,
                    "automation_id": control.AutomationId,
                    "native_window_handle": control.NativeWindowHandle,
                    "enabled": bool(control.IsEnabled),
                    "offscreen": bool(control.IsOffscreen),
                    "rect": {
                        "left": rect.left,
                        "top": rect.top,
                        "width": rect.right - rect.left,
                        "height": rect.bottom - rect.top,
                    },
                }
            )
            children = control.GetChildren()
            if depth < max_depth:
                budget = max_nodes - len(nodes) - len(stack)
                if len(children) > budget:
                    truncated = True
                stack.extend(
                    (child, depth + 1, element_id) for child in reversed(children[: max(0, budget)])
                )
            elif children:
                truncated = True
        self._elements = elements
        return {"window": window, "nodes": nodes, "truncated": truncated or bool(stack)}

    def _resolve_control(self, window, selector, max_depth, max_nodes):
        self._check_thread()
        if (
            not isinstance(selector, dict)
            or not selector
            or set(selector) - {"name", "automation_id", "control_type"}
            or not all(isinstance(value, str) and value for value in selector.values())
        ):
            raise ValueError("selector requires nonempty name, automation_id and/or control_type")
        self._native.window_rect(window)
        integer(max_depth, "max_depth", 0, 10)
        integer(max_nodes, "max_nodes", 1, 2000)
        try:
            import uiautomation as uia
        except ImportError as error:
            raise RuntimeError('Install UIA support: pip install "clawpaw[uia]"') from error
        properties = {
            "name": "Name",
            "automation_id": "AutomationId",
            "control_type": "ControlTypeName",
        }
        # Read only selector properties during search, not every rectangle and state.
        stack, matches = [(uia.ControlFromHandle(window), 0)], []
        visited, truncated = 0, False
        while stack and visited < max_nodes:
            control, depth = stack.pop()
            visited += 1
            if all(getattr(control, properties[key]) == value for key, value in selector.items()):
                matches.append(control)
                if len(matches) > 1:
                    raise ValueError("Selector is ambiguous; add name/automation_id/control_type")
            children = control.GetChildren()
            if depth < max_depth:
                budget = max_nodes - visited - len(stack)
                if len(children) > budget:
                    truncated = True
                stack.extend((child, depth + 1) for child in reversed(children[: max(0, budget)]))
            elif children:
                truncated = True
        if truncated or stack:
            raise ValueError("UIA search was truncated; increase max_depth/max_nodes before acting")
        if not matches:
            raise ValueError("No matching UIA control; inspect the window again")
        control = matches[0]
        if not control.IsEnabled or control.IsOffscreen:
            raise ValueError("Element is offscreen or disabled")
        return control

    def invoke(self, window, selector, max_depth=10, max_nodes=2000):
        """Resolve a unique fresh UIA target and invoke it without coordinate input."""
        control = self._resolve_control(window, selector, max_depth, max_nodes)
        import uiautomation as uia

        pattern = control.GetPattern(uia.PatternId.InvokePattern)
        if pattern is None:
            raise ValueError("Control does not support UIA InvokePattern")
        if not pattern.Invoke(waitTime=0):
            raise RuntimeError("UIA Invoke failed; inspect state before retrying")
        return {"window": window, "selector": selector, "invoked": True}

    def background_click(self, window, selector, max_depth=10, max_nodes=2000):
        """Click through UIA Invoke only; never activate, focus or send mouse input."""
        result = self.invoke(window, selector, max_depth, max_nodes)
        return {**result, "input_mode": "uia_invoke"}

    def set_toggle(self, window, selector, checked, max_depth=10, max_nodes=2000):
        """Set a checkbox through TogglePattern without focus or blind toggling."""
        if not isinstance(checked, bool):
            raise TypeError("checked must be a boolean")
        control = self._resolve_control(window, selector, max_depth, max_nodes)
        import uiautomation as uia

        pattern = control.GetPattern(uia.PatternId.TogglePattern)
        if pattern is None:
            raise ValueError("Control does not support UIA TogglePattern")
        state = pattern.ToggleState
        if state not in (0, 1):
            raise ValueError("Toggle state is indeterminate; inspect before acting")
        changed = bool(state) != checked
        if changed and not pattern.Toggle(waitTime=0):
            raise RuntimeError("UIA Toggle failed; inspect state before retrying")
        # Verify the result, but never toggle repeatedly on a delayed provider.
        if pattern.ToggleState != int(checked):
            raise RuntimeError("UIA toggle state not confirmed; inspect state before retrying")
        return {"window": window, "selector": selector, "checked": checked, "changed": changed}

    def set_value(self, window, selector, text, max_depth=10, max_nodes=2000):
        """Replace an editable control's value using ValuePattern, without typing."""
        if not isinstance(text, str) or len(text) > 100000:
            raise ValueError("text must be a string of at most 100000 characters")
        control = self._resolve_control(window, selector, max_depth, max_nodes)
        import uiautomation as uia

        pattern = control.GetPattern(uia.PatternId.ValuePattern)
        if pattern is None:
            raise ValueError("Control does not support UIA ValuePattern")
        if pattern.IsReadOnly:
            raise ValueError("Control is read-only")
        if not pattern.SetValue(text, waitTime=0):
            raise RuntimeError("UIA SetValue failed; inspect state before retrying")
        return {"window": window, "selector": selector, "characters": len(text)}

    def focus(self, window, selector, max_depth=10, max_nodes=2000):
        control = self._resolve_control(window, selector, max_depth, max_nodes)
        if not control.SetFocus():
            raise RuntimeError("UIA SetFocus failed")
        return {"window": window, "selector": selector, "focused": True}

    def run_actions(
        self, actions, screenshot_after=True, screenshot_options=None, background_only=False
    ):
        self._check_thread()
        if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
            raise ValueError("actions must contain 1 to 100 actions")
        if not isinstance(screenshot_after, bool):
            raise TypeError("screenshot_after must be a boolean")
        if not isinstance(background_only, bool):
            raise TypeError("background_only must be a boolean")
        options = {} if screenshot_options is None else screenshot_options
        if not isinstance(options, dict):
            raise TypeError("screenshot_options must be an object")
        signatures.signature(self.screenshot).bind(**options)
        allowed = {
            "activate",
            "move",
            "click",
            "type_text",
            "press_keys",
            "scroll",
            "wait",
            "invoke",
            "background_click",
            "set_value",
            "set_toggle",
            "focus",
        }
        prepared = []
        for action in actions:
            if not isinstance(action, dict) or set(action) - {"method", "params"}:
                raise ValueError("Each action requires method and optional params")
            method, params = action.get("method"), action.get("params", {})
            if not isinstance(method, str):
                raise TypeError("Action method must be a string")
            method = method.removeprefix("windows.")
            if method not in allowed or not isinstance(params, dict):
                raise ValueError(f"Unsupported batch action: {method}")
            if background_only and method not in {
                "wait",
                "invoke",
                "background_click",
                "set_value",
                "set_toggle",
            }:
                raise BackgroundActionError(method)
            fn = getattr(self, method)
            signatures.signature(fn).bind(**params)
            prepared.append((fn, params))
        results = []
        for index, (fn, params) in enumerate(prepared):
            try:
                results.append(fn(**params))
            except Exception as error:
                raise BatchError(index, error) from error
        result = {"results": results, "completed_actions": len(results)}
        if screenshot_after:
            try:
                result["screenshot"] = self.screenshot(**options)
            except Exception as error:
                raise BatchError(len(results), error) from error
        return result
