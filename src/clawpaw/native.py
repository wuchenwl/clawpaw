"""Small, pointer-safe Win32 bindings. No third-party input library required."""

import ctypes
import sys
import time
from ctypes import wintypes as wt


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", wt.WPARAM),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", wt.WPARAM),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wt.DWORD), ("value", INPUTUNION)]


KEYS = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "enter": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}
KEYS.update({chr(code).lower(): code for code in range(0x41, 0x5B)})
KEYS.update({str(n): 0x30 + n for n in range(10)})
KEYS.update({f"f{n}": 0x6F + n for n in range(1, 25)})


def keyboard_input(vk=0, scan=0, flags=0):
    return INPUT(type=1, ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags))


def mouse_input(flags, data=0):
    return INPUT(type=0, mi=MOUSEINPUT(dwFlags=flags, mouseData=data & 0xFFFFFFFF))


class ForegroundActivationError(OSError):
    def __init__(self, window, foreground_window, api_returned_success, recovery=None):
        super().__init__(
            f"Window {window} did not become foreground (current={foreground_window}). "
            "Windows may restrict foreground switching; this does not establish "
            "insufficient permissions."
        )
        self.data = {
            "reason": "foreground_not_changed",
            "window": window,
            "foreground_window": foreground_window,
            "api_returned_success": api_returned_success,
        }
        if recovery is not None:
            self.data["recovery"] = recovery


class Native:
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Desktop automation requires Windows.")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        specs = {
            "EnumWindows": ([self.callback_type, wt.LPARAM], wt.BOOL),
            "IsWindowVisible": ([wt.HWND], wt.BOOL),
            "IsWindow": ([wt.HWND], wt.BOOL),
            "IsIconic": ([wt.HWND], wt.BOOL),
            "GetWindowTextLengthW": ([wt.HWND], ctypes.c_int),
            "GetWindowTextW": ([wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowRect": ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
            "GetWindowThreadProcessId": ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
            "GetForegroundWindow": ([], wt.HWND),
            "ShowWindowAsync": ([wt.HWND, ctypes.c_int], wt.BOOL),
            "SetForegroundWindow": ([wt.HWND], wt.BOOL),
            "BringWindowToTop": ([wt.HWND], wt.BOOL),
            "AttachThreadInput": ([wt.DWORD, wt.DWORD, wt.BOOL], wt.BOOL),
            "PeekMessageW": ([ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT], wt.BOOL),
            "SetCursorPos": ([ctypes.c_int, ctypes.c_int], wt.BOOL),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
            "SendInput": ([wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wt.UINT),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetCurrentThreadId.argtypes = []
        self.kernel.GetCurrentThreadId.restype = wt.DWORD
        self.dpi_mode = self._set_dpi()

    def _set_dpi(self):
        # Thread context also works when a host has already fixed process DPI awareness.
        fn = getattr(self.user, "SetThreadDpiAwarenessContext", None)
        if fn is not None:
            fn.argtypes, fn.restype = [ctypes.c_void_p], ctypes.c_void_p
            if fn(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
                return "per_monitor_v2"
        raise OSError("Unable to enable per-monitor DPI awareness on this thread.")

    def bounds(self):
        x, y, w, h = (self.user.GetSystemMetrics(n) for n in (76, 77, 78, 79))
        return {"left": x, "top": y, "width": w, "height": h}

    def window_rect(self, handle):
        if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
            raise ValueError("window must be a positive integer HWND")
        if not self.user.IsWindow(handle):
            raise ValueError(f"Window no longer exists: {handle}")
        rect = wt.RECT()
        if not self.user.GetWindowRect(handle, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "left": rect.left,
            "top": rect.top,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
        }

    def list_windows(self):
        windows = []
        foreground = self.user.GetForegroundWindow()

        @self.callback_type
        def visit(handle, _):
            if not self.user.IsWindowVisible(handle):
                return True
            length = self.user.GetWindowTextLengthW(handle)
            if not length:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            self.user.GetWindowTextW(handle, title, len(title))
            pid = wt.DWORD()
            self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            try:
                rect = self.window_rect(handle)
            except (ValueError, OSError):
                return True  # A window may close during enumeration.
            windows.append(
                {
                    "handle": handle,
                    "title": title.value,
                    "pid": pid.value,
                    "rect": rect,
                    "foreground": handle == foreground,
                    "minimized": bool(self.user.IsIconic(handle)),
                }
            )
            return True

        if not self.user.EnumWindows(visit, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return windows

    @staticmethod
    def process_path(pid):
        """Read executable identity with limited query access; never elevate."""
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.OpenProcess.restype = wt.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [
            wt.HANDLE,
            wt.DWORD,
            wt.LPWSTR,
            ctypes.POINTER(wt.DWORD),
        ]
        kernel.QueryFullProcessImageNameW.restype = wt.BOOL
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.CloseHandle.restype = wt.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            length = wt.DWORD(32768)
            path = ctypes.create_unicode_buffer(length.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            return path.value
        finally:
            kernel.CloseHandle(handle)

    def activate(self, handle):
        self.window_rect(handle)
        if self.user.IsIconic(handle):
            self.user.ShowWindowAsync(handle, 9)
            deadline = time.monotonic() + 0.5
            while self.user.IsIconic(handle):
                if time.monotonic() >= deadline:
                    raise OSError(f"Window {handle} did not restore from minimized state.")
                time.sleep(0.02)
        # Foreground activation is idempotent; a rejected API call is irrelevant
        # if the target is already the active window.
        if self.user.GetForegroundWindow() == handle:
            return {"activation_method": "already_foreground"}
        api_returned_success = bool(self.user.SetForegroundWindow(handle))
        foreground = self._wait_foreground(handle)
        if foreground == handle:
            return {"activation_method": "set_foreground_window"}
        foreground, recovery = self._activate_with_input_queue(handle, foreground)
        if foreground != handle:
            raise ForegroundActivationError(handle, foreground, api_returned_success, recovery)
        return {"activation_method": "attached_input_queue", "recovery": recovery}

    def _wait_foreground(self, handle):
        deadline = time.monotonic() + 0.3
        while True:
            foreground = self.user.GetForegroundWindow()
            if foreground == handle or time.monotonic() >= deadline:
                return foreground
            time.sleep(0.02)

    def _activate_with_input_queue(self, handle, foreground):
        """One bounded Win32 recovery, with no synthetic keys or pointer movement."""
        recovery = {"method": "attach_thread_input", "attached_threads": []}
        caller = self.kernel.GetCurrentThreadId()
        target = self.user.GetWindowThreadProcessId(handle, None)
        active = self.user.GetWindowThreadProcessId(foreground, None) if foreground else 0
        if not caller or not target or not active:
            recovery["reason"] = "thread_unavailable"
            return foreground, recovery
        # A console caller may not have a USER input queue yet. PM_NOREMOVE
        # creates one without consuming pending messages or injecting input.
        message = wt.MSG()
        self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        attached = []
        try:
            for thread in dict.fromkeys((active, target)):
                if thread == caller:
                    continue
                if not self.user.AttachThreadInput(caller, thread, True):
                    recovery.update(reason="input_queue_attach_failed", failed_thread=thread)
                    return self.user.GetForegroundWindow(), recovery
                attached.append(thread)
                recovery["attached_threads"].append(thread)
            self.user.BringWindowToTop(handle)
            recovery["api_returned_success"] = bool(self.user.SetForegroundWindow(handle))
            return self._wait_foreground(handle), recovery
        finally:
            for thread in reversed(attached):
                self.user.AttachThreadInput(caller, thread, False)

    def move(self, x, y):
        bounds = self.bounds()
        if not (
            bounds["left"] <= x < bounds["left"] + bounds["width"]
            and bounds["top"] <= y < bounds["top"] + bounds["height"]
        ):
            raise ValueError("Coordinates are outside the virtual desktop.")
        if not self.user.SetCursorPos(x, y):
            raise ctypes.WinError(ctypes.get_last_error())

    def send(self, events):
        if not events:
            return
        array = (INPUT * len(events))(*events)
        ctypes.set_last_error(0)
        sent = self.user.SendInput(len(array), array, ctypes.sizeof(INPUT))
        if sent != len(array):
            raise OSError(
                f"SendInput inserted {sent}/{len(array)} events; "
                "check desktop access and target process integrity level."
            )
