"""Contain exported text/resources without rewriting approved text or imported bytes."""

import base64
import ipaddress
import json
import re
from collections.abc import Iterator
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlsplit

from incident_report.evidence_import.reader import safe_path


class UnsafePayload(ValueError):
    pass


# A real machine path: a drive letter + separator, a UNC share or a known absolute root.
# Drives and known roots can follow prose punctuation. Ordinary officer text such as
# "Note:<Arabic>", "checked /ref", "lane (/left)", "60 km/h" and "and/or" stays valid.
_MACHINE_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|(?:^|[\s\"'(\[<=,;])\\\\[^\s\\]"
    r"|(?<![\w/<-])(?:/(?:Users|home|root|tmp|mnt|srv|Volumes|private|var|etc|opt)/|~/)"
    r"|(?<![\w:/])//"
    r"|file://|\blocalhost\b|127\.0\.0\.1",
    re.I,
)


def _text(text: str, evidence_root: str | None = None) -> None:
    text = unquote(unescape(text))
    if evidence_root and len(evidence_root.strip("/\\")) > 1:
        roots = {evidence_root.casefold(), evidence_root.replace("\\", "/").casefold()}
        folded = text.replace("\\", "/").casefold()
        if any(root in text.casefold() or root in folded for root in roots):
            raise UnsafePayload("A payload contains the evidence storage location.")
    for url in re.findall(r'https?://[^\s"<>]+', text):
        try:
            parts = urlsplit(url)
            uri_text = unquote(parts.path + "?" + parts.query + "#" + parts.fragment)
            if re.search(r"[A-Za-z]:[\\/]|\\\\|(?:^|[?&#=])/(?:Users|home|tmp|mnt|var|etc|opt)(?:/|$)", uri_text):
                raise UnsafePayload("A payload URL contains a machine-local path.")
            host = (parts.hostname or "").lower().rstrip(".")
            if not host or parts.username or parts.password or host == "localhost" or host.endswith(".localhost"):
                raise UnsafePayload("A payload contains a machine-local host or credential URL.")
            if (
                all(part.isdecimal() or part.lower().startswith("0x") for part in host.split("."))
                and len(host.split(".")) != 4
            ):
                raise UnsafePayload("A payload contains an ambiguous numeric machine-local host.")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is None and (
                "." not in host
                or host.endswith((".local", ".internal", ".lan", ".home"))
                or all(part.isdecimal() or part.lower().startswith("0x") for part in host.split("."))
            ):
                raise UnsafePayload("A payload contains an ambiguous numeric or machine-local host.")
            if address is not None and not address.is_global:
                raise UnsafePayload("A payload contains a machine-local host.")
        except ValueError:
            raise UnsafePayload("A payload contains an unsafe host URL.") from None
    without_urls = re.sub(r'https?://[^\s"<>]+', "", text)
    if _MACHINE_PATH.search(without_urls):
        raise UnsafePayload("A payload contains a machine-local path or host.")


def _strings(value: Any) -> Iterator[str]:
    """Yield every decoded JSON string (keys and values); JSON escapes are never scanned in raw form."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _json_text(path: str, text: str, evidence_root: str | None) -> None:
    documents = text.splitlines() if path.endswith(".jsonl") else [text]
    try:
        decoded = [json.loads(line) for line in documents if line.strip()]
    except ValueError:
        _text(text, evidence_root)  # Not valid JSON: fall back to the strict whole-text scan.
        return
    for document in decoded:
        for string in _strings(document):
            _text(string, evidence_root)


def _css(css: str) -> None:
    """Allow only embedded woff2 fonts; every other CSS resource reference could fetch from the network."""
    # Our fixed report styles do not need CSS escapes. Refuse them rather than
    # letting escaped identifiers conceal url()/@import from the resource scan.
    if "\\" in css:
        raise UnsafePayload("CSS escapes are not permitted in offline stylesheets.")
    lowered = css.lower()
    if (
        "@import" in lowered
        or "image-set(" in lowered
        or any(
            not url.strip("\"' ").lower().startswith("data:font/woff2;base64,")
            for url in re.findall(r"url\((.*?)\)", css, re.S | re.I)
        )
    ):
        raise UnsafePayload("A stylesheet contains an active network resource.")


# Resource hints make the browser contact a host before anyone clicks a citation.
_NETWORK_HINTS = {"prefetch", "preload", "dns-prefetch", "preconnect", "prerender", "modulepreload"}


class _OfflineHTML(HTMLParser):
    def __init__(self, evidence_root: str | None = None) -> None:
        super().__init__()
        self.evidence_root = evidence_root
        self.in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {
            "script",
            "iframe",
            "object",
            "embed",
            "form",
            "video",
            "audio",
            "source",
            "img",
            "svg",
            "math",
            "base",
        }:
            raise UnsafePayload("An HTML payload contains an active resource.")
        values = {key: value or "" for key, value in attrs}
        if tag == "style":
            self.in_style = True
        if tag == "meta" and values.get("http-equiv", "").strip().lower() == "refresh":
            raise UnsafePayload("An HTML payload contains a redirect.")
        if tag == "link" and _NETWORK_HINTS & set(values.get("rel", "").lower().split()):
            raise UnsafePayload("An HTML payload contains a network resource hint.")
        for key, value in attrs:
            value = value or ""
            if key.startswith("on") or key in {"src", "srcset", "style", "action", "ping"}:
                raise UnsafePayload("An HTML payload contains an active resource attribute.")
            if key == "href" and tag == "link":
                if not value.startswith("data:text/css;base64,"):
                    raise UnsafePayload("A stylesheet is not embedded.")
                try:
                    css = base64.b64decode(value.split(",", 1)[1].split("#")[0], validate=True).decode()
                except (ValueError, UnicodeError):
                    raise UnsafePayload("An embedded stylesheet is invalid.") from None
                _css(css)
            elif key == "href":
                if not (value.startswith("#") or value.startswith(("https://", "http://"))):
                    raise UnsafePayload("An HTML link has an unsafe path or scheme.")
                _text(value, self.evidence_root)
            else:
                _text(value, self.evidence_root)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_style:
            _css(data)
        _text(data, self.evidence_root)


def validate_payload(path: str, content: bytes, *, evidence_root: str | None = None) -> None:
    """Refuse a payload that carries an active resource or a machine-local path/host; name it in the message."""
    try:
        _validate(path, content, evidence_root)
    except UnsafePayload as exc:
        raise UnsafePayload(f"{path}: {exc}") from None


def _validate(path: str, content: bytes, evidence_root: str | None) -> None:
    if path.startswith("views/"):
        return  # Original video bytes are hash checked before they reach this boundary.
    if path.startswith("reconstruction/") and path != "reconstruction/review_aid.html":
        if not path.endswith((".json", ".jsonl")):
            raise UnsafePayload("An imported artifact has an active or unsupported format.")
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        raise UnsafePayload("An artifact is not portable UTF-8 text.") from None
    if path in {"report.html", "reconstruction/review_aid.html"}:
        parser = _OfflineHTML(evidence_root)
        parser.feed(text)
        parser.close()
    elif path.endswith((".json", ".jsonl")):
        _json_text(path, text, evidence_root)
    else:
        _text(text, evidence_root)


def member_path(name: str) -> None:
    safe_path(name)
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    for part in name.split("/"):
        if (
            part.endswith((" ", "."))
            or any(char in '<>"|?*' for char in part)
            or part.split(".")[0].lower() in reserved
            or any(ord(c) < 32 or ord(c) == 127 for c in part)
        ):
            raise UnsafePayload("Unsafe or non-portable member path.")
