#!/usr/bin/env python
import argparse
import asyncio
import ipaddress
import json
import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler, WatchedFileHandler

import uvloop

from aiohttp import ClientSession, log, web
from aioredis import create_redis

from dotenv import load_dotenv
load_dotenv()

from price_cron import currency_list # Supported currencies

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

# Environment configuration

rpc_url = os.getenv('RPC_URL', 'http://127.0.0.1:4003') # Should be the PascalCoin daemon RPC address
debug_mode = True if int(os.getenv('DEBUG', 1)) != 0 else False

# Local constants

# Whitelisting specific json-rpc commands
rpc_whitelist = [
    'getaccount',
    'getblock',
    'getblocks',
    'getblockoperation',
    'getblockoperations',
    'getaccountoperations',
    'getpendings',
    'getpendingscount',
    'findoperation',
    'operationsinfo',
    'executeoperations'
]

# APIs

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

async def init_app():
    """ Initialize the main application instance and return it"""
    async def close_redis(app):
        """Close redis connections"""
        log.server_logger.info('Closing redis connections')
        app['rdata'].close()

    async def open_redis(app):
        """Open redis connections"""
        log.server_logger.info("Opening redis connections")
        app['rdata'] = await create_redis(('localhost', 6379),
                                                db=2, encoding='utf-8')

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
    app.add_routes([web.post('/rawrpc', whitelist_rpc)]) # HTTP API
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