import pytest

from edgeproxy.tunnel.certs import generate_self_signed


@pytest.fixture(scope="session")
def tunnel_certs(tmp_path_factory):
    d = tmp_path_factory.mktemp("certs")
    cert, key = d / "cert.pem", d / "key.pem"
    generate_self_signed(cert, key)
    return cert, key
