"""Check the public edge once the deployment and test scope are authorized."""

import argparse
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin", help="Exact production HTTPS origin")
    parser.add_argument("--authorized", action="store_true", help="Confirm permitted access to the supplied domain")
    args = parser.parse_args()
    url = urlsplit(args.origin)
    if (not args.authorized or not url.hostname or args.origin != f"https://{url.hostname}"
            or url.hostname.count(".") < 1):
        parser.error("An authorized, exact public HTTPS origin is required")
    addresses = {row[4][0] for row in socket.getaddrinfo(url.hostname, 443, type=socket.SOCK_STREAM)}
    if not addresses or not all(ipaddress.ip_address(address).is_global for address in addresses):
        parser.error("DNS must resolve exclusively to public addresses")
    with httpx.Client(base_url=args.origin, trust_env=False, verify=True, follow_redirects=False, timeout=15) as client:
        response = client.get(f"http://{url.hostname}/documents")
        assert response.status_code in (301, 308) and response.headers["location"] == args.origin + "/documents"
        html = client.get("/documents")
        assert html.is_success and "frame-ancestors 'none'" in html.headers["content-security-policy"]
        assert "max-age=" in html.headers["strict-transport-security"]
        assert html.headers["cache-control"] == "no-store"
        assert client.get("/api/health", headers={
            "X-Forwarded-For": "192.0.2.1, 192.0.2.2", "X-Forwarded-Proto": "http",
        }).json() == {"status": "ok"}
        assert client.get("/api/health", headers={"Host": "invalid.example"}).status_code in (400, 404, 421)
        csrf = client.get("/api/auth/csrf")
        assert csrf.is_success and "__Host-spi-csrf=" in csrf.headers["set-cookie"]
        assert {"secure", "httponly", "path=/", "samesite=lax"} <= {
            part.strip().lower() for part in csrf.headers["set-cookie"].split(";")[1:]}
        assert client.post("/api/auth/logout", headers={"Origin": args.origin}).status_code == 403
        for path in ("documents", "results", "issues", "export/results.json", "__ops/ready"):
            assert client.get("/api/" + path).status_code == 404
    print("PASS public edge DNS, TLS chain and hostname, redirect, HSTS, proxy, CSRF, cookie and route checks")
    print("Run the authenticated and host checks in docs/DEPLOYMENT.md to complete the public smoke report.")


if __name__ == "__main__":
    main()
