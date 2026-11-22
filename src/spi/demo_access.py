"""Verify a Cloudflare Access session for the loopback demo origin."""

import os
import re

from fastapi import HTTPException, Request


class DemoAccess:
    def __init__(self):
        import jwt

        origin = os.environ.get("SPI_DEMO_ACCESS_ORIGIN", "")
        team = os.environ.get("SPI_DEMO_ACCESS_TEAM", "")
        audience = os.environ.get("SPI_DEMO_ACCESS_AUD", "")
        if (not re.fullmatch(r"https://[a-z0-9-]+(?:\.[a-z0-9-]+)+", origin)
                or not re.fullmatch(r"[a-z0-9-]+", team)
                or not re.fullmatch(r"[a-f0-9]{64}", audience)):
            raise ValueError("Set an HTTPS demo origin, Cloudflare Access team and application AUD")
        self.jwt = jwt
        self.origin = origin
        self.host = origin.removeprefix("https://")
        self.issuer = f"https://{team}.cloudflareaccess.com"
        self.audience = audience
        self.keys = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", timeout=4)

    def authorize(self, request: Request):
        headers = request.headers
        if headers.getlist("host") != [self.host]:
            raise HTTPException(403, "demo_origin_mismatch")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            headers.getlist("origin") != [self.origin] or headers.getlist("x-spi-demo-csrf") != ["1"]
        ):
            raise HTTPException(403, "demo_csrf_required")
        token = headers.getlist("cf-access-jwt-assertion")
        if len(token) != 1 or len(token[0]) > 8192 or token[0].count(".") != 2:
            raise HTTPException(401, "demo_access_required", headers={"Cache-Control": "no-store"})
        try:
            jwt = self.jwt
            header = jwt.get_unverified_header(token[0])
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise jwt.InvalidTokenError()
            # Access uses a signed user assertion. Service-token and unsigned forwarded identity are insufficient.
            key = self.keys.get_signing_key_from_jwt(token[0])
            claims = jwt.decode(token[0], key.key, algorithms=["RS256"], issuer=self.issuer,
                                audience=self.audience, options={"require": ["exp", "iat", "aud", "iss", "sub"]})
            if not isinstance(claims.get("email"), str) or "@" not in claims["email"] or not claims["sub"]:
                raise jwt.InvalidTokenError()
        except self.jwt.PyJWKClientConnectionError as exc:
            raise HTTPException(503, "demo_access_keys_unavailable", headers={"Cache-Control": "no-store"}) from exc
        except self.jwt.PyJWTError as exc:
            raise HTTPException(401, "demo_access_required", headers={"Cache-Control": "no-store"}) from exc


def require_demo_access(request: Request):
    access = getattr(request.app.state, "demo_access", None)
    if access:
        access.authorize(request)
