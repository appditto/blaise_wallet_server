"""PASA Distribution"""
import datetime
import json

import base58
from aiohttp import web, log
from aioredis import Redis

from json_rpc import PascJsonRpc
from util import Util
from settings import SIGNER_ACCOUNT, PUBKEY_B58, PASA_HARD_EXPIRY, PASA_SOFT_EXPIRY, PASA_PRICE

class PASAApi():
    def __init__(self, rpc_client: PascJsonRpc):
        self.rpc_client = rpc_client

    async def get_last_borrowed(self, redis: Redis):
        """Get index to start in findaccounts request"""
        last_bor = await redis.get("last_borrowed_pasa")
        if last_bor is None:
            await redis.set("last_borrowed_pasa", str(0))
            return int(0)
        return int(last_bor)

    async def set_last_borrowed(self, redis: Redis, pasa: int):
        await redis.set("last_borrowed_pasa", str(pasa))

    async def pasa_is_borrowed(self, redis: Redis, pasa : int):
        """returns true if PASA is already borrowed"""
        borrowed = await redis.get(f"borrowedpasa_{str(pasa)}")
        if borrowed is None:
            return False
        return True

    async def reset_expiry(self, redis: Redis, pasa_obj: dict):
        """Reset the expiry for a PASA"""
        pasa_obj['expires'] = Util.ms_since_epoch(datetime.datetime.utcnow()) + PASA_SOFT_EXPIRY
        await redis.set(f"borrowedpasa_{str(pasa_obj['pasa'])}", json.dumps(pasa_obj), expire=PASA_HARD_EXPIRY)
        return pasa_obj

    async def get_borrowed_pasa(self, redis: Redis, pasa : int):
        """get borrowed pasa"""
        borrowed = await redis.get(f"borrowedpasa_{str(pasa)}")
        return None if borrowed is None else json.loads(borrowed)

    async def pubkey_has_borrowed(self, redis: Redis, pubkey: str):
        """Returns PASA object if public key has already borrowed an account, None otherwise"""
        pasa = await redis.get(f"borrowed_pasapub_{pubkey}")
        if pasa is None:
            return None
        bpasa = await self.get_borrowed_pasa(redis, int(pasa))
        if bpasa is not None:
            return bpasa
        await redis.delete(f"borrowed_pasapub_{pubkey}")
        return None

    async def check_and_clear_borrow(self, redis: Redis, pubkey: str):
        pasa = await redis.get(f"borrowed_pasapub_{pubkey}")
        if pasa is None:
            return None
        await redis.delete(f"borrowedpasa_{str(pasa)}")
        await redis.delete(f"borrowed_pasapub_{pubkey}")


    async def initiate_borrow(self, redis: Redis, pubkey: str, pasa: int):
        """Mark an account as borrowed"""
        borrow_obj = {
            'b58_pubkey': pubkey,
            'pasa': pasa,
            'expires': Util.ms_since_epoch(datetime.datetime.utcnow()) + PASA_SOFT_EXPIRY,
            'price': PASA_PRICE,
            'paid': False,
            'transferred': False,
            'transfer_ophash': None
        }
        await redis.set(f'borrowedpasa_{str(pasa)}', json.dumps(borrow_obj), expire=PASA_HARD_EXPIRY)
        await redis.set(f"borrowed_pasapub_{pubkey}", str(pasa), expire=PASA_HARD_EXPIRY)
        return borrow_obj

    async def send_funds(self, redis: Redis, bpasa: dict):
        """Transfer the fee of the borrowed account to the signer, and mark it as paid"""
        payload = "Blaise PASA Fee"
        hex_payload = payload.encode("utf-8").hex()
        resp = await self.rpc_client.sendto(int(bpasa['pasa']), SIGNER_ACCOUNT, PASA_PRICE, hex_payload)
        if resp is None:
            return None
        # Mark account as paid
        log.server_logger.info(f"Account {bpasa['pasa']} has been sold to {bpasa['b58_pubkey']}, ophash {resp['ophash']}")
        bpasa['paid'] = True
        await redis.set(f'borrowedpasa_{str(bpasa["pasa"])}', json.dumps(bpasa), expire=PASA_HARD_EXPIRY)
        return resp['ophash']

    async def transfer_account(self, redis: Redis, pasa: int):
        """Change the key of a purchased account"""
        bpasa = await redis.get(f"borrowedpasa_{str(pasa)}")
        if bpasa is None:
            return None
        bpasa = json.loads(bpasa)
        if bpasa['paid']:
            resp = await self.rpc_client.changeaccountinfo(SIGNER_ACCOUNT, bpasa['pasa'], bpasa['b58_pubkey'])
            if resp is not None:
                bpasa['transferred'] = True
                bpasa['transfer_ophash'] = resp['ophash']
                await redis.set(f'borrowedpasa_{str(bpasa["pasa"])}', json.dumps(bpasa), expire=PASA_HARD_EXPIRY)
                log.server_logger.info(f"Transferred account {bpasa['pasa']} to {bpasa['b58_pubkey']}. hash: {resp['ophash']}")
                # Sale complete
                return resp['ophash']

    async def getborrowed(self, r: web.Request):
        """Get a borrowed account, if it exists"""
        req_json = await r.json()
        if 'b58_pubkey' not in req_json:
            return web.HTTPBadRequest(reason="Bad request - missing b58_pubkey")
        elif len(req_json['b58_pubkey']) < 80:
            log.server_logger.info(f'received invalid pubkey {req_json["b58_pubkey"]} (length)')
            return web.json_response({'error': 'invalid public key'})
        try:
            base58.b58decode(req_json['b58_pubkey'])
        except ValueError:
            log.server_logger.info(f'received invalid pubkey {req_json["b58_pubkey"]} (b58decode)')
            return web.json_response({'error': 'invalid public key'})
        # Get the account that is borrowed
        redis: Redis = r.app['rdata']
        bpasa = await self.pubkey_has_borrowed(redis, req_json['b58_pubkey'])
        if bpasa is not None:
            expiry = int(bpasa['expires'])
            if Util.ms_since_epoch(datetime.datetime.utcnow()) > expiry:
                bpasa = None
        resp_json = {
            'borrowed_account': bpasa if bpasa is not None else ''
        }
        return web.json_response(resp_json)

    async def borrow_account(self, r: web.Request):
        """Borrow an account
        {
            'action':'borrow_account',
            'b58_pubkey':'3g00...',
        }
        response:
        {
            'pasa':31334,
            'expires'3333333,
            'price':0.25
        }
        error:
        {
            'error': 'failed'
        }
        """
        req_json = await r.json()
        if 'b58_pubkey' not in req_json:
            return web.HTTPBadRequest(reason="Bad request - missing b58_pubkey")
        elif len(req_json['b58_pubkey']) < 80:
            log.server_logger.info(f'received invalid pubkey {req_json["b58_pubkey"]} (length)')
            return web.json_response({'error': 'invalid public key'})
        try:
            base58.b58decode(req_json['b58_pubkey'])
        except ValueError:
            log.server_logger.info(f'received invalid pubkey {req_json["b58_pubkey"]} (b58decode)')
            return web.json_response({'error': 'invalid public key'})
        redis: Redis = r.app['rdata'] 
        # Ensure this pubkey does not already have a borrowed account
        bpasa = await self.pubkey_has_borrowed(redis, req_json['b58_pubkey'])
        if bpasa is not None:
            # Reset expiry and return result
            log.server_logger.debug(f'resetting expiry and returning {req_json["b58_pubkey"]}, pasa {bpasa["pasa"]}')
            return web.json_response(await self.reset_expiry(redis, bpasa))
        # Do findaccounts request
        last_borrowed = await self.get_last_borrowed(redis)
        accounts = await self.rpc_client.findaccounts(start=last_borrowed, b58_pubkey=PUBKEY_B58)
        if accounts is None:
            log.server_logger.error('findaccounts response failed')
            return web.json_response({'error':'findaccounts response failed'})
        resp = None
        for acct in accounts:
            acctnum = acct['account']
            if acctnum == SIGNER_ACCOUNT:
                continue
            # Skip PASA that is already borrowed
            if await self.pasa_is_borrowed(redis, acctnum):
                continue
            # Initiate a borrow of this pubkey
            log.server_logger.debug(f'{req_json["b58_pubkey"]} is borrowing {acctnum}')
            resp = await self.initiate_borrow(redis, req_json['b58_pubkey'], acctnum)
            break
        if resp is None and len(accounts) < 75:
            # Retry, restarting at initial index
            await self.set_last_borrowed(redis, 0)
            return await self.borrow_account(r)
        elif resp is None:
            return web.json_response({'error': 'could not lend any accounts, try again later'})
        await self.set_last_borrowed(redis, last_borrowed + 1)
        return web.json_response(resp)
