from aiohttp import log, ClientSession

class FreePasaAPI():
    def __init__(self, api_key : str):
        self.api_key = api_key
        self.request_url = 'https://freepasa.org/api/app/request.php'
        self.verify_url = 'https://freepasa.org/api/app/code.php'

    async def get_account(self, phone_iso : str, phone_number : str, pubkey : str) -> str:
        """Get an account from freepasa, return request ID if successful, None otherwise"""
        params = {
            'api_key': self.api_key,
            'phone_iso': phone_iso,
            'phone_number': phone_number,
            'public_key': pubkey
        }
        try:
            async with ClientSession() as session:
                async with session.get(self.request_url, params=params) as resp:
                    resp_json = await resp.json()
                    if 'status' in resp_json and resp_json['status'] == 'success' and 'request_id' in resp_json:
                        return resp_json['request_id']
                    log.server_logger.error(f'error response from freepasa {resp_json["status"]}, data: {str(resp_json["data"])}')
        except Exception:
            log.server_logger.exception()
        return None

    async def verify_sms(self, request_id: str, code: str) -> int:
        """Verify the freepasa SMS code, return an integer with account if successful"""
        params = {
            'api_key': self.api_key,
            'request_id': request_id,
            'code': code
        }
        try:
            async with ClientSession() as session:
                async with session.get(self.verify_url, params=params) as resp:
                    resp_json = await resp.json()
                    if 'status' in resp_json and resp_json['status'] == 'success':
                        return resp_json['data']['account']
                    elif 'status' in resp_json and resp_json['status'] == 'error':
                        for error in resp_json['data']:
                            if error == 'verification_failed':
                                return -1
                    log.server_logger.error(f'error response from freepasa {resp_json["status"]}, data: {str(resp_json["data"])}')
        except Exception:
            log.server_logger.exception()
        return None