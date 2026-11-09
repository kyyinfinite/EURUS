from __future__ import annotations

import ipaddress
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "mc_eid",
        "mc_cid",
        "igshid",
        "yclid",
        "_ga",
        "spm",
        "scm",
    }
)
MAX_URL_LENGTH = 2048

DEFAULT_PORTS = {"http": 80, "https": 443}


def is_http_url(url: str) -> bool:
    parts = urlsplit(url.strip())
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def strip_tracking_params(query: str) -> list[tuple[str, str]]:
    kept: list[tuple[str, str]] = []
    for name, value in parse_qsl(query, keep_blank_values=True):
        lowered = name.lower()
        if lowered in TRACKING_PARAMS:
            continue
        if any(lowered.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES):
            continue
        kept.append((name, value))
    return kept


def normalize_url(url: str) -> str:
    cleaned = url.strip()
    if len(cleaned) > MAX_URL_LENGTH:
        raise ValueError(f"URL exceeds {MAX_URL_LENGTH} characters")
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("URL has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL has an invalid port") from exc
    netloc = host if port is None or port == DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    path = parts.path or "/"
    kept = strip_tracking_params(parts.query)
    query = urlencode(kept, quote_via=quote) if kept else ""
    fragment = "" if parts.fragment in {"", "/"} else parts.fragment
    return urlunsplit((scheme, netloc, path, query, fragment))


def canonical_url(url: str) -> str:
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    path = parts.path.rstrip("/")
    kept = sorted(strip_tracking_params(parts.query))
    query = urlencode(kept, quote_via=quote) if kept else ""
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL has an invalid port") from exc
    netloc = host if port is None or port == DEFAULT_PORTS.get(parts.scheme) else f"{host}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def url_domain(url: str) -> str:
    host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    return host


def registrable_domain(url: str) -> str:
    host = url_domain(url)
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    public_suffixes = {
        "com",
        "net",
        "org",
        "edu",
        "gov",
        "io",
        "co",
        "ai",
        "dev",
        "app",
        "info",
        "biz",
        "me",
        "tv",
        "sh",
        "fm",
        "so",
        "to",
        "cc",
    }
    two_level = {
        "co.uk",
        "co.id",
        "co.in",
        "co.jp",
        "co.kr",
        "co.za",
        "com.au",
        "com.br",
        "com.cn",
        "com.mx",
        "com.sg",
        "com.tr",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "ac.id",
        "go.id",
        "go.uk",
        "or.id",
        "or.jp",
        "ne.jp",
        "go.jp",
    }
    if len(labels) >= 3:
        candidate = ".".join(labels[-2:])
        if candidate in two_level:
            return ".".join(labels[-3:])
    if labels[-1] in public_suffixes:
        return ".".join(labels[-2:])
    return ".".join(labels[-2:])
