"""出站地址校验：默认拒绝回环/内网，显式放开才允许。"""

import pytest

from competitor_intel.core.errors import CiError
from competitor_intel.core.netguard import ensure_allowed


def _resolver(addr: str):
    def resolve(host, port):
        return [(2, 1, 6, "", (addr, 0))]

    return resolve


def test_rejects_non_http_scheme():
    with pytest.raises(CiError) as excinfo:
        ensure_allowed("ftp://example.com/pricing")
    assert excinfo.value.code == "BAD_ARGUMENT"


def test_rejects_loopback():
    with pytest.raises(CiError) as excinfo:
        ensure_allowed("http://127.0.0.1:8000/pricing")
    assert excinfo.value.code == "BLOCKED"


def test_rejects_private_network():
    with pytest.raises(CiError) as excinfo:
        ensure_allowed("https://internal.example/pricing", resolver=_resolver("10.1.2.3"))
    assert excinfo.value.code == "BLOCKED"


def test_allows_public_address():
    ensure_allowed("https://example.com/pricing", resolver=_resolver("93.184.216.34"))


def test_allow_private_switch():
    ensure_allowed(
        "http://127.0.0.1:8000/pricing",
        allow_private=True,
        resolver=_resolver("127.0.0.1"),
    )
