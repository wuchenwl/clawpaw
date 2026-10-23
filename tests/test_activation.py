from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from clawpaw.native import ForegroundActivationError, Native
from clawpaw.protocol import handle_request


def native_with_user(foregrounds, api_result=False, minimized=False):
    native = Native.__new__(Native)
    native.window_rect = Mock(return_value={})
    native.user = SimpleNamespace(
        IsIconic=Mock(return_value=minimized),
        ShowWindowAsync=Mock(),
        GetForegroundWindow=Mock(side_effect=foregrounds),
        SetForegroundWindow=Mock(return_value=api_result),
        GetWindowThreadProcessId=Mock(side_effect=lambda handle, _: {99: 2, 123: 3}[handle]),
        AttachThreadInput=Mock(return_value=True),
        BringWindowToTop=Mock(),
        PeekMessageW=Mock(),
    )
    native.kernel = SimpleNamespace(GetCurrentThreadId=Mock(return_value=1))
    return native


def test_already_foreground_does_not_attempt_switch():
    native = native_with_user([123])
    native.activate(123)
    native.user.SetForegroundWindow.assert_not_called()


def test_actual_focus_wins_over_false_api_return():
    native = native_with_user([99, 123], api_result=False)
    native.activate(123)
    native.user.SetForegroundWindow.assert_called_once_with(123)


def test_true_api_return_does_not_claim_unverified_focus(monkeypatch):
    native = native_with_user([99, 99, 99], api_result=True)
    clock = iter([0, 1, 2, 3])
    monkeypatch.setattr("clawpaw.native.time.monotonic", lambda: next(clock))
    with pytest.raises(ForegroundActivationError) as error:
        native.activate(123)
    assert error.value.data == {
        "reason": "foreground_not_changed",
        "window": 123,
        "foreground_window": 99,
        "api_returned_success": True,
        "recovery": {
            "method": "attach_thread_input",
            "attached_threads": [2, 3],
            "api_returned_success": True,
        },
    }


def test_input_queue_recovery_verifies_focus_and_always_detaches():
    native = native_with_user([99], api_result=False)
    native._wait_foreground = Mock(side_effect=[99, 123])
    assert native.activate(123)["activation_method"] == "attached_input_queue"
    assert native.user.SetForegroundWindow.call_count == 2
    native.user.BringWindowToTop.assert_called_once_with(123)
    assert native.user.AttachThreadInput.call_args_list == [
        call(1, 2, True),
        call(1, 3, True),
        call(1, 3, False),
        call(1, 2, False),
    ]


def test_partial_attach_failure_detaches_and_does_not_attempt_switch():
    native = native_with_user([99, 99])
    native._wait_foreground = Mock(return_value=99)
    native.user.AttachThreadInput.side_effect = [True, False, True]
    with pytest.raises(ForegroundActivationError) as error:
        native.activate(123)
    assert error.value.data["recovery"]["reason"] == "input_queue_attach_failed"
    assert native.user.AttachThreadInput.call_args_list == [
        call(1, 2, True),
        call(1, 3, True),
        call(1, 2, False),
    ]
    native.user.BringWindowToTop.assert_not_called()
    native.user.SetForegroundWindow.assert_called_once_with(123)


def test_recovery_exception_still_detaches():
    native = native_with_user([99])
    native._wait_foreground = Mock(return_value=99)
    native.user.BringWindowToTop.side_effect = OSError("window closed during recovery")
    with pytest.raises(OSError, match="window closed"):
        native.activate(123)
    assert native.user.AttachThreadInput.call_args_list[-2:] == [
        call(1, 3, False),
        call(1, 2, False),
    ]


def test_same_input_thread_is_attached_once():
    native = native_with_user([99])
    native.user.GetWindowThreadProcessId.return_value = 2
    native.user.GetWindowThreadProcessId.side_effect = None
    native._wait_foreground = Mock(side_effect=[99, 123])
    native.activate(123)
    assert native.user.AttachThreadInput.call_args_list == [call(1, 2, True), call(1, 2, False)]


def test_minimized_window_restores_before_foreground_check():
    native = native_with_user([123], minimized=True)
    native.user.IsIconic.side_effect = [True, False]
    native.activate(123)
    native.user.ShowWindowAsync.assert_called_once_with(123, 9)
    native.user.SetForegroundWindow.assert_not_called()


def test_activation_error_preserves_focus_diagnostics():
    class Desktop:
        def activate(self, window):
            raise ForegroundActivationError(window, 99, False)

    response = handle_request(
        '{"jsonrpc":"2.0","id":1,"method":"windows.activate","params":{"window":123}}',
        Desktop,
    )
    assert response["error"]["code"] == -32000
    assert response["error"]["data"]["foreground_window"] == 99


def test_inspect_options_are_parameter_errors_not_permissions():
    class Desktop:
        def activate(self, window):
            raise AssertionError("must not run with wrong parameters")

    response = handle_request(
        '{"jsonrpc":"2.0","id":1,"method":"windows.activate",'
        '"params":{"window":123,"max_depth":6}}',
        Desktop,
    )
    assert response["error"]["code"] == -32602
    assert "max_depth" in response["error"]["message"]


def test_batch_preserves_activation_diagnostics():
    from clawpaw.desktop import BatchError

    class Desktop:
        def run_actions(self, actions):
            raise BatchError(0, ForegroundActivationError(123, 99, False))

    response = handle_request(
        '{"jsonrpc":"2.0","id":1,"method":"windows.run_actions",'
        '"params":{"actions":[{"method":"activate","params":{"window":123}}]}}',
        Desktop,
    )
    data = response["error"]["data"]
    assert data["completed_actions"] == 0
    assert data["reason"] == "foreground_not_changed"
    assert data["foreground_window"] == 99
