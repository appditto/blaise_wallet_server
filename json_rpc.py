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
            log.server_logger.exception('Exception in JRPC Request')
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
            'amount': amount - fee,
            'fee': fee,
            'payload': payload,
            'payload_method': 'none'
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'result' not in response or 'ophash' not in response['result']:
            if response is not None:
                log.server_logger.error(f'sendto: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']

    async def changeaccountinfo(self, signer: int, target: int, new_b58_pubkey: str, fee: float = 0.0001):
        method = 'changeaccountinfo'
        params = {
            'account_signer': signer,
            'account_target': target,
            'new_b58_pubkey': new_b58_pubkey,
            'fee': fee
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'error' in response or 'result' not in response or 'ophash' not in response['result']:
            if response is not None:
                log.server_logger.error(f'changeaccountinfo: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']

    async def changekey(self, account: int, new_b58_pubkey: str, fee: float = 0.0001):
        method = 'changekey'
        params = {
            'account': account,
            'new_b58_pubkey': new_b58_pubkey,
            'fee': fee
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'error' in response or 'result' not in response or 'ophash' not in response['result']:
            if response is not None:
                log.server_logger.error(f'changekey: received invalid jsonrpc response {json.dumps(response)}')
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

    async def findoperation(self, ophash: str):
        method = 'findoperation'
        params = {
            'ophash': ophash
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'result' not in response or 'error' in response:
            if response is not None:
                log.server_logger.error(f'findoperation: received invalid jsonrpc response {json.dumps(response)}')
            return None
        return response['result']

    async def send_and_transfer(self, sender: int, target: int, amount: int, payload: str, b58_pubkey:str, fee: int = 0.0002):
        method = 'multioperationaddoperation'
        senders = {
            'account': sender,
            'amount': amount,
            'payload': payload
        }
        receivers = {
            'target': target,
            'amount': amount - fee,
            'payload': payload
        }
        changers = {
            'account': sender,
            'new_b58_pubkey': b58_pubkey
        }
        params = {
            'auto_n_operation': True,
            'senders': senders,
            'receivers': receivers,
            'changesinfo': changers
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'result' not in response or 'error' in response:
            if response is not None:
                log.server_logger.error(f'multioperationaddoperation: received invalid jsonrpc response {json.dumps(response)}')
            return None
        rawop = response['result']['rawoperations']
        execop_resp = await self.executeoperations(rawop)
        return execop_resp

    async def executeoperations(self, rawoperations: str):
        method = 'executeoperations'
        params = {
            'rawoperations': rawoperations
        }
        response = await self.jsonrpc_request(method, params)
        if response is None or 'result' not in response or 'error' in response:
            if response is not None:
                log.server_logger.error(f'executeoperations: received invalid jsonrpc response {json.dumps(response)}')
            return None
        rawop = response['result']