import json
from aiohttp import ClientSession, log

class PascJsonRpc():
    def __init__(self, node_url: str = 'localhost', node_port: int = 4003):
        self.node_url = node_url
        self.node_port = node_port

    async def json_post(self, request_json: dict, timeout: int = 30) -> dict:
        try:
            async with ClientSession() as session:
                async with session.post(f'{self.node_url}:{self.node_port}', json=request_json, timeout=timeout) as resp:
                    if resp.status > 299:
                        log.server_logger.error('Received status code %d from request %s', resp.status, json.dumps(request_json))
                        raise Exception
                    return await resp.json(content_type=None)
        except Exception:
            log.server_logger.exception()
            return None

    async def findaccounts(self, start: int, b58_pubkey: str):
        request = {
            "jsonrpc": "2.0",
            "method":"findaccounts",
            "params": {
                'start': start,
                'b58_pubkey': b58_pubkey
            },
            "id": 1
        }
        response = await self.json_post(request)
        if response is None or 'result' not in response or not isinstance(response['result'], list):
            return None
        return response['result']