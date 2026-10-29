import ipaddress
import socket

import pytest


def _is_local(address):
    if not isinstance(address, tuple):
        return True  # Unix sockets.
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except ValueError:
        return address[0] == "localhost"


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """Tests must not reach Stripe, Cloudinary or any other remote service (CFG-01).

    The database drivers connect through their C libraries and are unaffected.
    """
    real_connect = socket.socket.connect

    def guarded_connect(sock, address):
        if not _is_local(address):
            raise RuntimeError(f"Tests may not open network connections (attempted {address!r}).")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda sock, address: guarded_connect(sock, address) or 0)
