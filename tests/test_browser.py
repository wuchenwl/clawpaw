import json
from types import SimpleNamespace

import pytest
from tests_helper import rpc

from clawpaw import Browser
from clawpaw.browser import discover_edge, local_endpoint
from clawpaw.protocol import handle_request, serve


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:9222",
        "https://127.0.0.1:9222",
        "http://127.0.0.1",
        "http://user:secret@localhost:9222",
        "http://localhost:9222/path",
        "http://localhost:9222?x=1",
        "http://127.0.0.1:70000",
        "http://127.0.0.1:0",
        "ws://remote.example:9222/devtools/browser/abc",
        "ws://127.0.0.1:9222/devtools/page/abc",
        "ws://127.0.0.1:9222/devtools/browser/abc?token=bad",
    ],
)
def test_remote_or_malformed_debugger_is_rejected(endpoint):
    with pytest.raises(ValueError):
        local_endpoint(endpoint)


def test_attach_persists_endpoint_and_disconnect_does_not_close_browser(monkeypatch, tmp_path):
    config = tmp_path / "browser.json"
    browser = Browser(config_path=config)
    stopped = []

    def connect():
        browser._driver = SimpleNamespace(stop=lambda: stopped.append(True))

    monkeypatch.setattr(browser, "_connect", connect)
    monkeypatch.setattr(browser, "tabs", list)
    browser.attach("http://127.0.0.1:9333")
    browser.close()
    assert stopped == [True]
    assert Browser(config_path=config).endpoint == "http://127.0.0.1:9333"


def test_failed_attach_keeps_previous_config(monkeypatch, tmp_path):
    config = tmp_path / "browser.json"
    config.write_text(json.dumps({"endpoint": "http://127.0.0.1:9333"}))
    browser = Browser(config_path=config)

    def fail():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(browser, "_connect", fail)
    with pytest.raises(RuntimeError):
        browser.attach("http://127.0.0.1:9444")
    assert browser.endpoint == "http://127.0.0.1:9333"
    assert json.loads(config.read_text())["endpoint"] == browser.endpoint


def test_browser_dispatch_never_initializes_desktop():
    def no_desktop():
        raise AssertionError("browser tool must not initialize desktop")

    response = handle_request(
        rpc("browser.status"),
        no_desktop,
        lambda: SimpleNamespace(status=lambda: {"connected": False}),
    )
    assert response["result"] == {"connected": False}


def test_protocol_reuses_browser_and_closes_on_eof():
    import io

    instances = []

    class FakeBrowser:
        def __init__(self):
            self.closed = False
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.closed = True

        def tabs(self):
            return [{"tab": "stable-id"}]

    output = io.StringIO()
    serve(
        io.StringIO(rpc("browser.tabs") + "\n" + rpc("browser.tabs")),
        output,
        browser_factory=FakeBrowser,
    )
    assert len(instances) == 1 and instances[0].closed
    assert len(output.getvalue().splitlines()) == 2


def test_launch_uses_own_profile_and_saves_attach_target(monkeypatch, tmp_path):
    import clawpaw.browser as backend

    checks, spawned = [], []

    def version(endpoint):
        checks.append(endpoint)
        if len(checks) == 1:
            raise OSError("not running")
        return {"Browser": "test"}

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def settimeout(self, _):
            pass

        def connect_ex(self, _):
            return 1

    monkeypatch.setattr(backend, "browser_home", lambda: tmp_path / "Clawpaw")
    monkeypatch.setattr(backend, "cdp_version", version)
    monkeypatch.setattr(backend.socket, "socket", Probe)
    monkeypatch.setattr(backend.Path, "is_file", lambda _: True)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda argv, **_: spawned.append(argv))
    browser = Browser(config_path=tmp_path / "config.json")
    monkeypatch.setattr(browser, "attach", lambda endpoint: {"endpoint": endpoint, "tabs": []})
    result = browser.launch(port=9333)
    assert result["separate_profile"]
    assert f"--user-data-dir={tmp_path / 'Clawpaw' / 'Edge'}" in spawned[0]
    assert "--remote-debugging-address=127.0.0.1" in spawned[0]
    assert result["endpoint"] == "http://127.0.0.1:9333"


def test_launch_refuses_existing_cdp_without_explicit_attach(monkeypatch, tmp_path):
    monkeypatch.setattr("clawpaw.browser.cdp_version", lambda _: {"Browser": "existing"})
    browser = Browser(config_path=tmp_path / "config.json")
    with pytest.raises(ValueError, match="attach explicitly"):
        browser.launch()


def test_local_browser_websocket_is_accepted():
    endpoint = "ws://127.0.0.1:54321/devtools/browser/abc-123"
    assert local_endpoint(endpoint) == endpoint


def test_discovery_missing_is_actionable_and_does_not_claim_edge_is_unavailable(tmp_path):
    discovered = discover_edge(str(tmp_path))
    assert discovered["reason"] == "debugging_metadata_unavailable"
    assert "edge://inspect/#remote-debugging" in discovered["hint"]
    assert "No separate login" in discovered["hint"]


@pytest.mark.parametrize(
    "metadata",
    [
        "0\n/devtools/browser/a",
        "70000\n/devtools/browser/a",
        "9222\nhttp://remote.example/path",
        "9222\n/devtools/page/a",
        "9222\n/devtools/browser/a\nextra",
    ],
)
def test_discovery_rejects_invalid_debugger_metadata(tmp_path, metadata):
    (tmp_path / "DevToolsActivePort").write_text(metadata, encoding="utf-8")
    assert discover_edge(str(tmp_path))["reason"] == "invalid_debugging_metadata"


def test_existing_attach_persists_profile_and_failure_preserves_previous(monkeypatch, tmp_path):
    browser = Browser(config_path=tmp_path / "config.json")
    discovered = {
        "available": True,
        "endpoint": "ws://127.0.0.1:53333/devtools/browser/abc",
        "user_data_dir": str(tmp_path / "Edge"),
    }
    monkeypatch.setattr(browser, "discover", lambda _: discovered)
    monkeypatch.setattr(browser, "_connect", lambda **_: None)
    monkeypatch.setattr(browser, "tabs", list)
    assert browser.attach_existing()["existing_session"]
    config = json.loads((tmp_path / "config.json").read_text())
    assert config["user_data_dir"] == discovered["user_data_dir"]
    assert Browser(config_path=tmp_path / "config.json")._user_data_dir == config["user_data_dir"]

    def rejected(**_):
        raise RuntimeError("consent denied")

    monkeypatch.setattr(browser, "_connect", rejected)
    monkeypatch.setattr(
        browser,
        "discover",
        lambda _: {**discovered, "endpoint": "ws://127.0.0.1:54444/devtools/browser/new"},
    )
    with pytest.raises(RuntimeError, match="consent denied"):
        browser.attach_existing()
    assert browser.endpoint == config["endpoint"]
    assert json.loads((tmp_path / "config.json").read_text()) == config


def test_reconnect_rediscovers_changed_port(monkeypatch, tmp_path):
    import sys

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "endpoint": "ws://127.0.0.1:53333/devtools/browser/old",
                "user_data_dir": str(tmp_path / "Edge"),
            }
        )
    )
    endpoint = "ws://127.0.0.1:54444/devtools/browser/new"
    monkeypatch.setattr(
        "clawpaw.browser.discover_edge", lambda _: {"available": True, "endpoint": endpoint}
    )
    connections = []
    remote = SimpleNamespace(is_connected=lambda: True)
    driver = SimpleNamespace(
        stop=lambda: None,
        chromium=SimpleNamespace(
            connect_over_cdp=lambda address, **_: connections.append(address) or remote
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: driver)),
    )
    with Browser(config_path=config) as browser:
        browser._connect()
    assert connections == [endpoint]


def test_attach_error_preserves_actual_timeout_and_protocol_diagnostics(monkeypatch, tmp_path):
    import sys

    def timeout(*_, **__):
        raise TimeoutError("connect_over_cdp: Timeout 30000ms exceeded during browser approval")

    stopped = []
    driver = SimpleNamespace(
        stop=lambda: stopped.append(True),
        chromium=SimpleNamespace(connect_over_cdp=timeout),
    )
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: driver)),
    )
    with Browser(endpoint="http://127.0.0.1:9222", config_path=tmp_path / "config.json") as b:
        response = handle_request(rpc("browser.tabs"), lambda: None, lambda: b)
    data = response["error"]["data"]
    assert data["reason"] == "connection_timeout"
    assert data["cause_type"] == "TimeoutError"
    assert "30000ms exceeded" in data["cause"]
    assert "do not infer debugging is disabled" in data["hint"]
    assert stopped == [True]


def test_opt_in_consent_is_scoped_to_one_attach_and_reported(monkeypatch, tmp_path):
    events = []

    class Consent:
        def __init__(self, window, timeout_ms):
            events.append(("scope", window, timeout_ms))
            self.result = {"requested": True, "approved": True}

        def __enter__(self):
            events.append("watching")
            return self

        def __exit__(self, *_):
            events.append("stopped")

    monkeypatch.setattr("clawpaw.consent.EdgeConsent", Consent)
    with Browser(config_path=tmp_path / "config.json") as b:
        monkeypatch.setattr(
            b,
            "discover",
            lambda _: {
                "available": True,
                "endpoint": "ws://127.0.0.1:9222/devtools/browser/test",
                "user_data_dir": str(tmp_path),
            },
        )
        monkeypatch.setattr(b, "_connect", lambda **_: events.append("connect"))
        monkeypatch.setattr(b, "tabs", lambda: events.append("tabs") or [])
        result = b.attach_existing(approve_window=123)
        assert result["consent"]["approved"]
        assert events == [("scope", 123, 30000), "watching", "connect", "stopped", "tabs"]
        events.clear()
        assert not b.attach_existing()["consent"]["requested"]
        assert events == ["connect", "tabs"]


def test_browser_batch_validates_before_attach_and_reuses_connection(monkeypatch, tmp_path):
    events = []
    with Browser(config_path=tmp_path / "config.json") as b:
        monkeypatch.setattr(
            b, "attach_existing", lambda *args: events.append(("attach", args)) or {}
        )
        monkeypatch.setattr(b, "tabs", lambda: events.append("tabs") or [])

        def snapshot(tab):
            events.append(("snapshot", tab))
            raise RuntimeError("page gone")

        monkeypatch.setattr(b, "snapshot", snapshot)
        with pytest.raises(TypeError):
            b.run_actions([{"method": "tabs"}, {"method": "snapshot", "params": {"wrong": 1}}])
        with pytest.raises(ValueError):
            b.run_actions([{"method": "close"}])
        assert events == []
        result = b.run_actions([{"method": "tabs"}, {"method": "browser.tabs"}], approve_window=123)
        assert result["completed_actions"] == 2
        assert events == [("attach", (None, 30000, 123)), "tabs", "tabs"]
        events.clear()
        from clawpaw.desktop import BatchError

        with pytest.raises(BatchError) as error:
            b.run_actions(
                [
                    {"method": "tabs"},
                    {"method": "snapshot", "params": {"tab": "observed"}},
                    {"method": "tabs"},
                ]
            )
        assert error.value.completed_actions == 1
        assert events[-1] == ("snapshot", "observed")
