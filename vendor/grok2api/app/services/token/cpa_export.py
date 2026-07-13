"""Export Grok SSO tokens as cli-proxy-api xAI OAuth auth files."""

import base64
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_ISSUER = "https://auth.x.ai"
SCOPES = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)

CPA_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CPA_TOKEN_ENDPOINT = f"{OIDC_ISSUER}/oauth2/token"
CPA_REDIRECT_URI = "http://127.0.0.1:56121/callback"
CPA_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._@+-]+")


class CpaExportError(Exception):
    pass


class CpaRateLimitedError(CpaExportError):
    pass


def _b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_jwt_payload(token: str) -> dict:
    try:
        return json.loads(_b64url_decode(token.split(".")[1]))
    except Exception:
        return {}


def rfc3339_sec(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_rate_limited(url: str, body: str = "") -> bool:
    blob = f"{url}\n{body}".lower()
    return (
        "rate_limited" in blob
        or "rate-limited" in blob
        or "too_many_requests" in blob
        or "ratelimit" in blob
        or "429" in blob
    )


def backoff_sec(base: float, attempt: int, cap: float = 120.0) -> float:
    shift = min(max(attempt, 1) - 1, 4)
    return min(max(base, 1.0) * (2**shift), cap) + secrets.randbelow(5)


def request_device_code() -> dict:
    data = urllib.parse.urlencode({"client_id": CLIENT_ID, "scope": SCOPES}).encode()
    req = urllib.request.Request(
        f"{OIDC_ISSUER}/oauth2/device/code",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise CpaExportError(f"device/code HTTP {exc.code}: {exc.read().decode()[:200]}") from exc
    except Exception as exc:
        raise CpaExportError(f"device/code failed: {exc}") from exc


def poll_token(device_code: str, interval: int, expires_in: int, timeout: int = 60) -> dict:
    deadline = time.time() + min(expires_in, timeout)
    while time.time() < deadline:
        time.sleep(interval)
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            }
        ).encode()
        req = urllib.request.Request(
            f"{OIDC_ISSUER}/oauth2/token",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read())
            except Exception:
                err = {}
            error = err.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise CpaExportError(f"token failed: {error or exc.code}") from exc
        except Exception as exc:
            raise CpaExportError(f"token failed: {exc}") from exc
    raise CpaExportError("token polling timed out")


def fetch_userinfo(access_token: str) -> dict:
    if not access_token:
        return {}
    req = urllib.request.Request(
        f"{OIDC_ISSUER}/oauth2/userinfo",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def enrich_token_with_userinfo(token: dict) -> dict:
    if not token or token.get("_email") or token.get("email"):
        return token
    info = fetch_userinfo(token.get("access_token") or token.get("key") or "")
    if info.get("email"):
        token["_email"] = info["email"]
        token["_email_verified"] = bool(info.get("email_verified"))
        token["_name"] = info.get("name") or ""
    return token


def sso_to_token(sso_cookie: str, max_retries: int = 8, base_delay: float = 15.0) -> dict:
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise CpaExportError("curl_cffi is not installed") from exc

    sso_cookie = str(sso_cookie or "").strip()
    if sso_cookie.startswith("sso="):
        sso_cookie = sso_cookie[4:]
    if not sso_cookie:
        raise CpaExportError("empty sso token")

    session = requests.Session()
    session.cookies.set("sso", sso_cookie, domain=".x.ai")

    try:
        resp = session.get("https://accounts.x.ai/", impersonate="chrome", timeout=15)
    except Exception as exc:
        raise CpaExportError(f"sso validation failed: {exc}") from exc
    if "sign-in" in resp.url or "sign-up" in resp.url:
        raise CpaExportError("sso is invalid")

    dc: dict | None = None
    rate_hits = 0

    def fresh_device() -> None:
        nonlocal dc
        dc = request_device_code()
        session.get(dc["verification_uri_complete"], impersonate="chrome", timeout=15)

    fresh_device()

    verify_ok = False
    approve_ok = False
    for attempt in range(1, max_retries + 1):
        resp = session.post(
            f"{OIDC_ISSUER}/oauth2/device/verify",
            data={"user_code": dc["user_code"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=15,
            allow_redirects=True,
        )
        body_snip = (resp.text or "")[:300] if hasattr(resp, "text") else ""
        if is_rate_limited(resp.url, body_snip):
            rate_hits += 1
            time.sleep(backoff_sec(base_delay, attempt, 180))
            fresh_device()
            continue
        if "consent" not in resp.url:
            raise CpaExportError(f"verify failed: {resp.url}")
        verify_ok = True

        resp = session.post(
            f"{OIDC_ISSUER}/oauth2/device/approve",
            data={
                "user_code": dc["user_code"],
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=15,
            allow_redirects=True,
        )
        body_snip = (resp.text or "")[:300] if hasattr(resp, "text") else ""
        if is_rate_limited(resp.url, body_snip):
            rate_hits += 1
            verify_ok = False
            time.sleep(backoff_sec(base_delay, attempt, 180))
            fresh_device()
            continue
        if "done" not in resp.url:
            raise CpaExportError(f"approve failed: {resp.url}")
        approve_ok = True
        break

    if not verify_ok:
        if rate_hits:
            raise CpaRateLimitedError("verify retry exhausted")
        raise CpaExportError("verify failed")
    if not approve_ok:
        if rate_hits:
            raise CpaRateLimitedError("approve retry exhausted")
        raise CpaExportError("approve failed")

    token = poll_token(
        dc["device_code"],
        dc.get("interval", 5),
        dc.get("expires_in", 1800),
    )
    return enrich_token_with_userinfo(token)


def cpa_filename(email: str = "", sub: str = "") -> str:
    email = safe_filename_part(email)
    sub = safe_filename_part(sub)
    if email:
        return f"xai-{email}.json"
    if sub:
        return f"xai-{sub}.json"
    return f"xai-anon_{secrets.token_hex(4)}.json"


def safe_filename_part(value: str) -> str:
    value = SAFE_FILENAME_RE.sub("_", (value or "").strip()).strip("._")
    return value[:120]


def token_to_cpa_entry(token: dict, email: str = "") -> tuple[str, dict]:
    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    id_token = token.get("id_token") or ""
    token_type = token.get("token_type") or "Bearer"
    expires_in = int(token.get("expires_in") or 21600)

    access_payload = decode_jwt_payload(access)
    id_payload = decode_jwt_payload(id_token) if id_token else {}
    sub = access_payload.get("sub") or access_payload.get("principal_id") or id_payload.get("sub") or ""
    resolved_email = (
        email
        or token.get("_email")
        or token.get("email")
        or id_payload.get("email")
        or access_payload.get("email")
        or ""
    )

    expired = rfc3339_sec(float(access_payload["exp"])) if "exp" in access_payload else rfc3339_sec(time.time() + expires_in)
    last_refresh = rfc3339_sec(float(access_payload["iat"])) if "iat" in access_payload else rfc3339_sec()

    entry = {
        "type": "xai",
        "auth_kind": "oauth",
        "access_token": access,
        "refresh_token": refresh,
        "token_type": token_type,
        "expires_in": expires_in,
        "expired": expired,
        "last_refresh": last_refresh,
        "email": resolved_email,
        "sub": sub,
        "base_url": CPA_BASE_URL,
        "token_endpoint": CPA_TOKEN_ENDPOINT,
        "redirect_uri": CPA_REDIRECT_URI,
        "disabled": False,
        "headers": dict(CPA_HEADERS),
        "id_token": id_token,
    }
    return cpa_filename(resolved_email, sub), entry


def sso_to_cpa_entry(sso_cookie: str, email: str = "", max_retries: int = 8) -> tuple[str, dict]:
    token = sso_to_token(sso_cookie, max_retries=max_retries)
    return token_to_cpa_entry(token, email=email)
