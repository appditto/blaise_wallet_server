import json
from aiohttp import ClientSession, log

class PascJsonRpc():
    def __init__(self, node_url: str = 'localhost:4003'):
        self.node_url = node_url
        self.req_id = 1

    async def jsonrpc_request(self, method: str, params: dict = None, timeout: int = 30) -> dict:
        try:
            request_json = {
                "jsonrpc": "2.0",
                "method":method,
                "id": self.req_id
            }
            if params is not None:
                request_json['params'] = params
            self.req_id += 1
            async with ClientSession() as session:
                async with session.post(f'{self.node_url}', json=request_json, timeout=timeout) as resp:
                    if resp.status > 299:
                        log.server_logger.error('Received status code %d from request %s', resp.status, json.dumps(request_json))
                        raise Exception
                    return await resp.json(content_type=None)
        except Exception:
            log.server_logger.exception()
            return None

    async def findaccounts(self, start: int, b58_pubkey: str):
        method = 'findaccounts'
        params = {
            'start': start,
            'b58_pubkey': b58_pubkey
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'result' not in response or not isinstance(response['result'], list):
            if response is not None:
                log.server_logger.error(f'findaccounts: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']

    async def getaccount(self, pasa: int):
        method = 'getaccount'
        params = {
            'account': pasa
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'result' not in response or 'balance' not in response['result']:
            if response is not None:
                log.server_logger.error(f'getaccount: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']

    async def sendto(self, sender: int, target: int, amount: float, payload:str, fee: float = 0.0001):
        method = 'sendto'
        params = {
            'sender': sender,
            'target': target,
            'amount': amount,
            'fee': fee,
            'payload': payload
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'result' not in response or 'ophash' not in response['result']:
            if response is not None:
                log.server_logger.error(f'sendto: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']

    async def changeaccountinfo(self, signer: int, new_b58_pubkey: str, fee: float = 0.0001):
        method = 'changeaccountinfo'
        params = {
            'account_signer': signer,
            'new_b58_pubkey': new_b58_pubkey,
            'fee': fee
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'result' not in response or 'ophash' not in response['result']:
            if response is not None:
                log.server_logger.error(f'changeaccountinfo: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']

    async def getblockcount(self):
        method = 'getblockcount'
        response = await self.jsonrpc_request(method)
        if response is None or 'result' not in response or not isinstance(response['result'], int):
            if response is not None:
                log.server_logger.error(f'getblockcount: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']

    async def getblockoperations(self, block: int):
        method = 'getblockoperations'
        params = {
            'block': block
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'result' not in response or not isinstance(response['result'], list):
            if response is not None:
                log.server_logger.error(f'getblockoperations: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']

    async def getpendings(self):
        method = 'getpendings'
        response = await self.jsonrpc_request(method)
        if response is None or 'result' not in response or not isinstance(response['result'], list):
            if response is not None:
                log.server_logger.error(f'getpendings: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']