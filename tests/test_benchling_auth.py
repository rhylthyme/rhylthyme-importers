"""How the Benchling client authenticates: a bare token is an OAuth
access token (Bearer); a personal API key is passed as a whole HTTP Basic
value built by ``basic_auth``."""

import base64
import json

import pytest

from rhylthyme_importers.benchling import client


class _Resp:
    status = 200

    def read(self):
        return json.dumps({"id": "ent_1"}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def sent(monkeypatch):
    headers = []

    def fake_urlopen(req, timeout=None):
        headers.append(req.get_header("Authorization"))
        return _Resp()

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    return headers


def test_a_bare_token_is_sent_as_bearer(sent):
    client.fetch_protocol(tenant="acme", token="access-1", protocol_id="prt_1")
    assert sent == ["Bearer access-1"]


def test_an_api_key_goes_as_basic(sent):
    client.fetch_protocol(tenant="acme", token=client.basic_auth("sk_key"), protocol_id="prt_1")
    assert sent == ["Basic " + base64.b64encode(b"sk_key:").decode()]


def test_a_whole_bearer_value_is_not_doubled(sent):
    client.fetch_protocol(tenant="acme", token="Bearer access-1", protocol_id="prt_1")
    assert sent == ["Bearer access-1"]


def test_callers_can_tell_the_client_takes_a_header_value():
    assert client.ACCEPTS_AUTHORIZATION_HEADER is True


@pytest.mark.parametrize("tenant", ["10.0.0.5:8443/x?", "acme.evil.com", "acme\n", "", "-x"])
def test_only_a_bare_subdomain_goes_into_the_url(sent, tenant):
    with pytest.raises(client.BenchlingError):
        client.fetch_protocol(tenant=tenant, token="t", protocol_id="prt_1")
    assert sent == []
