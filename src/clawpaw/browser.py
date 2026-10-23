"""Clawpaw's own browser controller: Playwright over loopback Chromium CDP.

An attached browser survives closing this client, including one-shot CLI calls.
No cookies are copied from a user's profile, and no remote endpoints are accepted.
"""

import inspect as signatures
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from .desktop import BatchError, integer


class BrowserConnectionError(RuntimeError):
    def __init__(self, endpoint, timeout_ms, cause):
        detail = str(cause)[:4000]
        reason = "connection_timeout" if "timeout" in detail.lower() else "cdp_attach_failed"
        super().__init__(f"Edge CDP attachment failed ({reason}): {detail}")
        self.data = {
            "reason": reason,
            "endpoint": endpoint,
            "timeout_ms": timeout_ms,
            "cause_type": type(cause).__name__,
            "cause": detail,
            "hint": "Discovery does not verify consent. Check this request's actual error and "
            "approval timing; do not infer debugging is disabled. Each new process may require "
            "a fresh approval. Use attach_existing/run_actions with an explicitly authorized "
            "approve_window, or reuse one live Browser/serve connection.",
        }


def local_endpoint(endpoint):
    if not isinstance(endpoint, str):
        raise TypeError("endpoint must be a string")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme not in {"http", "ws"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or (
            parsed.path not in {"", "/"}
            if parsed.scheme == "http"
            else re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", parsed.path) is None
        )
        or parsed.query
        or parsed.fragment
        or parsed.port is None
        or parsed.port < 1
    ):
        raise ValueError(
            "endpoint must be a loopback HTTP CDP address or browser WebSocket with a port"
        )
    return endpoint.rstrip("/")


def web_url(url):
    if (
        not isinstance(url, str)
        or urlparse(url).scheme not in {"http", "https"}
        or not urlparse(url).hostname
    ):
        raise ValueError("url must use http or https")
    return url


def browser_home():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "Clawpaw"


def edge_user_data():
    return (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        / "Microsoft/Edge/User Data"
    )


def discover_edge(user_data_dir=None):
    """Read debugging metadata only; never read cookies or alter browser settings."""
    if user_data_dir is not None and (not isinstance(user_data_dir, str) or not user_data_dir):
        raise ValueError("user_data_dir must be a nonempty string")
    profile = (Path(user_data_dir).expanduser() if user_data_dir else edge_user_data()).resolve()
    result = {"user_data_dir": str(profile), "available": False}
    try:
        with (profile / "DevToolsActivePort").open(encoding="utf-8") as metadata:
            lines = [line.strip() for line in metadata.read(4096).splitlines() if line.strip()]
    except OSError:
        return {
            **result,
            "reason": "debugging_metadata_unavailable",
            "hint": "In your existing Edge open edge://inspect/#remote-debugging and enable "
            "remote debugging. Keep it open, then call browser.attach_existing and "
            "approve Edge's connection prompt. No separate login is needed.",
        }
    try:
        if len(lines) != 2 or not lines[0].isdigit():
            raise ValueError("Invalid DevToolsActivePort format")
        endpoint = local_endpoint(f"ws://127.0.0.1:{int(lines[0])}{lines[1]}")
    except ValueError:
        return {
            **result,
            "reason": "invalid_debugging_metadata",
            "hint": "Check remote debugging in the selected Edge profile; do not guess a port.",
        }
    with socket.socket() as probe:
        probe.settimeout(0.3)
        reachable = probe.connect_ex(("127.0.0.1", int(lines[0]))) == 0
    return {
        **result,
        "available": reachable,
        "endpoint": endpoint,
        "reason": "connection_not_verified" if reachable else "debugging_endpoint_unreachable",
        "hint": "Call browser.attach_existing and approve Edge's connection prompt."
        if reachable
        else "Keep this Edge profile open and check its remote-debugging page.",
    }


def cdp_version(endpoint):
    # Do not send local debugger traffic through configured system proxies.
    opener = build_opener(ProxyHandler({}))
    with opener.open(endpoint + "/json/version", timeout=2) as response:
        return json.load(response)


class Browser:
    """Thread-affine, lazy client. Closing disconnects, never closes user tabs."""

    def __init__(self, endpoint=None, config_path=None):
        self._thread = threading.get_ident()
        self._config_path = Path(config_path) if config_path else browser_home() / "browser.json"
        self._user_data_dir = None
        if endpoint is None:
            endpoint = os.environ.get("CLAWPAW_BROWSER_ENDPOINT")
            if endpoint is None and self._config_path.exists():
                config = json.loads(self._config_path.read_text(encoding="utf-8"))
                endpoint = config["endpoint"]
                self._user_data_dir = config.get("user_data_dir")
        self.endpoint = local_endpoint(endpoint or "http://127.0.0.1:9222")
        self._driver = None
        self._browser = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check_thread(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError("Use this Browser on its creating thread.")

    def close(self):
        self._check_thread()
        if self._driver is not None:
            self._driver.stop()
        self._driver = self._browser = None

    def _connect(self, timeout_ms=8000):
        self._check_thread()
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        self.close()
        if self._user_data_dir is not None:
            discovered = discover_edge(self._user_data_dir)
            if not discovered["available"]:
                raise RuntimeError(discovered["hint"])
            self.endpoint = discovered["endpoint"]
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError('Install browser support: pip install "clawpaw[browser]"') from error
        self._driver = sync_playwright().start()
        try:
            self._browser = self._driver.chromium.connect_over_cdp(
                self.endpoint, timeout=timeout_ms
            )
        except Exception as error:
            self.close()
            raise BrowserConnectionError(self.endpoint, timeout_ms, error) from error
        return self._browser

    def _save_endpoint(self):
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._config_path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            config = {"endpoint": self.endpoint}
            if self._user_data_dir is not None:
                config["user_data_dir"] = self._user_data_dir
            temporary.write_text(json.dumps(config), encoding="utf-8")
            os.replace(temporary, self._config_path)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self):
        self._check_thread()
        if self._browser is not None and self._browser.is_connected():
            return {"connected": True, "endpoint": self.endpoint}
        if self.endpoint.startswith("ws:") or self._user_data_dir is not None:
            return {
                "connected": False,
                "endpoint": self.endpoint,
                "existing_edge": self.discover(self._user_data_dir),
                "hint": "WebSocket attachment needs verification; discovery does not request permission.",
            }
        try:
            version = cdp_version(self.endpoint)
        except (OSError, ValueError):
            return {
                "connected": False,
                "endpoint": self.endpoint,
                "existing_edge": self.discover(),
                "hint": "Configured endpoint unavailable. Check existing_edge and try "
                "browser.attach_existing for the signed-in Edge; 9222 is not required.",
            }
        return {
            "connected": bool(version.get("webSocketDebuggerUrl")),
            "endpoint": self.endpoint,
            "browser": version.get("Browser"),
        }

    def discover(self, user_data_dir=None):
        self._check_thread()
        return discover_edge(user_data_dir)

    def attach_existing(self, user_data_dir=None, timeout_ms=30000, approve_window=None):
        """Connect to an existing Edge profile, allowing time for browser-side consent."""
        self._check_thread()
        self._timeout(timeout_ms)
        if approve_window is not None:
            integer(approve_window, "approve_window", 1)
        discovered = self.discover(user_data_dir)
        if not discovered["available"]:
            raise RuntimeError(discovered["hint"])
        self.close()
        previous = self.endpoint, self._user_data_dir
        self.endpoint = discovered["endpoint"]
        self._user_data_dir = discovered["user_data_dir"]
        consent = None
        try:
            if approve_window is not None:
                from .consent import EdgeConsent

                consent = EdgeConsent(approve_window, timeout_ms)
            with consent if consent is not None else nullcontext():
                self._connect(timeout_ms=timeout_ms)
            tabs = self.tabs()
            self._save_endpoint()
        except Exception as error:
            if isinstance(error, BrowserConnectionError) and consent is not None:
                error.data["consent"] = consent.result
            self.close()
            self.endpoint, self._user_data_dir = previous
            raise
        return {
            "endpoint": self.endpoint,
            "user_data_dir": self._user_data_dir,
            "existing_session": True,
            "tabs": tabs,
            "consent": consent.result if consent is not None else {"requested": False},
        }

    def run_actions(self, actions, user_data_dir=None, timeout_ms=30000, approve_window=None):
        """Attach and perform a bounded browser plan in one approved connection."""
        self._check_thread()
        self._timeout(timeout_ms)
        if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
            raise ValueError("actions must contain 1 to 100 browser actions")
        allowed = {
            "tabs",
            "open",
            "navigate",
            "snapshot",
            "click",
            "fill",
            "press",
            "wait",
            "screenshot",
        }
        prepared = []
        for action in actions:
            if not isinstance(action, dict) or set(action) - {"method", "params"}:
                raise ValueError("Each action requires method and optional params")
            method, params = action.get("method"), action.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise TypeError("Action method must be a string and params an object")
            method = method.removeprefix("browser.")
            if method not in allowed:
                raise ValueError(f"Unsupported browser batch action: {method}")
            fn = getattr(self, method)
            signatures.signature(fn).bind(**params)
            prepared.append((fn, params))
        connection = self.attach_existing(user_data_dir, timeout_ms, approve_window)
        results = []
        for index, (fn, params) in enumerate(prepared):
            try:
                results.append(fn(**params))
            except Exception as error:
                raise BatchError(index, error) from error
        return {"connection": connection, "results": results, "completed_actions": len(results)}

    def attach(self, endpoint):
        """Connect only to an endpoint explicitly supplied by the caller; persist it."""
        self._check_thread()
        endpoint = local_endpoint(endpoint)
        self.close()
        previous = self.endpoint, self._user_data_dir
        self.endpoint = endpoint
        self._user_data_dir = None
        try:
            self._connect()
            tabs = self.tabs()
            self._save_endpoint()
        except Exception:
            self.close()
            self.endpoint, self._user_data_dir = previous
            raise
        return {"endpoint": self.endpoint, "tabs": tabs}

    def launch(self, port=9222):
        """Start Edge with a dedicated Clawpaw profile; leave it open after disconnect."""
        self._check_thread()
        integer(port, "port", 1024, 65535)
        endpoint = f"http://127.0.0.1:{port}"
        try:
            cdp_version(endpoint)
        except (OSError, ValueError):
            pass
        else:
            raise ValueError("Port already serves CDP; use browser.attach explicitly instead.")
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise ValueError("Port is occupied by another service; choose a different port.")
        candidates = [
            Path(os.environ.get(key, "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe"
            for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
        ]
        executable = next((path for path in candidates if path.is_file()), None)
        if executable is None:
            raise RuntimeError("Microsoft Edge executable not found.")
        profile = browser_home() / "Edge"
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                str(executable),
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                cdp_version(endpoint)
                result = self.attach(endpoint)
                return {**result, "profile": str(profile), "separate_profile": True}
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(
            "Edge did not expose CDP. If Clawpaw's Edge profile is already open on another "
            "port, attach to that port or close that profile before launching again."
        )

    @staticmethod
    def _target_id(page):
        session = page.context.new_cdp_session(page)
        try:
            return session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        finally:
            session.detach()

    def _pages(self):
        return [page for context in self._connect().contexts for page in context.pages]

    def tabs(self):
        return [
            {"tab": self._target_id(page), "url": page.url, "title": page.title()}
            for page in self._pages()
        ]

    def _page(self, tab):
        if not isinstance(tab, str) or not tab:
            raise ValueError("tab must be a target ID returned by browser.tabs or browser.open")
        for page in self._pages():
            if self._target_id(page) == tab:
                return page
        raise ValueError("Tab no longer exists; list browser.tabs again.")

    @staticmethod
    def _timeout(timeout_ms):
        return integer(timeout_ms, "timeout_ms", 1, 30000)

    @staticmethod
    def _locator(page, selector):
        if not isinstance(selector, dict):
            raise TypeError("selector must be an object")
        if set(selector) == {"role", "name"}:
            if not all(isinstance(v, str) and v for v in selector.values()):
                raise ValueError("role and name must be nonempty strings")
            return page.get_by_role(selector["role"], name=selector["name"], exact=True)
        if set(selector) == {"label"} and isinstance(selector["label"], str) and selector["label"]:
            return page.get_by_label(selector["label"], exact=True)
        if (
            set(selector) == {"test_id"}
            and isinstance(selector["test_id"], str)
            and selector["test_id"]
        ):
            return page.get_by_test_id(selector["test_id"])
        if (
            set(selector) == {"placeholder"}
            and isinstance(selector["placeholder"], str)
            and selector["placeholder"]
        ):
            return page.get_by_placeholder(selector["placeholder"], exact=True)
        if set(selector) == {"id"} and isinstance(selector["id"], str) and selector["id"]:
            # CSS quoted attribute value; escape control characters as CSS hexadecimal escapes.
            value = "".join(
                f"\\{ord(char):x} " if ord(char) < 32 or char in {'"', "\\"} else char
                for char in selector["id"]
            )
            return page.locator(f'[id="{value}"]')
        raise ValueError("selector requires role+name, label, test_id, placeholder, or id")

    def _unique_locator(self, page, selector, timeout_ms):
        self._timeout(timeout_ms)
        locator = self._locator(page, selector)
        if locator.count() > 1:
            raise ValueError("Selector is ambiguous; refine it from a fresh snapshot.")
        locator.wait_for(state="visible", timeout=self._timeout(timeout_ms))
        if locator.count() != 1:
            raise ValueError("Selector is ambiguous; refine it from a fresh snapshot.")
        return locator

    def open(self, url, timeout_ms=15000):
        url = web_url(url)
        self._timeout(timeout_ms)
        contexts = self._connect().contexts
        if not contexts:
            raise RuntimeError("Attached browser has no context.")
        page = contexts[0].new_page()
        tab = self._target_id(page)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as error:
            # Keep and identify the tab, so an agent can inspect it instead of creating duplicates.
            raise RuntimeError(
                f"Navigation failed in tab {tab}; inspect it before retrying."
            ) from error
        return {"tab": tab, "url": page.url, "title": page.title()}

    def navigate(self, tab, url, timeout_ms=15000):
        url = web_url(url)
        self._timeout(timeout_ms)
        page = self._page(tab)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return {"tab": tab, "url": page.url, "title": page.title()}

    def snapshot(self, tab, max_chars=16000, max_elements=100, timeout_ms=8000):
        integer(max_chars, "max_chars", 100, 100000)
        integer(max_elements, "max_elements", 1, 500)
        self._timeout(timeout_ms)
        page = self._page(tab)
        tree = page.locator("body").aria_snapshot(timeout=timeout_ms)
        # Visible DOM attributes complement ARIA for unnamed rich-text editors.
        candidates = page.locator(
            'button,input,textarea,select,a[href],[contenteditable="true"],[role="button"],[role="textbox"]'
        ).evaluate_all(
            """(elements, limit) => {
            const result = [];
            let count = 0;
            for (const e of elements) {
                const box = e.getBoundingClientRect();
                const style = getComputedStyle(e);
                if (!box.width || !box.height || style.visibility === 'hidden' ||
                    style.display === 'none' || e.closest('[hidden],[aria-hidden="true"]')) continue;
                count++;
                if (result.length < limit) result.push({
                    tag: e.tagName.toLowerCase(), id: e.id || null,
                    role: e.getAttribute('role'), label: e.getAttribute('aria-label'),
                    placeholder: e.getAttribute('placeholder') || e.getAttribute('aria-placeholder'),
                    test_id: e.getAttribute('data-testid'),
                    text: /^(BUTTON|A)$/.test(e.tagName) ? e.textContent.trim().slice(0, 200) : null
                });
            }
            return {elements: result, truncated: count > limit};
        }""",
            max_elements,
        )
        return {
            "tab": tab,
            "url": page.url,
            "title": page.title(),
            "tree": tree[:max_chars],
            "truncated": len(tree) > max_chars,
            "elements": candidates["elements"],
            "elements_truncated": candidates["truncated"],
        }

    def click(self, tab, selector, timeout_ms=8000):
        page = self._page(tab)
        self._unique_locator(page, selector, timeout_ms).click(timeout=timeout_ms)
        return {"tab": tab, "action": "click", "selector": selector}

    def fill(self, tab, selector, text, timeout_ms=8000):
        if not isinstance(text, str) or len(text) > 100000:
            raise ValueError("text must be a string of at most 100000 characters")
        page = self._page(tab)
        self._unique_locator(page, selector, timeout_ms).fill(text, timeout=timeout_ms)
        return {"tab": tab, "action": "fill", "characters": len(text)}

    def press(self, tab, selector, key, timeout_ms=8000):
        if not isinstance(key, str) or not key or len(key) > 100:
            raise ValueError(
                "key must be a nonempty Playwright key chord of at most 100 characters"
            )
        page = self._page(tab)
        self._unique_locator(page, selector, timeout_ms).press(key, timeout=timeout_ms)
        return {"tab": tab, "action": "press", "key": key}

    def wait(self, tab, selector, state="visible", timeout_ms=15000):
        if state not in {"visible", "hidden", "attached", "detached"}:
            raise ValueError("Unsupported wait state")
        self._timeout(timeout_ms)
        self._locator(self._page(tab), selector).wait_for(state=state, timeout=timeout_ms)
        return {"tab": tab, "state": state}

    def screenshot(self, tab, path=None, full_page=False, timeout_ms=8000):
        if not isinstance(full_page, bool):
            raise TypeError("full_page must be a boolean")
        if path is not None and (not isinstance(path, str) or not path):
            raise ValueError("path must be a nonempty string")
        self._timeout(timeout_ms)
        page = self._page(tab)
        destination = (
            Path(path or f"captures/browser-{uuid.uuid4().hex}.png").expanduser().resolve()
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = page.screenshot(path=str(destination), full_page=full_page, timeout=timeout_ms)
        return {"tab": tab, "path": str(destination), "mime_type": "image/png", "bytes": len(data)}
