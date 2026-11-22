import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from spi.api import create_app
from test_uploads import TOKEN, pdf_bytes, private_uploads  # noqa: F401

jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")


def test_cloudflare_access_checks_signed_identity_and_same_origin(private_uploads, monkeypatch):
    origin = "https://demo.example.org"
    audience = "0" * 64
    monkeypatch.setenv("SPI_DEMO_ACCESS", "true")
    monkeypatch.setenv("SPI_DEMO_ACCESS_ORIGIN", origin)
    monkeypatch.setenv("SPI_DEMO_ACCESS_TEAM", "example")
    monkeypatch.setenv("SPI_DEMO_ACCESS_AUD", audience)
    app = create_app()
    access = app.state.demo_access
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(access.keys, "get_signing_key_from_jwt",
                        lambda _: SimpleNamespace(key=private_key.public_key()))
    claims = {"aud": [audience], "iss": access.issuer, "iat": int(time.time()), "exp": int(time.time()) + 60,
              "sub": "test-user", "email": "viewer@example.org"}

    def signed(**changed):
        return {"CF-Access-Jwt-Assertion": jwt.encode({**claims, **changed}, private_key, algorithm="RS256",
                                                     headers={"kid": "test"})}

    with TestClient(app, base_url=origin) as client:
        assert client.get("/auth/config").json() == {"mode": "demo-access"}
        assert client.get("/uploads", headers=TOKEN).status_code == 401
        assert client.get("/documents", headers={"CF-Access-Authenticated-User-Email": claims["email"]}).status_code == 401
        assert client.get("/uploads", headers=signed(exp=int(time.time()) - 1)).status_code == 401
        assert client.get("/uploads", headers=signed(aud=["1" * 64])).status_code == 401
        assert client.get("/uploads", headers=signed()).status_code == 200
        payload = {"data": {"domain": "solar"}, "files": {"file": ("example.pdf", pdf_bytes(), "application/pdf")}}
        assert client.post("/uploads", headers=signed(), **payload).status_code == 403
        assert client.post("/uploads", headers={**signed(), "Origin": "https://elsewhere.example.org",
                                               "X-SPI-Demo-CSRF": "1"}, **payload).status_code == 403
        assert client.post("/uploads", headers={**signed(), "Origin": origin, "X-SPI-Demo-CSRF": "1"},
                           **payload).status_code == 201
