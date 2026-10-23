import json


def rpc(method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
