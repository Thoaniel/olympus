"""Film og serier på TV-en: PC-en finner TV-ene på hjemmenettet og styrer dem selv – appen og Zeus sier bare «spill
X på TV-en i stua».

- Chromecast / Google TV / Android TV (Philips, Sony, TCL …): Cast-protokollen (pychromecast).
- Smart-TV-er med DLNA/UPnP (Samsung, LG, Panasonic, de fleste andre): AVTransport (SSDP + SOAP, skrevet her).
- Apple TV / AirPlay-TV-er: AirPlay (pyatv).

TV-en henter filmen selv fra en egen strømmeport på hjemmenettet (CastServer). Den svarer bare på /cast/<nøkkel>/…,
der nøkkelen er tilfeldig, gjelder én film og går ut etter 6 timer – resten av Olympus er aldri synlig på nettet.
Formater TV-en ikke kan spille (MKV, HEVC, EAC3 på en Chromecast), omkodes på PC-en (GPU først) til H.264/AAC.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

TOKEN_TTL = 6 * 3600
DISCOVER_TTL = 60.0

# Hva hver TV-type spiller direkte (resten omkodes). Chromecast (1.–3. gen.) er strengest.
DIRECT = {
    "chromecast": ({"h264", "vp8", "vp9"}, {"aac", "mp3", "opus", "vorbis", ""}, ("mp4", "mov", "webm")),
    "dlna": ({"h264", "hevc", "mpeg4"}, {"aac", "ac3", "eac3", "mp3", ""}, ("mp4", "mov", "matroska")),
    "airplay": ({"h264", "hevc"}, {"aac", "ac3", "eac3", ""}, ("mp4", "mov")),
}


@dataclass
class Device:
    id: str
    name: str
    kind: str  # chromecast | dlna | airplay
    host: str
    model: str = ""
    control: str = ""  # DLNA: AVTransport-kontroll-URL
    ref: Any = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        label = {"chromecast": "Chromecast/Google TV", "dlna": "Smart-TV (DLNA)", "airplay": "AirPlay"}[self.kind]
        return {"id": self.id, "name": self.name, "kind": self.kind, "type": label, "model": self.model}


def can_direct(kind: str, v: dict[str, Any]) -> bool:
    vids, auds, conts = DIRECT[kind]
    container = (v.get("container") or "").split(",")
    return v.get("vcodec") in vids and (v.get("acodec") or "") in auds and any(c in conts for c in container)


def caps(dev: Device) -> dict[str, Any]:
    """Hva TV-en tåler: HEVC og høyeste oppløsning. Chromecast 1.–3. gen. («Chromecast», «Eureka Dongle») er
    1080p H.264; Ultra, Google TV og Android TV-er tar 4K HEVC. DLNA-TV-er og Apple TV 4K tar HEVC."""
    if dev.kind == "chromecast":
        m = dev.model.lower()
        old = m in ("chromecast", "eureka dongle", "") or "chromecast hd" in m
        return {"hevc": not old or "hd" in m, "max_h": 1080 if old else 2160}
    return {"hevc": True, "max_h": 2160}


@functools.lru_cache(maxsize=512)
def video_height(path: str) -> int:
    """Høyden på videoen (ffprobe, hurtiglagret) – biblioteket lagrer ikke oppløsning."""
    import subprocess

    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=15).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


def plan(dev: Device, v: dict[str, Any]) -> tuple[str, int]:
    """(modus, høyde): direct = fila som den er; remux = bildet urørt (4K beholdes), ny innpakning og lyd;
    transcode = H.264 i høyden TV-en tåler."""
    c = caps(dev)
    h = v.get("height") or (video_height(v["path"]) if v.get("path") else 0)
    if can_direct(dev.kind, v) and h <= c["max_h"]:
        return "direct", 0
    vc = v.get("vcodec")
    if (vc == "h264" or (vc == "hevc" and c["hevc"])) and h <= c["max_h"]:
        return "remux", 0
    return "transcode", c["max_h"]


def lan_ip() -> str | None:
    """Adressen PC-en har på hjemmenettet (den TV-ene når), ikke Tailscale."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))  # ingen pakker sendes; velger bare utgående grensesnitt
        ip = s.getsockname()[0]
        if not ip.startswith(("127.", "100.")):
            return ip
    return None


# --- DLNA -----------------------------------------------------------------------------------------
AVT = "urn:schemas-upnp-org:service:AVTransport:1"


async def ssdp_renderers(timeout: float = 2.5) -> list[tuple[str, str]]:
    """(vert, beskrivelses-URL) for alle UPnP MediaRenderer-enheter som svarer."""
    loop = asyncio.get_running_loop()
    found: dict[str, str] = {}

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            m = re.search(rb"(?im)^location:\s*(\S{1,300})", data[:4096])
            if not m:
                return
            loc = m.group(1).decode(errors="replace")
            # bare gyldige svar teller (lenken peker på avsenderen), og én per enhet: en enhet som oversvømmer
            # søket med falske svar kan ikke skyve de ekte TV-ene ut
            if _same_lan_host(loc, addr[0]) and addr[0] not in found.values() and len(found) < 64:
                found[loc] = addr[0]

    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
    transport, _ = await loop.create_datagram_endpoint(P, local_addr=("0.0.0.0", 0), family=socket.AF_INET)
    try:
        transport.sendto(msg, ("239.255.255.250", 1900))
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return [(host, loc) for loc, host in found.items()][:32]  # høyst 32 enheter spørres


def _same_lan_host(url: str, host: str) -> bool:
    """SSRF-vern: lenker fra et SSDP-svar må peke tilbake på enheten som svarte – en privat adresse på hjemmenettet,
    aldri PC-en selv (127.x, Olympus' egne porter) eller noe utenfor."""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url)
        ip = ipaddress.ip_address(u.hostname or "")
        sender = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (u.scheme == "http" and ip == sender and ip.is_private and not ip.is_loopback and not ip.is_link_local
            and str(ip) != (lan_ip() or ""))


def clean_name(name: str) -> str:
    """Navn kommer fra enhetene selv: bare vanlige tegn, én linje, kort (de havner i Zeus' verktøysvar)."""
    return re.sub(r"[^\w .,'()&+\-]", "", re.sub(r"\s+", " ", name)).strip()[:60] or "TV"


async def dlna_device(client: Any, host: str, location: str) -> Device | None:
    from urllib.parse import urljoin

    if not _same_lan_host(location, host):
        return None
    try:  # les høyst 200 kB: en enhet på nettet skal ikke kunne fylle minnet
        buf = b""
        async with client.stream("GET", location, timeout=3, follow_redirects=False) as r:
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > 200_000:
                    break
        xml = buf[:200_000].decode(errors="replace")
    except Exception:  # noqa: BLE001 – en enhet som ikke svarer, hoppes over
        return None
    name = re.search(r"<friendlyName>([^<]*)", xml)
    model = re.search(r"<modelName>([^<]*)", xml)
    udn = re.search(r"<UDN>([^<]*)", xml)
    for svc in re.findall(r"<service>(.*?)</service>", xml, re.S):
        if "AVTransport" in svc:
            ctl = re.search(r"<controlURL>([^<]*)", svc)
            control = urljoin(location, ctl.group(1).strip()) if ctl else ""
            if control and _same_lan_host(control, host):
                return Device(id=f"dlna:{clean_name(udn.group(1) if udn else host)}", name=clean_name(name.group(1) if name else host),
                              kind="dlna", host=host, model=clean_name(model.group(1) if model else ""), control=control)
    return None


async def dlna_call(client: Any, dev: Device, action: str, args: dict[str, str]) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    env = (f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} xmlns:u="{AVT}">'
           f"<InstanceID>0</InstanceID>{body}</u:{action}></s:Body></s:Envelope>")
    r = await client.post(dev.control, content=env.encode(), timeout=8, follow_redirects=False, headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{AVT}#{action}"'})
    if r.status_code >= 400:
        raise CastError(f"{dev.name} avviste {action} ({r.status_code}).")
    return r.text


def didl(url: str, title: str, mime: str, srt: str | None = None) -> str:
    """Metadata TV-en viser (tittel) og forstår (protocolInfo). Mange TV-er spiller ikke uten. Undertekst (SRT):
    som egen res (LG o.fl.) og sec:CaptionInfoEx (Samsung)."""
    flags = "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000" if mime == "video/mp4" else "*"
    sub = (f'<res protocolInfo="http-get:*:text/srt:*">{escape(srt)}</res>'
           f'<sec:CaptionInfoEx sec:type="srt">{escape(srt)}</sec:CaptionInfoEx>') if srt else ""
    return escape(
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:sec="http://www.sec.co.kr/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title><upnp:class>object.item.videoItem.movie</upnp:class>"
        f'<res protocolInfo="http-get:*:{mime}:{flags}">{escape(url)}</res>{sub}</item></DIDL-Lite>')


class CastError(Exception):
    pass


SUB_PREF = ("nob", "nor", "no", "nb", "nno", "nn", "swe", "sv", "dan", "da", "eng", "en")


def pick_subtitle(subs: list[dict[str, Any]], want: str = "auto") -> dict[str, Any] | None:
    """Sporet som vises på TV-en: «av» = ingen, et tall = det sporet, «auto» = norsk, så svensk/dansk, så engelsk
    (uten SDH/hørselshemmet først), ellers ingen (et ukjent språk er sjelden det man vil ha)."""
    if not subs or want in ("av", "off", "none"):
        return None
    if want.isdigit():
        return next((x for x in subs if x["idx"] == int(want)), None)
    for lang in SUB_PREF:
        hits = [x for x in subs if (x.get("lang") or "").lower() == lang]
        if hits:
            plain = [x for x in hits if "sdh" not in (x.get("title") or "").lower() and "forced" not in (x.get("title") or "").lower()]
            return (plain or hits)[0]
    return None


def lang_tag(lang: str) -> str:
    return {"nob": "nb", "nor": "nb", "no": "nb", "nno": "nn", "swe": "sv", "dan": "da", "eng": "en"}.get(lang, lang or "und")


# --- Caster ---------------------------------------------------------------------------------------
class Caster:
    def __init__(self) -> None:
        self.lib: Any = None
        self.host: str | None = None
        self.port = 8789
        self.tokens: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, Device] = {}
        self.found_at = 0.0
        self.now: dict[str, Any] | None = None  # {device, video, title, started, start, mode}
        self._lock = asyncio.Lock()
        self._cc_conn: dict[str, Any] = {}
        self.last_token = ""

    # oppdagelse
    async def discover(self, force: bool = False) -> list[Device]:
        async with self._lock:
            if not force and self.devices and time.monotonic() - self.found_at < DISCOVER_TTL:
                return list(self.devices.values())
            results = await asyncio.gather(self._chromecasts(), self._dlna(), self._airplay(), return_exceptions=True)
            devs = [d for r in results if isinstance(r, list) for d in r]
            # samme TV kan svare både som Chromecast og DLNA: Cast vinner (bedre styring)
            cast_hosts = {d.host for d in devs if d.kind == "chromecast"}
            devs = [d for d in devs if d.kind == "chromecast" or d.host not in cast_hosts]
            self.devices = {d.id: d for d in devs}
            self.found_at = time.monotonic()
            return devs

    async def _chromecasts(self) -> list[Device]:
        import pychromecast

        def find() -> list[Any]:
            casts, browser = pychromecast.get_chromecasts(timeout=4)
            browser.stop_discovery()
            return casts

        casts = await asyncio.to_thread(find)
        # ref = (vert, port, uuid, modell, navn): koblingen lages ved bruk (get_chromecast_from_host) – objektene fra
        # søket trenger søkets zeroconf, som stoppes her, og ble hengende i wait()
        return [Device(id=f"cast:{c.cast_info.uuid}", name=clean_name(c.cast_info.friendly_name), kind="chromecast",
                       host=c.cast_info.host, model=clean_name(c.cast_info.model_name or ""),
                       ref=(c.cast_info.host, c.cast_info.port, c.cast_info.uuid, c.cast_info.model_name, c.cast_info.friendly_name))
                for c in casts if c.cast_info.cast_type in ("cast", None)]  # ikke rene lydgrupper/høyttalere

    async def _dlna(self) -> list[Device]:
        import httpx

        found = await ssdp_renderers()
        async with httpx.AsyncClient() as client:
            devs = await asyncio.gather(*(dlna_device(client, h, loc) for h, loc in found))
        return [d for d in devs if d is not None]

    async def _airplay(self) -> list[Device]:
        import pyatv
        from pyatv.const import Protocol

        confs = await pyatv.scan(asyncio.get_running_loop(), timeout=3)
        return [Device(id=f"airplay:{clean_name(str(c.identifier))}", name=clean_name(c.name), kind="airplay", host=str(c.address), ref=c)
                for c in confs if c.get_service(Protocol.AirPlay) is not None]

    async def find(self, ref: str) -> Device:
        devs = await self.discover()
        if not devs:
            raise CastError("Fant ingen TV-er på hjemmenettet. Er TV-en på, og på samme nett som PC-en?")
        if not ref:
            if len(devs) == 1:
                return devs[0]
            raise CastError("Flere TV-er: " + ", ".join(d.name for d in devs) + ". Si hvilken.")
        low = ref.lower()
        for d in devs:
            if ref == d.id or d.name.lower() == low:
                return d
        hits = [d for d in devs if low in d.name.lower()]
        if len(hits) == 1:
            return hits[0]
        raise CastError(f"Fant ingen TV som heter «{ref}». Kjente: " + ", ".join(d.name for d in devs))

    # strømmelenker
    def sub_url(self, tok: str, fmt: str) -> str:
        return f"http://{self.host}:{self.port}/cast/{tok}/sub.{fmt}"

    def url(self, video: dict[str, Any], dev: Device, start: float) -> tuple[str, str, str]:
        """(lenke, mime, modus). Nøkkelen gjelder bare denne filmen og går ut etter 6 timer."""
        if not self.host:
            raise CastError("PC-en har ingen adresse på hjemmenettet (bare Tailscale) – TV-en kan ikke hente filmen.")
        now = time.time()
        self.tokens = {k: t for k, t in self.tokens.items() if t["expires"] > now}
        mode, height = plan(dev, video)
        tok = secrets.token_urlsafe(24)
        self.tokens[tok] = {"video": video["id"], "path": video["path"], "mode": mode, "height": height,
                            "hevc": video.get("vcodec") == "hevc", "start": 0.0 if mode == "direct" else start,
                            "expires": now + TOKEN_TTL, "sub": video.get("_sub")}
        self.last_token = tok
        suffix = Path(video["path"]).suffix.lower() if mode == "direct" else ".mp4"
        mime = {".mkv": "video/x-matroska", ".webm": "video/webm", ".mov": "video/quicktime"}.get(suffix, "video/mp4")
        return f"http://{self.host}:{self.port}/cast/{tok}/video{suffix}", mime, mode

    # avspilling
    async def play(self, device_ref: str, video: dict[str, Any], start: float = 0.0, subtitle: str = "auto") -> dict[str, Any]:
        dev = await self.find(device_ref)
        sub = pick_subtitle(video.get("subtitles") or [], subtitle)
        video = {**video, "_sub": sub["idx"] if sub else None}
        if "container" not in video and self.lib is not None:  # lib.video() tar ikke med beholderen
            row = self.lib.db.execute("SELECT container FROM videos WHERE id = ?", (video["id"],)).fetchone()
            video = {**video, "container": row["container"] if row else ""}
        url, mime, mode = self.url(video, dev, start)
        title = video.get("title") or "Film"
        seek = start if mode == "direct" else 0.0  # omkodet strøm starter allerede der
        tok = self.last_token
        subs = {"vtt": self.sub_url(tok, "vtt"), "srt": self.sub_url(tok, "srt"), "lang": lang_tag(sub.get("lang", ""))} if sub else None
        if dev.kind == "chromecast":
            await asyncio.to_thread(self._cc_play, dev, url, mime, title, seek, subs)
        elif dev.kind == "dlna":
            import httpx

            async with httpx.AsyncClient() as client:
                with contextlib.suppress(CastError):
                    await dlna_call(client, dev, "Stop", {})
                await dlna_call(client, dev, "SetAVTransportURI", {"CurrentURI": escape(url),
                                                                   "CurrentURIMetaData": didl(url, title, mime, subs["srt"] if subs else None)})
                await dlna_call(client, dev, "Play", {"Speed": "1"})
                if seek > 1:
                    await asyncio.sleep(2)
                    with contextlib.suppress(CastError):
                        await dlna_call(client, dev, "Seek", {"Unit": "REL_TIME", "Target": _hms(seek)})
        else:
            await self._airplay_play(dev, url, seek)
        self.now = {"device": dev.id, "device_name": dev.name, "video": video["id"], "title": title, "mode": mode,
                    "start": start, "started": time.time(), "paused": False, "subtitle": subtitle,
                    "subtitle_name": (sub.get("title") or sub.get("lang") or "på") if sub else ""}
        return self.status()

    def close(self) -> None:
        """Lukk Chromecast-koblingene (trådene deres holdt ellers prosessen i live ved omstart)."""
        for cc in self._cc_conn.values():
            with contextlib.suppress(Exception):
                cc.disconnect(timeout=3)
        self._cc_conn.clear()

    def _cc(self, dev: Device) -> Any:
        """Koblingen til Chromecasten (gjenbrukes mens den lever)."""
        import pychromecast

        cc = self._cc_conn.get(dev.id)
        if cc is None or not cc.socket_client.is_connected:
            cc = pychromecast.get_chromecast_from_host(dev.ref, tries=2, timeout=10)
            cc.wait(timeout=15)
            self._cc_conn[dev.id] = cc
        return cc

    def _cc_play(self, dev: Device, url: str, mime: str, title: str, seek: float, subs: dict | None = None) -> None:
        cc = self._cc(dev)
        mc = cc.media_controller
        extra = {"subtitles": subs["vtt"], "subtitles_lang": subs["lang"], "subtitles_mime": "text/vtt"} if subs else {}
        mc.play_media(url, mime, title=title, current_time=seek or None, stream_type="BUFFERED", **extra)
        mc.block_until_active(timeout=15)
        if subs:
            with contextlib.suppress(Exception):
                mc.enable_subtitle(1)

    async def _airplay_play(self, dev: Device, url: str, seek: float) -> None:
        import pyatv

        atv = await pyatv.connect(dev.ref, asyncio.get_running_loop())
        try:
            await atv.stream.play_url(url)
        finally:
            atv.close()

    async def control(self, action: str, position: float | None = None) -> dict[str, Any]:
        if not self.now:
            raise CastError("Ingenting spilles på TV-en fra Olympus.")
        dev = self.devices.get(self.now["device"]) or await self.find(self.now["device"])
        if action == "seek":
            if position is None:
                raise CastError("seek trenger position (sekunder).")
            if self.now["mode"] != "direct":  # en omkodet/ompakket strøm kan ikke spoles: start på nytt derfra
                video = self.lib.video(self.now["video"])
                return await self.play(dev.id, video, position, self.now.get("subtitle", "auto"))
        if dev.kind == "chromecast":
            await asyncio.to_thread(self._cc_control, dev, action, position)
        elif dev.kind == "dlna":
            import httpx

            soap = {"pause": ("Pause", {}), "resume": ("Play", {"Speed": "1"}), "stop": ("Stop", {}),
                    "seek": ("Seek", {"Unit": "REL_TIME", "Target": _hms(position or 0)})}[action]
            async with httpx.AsyncClient() as client:
                await dlna_call(client, dev, *soap)
        else:
            raise CastError("AirPlay-TV-en styres med fjernkontrollen etter at filmen har startet.")
        if action == "stop":
            self.now = None
            return {"playing": False}
        self.now["paused"] = action == "pause"
        return self.status()

    def _cc_control(self, dev: Device, action: str, position: float | None) -> None:
        cc = self._cc(dev)
        mc = cc.media_controller
        {"pause": mc.pause, "resume": mc.play, "stop": lambda: cc.quit_app(),
         "seek": lambda: mc.seek(position or 0)}[action]()

    def status(self) -> dict[str, Any]:
        if not self.now:
            return {"playing": False}
        n = dict(self.now)
        dev = self.devices.get(n["device"])
        cc = self._cc_conn.get(n["device"])
        if dev is not None and dev.kind == "chromecast" and cc is not None:
            with contextlib.suppress(Exception):
                s = cc.media_controller.status
                n["position"] = (s.adjusted_current_time or 0) + (n["start"] if n["mode"] != "direct" else 0)
                n["paused"] = s.player_state == "PAUSED"
                if s.player_state in ("IDLE", "UNKNOWN") and time.time() - n["started"] > 20:
                    n["ended"] = True
        return {"playing": True, **n}


def _hms(sec: float) -> str:
    s = int(max(0, sec))
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}"


CASTER = Caster()


# --- strømmeporten på hjemmenettet ------------------------------------------------------------------
def cast_app(stream_transcode: Any, stream_remux: Any = None, subtitle_bytes: Any = None) -> Any:
    """Egen liten web-app på hjemmenettet: bare /cast/<nøkkel>/video.*. stream_transcode(request, path, start, height)
    og stream_remux(request, path, start, hevc) er fra media_api (samme ffmpeg-oppsett som til telefonen)."""
    from aiohttp import web

    async def video(request: web.Request) -> web.StreamResponse:
        t = CASTER.tokens.get(request.match_info["tok"])
        if t is None or t["expires"] < time.time() or not Path(t["path"]).is_file():
            raise web.HTTPNotFound()
        if t["mode"] == "direct":
            headers = {"transferMode.dlna.org": "Streaming", "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_CI=0"}
            if t.get("sub") is not None and request.headers.get("getcaptioninfo.sec"):  # Samsung spør slik
                headers["CaptionInfo.sec"] = f"http://{request.host}/cast/{request.match_info['tok']}/sub.srt"
            return web.FileResponse(t["path"], headers=headers)
        if t["mode"] == "remux" and stream_remux is not None:
            return await stream_remux(request, t["path"], t["start"], t["hevc"])
        return await stream_transcode(request, t["path"], t["start"], t.get("height") or 1080)

    async def subtitle(request: web.Request) -> web.Response:
        t = CASTER.tokens.get(request.match_info["tok"])
        if t is None or t["expires"] < time.time() or t.get("sub") is None or subtitle_bytes is None:
            raise web.HTTPNotFound()
        fmt = "srt" if request.match_info["fmt"] == "srt" else "webvtt"
        out = await subtitle_bytes(CASTER.lib, t["video"], t["sub"], t["start"], fmt)
        if out is None:
            raise web.HTTPNotFound()
        # Chromecast henter undertekster med CORS
        return web.Response(body=out, content_type="text/vtt" if fmt == "webvtt" else "application/x-subrip",
                            charset="utf-8", headers={"Access-Control-Allow-Origin": "*"})

    app = web.Application()
    app.router.add_route("GET", "/cast/{tok}/sub.{fmt}", subtitle)
    app.router.add_route("GET", "/cast/{tok}/{name}", video)
    app.router.add_route("HEAD", "/cast/{tok}/{name}", video)
    return app
