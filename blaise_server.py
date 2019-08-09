#!/usr/bin/env python
import argparse
import asyncio
import ipaddress
import json
import logging
import os
import sys
import time
import datetime
import uuid
from logging.handlers import TimedRotatingFileHandler, WatchedFileHandler
from threading import active_count

import aiofcm
import uvloop
from aiohttp import ClientSession, WSMessage, WSMsgType, log, web
from aioredis import Redis, create_redis_pool
from dotenv import load_dotenv
from requests.api import request

from account_distribution import PASAApi
from json_rpc import PascJsonRpc
from price_cron import currency_list  # Supported currencies
from settings import PASA_PRICE
from util import Util

load_dotenv()


# Use uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

# Parse paramters
parser = argparse.ArgumentParser(description="Blaise Wallet Server")
parser.add_argument('--host', type=str, help='Host to listen on (e.g. 127.0.0.1)', default='127.0.0.1')
parser.add_argument('--path', type=str, help='(Optional) Path to run application on (for unix socket, e.g. /tmp/blaiseapp.sock', default=None)
parser.add_argument('-p', '--port', type=int, help='Port to listen on', default=4443)
parser.add_argument('--log-file', type=str, help='Log file location', default='blaise_server.log')
options = parser.parse_args()

try:
    listen_host = str(ipaddress.ip_address(options.host))
    listen_port = int(options.port)
    log_file = options.log_file
    app_path = options.path
    if app_path is None:
        server_desc = f'on {listen_host} port {listen_port}'
    else:
        server_desc = f'on {app_path}'
    print(f'Starting BLAISE Server {server_desc}')
except Exception:
    parser.print_help()
    sys.exit(0)

price_prefix = 'coingecko:pasc'
util = Util()

# Environment configuration

rpc_url = os.getenv('RPC_URL', 'http://127.0.0.1:4003') # Should be the PascalCoin daemon RPC address
debug_mode = True if int(os.getenv('DEBUG', 1)) != 0 else False

jrpc_client = PascJsonRpc(rpc_url)
pasa_api = PASAApi(jrpc_client)

# Local constants

fcm_api_key = os.getenv('FCM_API_KEY', None)
fcm_sender_id = os.getenv('FCM_SENDER_ID', None)
fcm_client = aiofcm.FCM(fcm_sender_id, fcm_api_key) if fcm_api_key is not None and fcm_sender_id is not None else None

# Whitelisting specific json-rpc commands
rpc_whitelist = [
    'getaccount',
    'getblock',
    'getblocks',
    'getblockcount',
    'getblockoperation',
    'getblockoperations',
    'getaccountoperations',
    'getpendings',
    'getpendingscount',
    'findaccounts',
    'findoperation',
    'operationsinfo',
    'executeoperations'
]

# Whitelisting proprietary WS actions
ws_whitelist = [
    'account_subscribe',
    'fcm_update',
    'fcm_update_bulk',
    'fcm_delete_account'
]

# Push notifications

async def delete_fcm_token(account: int, token : str, r : web.Request):
    await r.app['rdata'].delete(f'fcm_{token}')
    await delete_fcm_account(account, token, r)

async def delete_fcm_account(account: int, token: str, r: web.Request, explicit: bool = False):
    acct_list = await r.app['rdata'].get(str(account))
    if acct_list is not None:
        acct_list = json.loads(acct_list.replace('\'', '"'))
        acct_list['data'].remove(token)
        await r.app['rdata'].set(str(account), json.dumps(acct_list))
        # Flag explicit deletes to prevent them from coming back if transferred account, etc.
        if explicit:
            await r.app['rdata'].set(f'explicitfcmdel_{str(account)}', 'true', expire=1000)

async def update_fcm_token_for_account(account : int, token : str, r : web.Request):
    """Store device FCM registration tokens in redis"""
    # Dont add explicit deletes
    ed = await r.app['rdata'].get(f'explicitfcmdel_{str(account)}')
    if ed is not None:
        return
    redis: Redis = r.app['rdata']
    # We keep a list of FCM tokens and accounts, because of multiple accounts
    token_account_list = await redis.get(f'fcm_{token}')
    if token_account_list is None:
        token_account_list = [account]
        await redis.set(f'fcm_{token}', json.dumps(token_account_list), expire=2592000)
    else:
        token_account_list = json.loads(token_account_list)
        if account not in token_account_list:
            token_account_list.append(account)
            await redis.set(f'fcm_{token}', json.dumps(token_account_list), expire=2592000)
    # Keep a list of tokens associated with this account, in case of multiple devices
    cur_list = await redis.get(str(account))
    if cur_list is not None:
        cur_list = json.loads(cur_list.replace('\'', '"'))
    else:
        cur_list = {}
    if 'data' not in cur_list:
        cur_list['data'] = []
    if token not in cur_list['data']:
        cur_list['data'].append(token)
    await redis.set(str(account), json.dumps(cur_list), expire=2592000)

async def get_fcm_tokens(account : int, redis : Redis) -> list:
    """Return list of FCM tokens that belong to this account"""
    tokens = await redis.get(str(account))
    if tokens is None:
        return []
    tokens = json.loads(tokens.replace('\'', '"'))
    # Rebuild the list for this account removing tokens that dont belong anymore
    new_token_list = {}
    new_token_list['data'] = []
    if 'data' not in tokens:
        return []
    for t in tokens['data']:
        account_list = await redis.get(f'fcm_{t}')
        if account_list is None:
            continue
        else:
            account_list = json.loads(account_list)
        if account not in account_list:
            continue
        new_token_list['data'].append(t)
    await redis.set(str(account), json.dumps(new_token_list))
    return new_token_list['data']

async def check_and_do_push_notification(redis: Redis, op):
    # Only if configured
    if fcm_client is None:
        return
    # Only do pending operations, and only to the receiver
    if op['maturation'] is not None or 'receivers' not in op or not op['receivers']:
        return
    # Check if we already pushed this op
    op_pushed = await redis.get(f'pncache_{op["ophash"]}')
    if op_pushed is not None:
        return
    else:
        await redis.set(f'pncache_{op["ophash"]}', 'tru', expire=10000)
    # Check tokens
    fcm_tokens = await get_fcm_tokens(op['receivers'][0]['account'], redis)
    notification_title = f"Received {abs(float(op['receivers'][0]['amount']))} PASCAL"
    notification_body = f"Open Blaise to view this transaction."
    for t in fcm_tokens:
        message = aiofcm.Message(
            device_token = t,
            notification = {
                "title":notification_title,
                "body":notification_body,
                "sound":"default",
                "tag":str(op['receivers'][0]['account'])
            },
            data = {
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
                "account": str(op['receivers'][0]['account'])
            },
            priority=aiofcm.PRIORITY_HIGH
        )
        await fcm_client.send_message(message)

# APIs

# Websocket
async def ws_reconnect(ws : web.WebSocketResponse, r : web.Response, account : int):
    """When a websocket connection sends a subscribe request, do this reconnection step"""
    log.server_logger.info('reconnecting;' + util.get_request_ip(r) + ';' + ws.id)

    if account is not None and account in r.app['subscriptions']:
        log.server_logger.info(f"subscribing account {account}")
        r.app['subscriptions'][account].add(ws.id)
    elif account is not None:
        log.server_logger.info(f"subscribing account {account}")
        r.app['subscriptions'][account] = set()
        r.app['subscriptions'][account].add(ws.id)
    r.app['cur_prefs'][ws.id] = await r.app['rdata'].hget(ws.id, "currency")
    price_cur = await r.app['rdata'].hget("prices", f"{price_prefix}-" + r.app['cur_prefs'][ws.id].lower())
    price_btc = await r.app['rdata'].hget("prices", f"{price_prefix}-btc")
    response = {}
    response['currency'] = r.app['cur_prefs'][ws.id].lower()
    response['price'] = float(price_cur)
    response['btc'] = float(price_btc)
    response = json.dumps(response)

    log.server_logger.info(
        'reconnect response sent ; %s bytes; %s; %s', str(len(response)), util.get_request_ip(r), ws.id)

    await ws.send_str(response)

async def ws_connect(ws : web.WebSocketResponse, r : web.Response, account : int, currency : str):
    """Clients subscribing for the first time"""
    log.server_logger.info('subscribing;%s;%s', util.get_request_ip(r), ws.id)

    if account is not None and account in r.app['subscriptions']:
        log.server_logger.info(f"subscribing account {account}")
        r.app['subscriptions'][account].add(ws.id)
        await r.app['rdata'].hset(ws.id, "account", json.dumps([account]))
    elif account is not None:
        log.server_logger.info(f"subscribing account {account}")
        r.app['subscriptions'][account] = set()
        r.app['subscriptions'][account].add(ws.id)
        await r.app['rdata'].hset(ws.id, "account", json.dumps([account]))
    r.app['cur_prefs'][ws.id] = currency
    await r.app['rdata'].hset(ws.id, "currency", currency)
    await r.app['rdata'].hset(ws.id, "last-connect", float(time.time()))
    response = {}
    response['uuid'] = ws.id
    price_cur = await r.app['rdata'].hget("prices", f"{price_prefix}-" + r.app['cur_prefs'][ws.id].lower())
    price_btc = await r.app['rdata'].hget("prices", f"{price_prefix}-btc")
    response['currency'] = r.app['cur_prefs'][ws.id].lower()
    response['price'] = float(price_cur)
    response['btc'] = float(price_btc)
    response = json.dumps(response)

    log.server_logger.info(
        'subscribe response sent ; %s bytes; %s; %s', str(len(response)), util.get_request_ip(r), ws.id)

    await ws.send_str(response)

async def handle_user_message(r : web.Request, message : str, ws : web.WebSocketResponse = None):
    """Process data sent by client"""
    address = util.get_request_ip(r)
    uid = ws.id if ws is not None else '0'
    now = int(round(time.time() * 1000))
    if address in r.app['last_msg']:
        if (now - r.app['last_msg'][address]['last']) < 25:
            if r.app['last_msg'][address]['count'] > 3:
                log.server_logger.error('client messaging too quickly: %s ms; %s; %s; User-Agent: %s', str(
                    now - r.app['last_msg'][address]['last']), address, uid, str(
                    r.headers.get('User-Agent')))
                return None
            else:
                r.app['last_msg'][address]['count'] += 1
        else:
            r.app['last_msg'][address]['count'] = 0
    else:
        r.app['last_msg'][address] = {}
        r.app['last_msg'][address]['count'] = 0
    r.app['last_msg'][address]['last'] = now
    log.server_logger.info('request; %s, %s, %s', message, address, uid)
    if message not in r.app['active_messages']:
        r.app['active_messages'].add(message)
    else:
        log.server_logger.error('request already active; %s; %s; %s', message, address, uid)
        return None
    ret = None
    try:
        request_json = json.loads(message)
        if request_json['action'] in ws_whitelist:
            # rpc: account_subscribe (only applies to websocket connections)
            if request_json['action'] == "account_subscribe" and ws is not None:
                # already subscribed, reconnect (websocket connections)
                if 'uuid' in request_json:
                    uid = request_json['uuid']
                    try:
                        del r.app['clients'][uid]
                    except Exception:
                        pass
                    ws.id = uid
                    r.app['clients'][uid] = ws
                    log.server_logger.info('reconnection request;' + address + ';' + uid)
                    try:
                        if 'currency' in request_json and request_json['currency'] in currency_list:
                            currency = request_json['currency']
                            r.app['cur_prefs'][uid] = currency
                            await r.app['rdata'].hset(uid, "currency", currency)
                        else:
                            setting = await r.app['rdata'].hget(uid, "currency")
                            if setting is not None:
                                r.app['cur_prefs'][uid] = setting
                            else:
                                r.app['cur_prefs'][uid] = 'usd'
                                await r.app['rdata'].hset(uid, "currency", 'usd')

                        # Get relevant account
                        account = None
                        if 'account' in request_json:
                            account = request_json['account']

                        await ws_reconnect(ws, r, account)

                        if 'fcm_token' in request_json and account is not None:
                            if request_json['notification_enabled']:
                                await update_fcm_token_for_account(account, request_json['fcm_token'], r)
                            else:
                                await delete_fcm_token(account, request_json['fcm_token'], r)
                    except Exception as e:
                        log.server_logger.error('reconnect error; %s; %s; %s', str(e), address, uid)
                        reply = {'error': 'reconnect error', 'detail': str(e)}
                        ret = json.dumps(reply)
                # new user, setup uuid(or use existing if available) and account info
                else:
                    log.server_logger.info('subscribe request; %s; %s', util.get_request_ip(r), uid)
                    try:
                        if 'currency' in request_json and request_json['currency'] in currency_list:
                            currency = request_json['currency']
                        else:
                            currency = 'usd'
                        account = request_json['account'] if 'account' in request_json else None
                        await ws_connect(ws, r, account, currency)

                        if 'fcm_token' in request_json and account is not None:
                            if request_json['notification_enabled']:
                                await update_fcm_token_for_account(account, request_json['fcm_token'], r)
                            else:
                                await delete_fcm_token(account, request_json['fcm_token'], r)
                    except Exception as e:
                        log.server_logger.error('subscribe error;%s;%s;%s', str(e), address, uid)
                        reply = {'error': 'subscribe error', 'detail': str(e)}
                        ret = json.dumps(reply)
            elif request_json['action'] == "fcm_update":
                # Updating FCM token
                if 'fcm_token' in request_json and 'account' in request_json and 'enabled' in request_json:
                    if request_json['enabled']:
                        await update_fcm_token_for_account(request_json['account'], request_json['fcm_token'], r)
                    else:
                        await delete_fcm_token(request_json['account'], request_json['fcm_token'], r)
            elif request_json['action'] == 'fcm_update_bulk':
                if 'fcm_token' in request_json and 'accounts' in request_json and 'enabled' in request_json:
                    if request_json['enabled']:
                        for account in request_json['accounts']:
                            await update_fcm_token_for_account(account, request_json['fcm_token'], r)
                    else:
                        for account in request_json['accounts']:
                            await delete_fcm_token(account, request_json['fcm_token'], r)
            elif request_json['action'] == 'fcm_delete_account':
                if 'fcm_token' in request_json and 'account' in request_json:
                    await delete_fcm_account(request_json['account'], request_json['fcm_token'], r, explicit=True)             
    except Exception as e:
        log.server_logger.exception('uncaught error;%s;%s', util.get_request_ip(r), uid)
        ret = json.dumps({
            'error':'general error',
            'detail':str(sys.exc_info())
        })
    finally:
        r.app['active_messages'].remove(message)
        return ret

async def websocket_handler(r : web.Request):
    """Handler for websocket connections and messages"""

    ws = web.WebSocketResponse()
    await ws.prepare(r)

    # Connection Opened
    ws.id = str(uuid.uuid4())
    r.app['clients'][ws.id] = ws
    log.server_logger.info('new connection;%s;%s;User-Agent:%s', util.get_request_ip(r), ws.id, str(
        r.headers.get('User-Agent')))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if msg.data == 'close':
                    await ws.close()
                else:
                    reply = await handle_user_message(r, msg.data, ws=ws)
                    if reply is not None:
                        log.server_logger.debug('Sending response %s to %s', reply, util.get_request_ip(r))
                        await ws.send_str(reply)
            elif msg.type == WSMsgType.CLOSE:
                log.server_logger.info('WS Connection closed normally')
                break
            elif msg.type == WSMsgType.ERROR:
                log.server_logger.info('WS Connection closed with error %s', ws.exception())
                break

        log.server_logger.info('WS connection closed normally')
    except Exception:
        log.server_logger.exception('WS Closed with exception')
    finally:
        if ws.id in r.app['clients']:
            del r.app['clients'][ws.id]
        for acct in r.app['subscriptions']:
            if ws.id in r.app['subscriptions'][acct]:
                if len(r.app['subscriptions'][acct]) == 1:
                    del r.app['subscriptions'][acct]
                    break
                else:
                    r.app['subscriptions'][acct].remove(ws.id)
                    break
        await ws.close()

    return ws

# HTTP

async def http_api(r: web.Request):
    try:
        request_json = await r.json()
        if 'action' not in request_json:
            return web.HTTPBadRequest(reason="Bad request")
        elif request_json['action'] == 'price_data':
            return web.json_response({'price':await r.app['rdata'].hget("prices", "coingecko:pasc-usd")})
        elif request_json['action'] == 'borrow_account':
            return await pasa_api.borrow_account(r)
        elif request_json['action'] == 'getborrowed':
            return await pasa_api.getborrowed(r)
        return web.HTTPBadRequest(reason="Bad request")
    except Exception:
        log.server_logger.exception('Exception in http_api')
        return web.HTTPBadRequest(reason="Bad request")


async def whitelist_rpc(r: web.Request):
    """For sending stanard json-rpc requests to this server, methods will be filtered by rpc_whitelist"""
    try:
        request_json = await r.json()
        if 'method' not in request_json:
            return web.HTTPBadRequest(reason=f"Bad request")
        elif request_json['method'].lower().strip() not in rpc_whitelist:
            return web.HTTPBadRequest(reason=f'{request_json["method"]} is not allowed')

        # Make request to node
        try:
            async with ClientSession() as session:
                async with session.post(rpc_url, json=request_json, timeout=60) as resp:
                    if resp.status > 299:
                        log.server_logger.error('Received status code %d from request %s', resp.status, json.dumps(request_json))
                        raise Exception
                    resp_json = await resp.json(content_type=None)
                    if request_json['method'] == 'findaccounts' and 'result' in resp_json:
                        bpasa = None
                        if 'b58_pubkey' in request_json:
                            bpasa = await pasa_api.pubkey_has_borrowed(r.app['rdata'], request_json['b58_pubkey'])
                            if bpasa is not None:
                                expiry = int(bpasa['expiry'])
                                if Util.ms_since_epoch(datetime.datetime.utcnow()) > expiry:
                                    bpasa = None
                        resp_json['borrowed_account'] = bpasa if bpasa is not None else None
                    return web.json_response(resp_json)
        except Exception:
            log.server_logger.exception('Exception in RPC HTTP Request')
            return web.HTTPInternalServerError()
    except Exception:
        log.server_logger.exception("received exception in http_api")
        return web.HTTPInternalServerError(reason=f"Something went wrong {str(sys.exc_info())}")

# Periodic Tasks
async def send_prices(app):
    """Send price updates to connected clients once per minute"""
    while True:
        try:
            if len(app['clients']):
                log.server_logger.info('pushing price data to %d connections', len(app['clients']))
                btc = float(await app['rdata'].hget("prices", f"{price_prefix}-btc"))
                for client, ws in list(app['clients'].items()):
                    try:
                        try:
                            currency = app['cur_prefs'][client]
                        except Exception:
                            currency = 'usd'
                        price = float(await app['rdata'].hget("prices", f"{price_prefix}-" + currency.lower()))

                        response = {
                            'currency':currency.lower(),
                            "price":str(price),
                            'btc':str(btc)
                        }
                        await ws.send_str(json.dumps(response))
                    except Exception:
                        log.server_logger.exception('error pushing prices for client %s', client)
        except Exception:
            log.server_logger.exception("exception pushing price data")
        finally:
            await asyncio.sleep(60)

async def check_borrowed_pasa(app):
    while True:
        try:
            # Get list of PASAs which are borrowed
            redis: Redis = app['rdata']
            match = 'borrowedpasa_*'
            cur = b'0'
            pasa_list = []
            while cur:
                cur, keys = await redis.scan(cur, match=match)
                for key in keys:
                    try:
                        pasa_list.append(json.loads(await redis.get(key)))
                    except Exception:
                        pass
            # Check each borrowed account to see if it has a balance >= threshold
            for bpasa in pasa_list:
                if bpasa['paid'] and 'transferred' in bpasa and not bpasa['transferred']:
                    await pasa_api.transfer_account(redis, int(bpasa['pasa']))
                    continue
                elif 'transferred' in bpasa and bpasa['transferred']:
                    op = await jrpc_client.findoperation(bpasa['transfer_ophash'])
                    if op is None:
                        await pasa_api.transfer_account(redis, int(bpasa['pasa']))
                        continue
                    elif op is not None and op['maturation'] is not None:
                        await pasa_api.check_and_clear_borrow(redis, bpasa['b58_pubkey'])
                        continue
                    else:
                        continue
                elif 'transferred' not in bpasa:
                    continue
                acct = await jrpc_client.getaccount(int(bpasa['pasa']))
                if acct is None:
                    continue
                elif float(acct['balance']) >= float(bpasa['price']):
                    # Account is sold, transfer the funds
                    await pasa_api.send_funds(redis, bpasa)
        except Exception:
            log.server_logger.exception("exception checking borrowed PASA")
        finally:
            await asyncio.sleep(60)

async def check_and_send_operations(app, operation_list):
    for op in operation_list:
        # Don't send it twice
        if 'ophash' in op:
            redis: Redis = app['rdata']
            sent_op = await redis.get(f'sentoperation_{op["ophash"]}')
            if sent_op is not None:
                continue
        # Check push notification
        await check_and_do_push_notification(app['rdata'], op)
        if len(op['senders']) > 0 and len(op['receivers']) > 0:
            acct_to_check = int(op['senders'][0]['account'])
            log.server_logger.debug(f"checking if {acct_to_check} is subscribed from senders")
            if app['subscriptions'].get(acct_to_check):
                # Send it
                for sub in app['subscriptions'][acct_to_check]:
                    if sub in app['clients']:
                        try:
                            log.server_logger.debug(f"Sending {json.dumps(op)}")
                            await app['clients'][sub].send_str(json.dumps(op))
                        except Exception:
                            pass
            acct_to_check = int(op['receivers'][0]['account'])
            log.server_logger.debug(f"checking if {acct_to_check} is subscribed from receivers")
            if app['subscriptions'].get(acct_to_check):
                # Send it
                for sub in app['subscriptions'][acct_to_check]:
                    if sub in app['clients']:
                        try:
                            log.server_logger.debug(f"Sending {json.dumps(op)}")
                            await app['clients'][sub].send_str(json.dumps(op))
                        except Exception:
                            pass

async def push_new_operations_task(app):
    """Push new operations to connected clients"""
    while True:
        try:
            # Only do this if clients are connected
            if len(app['subscriptions']) > 0:
                log.server_logger.debug("Checking new operations")
                # Get confirmed operations
                # Get last block count
                redis: Redis = app['rdata']
                block_count = await jrpc_client.getblockcount()
                log.server_logger.debug(f"Received block_count {block_count}")
                if block_count is not None:
                    block_count -= 1 # Check blockcount - 1
                    # See if we already checked this block
                    last_checked_block = await redis.get('last_checked_block')
                    should_check = True
                    if last_checked_block is None:
                        await redis.set('last_checked_block', str(block_count), expire=1000)
                    else:
                        last_checked_block = int(last_checked_block)
                        if last_checked_block == block_count:
                            should_check = False
                    # Iterate block operations, and push them to connected clients if applicable
                    if should_check:
                        block_operations = await jrpc_client.getblockoperations(block_count)
                        log.server_logger.debug(f"Got {len(block_operations)} operations for block {block_count}")
                        if block_operations is not None:
                            await redis.set('last_checked_block', str(block_count), expire=1000)
                            await check_and_send_operations(app, block_operations)
            # Also check pending operations, regarldess of whether clients are connected (due to push notificaions)
            pendings = await jrpc_client.getpendings()
            log.server_logger.debug(f"Got {len(pendings)} pending operations")
            if pendings is not None:
                await check_and_send_operations(app, pendings)
        except Exception:
            pass
        finally:
            await asyncio.sleep(30)

async def init_app():
    """ Initialize the main application instance and return it"""
    async def close_redis(app):
        """Close redis connections"""
        log.server_logger.info('Closing redis connections')
        app['rdata'].close()

    async def open_redis(app):
        """Open redis connections"""
        log.server_logger.info("Opening redis connections")
        app['rdata'] = await create_redis_pool(('localhost', 6379),
                                                db=2, encoding='utf-8', minsize=2, maxsize=50)
        app['clients'] = {} # Keep track of connected clients
        app['last_msg'] = {} # Last time a client has sent a message
        app['active_messages'] = set() # Avoid duplicate messages from being processes simultaneously
        app['cur_prefs'] = {} # Client currency preferences
        app['subscriptions'] = {} # Store subscription UUIDs, this is used for targeting new operation pushes

    # Setup logger
    if debug_mode:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    root = logging.getLogger('aiohttp.server')
    logging.basicConfig(level=logging.INFO)
    handler = WatchedFileHandler(log_file)
    formatter = logging.Formatter("%(asctime)s;%(levelname)s;%(message)s", "%Y-%m-%d %H:%M:%S %z")
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.addHandler(TimedRotatingFileHandler(log_file, when="d", interval=1, backupCount=100))        

    app = web.Application()
    app.add_routes([web.get('/', websocket_handler)]) # All WS requests
    app.add_routes([web.post('/rawrpc', whitelist_rpc)]) # HTTP API
    app.add_routes([web.post('/v1', http_api)]) # HTTP API
    app.on_startup.append(open_redis)
    app.on_shutdown.append(close_redis)

    return app

app = asyncio.get_event_loop().run_until_complete(init_app())

def main():
    """Main application loop"""

     # Periodic price job
    price_task = asyncio.get_event_loop().create_task(send_prices(app))
    # For PASA Purchases
    pasa_task = asyncio.get_event_loop().create_task(check_borrowed_pasa(app))
    # For new operation pushes/push notifications
    newop_task = asyncio.get_event_loop().create_task(push_new_operations_task(app))

    # Start web/ws server
    async def start():
        runner = web.AppRunner(app)
        await runner.setup()
        if app_path is not None:
            site = web.UnixSite(runner, app_path)
        else:
            site = web.TCPSite(runner, listen_host, listen_port)
        await site.start()

    async def end():
        await app.shutdown()

    asyncio.get_event_loop().run_until_complete(start())

    # Main program
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.get_event_loop().run_until_complete(end())

    asyncio.get_event_loop().close()

if __name__ == "__main__":
    main()
