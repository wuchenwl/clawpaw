import json

import pytest

from clawpaw import __main__ as cli


class FakeDesktop:
    closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def list_windows(self):
        return [{"handle": 123, "title": "中文窗口"}]

    def type_text(self, text):
        return {"text": text}


@pytest.fixture
def output(capsys):
    return capsys


def test_call_closes_session_and_needs_no_stdin(monkeypatch, output):
    desktop = FakeDesktop()
    monkeypatch.setattr(cli, "Desktop", lambda: desktop)
    assert cli.main(["call", "windows.list_windows"]) == 0
    response = json.loads(output.readouterr().out)
    assert response["result"][0]["title"] == "中文窗口"
    assert desktop.closed


def test_call_discovery_stays_lazy(monkeypatch, output):
    def unavailable():
        raise AssertionError("must stay lazy")

    monkeypatch.setattr(cli, "Desktop", unavailable)
    assert cli.main(["call", "tools.list"]) == 0
    assert "tools" in json.loads(output.readouterr().out)["result"]


def test_call_unknown_method_returns_error_and_nonzero_exit(output):
    assert cli.main(["call", "windows.not_a_tool"]) == 1
    assert json.loads(output.readouterr().out)["error"]["code"] == -32601


def test_call_bom_file_and_unicode(monkeypatch, output, tmp_path):
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"text": "你好👋"}, ensure_ascii=False), encoding="utf-8-sig")
    monkeypatch.setattr(cli, "Desktop", FakeDesktop)
    assert cli.main(["call", "windows.type_text", "--params-file", str(path)]) == 0
    assert json.loads(output.readouterr().out)["result"]["text"] == "你好👋"


@pytest.mark.parametrize("params", ['{"text":', "[1]", '{"text":NaN}'])
def test_call_bad_params_returns_error(output, params):
    assert cli.main(["call", "windows.type_text", "--params", params]) == 1
    assert json.loads(output.readouterr().out)["error"]["code"] == -32602


def test_call_missing_params_file_returns_json_error(output, tmp_path):
    assert cli.main(["call", "windows.screenshot", "--params-file", str(tmp_path / "missing")]) == 1
    assert json.loads(output.readouterr().out)["error"]["code"] == -32602


def test_describe_exact_batch_schema_needs_no_desktop(monkeypatch, output):
    def unavailable():
        raise AssertionError("must stay lazy")

    monkeypatch.setattr(cli, "Desktop", unavailable)
    assert cli.main(["describe", "windows.run_actions"]) == 0
    schema = json.loads(output.readouterr().out)["result"]["input_schema"]
    assert set(schema["properties"]) == {
        "actions",
        "screenshot_after",
        "screenshot_options",
        "background_only",
    }
