#!/usr/bin/env python
import argparse
import asyncio
import ipaddress
import json
import os
import sys
import logging
import uuid
import time
from logging.handlers import TimedRotatingFileHandler, WatchedFileHandler

import uvloop

from aiohttp import ClientSession, log, web, WSMessage, WSMsgType
from aioredis import create_redis_pool, Redis

from dotenv import load_dotenv
load_dotenv()

from price_cron import currency_list # Supported currencies
from util import Util
from account_distribution import PASAApi
from json_rpc import PascJsonRpc
from settings import PASA_PRICE

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
    'account_subscribe'
]

# APIs

# Websocket

async def ws_reconnect(ws : web.WebSocketResponse, r : web.Response, account : int):
    """When a websocket connection sends a subscribe request, do this reconnection step"""
    log.server_logger.info('reconnecting;' + util.get_request_ip(r) + ';' + ws.id)

    if account is not None and account in r.app['subscriptions']:
        r.app['subscriptions'][account].add(ws.id)
    elif account is not None:
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
        r.app['subscriptions'][account].add(ws.id)
        await r.app['rdata'].hset(ws.id, "account", json.dumps([account]))
    elif account is not None:
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
                    except Exception as e:
                        log.server_logger.error('subscribe error;%s;%s;%s', str(e), address, uid)
                        reply = {'error': 'subscribe error', 'detail': str(e)}
                        ret = json.dumps(reply)
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
                    return web.json_response(await resp.json(content_type=None))
        except Exception:
            log.server_logger.exception()
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
                if bpasa['paid']:
                    await pasa_api.transfer_account(redis, int(bpasa['pasa']))
                    continue
                acct = await jrpc_client.getaccount(int(bpasa['pasa']))
                if acct is None:
                    continue
                elif float(acct['balance']) >= float(bpasa['price']):
                    # Account is sold, transfer the funds
                    await pasa_api.send_funds(redis, bpasa)
                    # Change public key
                    await pasa_api.transfer_account(redis, int(bpasa['pasa']))
        except Exception:
            log.server_logger.exception("exception checking borrowed PASA")
        finally:
            await asyncio.sleep(60)

async def check_and_send_operations(app, operation_list):
    for op in operation_list:
        if len(op['senders'] > 0) and len(op['receivers']) > 0:
            acct_to_check = int(op['senders'][0]['account'])
            log.server_logger.info(f"checking if {acct_to_check} is subscribed from senders")
            if app['subscriptions'].get(acct_to_check):
                # Send it
                for sub in app['subscriptions'][acct_to_check]:
                    if sub in app['clients']:
                        await app['clients'][sub].send_str(json.dumps(op))
            acct_to_check = int(op['receivers'][0]['account'])
            log.server_logger.info(f"checking if {acct_to_check} is subscribed from receivers")
            if app['subscriptions'].get(acct_to_check):
                # Send it
                for sub in app['subscriptions'][acct_to_check]:
                    if sub in app['clients']:
                        await app['clients'][sub].send_str(json.dumps(op))

async def push_new_operations_task(app):
    """Push new operations to connected clients"""
    while True:
        try:
            # Only do this if clients are connected
            if len(app['subscriptions']) > 0:
                log.server_logger.info("Checking new operations")
                # Get confirmed operations
                # Get last block count
                redis: Redis = app['rdata']
                block_count = await jrpc_client.getblockcount()
                log.server_logger.info(f"Received block_count {block_count}")
                if block_count is not None:
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
                        log.server_logger.info(f"Got {len(block_operations)} operations for block {block_count}")
                        if block_operations is not None:
                            await check_and_send_operations(app, block_operations)
                # Also check pending operations
                pendings = await jrpc_client.getpendings()
                log.server_logger.info(f"Got {len(pendings)} pending operations")
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

     # Periodic price job
    price_task = asyncio.get_event_loop().create_task(send_prices(app))
    # For PASA Purchases
    pasa_task = asyncio.get_event_loop().create_task(check_borrowed_pasa(app))
    # For new operation pushes/push notifications
    newop_task = asyncio.get_event_loop().create_task(push_new_operations_task)

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
