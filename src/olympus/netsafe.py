"""Trygg henting fra nettet for serveren (lenker brukeren eller Zeus limer inn): aldri inn i maskinen eller hjemmenettet.

- Bare http/https (magnet: og .torrent håndteres av kalleren) – aldri file://, ftp:// eller andre skjemaer.
- Vertsnavnet slås opp, og ALLE adressene må være offentlige: ikke localhost, private nett, link-local,
  CGNAT/Tailscale (100.64/10), multicast, reserverte eller IPv4 inni IPv6.
- Adressen som sjekkes, er adressen det kobles til (festes i nettverkslaget for hver tilkobling) – også etter hver
  videresending. DNS som svarer noe annet andre gang («rebinding»), hjelper ikke.
- Maks størrelse og tid, ingen proxy fra miljøet.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx

UA = "OlympusLab/0.4 (personlig mediabibliotek)"
_EXTRA_BLOCK = [ipaddress.ip_network(n) for n in ("100.64.0.0/10", "0.0.0.0/8", "192.0.0.0/24", "198.18.0.0/15",
                                                  "240.0.0.0/4", "64:ff9b::/96", "2002::/16", "fc00::/7", "fe80::/10")]


class UnsafeURL(ValueError):
    """Lenken er ikke lov å hente (skjema, intern adresse, for stor, feil type)."""


def ip_is_internal(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            or not ip.is_global):
        return True
    return any(ip in n for n in _EXTRA_BLOCK if n.version == ip.version)


def resolve_public(host: str, port: int = 443) -> str:
    """Slå opp verten; feil hvis noen av adressene er interne. Returnerer adressen det skal kobles til."""
    host = (host or "").strip("[]")
    if not host:
        raise UnsafeURL("mangler vertsnavn")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise UnsafeURL(f"fant ikke verten {host}") from e
    addrs = [i[4][0] for i in infos]
    if not addrs or any(ip_is_internal(a) for a in addrs):
        raise UnsafeURL(f"lenken peker til en intern adresse ({host})")
    return addrs[0]


def is_internal_host(host: str) -> bool:
    try:
        resolve_public(host)
        return False
    except UnsafeURL:
        return True


def check_url(url: str) -> str:
    """Skjema og form (ingen nettverk). Returnerer lenken uten mellomrom rundt."""
    url = (url or "").strip()
    if len(url) > 4000:
        raise UnsafeURL("lenken er for lang")
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL("bare http(s)-lenker er lov")
    if any(ch in url for ch in "\r\n\t\x00 "):
        raise UnsafeURL("ugyldige tegn i lenken")
    return url


class _GuardedBackend(httpcore.NetworkBackend):
    """Hver tilkobling: slå opp verten selv, avvis interne adresser og koble til akkurat den adressen som ble sjekket.
    TLS/SNI og Host-headeren bruker fortsatt vertsnavnet (httpcore tar det fra opphavet, ikke herfra)."""

    def __init__(self) -> None:
        self._inner = httpcore.SyncBackend()

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                    socket_options: Any = None) -> httpcore.NetworkStream:
        ip = resolve_public(host, port)
        return self._inner.connect_tcp(ip, port, timeout, local_address, socket_options)

    def connect_unix_socket(self, *a: Any, **kw: Any) -> httpcore.NetworkStream:  # pragma: no cover
        raise UnsafeURL("unix-sokler er ikke lov")

    def sleep(self, seconds: float) -> None:  # pragma: no cover
        self._inner.sleep(seconds)


def client(timeout: float = 20.0, any_host: bool = False) -> httpx.Client:
    """httpx-klient som bare kobler til offentlige adresser (any_host=True: alle adresser – nedlastinger du ber om).
    Følger IKKE videresendinger selv (se `open_url`)."""
    transport = httpx.HTTPTransport(retries=0)
    if not any_host:
        transport._pool._network_backend = _GuardedBackend()  # type: ignore[attr-defined]
    return httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False,
                        headers={"User-Agent": UA})


class Fetch:
    """Et svar fra `open_url`: endelig lenke, headere og en strøm med størrelses- og tidsgrense."""

    def __init__(self, c: httpx.Client, resp: httpx.Response, max_bytes: int, deadline: float) -> None:
        self.client, self.resp, self.max_bytes, self.deadline = c, resp, max_bytes, deadline
        self.url = str(resp.url)
        self.headers = resp.headers
        self.status = resp.status_code

    @property
    def length(self) -> int | None:
        cr = self.headers.get("content-range", "")
        if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
            return int(cr.rsplit("/", 1)[1])
        cl = self.headers.get("content-length", "")
        return int(cl) if cl.isdigit() else None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def iter_bytes(self, chunk: int = 1 << 16):
        got = 0
        for part in self.resp.iter_bytes(chunk):
            got += len(part)
            if got > self.max_bytes:
                raise UnsafeURL(f"filen er større enn grensen ({self.max_bytes // 1_000_000} MB)")
            if time.monotonic() > self.deadline:
                raise UnsafeURL("hentingen tok for lang tid")
            yield part

    def read(self) -> bytes:
        return b"".join(self.iter_bytes())

    def close(self) -> None:
        self.resp.close()
        self.client.close()

    def __enter__(self) -> Fetch:
        return self

    def __exit__(self, *_a: Any) -> None:
        self.close()


def open_url(url: str, *, method: str = "GET", max_bytes: int = 10 * 1024 * 1024, timeout: float = 20.0,
             max_redirects: int = 5, headers: dict[str, str] | None = None, any_host: bool = False) -> Fetch:
    """Åpne en lenke trygt. Videresendinger følges for hånd, og hvert steg sjekkes (skjema + adresse). Kalleren
    må lukke svaret (`with open_url(...) as f:`)."""
    url = check_url(url)
    deadline = time.monotonic() + timeout
    c = client(timeout=min(timeout, 30.0), any_host=any_host)
    try:
        for _ in range(max_redirects + 1):
            req = c.build_request(method, url, headers=headers or {})
            resp = c.send(req, stream=True)
            if resp.is_redirect:
                loc = resp.headers.get("location", "")
                resp.close()
                url = check_url(str(httpx.URL(url).join(loc)))
                if time.monotonic() > deadline:
                    raise UnsafeURL("hentingen tok for lang tid")
                continue
            if resp.status_code >= 400:
                resp.close()
                raise httpx.HTTPStatusError(f"serveren svarte {resp.status_code}", request=req, response=resp)
            return Fetch(c, resp, max_bytes, deadline)
        raise UnsafeURL("for mange videresendinger")
    except BaseException:
        c.close()
        raise


def fetch_bytes(url: str, max_bytes: int = 10 * 1024 * 1024, timeout: float = 20.0,
                any_host: bool = False) -> tuple[bytes, str]:
    """Hele svaret (liten fil, f.eks. et bilde eller en .torrent) og innholdstypen."""
    with open_url(url, max_bytes=max_bytes, timeout=timeout, any_host=any_host) as f:
        if f.length is not None and f.length > max_bytes:
            raise UnsafeURL(f"filen er større enn grensen ({max_bytes // 1_000_000} MB)")
        return f.read(), f.content_type


MAGNET_HOST_KEYS = ("tr", "ws", "xs", "as", "x.pe")  # adresser Transmission ville koblet seg til
MAGNET_KEEP = {"xt", "dn", "xl", "kt", "so", *MAGNET_HOST_KEYS}


def safe_magnet(url: str) -> tuple[str, list[str]]:
    """Fjern trackere/webseeds/peers i en magnetlenke som peker inn i ditt eget nett (eller ikke finnes) – ellers
    ville Transmission koblet seg til dem for deg. Returnerer (ny lenke, det som ble fjernet).
    Kjent rest: Transmission slår opp navnene selv senere, så et navn som bytter til en intern adresse etter denne
    sjekken (DNS-rebinding) slipper gjennom. Lenkene kommer fra deg selv, så det godtas."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import parse_qsl, quote

    raw = parse_qsl(url.split("?", 1)[1] if "?" in url else "", keep_blank_values=True)
    # Bare kjente nøkler (små bokstaver, «tr.1» = «tr»); alt annet kastes, så ingen ukjent nøkkel smetter forbi.
    pairs: list[tuple[str, str]] = []
    dropped: list[str] = []
    for k, v in raw:
        k = k.strip().lower()
        k = k.split(".", 1)[0] if k.split(".", 1)[0] in ("tr", "ws", "xs", "as") else k
        (pairs.append((k, v)) if k in MAGNET_KEEP else dropped.append(f"{k}={v}"))
    hosts: dict[str, str] = {}
    for k, v in pairs:
        if k in MAGNET_HOST_KEYS:
            if any(c in v for c in "\\@ \t\r\n\x00") or (k != "x.pe" and urlparse(v).scheme.lower() not in
                                                            ("http", "https", "udp", "ws", "wss")):
                hosts[v] = ""  # rar form som tolkes ulikt av ulike parsere: fjernes
                continue
            if k == "x.pe":
                host = v.rsplit(":", 1)[0] if v.count(":") == 1 or v.startswith("[") else v
            else:
                host = urlparse(v).hostname or ""
            hosts[v] = host.strip("[]")

    def public(host: str) -> bool:
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return not ip_is_internal(host)
        except ValueError:
            return not is_internal_host(host)

    uniq = sorted(set(hosts.values()))[:50]
    with ThreadPoolExecutor(max_workers=8) as pool:
        ok = dict(zip(uniq, pool.map(public, uniq)))
    keep = []
    for k, v in pairs:
        if k in MAGNET_HOST_KEYS and not ok.get(hosts.get(v, ""), False):
            dropped.append(v)
            continue
        keep.append(f"{k}={quote(v, safe=':/')}")
    return "magnet:?" + "&".join(keep), dropped
