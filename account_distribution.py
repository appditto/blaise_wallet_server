"""PASA Distribution"""
import json
import datetime
import base58
from aiohttp import web
from aioredis import Redis

from json_rpc import PascJsonRpc
from util import Util

# Account used to sign transactions with fees
SIGNER_ACCOUNT = 1185739
# Public key of our account
PUBKEY_B58 = '3Ghhbokf2qNsZYmoK5kNvzo4zdoR5vcdMGEzf6Jgz3xZDrniNMFgrcwQaTjtkpvUvm1S7JStjxZZJWSSgxQECJdu4BhpAjPqQrDjoY'
# Soft Expiry of Borrowed Pasa (milliseconds)
PASA_SOFT_EXPIRY = 259200000 # 3 days
# Hard expiry of borrowed pasa (seconds)
PASA_HARD_EXPIRY = 2592000 # 30 days

class PASAApi():
    def __init__(self, rpc_client: PascJsonRpc):
        self.rpc_client = rpc_client

    async def get_last_borrowed(self, redis: Redis):
        """Get index to start in findaccounts request"""
        last_bor = await redis.get("last_borrowed_pasa")
        if last_bor is None:
            await redis.set("last_borrowed_pasa", str(SIGNER_ACCOUNT))
            return int(SIGNER_ACCOUNT)
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
        pasa_obj['expires'] = Util.ms_since_epoch(datetime.datetime.utcnow())
        await redis.set(f"borrowedpasa_{str(pasa_obj['pasa'])}", json.dumps(pasa_obj), expire=PASA_HARD_EXPIRY)
        return pasa_obj

    async def get_borrowed_pasa(self, redis: Redis, pasa : int):
        """get borrowed pasa"""
        borrowed = await redis.get(f"borrowedpasa_{str(pasa)}")
        return None if borrowed is None else json.loads(borrowed)

    async def pubkey_has_borrowed(self, redis: Redis, pubkey: str):
        """Returns PASA object if public key has already borrowed an account, None otherwise"""
        pasa = await redis.get(f"borrowed_pasa_{pubkey}")
        if pasa is None:
            return None
        bpasa = await self.get_borrowed_pasa(redis, int(pasa))
        if bpasa is not None:
            return bpasa
        await redis.delete(f"borrowed_pasa_{pubkey}")
        return None

    async def initiate_borrow(self, redis: Redis, pubkey: str, pasa: int):
        """Mark an account as borrowed"""
        borrow_obj = {
            'b58_pubkey': pubkey,
            'pasa': pasa,
            'expires': Util.ms_since_epoch(datetime.datetime.utcnow())
        }
        await redis.set(f'borrowedpasa_{str(pasa)}', json.dumps(borrow_obj), expire=PASA_HARD_EXPIRY)
        await redis.set(f"borrowed_pasa_{pubkey}", str(pasa), expire=PASA_HARD_EXPIRY)
        return borrow_obj


    async def borrow_account(self, r: web.Request):
        """Borrow an account
        {
            'action':'borrow_account',
            'b58_pubkey':'3g00...',
        }
        response:
        {
            'borrowed_account':31334,
            'price':0.25
        }
        error:
        {
            'error': 'failed'
        }
        """
        req_json = r.json()
        if 'b58_pubkey' not in req_json:
            return web.HTTPBadRequest(reason="Bad request - missing b58_pubkey")
        elif len(req_json['b58_pubkey']) < 80:
            return web.json_response({'error': 'invalid public key'})
        try:
            base58.b58decode(req_json['b58_pubkey'])
        except ValueError:
            return web.json_response({'error': 'invalid public key'})
        redis: Redis = r.app['rdata'] 
        # Ensure this pubkey does not already have a borrowed account
        bpasa = await self.pubkey_has_borrowed(redis, req_json['b58_pubkey'])
        if bpasa is not None:
            # Reset expiry and return result
            return web.json_response(await self.reset_expiry(redis, bpasa))
        # Do findaccounts request
        last_borrowed = await self.get_last_borrowed(redis)
        accounts = await self.rpc_client.findaccounts(start=await self.get_last_borrowed(redis), b58_pubkey=PUBKEY_B58)
        if accounts is None:
            return web.json_response({'error':'findaccounts response failed'})
        resp = None
        for acct in accounts:
            acctnum = acct['account']
            if self.pasa_is_borrowed(redis, acctnum):
                continue
            resp = await self.initiate_borrow(redis, req_json['b58_pubkey'], acctnum)
            break
        if resp is None and len(accounts) < 75 and last_borrowed != SIGNER_ACCOUNT:
            # Retry, restarting at initial index
            await self.set_last_borrowed(redis, SIGNER_ACCOUNT)
            return await self.borrow_account(r)
        await self.set_last_borrowed(redis, last_borrowed + 1)
        return web.json_response(resp)

        

