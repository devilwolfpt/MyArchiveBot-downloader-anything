"""Gestão de cookies de sessão.

Suporta:
  * cookies fornecidos pelo cliente (dict simples);
  * ficheiro Netscape ``cookies.txt`` (formato curl/extensões "Get cookies.txt LOCALLY");
  * cookies em formato JSON (Cookie-Editor, EditThisCookie);
  * extracção de cookies de um browser local (opcional, requer ``browser-cookie3``);
  * propagação automática do ``Set-Cookie`` entre pedidos (via httpx.Cookies).
"""
from __future__ import annotations

import json
import logging
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


def parse_cookie_string(raw: str) -> dict[str, str]:
    """Converte ``"a=1; b=2"`` num dict."""
    out: dict[str, str] = {}
    if not raw:
        return out
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # pragma: no cover - input malformado
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    for key, morsel in jar.items():
        out[key] = morsel.value
    return out


def load_netscape_cookie_file(path: str | Path) -> dict[str, str]:
    """Lê um ficheiro ``cookies.txt`` no formato Netscape para um dict simples."""
    cookies: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Ficheiro de cookies não encontrado: {p}")
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies


def load_netscape_cookiejar(path: str | Path) -> MozillaCookieJar:
    """Lê um ficheiro ``cookies.txt`` no formato Netscape para um MozillaCookieJar completo com domínios."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Ficheiro de cookies não encontrado: {p}")
    jar = MozillaCookieJar(str(p))
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def parse_cookies_text(raw: str) -> tuple[MozillaCookieJar, list[str]]:
    """Converte texto em formato Netscape ou JSON num MozillaCookieJar e lista de domínios únicos."""
    raw = raw.strip()
    jar = MozillaCookieJar()
    domains: set[str] = set()
    if not raw:
        return jar, []

    # 1. Tenta formato JSON (ex: Cookie-Editor)
    if raw.startswith("[") and raw.endswith("]"):
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    value = item.get("value")
                    domain = item.get("domain", "")
                    path = item.get("path", "/")
                    secure = bool(item.get("secure", False))
                    expires = item.get("expirationDate")
                    if not name or value is None:
                        continue
                    clean_domain = domain.lstrip(".")
                    if clean_domain:
                        domains.add(clean_domain)
                    cookie = Cookie(
                        version=0,
                        name=name,
                        value=value,
                        port=None,
                        port_specified=False,
                        domain=domain,
                        domain_specified=bool(domain.startswith(".")),
                        domain_initial_dot=domain.startswith("."),
                        path=path,
                        path_specified=True,
                        secure=secure,
                        expires=int(expires) if expires else None,
                        discard=False,
                        comment=None,
                        comment_url=None,
                        rest={},
                        rfc2109=False,
                    )
                    jar.set_cookie(cookie)
                return jar, sorted(domains)
        except Exception as exc:
            log.debug("Não foi possível parsear cookies como JSON: %s", exc)

    # 2. Formato Netscape (Get cookies.txt LOCALLY, curl/wget)
    lines = raw.splitlines()
    has_magic = any(line.startswith("# Netscape HTTP Cookie File") for line in lines[:5])
    if not has_magic:
        lines.insert(0, "# Netscape HTTP Cookie File")

    with NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".txt") as tmp:
        tmp.write("\n".join(lines) + "\n")
        tmp_path = Path(tmp.name)

    try:
        temp_jar = MozillaCookieJar(str(tmp_path))
        temp_jar.load(ignore_discard=True, ignore_expires=True)
        for c in temp_jar:
            d = c.domain.lstrip(".")
            if d:
                domains.add(d)
            jar.set_cookie(c)
        return jar, sorted(domains)
    except Exception as exc:
        log.warning("Falha ao parsear ficheiro Netscape: %s", exc)
        raise ValueError(f"Formato de cookies inválido: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)


def save_cookie_jar(jar: CookieJar, path: str | Path) -> None:
    """Guarda um CookieJar num ficheiro Netscape standard."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    mozilla_jar = MozillaCookieJar(str(p))
    for c in jar:
        mozilla_jar.set_cookie(c)
    mozilla_jar.save(ignore_discard=True, ignore_expires=True)


def get_cookie_file_stats(path: str | Path) -> dict[str, Any]:
    """Retorna estatísticas sobre o ficheiro de cookies atual."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {
            "present": False,
            "count": 0,
            "domains": [],
            "updated_at": None,
            "size_bytes": 0,
        }
    try:
        jar = load_netscape_cookiejar(p)
        cookies = list(jar)
        domains = sorted({c.domain.lstrip(".") for c in cookies if c.domain})
        return {
            "present": True,
            "count": len(cookies),
            "domains": domains,
            "updated_at": p.stat().st_mtime,
            "size_bytes": p.stat().st_size,
        }
    except Exception as exc:
        log.warning("Erro ao ler estatísticas de %s: %s", p, exc)
        return {
            "present": True,
            "count": 0,
            "domains": [],
            "updated_at": p.stat().st_mtime,
            "size_bytes": p.stat().st_size,
            "error": str(exc),
        }


def load_cookies_from_browser(browser: str, url: str) -> CookieJar:
    """Lê cookies do browser para o domínio do URL, mantendo o âmbito original."""
    try:
        import browser_cookie3  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependência de runtime
        raise RuntimeError(
            "A leitura de cookies do browser requer a dependência browser-cookie3."
        ) from exc

    browser_name = browser.lower()
    loader = getattr(browser_cookie3, browser_name, None)
    if loader is None:
        raise ValueError(f"Browser não suportado: {browser}")
    host = urlsplit(url).hostname
    if not host:
        raise ValueError("Não foi possível determinar o domínio para ler cookies.")

    try:
        jar = loader(domain_name=host)
    except Exception as exc:
        log.warning("Não foi possível ler cookies do browser %s para %s", browser, host)
        raise RuntimeError(
            f"Não foi possível ler cookies de {browser} para {host}. "
            "Confirma que a sessão está autenticada e, se necessário, fecha o browser."
        ) from exc
    if not any(jar):
        raise RuntimeError(
            f"Não foram encontrados cookies de {browser} para {host}. "
            "Inicia sessão no site nesse browser e tenta novamente."
        )
    return jar


def merge_cookies(*sources: dict[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for src in sources:
        if src:
            merged.update(src)
    return merged


def cookies_to_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host
