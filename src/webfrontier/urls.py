"""Pure data model for the crawler: URL identity, link discovery, HTML parsing.

Everything in this module is deterministic and network-free so the rules can be
unit tested without starting a synthetic site.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlsplit

USER_AGENT = "CoopDirectoryBot/1.0 (+local synthetic directory crawler)"

# RFC 3986 unreserved octets.  Percent-encoded unreserved octets are decoded
# for identity purposes so /a%7Eb and /a~b share one identity.
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_HEX = frozenset("0123456789abcdefABCDEF")

_DEFAULT_PORTS = {"http": 80, "https": 443}
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_BINARY_SUFFIX_RE = re.compile(
    r"\.(?:pdf|png|jpe?g|gif|zip|tgz|tar|gz|bin|exe|mp4|mp3|svg|csv|xlsx?|docx?|pptx?)$",
    re.I,
)


class URLIdentityError(ValueError):
    """URL cannot be turned into a stable http(s) identity."""


@dataclass(frozen=True)
class URLIdentity:
    scheme: str
    host: str
    port: int
    path: str          # percent-encoded path, always starts with "/"
    query: str | None  # raw query string without "?", None when absent

    @property
    def origin(self) -> str:
        default = _DEFAULT_PORTS[self.scheme]
        host = self.host
        if ":" in host and not host.startswith("["):  # IPv6 literal
            host = f"[{host}]"
        if self.port == default:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def path_query(self) -> str:
        return self.path if self.query is None else f"{self.path}?{self.query}"

    @property
    def canonical(self) -> str:
        return self.origin + self.path_query

    @property
    def request_url(self) -> str:
        """URL used on the wire when no original spelling is available."""
        return self.canonical


def _normalise_path(path: str) -> str:
    """Resolve dot segments, encode unsafe octets and decode unreserved ones.

    Encoded slashes (``%2F``) are reserved and therefore never become a real
    path separator.  Percent escapes of reserved characters are upper-cased
    for a stable identity.
    """
    path = _remove_dot_segments(path) or "/"
    if not path.startswith("/"):
        path = "/" + path
    out: list[str] = []
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == "%" and i + 2 < n and path[i + 1] in _HEX and path[i + 2] in _HEX:
            octet = int(path[i + 1 : i + 3], 16)
            if chr(octet) in _UNRESERVED:
                out.append(chr(octet))
            else:
                out.append("%" + path[i + 1 : i + 3].upper())
            i += 3
            continue
        code = ord(ch)
        if code <= 0x20 or code >= 0x7F:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4, preserving empty segments and escapes."""
    if "." not in path:
        return path
    output: list[str] = []
    remaining = path
    while remaining:
        if remaining.startswith("../"):
            remaining = remaining[3:]
        elif remaining.startswith("./"):
            remaining = remaining[2:]
        elif remaining.startswith("/./"):
            remaining = "/" + remaining[3:]
        elif remaining == "/.":
            remaining = "/"
        elif remaining.startswith("/../"):
            remaining = "/" + remaining[4:]
            if output:
                output.pop()
        elif remaining == "/..":
            remaining = "/"
            if output:
                output.pop()
        elif remaining in (".", ".."):
            remaining = ""
        else:
            start = 1 if remaining.startswith("/") else 0
            end = remaining.find("/", start)
            if end == -1:
                output.append(remaining)
                remaining = ""
            else:
                output.append(remaining[:end])
                remaining = remaining[end:]
    return "".join(output) or "/"


def _validate_no_unsafe(text: str, where: str) -> None:
    for ch in text:
        if ord(ch) < 0x20 or ord(ch) == 0x7F or ch == " ":
            raise URLIdentityError(f"unsafe character in {where}")


def parse_identity(url: str) -> URLIdentity:
    """Parse ``url`` into a normalised identity.

    * fragment removed; scheme must be http(s); host lower-cased;
    * default port removed; dot segments resolved; empty path becomes ``/``;
    * query preserved exactly: parameter order and repeated parameters are
      never sorted or collapsed;
    * ``%2F`` stays encoded and never becomes a path separator.
    """
    if not isinstance(url, str) or not url:
        raise URLIdentityError("empty url")
    _validate_no_unsafe(url, "url")
    # Drop the fragment before parsing so "#..." never influences identity.
    url = url.split("#", 1)[0]
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise URLIdentityError(f"unsupported scheme {scheme!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise URLIdentityError("url has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise URLIdentityError(str(exc)) from exc
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if not (0 < port < 65536):
        raise URLIdentityError("port out of range")

    path = _normalise_path(parts.path or "/")
    query: str | None = parts.query if "?" in url else None
    if query is not None:
        _validate_no_unsafe(query, "query")
        # Keep parameter order and duplicates verbatim.  Only escape octets
        # that cannot appear on the request line.
        query = "".join(
            ch if 0x20 < ord(ch) < 0x7F else "".join(f"%{b:02X}" for b in ch.encode())
            for ch in query
        )
    return URLIdentity(scheme, host, port, path, query)


def strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def resolve_link(base: str, href: str) -> str | None:
    """Resolve ``href`` against ``base``.

    Returns an absolute URL with the fragment removed, or ``None`` when the
    link cannot identify an http(s) resource (javascript:, mailto:, bare
    fragment, empty href).
    """
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    if _URI_RE.match(href):
        scheme = href.split(":", 1)[0].lower()
        if scheme not in _DEFAULT_PORTS:
            return None
        return strip_fragment(href)
    try:
        return strip_fragment(urljoin(base, href))
    except ValueError:
        return None


def media_type(header_value: str | None) -> str:
    if not header_value:
        return ""
    return header_value.split(";", 1)[0].strip().lower()


HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def looks_like_page_path(path: str) -> bool:
    return not _BINARY_SUFFIX_RE.search(path)


@dataclass
class DiscoveredLink:
    raw_href: str
    request_url: str  # absolute wire URL from resolution (fragment removed)
    canonical: str    # stable identity string
    text: str
    base_url: str     # base used for resolution (document URL or <base>)
    is_page_hint: bool

    def to_row(self) -> dict:
        return {
            "raw_href": self.raw_href,
            "request_url": self.request_url,
            "canonical": self.canonical,
            "text": self.text,
            "base_url": self.base_url,
            "is_page_hint": self.is_page_hint,
        }


@dataclass
class ParsedHTML:
    title: str
    item_id: str | None
    base_hrefs: list[str]
    base_url: str
    links: list[DiscoveredLink] = field(default_factory=list)
    attachments: list[DiscoveredLink] = field(default_factory=list)
    unparsed_links: int = 0


_ATTACHMENT_HINT = re.compile(
    r"\.(?:pdf|zip|tgz|tar\.gz|gz|bin|csv|xlsx?|docx?|pptx?|png|jpe?g|gif)$", re.I
)


def _first_valid_base(fetch_url: str, base_hrefs: list[str]) -> str | None:
    for href in base_hrefs:
        absolute = resolve_link(fetch_url, href)
        if absolute is None:
            continue
        try:
            parse_identity(absolute)
        except URLIdentityError:
            continue
        return absolute
    return None


def parse_html(fetch_url: str, body: bytes) -> ParsedHTML:
    """Parse a page; raises ``ValueError`` when the HTML cannot be parsed.

    A response declared as HTML must be valid UTF-8 (RFC 9309 aside, HTML
    documents that decode successfully are the only ones whose links can be
    trusted).  Invalid bytes are a parse failure, not a silently-linkless page.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"html decode error: {exc}") from exc
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(text)
    except Exception as exc:
        raise ValueError(f"html parse error: {exc}") from exc

    title_node = tree.css_first("title")
    title = title_node.text(separator=" ", strip=True) if title_node else ""
    h1 = tree.css_first("h1")
    if not title and h1 is not None:
        title = h1.text(separator=" ", strip=True)

    item_id = None
    for selector in ('meta[name="item_id"]', "[data-item-id]"):
        node = tree.css_first(selector)
        if node is None:
            continue
        attrs = node.attributes or {}
        value = attrs.get("content") if selector.startswith("meta") else attrs.get("data-item-id")
        if value and value.strip():
            item_id = value.strip()
            break

    base_hrefs = [(n.attributes or {}).get("href", "") for n in tree.css("base[href]")]
    base_url = _first_valid_base(fetch_url, base_hrefs) or fetch_url

    links: list[DiscoveredLink] = []
    attachments: list[DiscoveredLink] = []
    unparsed = 0
    for node in tree.css("a[href]"):
        raw = (node.attributes or {}).get("href", "")
        text = node.text(separator=" ", strip=True)
        absolute = resolve_link(base_url, raw)
        if absolute is None:
            if raw and (raw.startswith("#") or _URI_RE.match(raw)):
                continue  # fragment/other scheme: not a discoverable page
            unparsed += 1
            continue
        try:
            ident = parse_identity(absolute)
        except URLIdentityError:
            unparsed += 1
            continue
        link = DiscoveredLink(
            raw_href=raw,
            request_url=identity_wire_url(absolute, ident),
            canonical=ident.canonical,
            text=text,
            base_url=base_url,
            is_page_hint=looks_like_page_path(ident.path),
        )
        if _ATTACHMENT_HINT.search(ident.path) or not link.is_page_hint:
            attachments.append(link)
        else:
            links.append(link)
    return ParsedHTML(
        title=title,
        item_id=item_id,
        base_hrefs=base_hrefs,
        base_url=base_url,
        links=links,
        attachments=attachments,
        unparsed_links=unparsed,
    )


def identity_wire_url(absolute: str, ident: URLIdentity) -> str:
    """Keep the caller's spelling for the request, minus the fragment.

    Network address and canonical identity are stored separately: this is the
    former.  Falls back to the canonical form if the original cannot be used.
    """
    wire = strip_fragment(absolute).strip()
    return wire or ident.canonical


@dataclass
class Scope:
    """Origin/path-prefix allow list from the task configuration."""

    entries: list[tuple[str, str]] = field(default_factory=list)

    def allows(self, ident: URLIdentity) -> bool:
        origin = ident.origin
        return any(
            origin == allowed_origin and ident.path.startswith(prefix)
            for allowed_origin, prefix in self.entries
        )

    def origin_allowed(self, origin: str) -> bool:
        return any(origin == allowed_origin for allowed_origin, _ in self.entries)


def normalise_linked(links: Iterable[DiscoveredLink]) -> list[DiscoveredLink]:
    return list(links)
