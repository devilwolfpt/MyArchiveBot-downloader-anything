"""Camada HTTP do core."""
from .client import HttpClient, HttpResult, is_private_host
from .cookies import (
    cookies_to_header,
    get_cookie_file_stats,
    load_cookies_from_browser,
    load_netscape_cookie_file,
    load_netscape_cookiejar,
    merge_cookies,
    parse_cookie_string,
    parse_cookies_text,
    save_cookie_jar,
)
from .headers import HeaderFactory, default_headers_for

__all__ = [
    "HttpClient",
    "HttpResult",
    "HeaderFactory",
    "default_headers_for",
    "is_private_host",
    "cookies_to_header",
    "get_cookie_file_stats",
    "load_cookies_from_browser",
    "load_netscape_cookie_file",
    "load_netscape_cookiejar",
    "merge_cookies",
    "parse_cookie_string",
    "parse_cookies_text",
    "save_cookie_jar",
]
