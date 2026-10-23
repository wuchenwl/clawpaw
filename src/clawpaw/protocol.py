"""Local JSON-RPC 2.0 over newline-delimited JSON; deliberately not an MCP server."""

import inspect as signatures
import json
from contextlib import ExitStack

from .browser import Browser, BrowserConnectionError
from .desktop import BackgroundActionError, BatchError, Desktop
from .native import ForegroundActivationError


def field(kind, **kwargs):
    return {"type": kind, **kwargs}


REGION = field(
    "object",
    properties={name: field("integer") for name in ("left", "top", "width", "height")},
    required=["left", "top", "width", "height"],
    additionalProperties=False,
)
SCREENSHOT_PROPERTIES = {
    "window": field("integer", minimum=1),
    "region": REGION,
    "max_width": field("integer", minimum=1, maximum=32768),
    "format": field("string", enum=["png", "jpeg"], default="png"),
    "quality": field("integer", minimum=1, maximum=95, default=80),
    "path": field("string", minLength=1),
    "inline": field("boolean", default=False),
}
UIA_PROPERTIES = {
    "window": field("integer", minimum=1),
    "selector": field(
        "object",
        properties={
            key: field("string", minLength=1) for key in ("name", "automation_id", "control_type")
        },
        minProperties=1,
        additionalProperties=False,
    ),
    "max_depth": field("integer", minimum=0, maximum=10, default=10),
    "max_nodes": field("integer", minimum=1, maximum=2000, default=2000),
}


def tool(name, description, properties=None, required=(), namespace="windows"):
    return {
        "name": f"{namespace}.{name}",
        "description": description,
        "input_schema": field(
            "object",
            properties=properties or {},
            required=list(required),
            additionalProperties=False,
        ),
    }


TOOLS = [
    tool(
        "list", "List all supported tool schemas without starting any backend.", namespace="tools"
    ),
    tool(
        "describe",
        "Get one tool's exact parameter schema without starting any backend.",
        {"name": field("string")},
        ["name"],
        namespace="tools",
    ),
    tool("list_windows", "List visible titled windows with HWND, process and physical bounds."),
    tool(
        "activate",
        "Restore and bring a window to the foreground; success verifies the actual "
        "foreground window. Includes one input-queue recovery without mouse or key input. "
        "Takes only window (HWND), not inspect parameters.",
        {"window": field("integer", minimum=1)},
        ["window"],
    ),
    tool(
        "open_url",
        "Open a URL in an explicitly selected browser HWND using Windows input. "
        "Default opens a new tab and returns a fresh screenshot. Useful before CDP attach; "
        "navigation_requested does not prove page load or debugging enabled.",
        {
            "window": field("integer", minimum=1),
            "url": field("string", minLength=1, maxLength=10000),
            "new_tab": field("boolean", default=True),
            "screenshot_after": field("boolean", default=True),
        },
        ["window", "url"],
    ),
    tool(
        "screenshot",
        "Capture visible desktop pixels; window only crops its screen rectangle and can "
        "include other windows covering it. Does not activate or capture a hidden render layer. "
        "Default: a local file; inline returns base64. Map image x/y through region origin "
        "and desktop_pixels_per_image_pixel.",
        SCREENSHOT_PROPERTIES,
    ),
    tool(
        "move",
        "Move pointer to physical desktop coordinates, including negative coordinates.",
        {"x": field("integer"), "y": field("integer")},
        ["x", "y"],
    ),
    tool(
        "click",
        "Click x/y or an element_id from the most recent inspect. "
        "Element clicks use the current rectangle center.",
        {
            "x": field("integer"),
            "y": field("integer"),
            "element_id": field("string"),
            "button": field("string", enum=["left", "right", "middle"], default="left"),
            "count": field("integer", minimum=1, maximum=2, default=1),
        },
    ),
    tool(
        "type_text",
        "Type Unicode text into the currently focused control.",
        {"text": field("string", maxLength=100000)},
        ["text"],
    ),
    tool(
        "press_keys",
        "Press a chord, e.g. [ctrl, a] or [enter]; release in reverse order.",
        {"keys": field("array", items=field("string"), minItems=1, maxItems=8)},
        ["keys"],
    ),
    tool(
        "scroll",
        "Scroll at the pointer. Positive steps: up, or right if horizontal.",
        {
            "steps": field("integer", minimum=-100, maximum=100),
            "horizontal": field("boolean", default=False),
        },
        ["steps"],
    ),
    tool(
        "wait",
        "Wait for the application to settle; bounded to 60 seconds.",
        {"seconds": field("number", minimum=0, maximum=60, default=0.2)},
    ),
    tool(
        "inspect",
        "Read a bounded UIA tree. Requires the optional uia extra. "
        "Includes native_window_handle for scoping dialog inspection. "
        "Element IDs expire on the next successful inspect or session close.",
        {
            "window": field("integer", minimum=1),
            "max_depth": field("integer", minimum=0, maximum=10, default=4),
            "max_nodes": field("integer", minimum=1, maximum=2000, default=200),
        },
        ["window"],
    ),
    tool(
        "invoke",
        "Invoke a unique freshly resolved UIA control using InvokePattern.",
        UIA_PROPERTIES,
        ["window", "selector"],
    ),
    tool(
        "background_click",
        "Click a unique UIA button through InvokePattern without pointer input, "
        "activation or SetFocus. Unsupported controls fail; no coordinate fallback.",
        UIA_PROPERTIES,
        ["window", "selector"],
    ),
    tool(
        "set_toggle",
        "Set a UIA checkbox to checked/unchecked through TogglePattern without "
        "mouse or focus. Already-correct state is unchanged; result is verified.",
        {**UIA_PROPERTIES, "checked": field("boolean")},
        ["window", "selector", "checked"],
    ),
    tool(
        "set_value",
        "Replace a unique UIA control's value using ValuePattern; no keyboard input.",
        {**UIA_PROPERTIES, "text": field("string", maxLength=100000)},
        ["window", "selector", "text"],
    ),
    tool(
        "focus",
        "Set focus on a unique freshly resolved UIA control.",
        UIA_PROPERTIES,
        ["window", "selector"],
    ),
    tool(
        "run_actions",
        "Run actions sequentially and optionally capture once at the end. "
        "background_only rejects all foreground input before any action starts. "
        "On failure, completed_actions counts finished actions; the failed action may "
        "have partially executed. Completed actions are not rolled back.",
        {
            "actions": field(
                "array",
                minItems=1,
                maxItems=100,
                items=field(
                    "object",
                    properties={
                        "method": field(
                            "string",
                            enum=[
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
                            ],
                        ),
                        "params": field("object"),
                    },
                    required=["method"],
                    additionalProperties=False,
                ),
            ),
            "screenshot_after": field("boolean", default=True),
            "background_only": field("boolean", default=False),
            "screenshot_options": field(
                "object", properties=SCREENSHOT_PROPERTIES, additionalProperties=False
            ),
        },
        ["actions"],
    ),
]
TAB = {"tab": field("string", minLength=1)}
TIMEOUT = {"timeout_ms": field("integer", minimum=1, maximum=30000, default=8000)}
NAV_TIMEOUT = {"timeout_ms": field("integer", minimum=1, maximum=30000, default=15000)}
BROWSER_SELECTOR = field(
    "object",
    oneOf=[
        field(
            "object",
            properties={"role": field("string", minLength=1), "name": field("string", minLength=1)},
            required=["role", "name"],
            additionalProperties=False,
        ),
        field(
            "object",
            properties={"label": field("string", minLength=1)},
            required=["label"],
            additionalProperties=False,
        ),
        field(
            "object",
            properties={"test_id": field("string", minLength=1)},
            required=["test_id"],
            additionalProperties=False,
        ),
        field(
            "object",
            properties={"placeholder": field("string", minLength=1)},
            required=["placeholder"],
            additionalProperties=False,
        ),
        field(
            "object",
            properties={"id": field("string", minLength=1)},
            required=["id"],
            additionalProperties=False,
        ),
    ],
)
TARGET = {**TAB, "selector": BROWSER_SELECTOR, **TIMEOUT}


def browser_tool(name, description, properties=None, required=()):
    entry = tool(name, description, properties, required)
    entry["name"] = f"browser.{name}"
    return entry


TOOLS.extend(
    [
        browser_tool(
            "status", "Check Clawpaw's loopback browser endpoint; does not launch a browser."
        ),
        browser_tool(
            "attach",
            "Attach to an explicitly supplied loopback HTTP or browser WebSocket CDP endpoint and save it.",
            {"endpoint": field("string")},
            ["endpoint"],
        ),
        browser_tool(
            "discover",
            "Discover existing Edge debugging metadata without connecting or changing settings.",
            {"user_data_dir": field("string", minLength=1)},
        ),
        browser_tool(
            "attach_existing",
            "Attach to the signed-in Edge using its DevToolsActivePort metadata. "
            "approve_window opts in to background approval of this attempt's fresh Edge "
            "prompt, only with prior user authorization. No separate login. CLI exit disconnects.",
            {
                "user_data_dir": field("string", minLength=1),
                "timeout_ms": field("integer", minimum=1, maximum=30000, default=30000),
                "approve_window": field("integer", minimum=1),
            },
        ),
        browser_tool(
            "run_actions",
            "Connect to existing Edge and perform actions in one process/approved connection. "
            "Optional approve_window is explicitly authorized background consent for this "
            "attempt only. Prevalidates signatures; stops on first failure with completed_actions.",
            {
                "user_data_dir": field("string", minLength=1),
                "timeout_ms": field("integer", minimum=1, maximum=30000, default=30000),
                "approve_window": field("integer", minimum=1),
                "actions": field(
                    "array",
                    minItems=1,
                    maxItems=100,
                    items=field(
                        "object",
                        properties={
                            "method": field(
                                "string",
                                enum=[
                                    "tabs",
                                    "open",
                                    "navigate",
                                    "snapshot",
                                    "click",
                                    "fill",
                                    "press",
                                    "wait",
                                    "screenshot",
                                ],
                            ),
                            "params": field("object"),
                        },
                        required=["method"],
                        additionalProperties=False,
                    ),
                ),
            },
            ["actions"],
        ),
        browser_tool(
            "launch",
            "Launch Edge with a separate persistent Clawpaw profile; login separately.",
            {"port": field("integer", minimum=1024, maximum=65535, default=9222)},
        ),
        browser_tool("tabs", "List browser tabs with stable CDP target IDs, URLs and titles."),
        browser_tool(
            "open",
            "Open an http/https URL in a new tab; leaves it open after client exit.",
            {"url": field("string"), **NAV_TIMEOUT},
            ["url"],
        ),
        browser_tool(
            "navigate",
            "Navigate a specific tab to an http/https URL.",
            {**TAB, "url": field("string"), **NAV_TIMEOUT},
            ["tab", "url"],
        ),
        browser_tool(
            "snapshot",
            "Read main-frame accessibility roles/names and text; bounded output.",
            {
                **TAB,
                "max_chars": field("integer", minimum=100, maximum=100000, default=16000),
                "max_elements": field("integer", minimum=1, maximum=500, default=100),
                **TIMEOUT,
            },
            ["tab"],
        ),
        browser_tool(
            "click",
            "Click a unique visible element using an exact semantic selector or observed DOM id.",
            TARGET,
            ["tab", "selector"],
        ),
        browser_tool(
            "fill",
            "Replace text in a uniquely matching input or contenteditable element.",
            {**TARGET, "text": field("string", maxLength=100000)},
            ["tab", "selector", "text"],
        ),
        browser_tool(
            "press",
            "Press a Playwright key chord on a unique element, e.g. Enter or Control+A.",
            {**TARGET, "key": field("string", minLength=1, maxLength=100)},
            ["tab", "selector", "key"],
        ),
        browser_tool(
            "wait",
            "Wait for a selected element state without fixed sleeps.",
            {
                **TARGET,
                **NAV_TIMEOUT,
                "state": field(
                    "string", enum=["visible", "hidden", "attached", "detached"], default="visible"
                ),
            },
            ["tab", "selector"],
        ),
        browser_tool(
            "screenshot",
            "Capture a browser page to an absolute PNG path.",
            {
                **TAB,
                "path": field("string", minLength=1),
                "full_page": field("boolean", default=False),
                **TIMEOUT,
            },
            ["tab"],
        ),
    ]
)
METHODS = {entry["name"]: entry["name"].split(".", 1)[1] for entry in TOOLS}
TOOL_BY_NAME = {entry["name"]: entry for entry in TOOLS}


def error_response(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def reject_constant(value):
    raise ValueError(f"Non-JSON number: {value}")


def handle_request(raw, get_desktop, get_browser=None):
    try:
        request = json.loads(raw, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return error_response(None, -32700, "Parse error")
    if (
        not isinstance(request, dict)
        or request.get("jsonrpc") != "2.0"
        or not isinstance(request.get("method"), str)
    ):
        return error_response(None, -32600, "Invalid Request")
    request_id = request.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int, type(None))):
        return error_response(None, -32600, "id must be a string, integer or null")
    notification = "id" not in request
    method, params = request["method"], request.get("params", {})
    if not isinstance(params, dict):
        response = error_response(request_id, -32602, "params must be an object")
    elif method not in METHODS and method != "tools.list":
        response = error_response(request_id, -32601, "Method not found")
    else:
        binding_failed = False
        try:
            if method == "tools.list":
                if params:
                    raise ValueError("tools.list takes no parameters")
                result = {"tools": TOOLS}
            elif method == "tools.describe":
                if set(params) != {"name"} or not isinstance(params["name"], str):
                    raise ValueError("tools.describe requires exactly one string parameter: name")
                if params["name"] not in TOOL_BY_NAME:
                    raise ValueError("Unknown tool name; use tools.list")
                result = TOOL_BY_NAME[params["name"]]
            else:
                if method.startswith("browser."):
                    if get_browser is None:
                        raise RuntimeError("Browser factory is unavailable")
                    backend = get_browser()
                else:
                    backend = get_desktop()
                fn = getattr(backend, METHODS[method])
                try:
                    signatures.signature(fn).bind(**params)
                except TypeError:
                    binding_failed = True
                    raise
                result = fn(**params)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except BatchError as error:
            response = error_response(request_id, -32000, str(error), error.data)
        except ForegroundActivationError as error:
            response = error_response(request_id, -32000, str(error), error.data)
        except BrowserConnectionError as error:
            response = error_response(request_id, -32000, str(error), error.data)
        except BackgroundActionError as error:
            response = error_response(
                request_id,
                -32000,
                str(error),
                {
                    "reason": "foreground_input_rejected",
                    "stage": "batch_preflight",
                    "actions_started": False,
                    "rejected_action": error.method,
                    "hint": "Keep background_only enabled and use UIA or browser element actions.",
                },
            )
        except (TypeError, ValueError) as error:
            data = {
                "method": method,
                "input_schema": TOOL_BY_NAME[method]["input_schema"],
                "hint": "Correct the arguments from input_schema and retry. For shell JSON "
                "quoting use --params-file; tools.describe returns a single tool schema.",
            }
            if binding_failed:
                data.update(stage="parameter_binding", actions_started=False)
            response = error_response(request_id, -32602, str(error), data)
        except Exception as error:  # noqa: BLE001 -- keep the transport alive on backend failures
            response = error_response(request_id, -32000, str(error))
    return None if notification else response


def serve(input_stream, output_stream, desktop_factory=Desktop, browser_factory=Browser):
    """One persistent desktop, sequential actions, one response per request line."""
    with ExitStack() as stack:
        desktop = None
        browser = None

        def get_desktop():
            nonlocal desktop
            if desktop is None:
                desktop = stack.enter_context(desktop_factory())
            return desktop

        def get_browser():
            nonlocal browser
            if browser is None:
                browser = stack.enter_context(browser_factory())
            return browser

        for raw in input_stream:
            if not raw.strip():
                continue
            response = handle_request(raw, get_desktop, get_browser)
            if response is not None:
                output_stream.write(
                    json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n"
                )
                output_stream.flush()
