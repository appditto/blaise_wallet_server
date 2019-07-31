from aiohttp import web
import datetime

class Util:
    def get_request_ip(self, r : web.Request) -> str:
        host = r.headers.get('X-FORWARDED-FOR',None)
        if host is None:
            peername = r.transport.get_extra_info('peername')
            if peername is not None:
                host, _ = peername
        return host

    @staticmethod
    def ms_since_epoch(dt: datetime.datetime):
        epoch = datetime.datetime.utcfromtimestamp(0)
        return int((dt - epoch).total_seconds() * 1000.0)