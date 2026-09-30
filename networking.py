import socket

import urllib3.util.connection


def force_ipv4():
    """Force urllib3/requests connections in this process to use IPv4."""
    urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
