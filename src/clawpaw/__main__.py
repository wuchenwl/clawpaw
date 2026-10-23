import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path

from . import Browser, Desktop, __version__
from .protocol import TOOLS, error_response, handle_request, reject_constant, serve


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clawpaw Windows and browser tools")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Persistent JSONL server; stdin must remain open")
    commands.add_parser("tools", help="Print tool schemas and exit")
    describe_parser = commands.add_parser("describe", help="Print one tool's exact schema and exit")
    describe_parser.add_argument("method", help="For example: windows.run_actions")
    call_parser = commands.add_parser("call", help="Run one tool, print JSON and exit")
    call_parser.add_argument("method", help="For example: windows.list_windows")
    params = call_parser.add_mutually_exclusive_group()
    params.add_argument("--params", help="JSON object; use --params-file for Windows shell quoting")
    params.add_argument("--params-file", type=Path, help="Read a UTF-8 JSON object (BOM accepted)")
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if args.command == "tools":
        print(json.dumps({"tools": TOOLS}, ensure_ascii=False, indent=2))
    elif args.command == "describe":
        response = handle_request(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools.describe",
                    "params": {"name": args.method},
                }
            ),
            lambda: None,
        )
        print(json.dumps(response, ensure_ascii=False, allow_nan=False))
        return 1 if "error" in response else 0
    elif args.command == "call":
        try:
            text = (
                args.params_file.read_text(encoding="utf-8-sig")
                if args.params_file is not None
                else args.params or "{}"
            )
            parameters = json.loads(text, parse_constant=reject_constant)
        except (OSError, ValueError) as error:
            print(json.dumps(error_response(1, -32602, str(error)), ensure_ascii=False))
            return 1
        with ExitStack() as stack:
            desktop = None
            browser = None

            def get_desktop():
                nonlocal desktop
                if desktop is None:
                    desktop = stack.enter_context(Desktop())
                return desktop

            def get_browser():
                nonlocal browser
                if browser is None:
                    browser = stack.enter_context(Browser())
                return browser

            response = handle_request(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": args.method, "params": parameters}
                ),
                get_desktop,
                get_browser,
            )
            print(json.dumps(response, ensure_ascii=False, allow_nan=False))
            return 1 if "error" in response else 0
    else:
        try:
            serve(sys.stdin, sys.stdout)
        except KeyboardInterrupt:
            return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
