from aiohttp import web

class Util:
    def get_request_ip(self, r : web.Request) -> str:
        host = r.headers.get('X-FORWARDED-FOR',None)
        if host is None:
            peername = r.transport.get_extra_info('peername')
            if peername is not None:
                host, _ = peername
        return host