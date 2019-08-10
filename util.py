from aiohttp import web
import datetime
import base58

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

    @staticmethod
    def validate_pubkey(pubkey: str) -> bool:
        try:
            decoded = base58.b58decode(pubkey)
            decoded = decoded[1:-4]
            # First 2 bytes are curve
            curve = int.from_bytes(decoded[0:2], byteorder='little')
            if curve not in [714, 715, 729, 716]:
                return False
            # X Length
            xl = int.from_bytes(decoded[2:4], byteorder='little')
            # Y Length
            yl = int.from_bytes(decoded[4+xl:4+xl+2], byteorder='little')
            # X
            x = decoded[4:4+xl]
            # Y
            y = decoded[4+xl+2:4+xl+2+yl]
            # Some validation
            if curve == 714 and (len(x) > 32 or len(y) > 32):
                # X, Y expected length is 32
                return False
            elif curve == 715 and (len(x) > 48 or len(y) > 48):
                # X, Y expected length is 48
                return False
            elif curve == 716 and (len(x) > 66 or len(y) > 66):
                # X, Y expected length is 66
                return False  
            elif curve == 729 and (len(x) > 36 or len(y) > 36):
                # X, Y expected length is 36
                return False
            return True
        except Exception:
            return False