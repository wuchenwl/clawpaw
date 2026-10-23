import io
import json
from typing import ClassVar

from clawpaw.desktop import BackgroundActionError, BatchError
from clawpaw.protocol import handle_request, serve


class FakeDesktop:
    instances: ClassVar[list] = []

    def __init__(self):
        self.closed = False
        self.calls = 0
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def list_windows(self):
        self.calls += 1
        return [{"handle": 123, "title": "中文窗口"}]

    def run_actions(self, actions):
        raise BatchError(1, RuntimeError("target disappeared"))


def request(method, params=None, request_id=1):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {} if params is None else params,
        }
    )


def test_discovery_does_not_initialize_desktop():
    def unavailable():
        raise AssertionError("must stay lazy")

    response = handle_request(request("tools.list"), unavailable)
    assert any(t["name"] == "windows.screenshot" for t in response["result"]["tools"])


def test_session_reuse_unicode_and_cleanup():
    FakeDesktop.instances.clear()
    input_stream = io.StringIO(
        "\n".join(
            [
                request("tools.list"),
                request("windows.list_windows", request_id=2),
                request("windows.list_windows", request_id=3),
            ]
        )
    )
    output = io.StringIO()
    serve(input_stream, output, FakeDesktop)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert responses[1]["result"][0]["title"] == "中文窗口"
    assert len(FakeDesktop.instances) == 1
    assert FakeDesktop.instances[0].calls == 2
    assert FakeDesktop.instances[0].closed


def test_bad_request_recovers_and_blocks_private_methods():
    output = io.StringIO()
    serve(
        io.StringIO(
            "{bad}\n[]\n"
            + request("windows.__getattribute__")
            + "\n"
            + request("windows.list_windows", {"unexpected": 1})
            + "\n"
            + request("windows.list_windows")
            + "\n"
        ),
        output,
        FakeDesktop,
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [r["error"]["code"] for r in responses[:4]] == [-32700, -32600, -32601, -32602]
    assert "result" in responses[4]


def test_notifications_have_no_response():
    desktop = FakeDesktop()
    response = handle_request('{"jsonrpc":"2.0","method":"windows.list_windows"}', lambda: desktop)
    assert response is None
    assert desktop.calls == 1


def test_batch_error_exposes_completed_actions():
    response = handle_request(request("windows.run_actions", {"actions": []}), FakeDesktop)
    assert response["error"]["data"] == {"completed_actions": 1}


def test_background_refusal_reports_no_actions_without_advising_parameter_retry():
    class BackgroundDesktop:
        def run_actions(self, actions, background_only=False):
            raise BackgroundActionError("focus")

    response = handle_request(
        request("windows.run_actions", {"actions": [{"method": "focus"}], "background_only": True}),
        BackgroundDesktop,
    )
    assert response["error"]["code"] == -32000
    data = response["error"]["data"]
    assert data["reason"] == "foreground_input_rejected"
    assert data["actions_started"] is False
    assert data["rejected_action"] == "focus"


def test_nan_is_not_json():
    response = handle_request('{"jsonrpc":"2.0","id":NaN,"method":"tools.list"}', FakeDesktop)
    assert response["error"]["code"] == -32700


def test_describe_is_lazy_and_returns_only_requested_schema():
    def unavailable():
        raise AssertionError("No backend is needed")

    response = handle_request(
        request("tools.describe", {"name": "windows.run_actions"}), unavailable
    )
    schema = response["result"]["input_schema"]
    assert response["result"]["name"] == "windows.run_actions"
    assert "actions" in schema["properties"] and "window" not in schema["properties"]


def test_logged_bad_batch_returns_schema_and_never_runs_actions():
    desktop = FakeDesktop()
    response = handle_request(
        request(
            "windows.run_actions",
            {
                "window": 123456,
                "actions": [{"action": "activate"}],
                "screenshot": True,
            },
        ),
        lambda: desktop,
    )
    data = response["error"]["data"]
    assert response["error"]["code"] == -32602
    assert data["stage"] == "parameter_binding" and not data["actions_started"]
    assert data["input_schema"]["additionalProperties"] is False


def test_tools_list_bad_params_is_recoverable():
    response = handle_request(request("tools.list", {"window": 1}), FakeDesktop)
    assert response["error"]["code"] == -32602
