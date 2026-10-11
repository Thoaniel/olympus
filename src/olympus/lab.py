"""Olympus Lab's: kalenderen, parede enheter og AI-bryteren – det Zeus-appen og Lab's-visningen på PC-en deler.

Alt ligger i én SQLite-fil (~/.olympus/lab.db). Hver endring i planen får et løpenummer i `changes`, så en klient
som har vært borte bare henter det som er nytt (`changes_since`), og så alt kan angres. Telefonen pares med en
engangskode fra `olympus pair` og får sin egen nøkkel; bare et hash av nøkkelen lagres, og enheten kan fjernes.
Appen virker uten AI: planen endres her direkte, Zeus er bare én av dem som kan gjøre det (og må da godkjennes).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PAIR_TTL = 600  # sekunder en paringskode varer

# Dine data – det som må tas vare på og tas backup av. (Mediebiblioteket i media.db er bare en indeks som kan
# bygges på nytt fra filene.)
LAB_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, title TEXT NOT NULL, start TEXT NOT NULL,
        "end" TEXT, all_day INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'du', created REAL, updated REAL, deleted INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS events_start ON events(start);
    CREATE TABLE IF NOT EXISTS changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, who TEXT, action TEXT,
        event_id TEXT, before TEXT, after TEXT);
    CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT, token_hash TEXT UNIQUE, created REAL,
        last_seen REAL, revoked INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS pair_codes (code_hash TEXT PRIMARY KEY, expires REAL);
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """,
    """
    -- Spillelister og framdrift er dine data (ikke en del av indeksen): de flyttes hit fra media.db.
    CREATE TABLE IF NOT EXISTS playlists (id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL, updated REAL);
    CREATE TABLE IF NOT EXISTS playlist_items (playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        pos INTEGER NOT NULL, track_id TEXT NOT NULL, PRIMARY KEY (playlist_id, pos));
    CREATE TABLE IF NOT EXISTS progress (item_id TEXT PRIMARY KEY, position REAL NOT NULL DEFAULT 0,
        duration REAL NOT NULL DEFAULT 0, updated REAL);
    CREATE INDEX IF NOT EXISTS changes_event ON changes(event_id);
    CREATE INDEX IF NOT EXISTS progress_updated ON progress(updated);
    CREATE INDEX IF NOT EXISTS devices_token ON devices(token_hash);
    """,
    """
    -- Nedlastinger (torrent via Transmission eller direkte) til de er sortert inn i biblioteket.
    CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, url TEXT,
        method TEXT, torrent_hash TEXT, folder TEXT, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
        meta TEXT, error TEXT NOT NULL DEFAULT '', added REAL, updated REAL);
    CREATE INDEX IF NOT EXISTS downloads_status ON downloads(status);
    """,
    """
    -- Musikksmaken din: hva du har hørt på (hvor lenge, hoppet over?) og hva du har merket med ♥ eller ✕.
    CREATE TABLE IF NOT EXISTS plays (id INTEGER PRIMARY KEY AUTOINCREMENT, track_id TEXT NOT NULL, ts REAL NOT NULL,
        listened REAL NOT NULL DEFAULT 0, duration REAL NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS plays_track ON plays(track_id);
    CREATE INDEX IF NOT EXISTS plays_ts ON plays(ts);
    CREATE TABLE IF NOT EXISTS likes (item_id TEXT PRIMARY KEY, value INTEGER NOT NULL, ts REAL NOT NULL);
    """,
    """
    -- Det du har rettet på i biblioteket (tittel, artist, år …) for filer som ikke kan ha tagger (mkv, epub, wav):
    -- nøkkelen er filbanen, verdien et JSON-objekt som legges over det skanningen leser.
    CREATE TABLE IF NOT EXISTS media_overrides (path TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL);
    """,
    """
    -- Gjentatte avtaler: hver forekomst er en vanlig avtale (synk, angring og sletting virker som før); series
    -- binder dem sammen så «slett denne og resten» er mulig.
    ALTER TABLE events ADD COLUMN series TEXT NOT NULL DEFAULT '';
    CREATE INDEX IF NOT EXISTS events_series ON events(series);
    """,
    """
    -- batch: endringer gjort i samme slengen (en hel serie lagt til / resten slettet) angres samlet; undone hindrer
    -- at samme slengen angres to ganger. client: telefonens midlertidige id, så et nytt forsøk etter tapt svar ikke
    -- lager avtalen (eller hele serien) en gang til.
    ALTER TABLE changes ADD COLUMN batch TEXT NOT NULL DEFAULT '';
    ALTER TABLE changes ADD COLUMN undone INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE events ADD COLUMN client TEXT NOT NULL DEFAULT '';
    CREATE INDEX IF NOT EXISTS changes_batch ON changes(batch);
    """,
]
REPEATS = {"daily": "hver dag", "weekdays": "hverdager", "weekly": "hver uke", "biweekly": "annenhver uke",
           "monthly": "hver måned"}
SERIES_MAX = 366
FIELDS = ("title", "start", "end", "all_day", "note")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(value: str | None) -> str | None:
    """«2026-10-10 14:00» / «2026-10-10T14:00» / «2026-10-10» → ISO uten sekunder. Ugyldig → ValueError."""
    if value in (None, ""):
        return None
    v = str(value).strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        # «fredag 10», «i morgen 18», «24.12»: tolkes her, så Zeus slipper å regne ut ukedager selv
        from olympus.dates import parse_when

        iso = parse_when(str(value))
        if iso is None:
            raise ValueError(f"Forstår ikke tidspunktet «{str(value)[:40]}».") from None
        return iso
    return dt.strftime("%Y-%m-%d") if len(v) == 10 else dt.strftime("%Y-%m-%dT%H:%M")


def _bool(value: Any) -> bool:
    """JSON-sannhet uten overraskelser: strengen «false» er usann."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "ja", "yes", "on")
    return bool(value)


def _text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} må være tekst.")
    value = value.strip()
    if required and not value:
        raise ValueError("Avtalen trenger en tittel.")
    return value[:limit]


def _check_order(start: str | None, end: str | None) -> None:
    if start and end and end[:len(start)] < start[:len(end)]:
        raise ValueError("Slutten kan ikke være før starten.")


def series_dates(start: str, end: str | None, repeat: str, until: str | None = None,
                 count: int | None = None) -> list[tuple[str, str | None]]:
    """Forekomstene i en gjentatt avtale: [(start, slutt)]. Uten until/count: tre måneder fram (månedlig: ett år).
    Aldri over SERIES_MAX eller to år. Den 31. hoppes over i måneder uten den; «hverdager» starter på første hverdag.
    Brukes både av godkjenningskortet og når serien lages, så kortet viser nøyaktig det som blir laget."""
    if repeat not in REPEATS:
        raise ValueError(f"Ukjent gjentakelse «{repeat}» (bruk {', '.join(REPEATS)}).")
    start_iso, end_iso = _iso(start), _iso(end)
    if start_iso is None:
        raise ValueError("Avtalen trenger et tidspunkt.")
    _check_order(start_iso, end_iso)
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
        raise ValueError("count må være et heltall på minst 1.")
    first = datetime.fromisoformat(start_iso)
    length = datetime.fromisoformat(end_iso) - first if end_iso else None
    stop = first + timedelta(days=365 * 2)
    if until:
        u = _iso(until)
        if u is None:
            raise ValueError("Ugyldig sluttdato.")
        stop = min(stop, datetime.fromisoformat(u[:10]) + timedelta(days=1))
        if stop <= first:
            raise ValueError("Sluttdatoen er før første avtale.")
    elif not count:
        stop = first + timedelta(days=365 if repeat == "monthly" else 91)
    n_max = min(count, SERIES_MAX) if count else SERIES_MAX
    dates: list[datetime] = []
    for i in range(SERIES_MAX * 2):
        if repeat == "monthly":
            y, m = divmod(first.month - 1 + i, 12)
            try:
                d = first.replace(year=first.year + y, month=m + 1)
            except ValueError:
                continue
        else:
            d = first + timedelta(days=i * {"daily": 1, "weekdays": 1, "weekly": 7, "biweekly": 14}[repeat])
        if d >= stop or len(dates) >= n_max:
            break
        if repeat != "weekdays" or d.weekday() < 5:
            dates.append(d)
    if not dates:
        raise ValueError("Ingen avtaler i den perioden (f.eks. hverdager fra lørdag til søndag).")
    fmt = (lambda x: x.strftime("%Y-%m-%d")) if len(start_iso) == 10 else (lambda x: x.isoformat(timespec="minutes"))
    return [(fmt(d), fmt(d + length) if length is not None else None) for d in dates]


class Lab:
    def __init__(self, path: Path) -> None:
        from olympus.db import connect, migrate

        self.path = path
        self.db = connect(path)
        migrate(self.db, LAB_SCHEMA)
        self.listeners: list[Callable[[int], Any]] = []
        self._batch = ""  # settes mens en serie legges til/slettes, så hele slengen kan angres samlet

    # --- innstillinger ----------------------------------------------------
    def setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        self.db.commit()

    @property
    def ai_on(self) -> bool:
        return self.setting("ai", "1") == "1"

    def set_ai(self, on: bool) -> None:
        self.set_setting("ai", "1" if on else "0")

    # --- planen -----------------------------------------------------------
    @staticmethod
    def _event(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"id": row["id"], "title": row["title"], "start": row["start"], "end": row["end"],
                "all_day": bool(row["all_day"]), "note": row["note"], "source": row["source"],
                "updated": row["updated"], "deleted": bool(row["deleted"]),
                "series": row["series"] if "series" in row.keys() else ""}

    def get(self, event_id: str) -> dict[str, Any] | None:
        return self._event(self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())

    def events(self, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        """Avtaler (ikke slettede) som starter i [start, end). Uten grenser: alle."""
        q, args = "SELECT * FROM events WHERE deleted = 0", []
        if start:
            q += " AND start >= ?"
            args.append(_iso(start))
        if end:
            q += " AND start < ?"
            args.append(_iso(end))
        return [self._event(r) for r in self.db.execute(q + " ORDER BY start", args)]  # type: ignore[misc]

    def day(self, date: str) -> list[dict[str, Any]]:
        d = datetime.fromisoformat(date[:10])
        return self.events(d.strftime("%Y-%m-%d"), (d + timedelta(days=1)).strftime("%Y-%m-%d"))

    def _log(self, who: str, action: str, event_id: str, before: dict | None, after: dict | None) -> int:
        cur = self.db.execute("INSERT INTO changes (ts, who, action, event_id, before, after, batch) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (time.time(), who, action, event_id, json.dumps(before), json.dumps(after), self._batch))
        self.db.commit()
        seq = int(cur.lastrowid or 0)
        for fn in list(self.listeners):
            fn(seq)
        return seq

    def add(self, title: str, start: str, end: str | None = None, all_day: bool = False, note: str = "",
            who: str = "du", client: str = "") -> dict[str, Any]:
        if (old := self.by_client(client)):
            return old[0]
        title = _text(title, "Tittelen", 200, required=True)
        note = _text(note or "", "Notatet", 2000)
        start_iso = _iso(start)
        if start_iso is None:
            raise ValueError("Avtalen trenger et tidspunkt.")
        _check_order(start_iso, _iso(end))
        all_day = _bool(all_day)
        eid, now = secrets.token_hex(6), time.time()
        self.db.execute('INSERT INTO events (id, title, start, "end", all_day, note, source, created, updated) '
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (eid, title[:200], start_iso, _iso(end), int(all_day or len(start_iso) == 10), note[:2000],
                         "zeus" if who == "zeus" else "du", now, now))
        if client:
            self.db.execute("UPDATE events SET client = ? WHERE id = ?", (client[:64], eid))
        ev = self.get(eid)
        self._log(who, "add", eid, None, ev)
        return ev  # type: ignore[return-value]

    def by_client(self, client: str) -> list[dict[str, Any]]:
        """Avtalene som ble laget fra telefonens midlertidige id (et nytt forsøk etter tapt svar)."""
        if not client:
            return []
        row = self.db.execute("SELECT * FROM events WHERE client = ? ORDER BY created LIMIT 1", (client,)).fetchone()
        ev = self._event(row)
        if ev is None:
            return []
        if not ev["series"]:
            return [ev]
        return [self._event(r) for r in self.db.execute(  # type: ignore[misc]
            "SELECT * FROM events WHERE series = ? ORDER BY start", (ev["series"],))]

    def add_series(self, title: str, start: str, end: str | None = None, all_day: bool = False, note: str = "",
                   repeat: str = "weekly", until: str | None = None, count: int | None = None,
                   who: str = "du", client: str = "") -> list[dict[str, Any]]:
        """Gjentatt avtale: én vanlig avtale per forekomst med felles series-id (se series_dates)."""
        if (old := self.by_client(client)):
            return old
        times = series_dates(start, end, repeat, until, count)
        title = _text(title, "Tittelen", 200, required=True)
        sid = "s" + secrets.token_hex(5)
        self._batch = "b" + secrets.token_hex(5)
        out = []
        try:
            for st, en in times:
                ev = self.add(title, st, en, all_day, note, who=who)
                self.db.execute("UPDATE events SET series = ?, client = ? WHERE id = ?",
                                (sid, client if not out else "", ev["id"]))
                self.db.commit()
                out.append({**ev, "series": sid})
        finally:
            self._batch = ""
        return out

    def delete_series(self, event_id: str, who: str = "du") -> list[dict[str, Any]]:
        """Slett denne avtalen og resten av serien den hører til (de som kommer etter). Angres samlet."""
        ev = self.get(event_id)
        if ev is None or ev["deleted"]:
            raise KeyError(event_id)
        if not ev["series"]:
            return [self.delete(event_id, who=who)]
        rows = self.db.execute("SELECT id FROM events WHERE series = ? AND deleted = 0 AND start >= ? ORDER BY start",
                               (ev["series"], ev["start"])).fetchall()
        self._batch = "b" + secrets.token_hex(5)
        try:
            return [self.delete(r["id"], who=who) for r in rows]
        finally:
            self._batch = ""

    def update(self, event_id: str, who: str = "du", **changes: Any) -> dict[str, Any]:
        before = self.get(event_id)
        if before is None or before["deleted"]:
            raise KeyError(event_id)
        vals = {k: v for k, v in changes.items() if k in FIELDS and v is not None}
        if "start" in vals:
            vals["start"] = _iso(vals["start"])
        if "end" in vals:
            vals["end"] = _iso(vals["end"])
        if "all_day" in vals:
            vals["all_day"] = int(_bool(vals["all_day"]))
        if "title" in vals:
            vals["title"] = _text(vals["title"], "Tittelen", 200, required=True)
        if "note" in vals:
            vals["note"] = _text(vals["note"], "Notatet", 2000)
        if "start" in vals and vals["start"] is None:
            raise ValueError("Avtalen trenger et tidspunkt.")
        _check_order(vals.get("start", before["start"]), vals.get("end", before["end"]))
        if not vals:
            return before
        sets = ", ".join(f'"{k}" = ?' for k in vals)
        self.db.execute(f"UPDATE events SET {sets}, updated = ? WHERE id = ?", (*vals.values(), time.time(), event_id))
        after = self.get(event_id)
        self._log(who, "update", event_id, before, after)
        return after  # type: ignore[return-value]

    def delete(self, event_id: str, who: str = "du") -> dict[str, Any]:
        before = self.get(event_id)
        if before is None or before["deleted"]:
            raise KeyError(event_id)
        self.db.execute("UPDATE events SET deleted = 1, updated = ? WHERE id = ?", (time.time(), event_id))
        after = self.get(event_id)
        self._log(who, "delete", event_id, before, after)
        return after  # type: ignore[return-value]

    def undo(self, seq: int, who: str = "du") -> dict[str, Any] | None:
        """Sett avtalen tilbake slik den var før endring nr. `seq`. Hørte endringen til en slengen (en hel serie lagt
        til, eller resten slettet), angres hele slengen – én gang. Angringen er selv en slengen, så «angre» igjen
        setter alt tilbake."""
        row = self.db.execute("SELECT * FROM changes WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            raise KeyError(seq)
        if not row["batch"]:
            return self._undo_one(row, who)
        rows = self.db.execute("SELECT * FROM changes WHERE batch = ? AND undone = 0 ORDER BY seq DESC",
                               (row["batch"],)).fetchall()
        if not rows:
            return self.get(row["event_id"])
        self._batch = "b" + secrets.token_hex(5)
        out = None
        try:
            for r in rows:
                out = self._undo_one(r, who)
            self.db.execute("UPDATE changes SET undone = 1 WHERE batch = ?", (row["batch"],))
            self.db.commit()
        finally:
            self._batch = ""
        return out

    def _undo_one(self, row: sqlite3.Row, who: str) -> dict[str, Any] | None:
        before, eid = json.loads(row["before"]), row["event_id"]
        now = time.time()
        if before is None:  # angre en ny avtale = slett den
            self.db.execute("UPDATE events SET deleted = 1, updated = ? WHERE id = ?", (now, eid))
        else:
            # «deleted» fra før-tilstanden: å angre en angring av en sletting skal slette igjen
            self.db.execute('UPDATE events SET title = ?, start = ?, "end" = ?, all_day = ?, note = ?, deleted = ?, '
                            "updated = ? WHERE id = ?", (before["title"], before["start"], before["end"],
                                                         int(before["all_day"]), before["note"],
                                                         int(bool(before.get("deleted"))), now, eid))
        after = self.get(eid)
        self._log(who, "undo", eid, json.loads(row["after"]), after)
        return after

    def changes_since(self, seq: int) -> dict[str, Any]:
        """Det en klient trenger for å ta igjen: avtaler endret etter `seq` (også slettede) og siste løpenummer."""
        rows = self.db.execute("SELECT DISTINCT event_id FROM changes WHERE seq > ?", (seq,)).fetchall()
        last = self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0]
        return {"seq": int(last), "events": [e for r in rows if (e := self.get(r["event_id"]))]}

    def log(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM changes ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "who": r["who"], "action": r["action"], "event_id": r["event_id"],
                 "title": (json.loads(r["after"] or "null") or json.loads(r["before"] or "null") or {}).get("title", "")}
                for r in rows]

    # --- paring og enheter ------------------------------------------------
    def new_pair_code(self) -> str:
        code = secrets.token_urlsafe(9)
        now = time.time()
        self.db.execute("DELETE FROM pair_codes WHERE expires < ?", (now,))
        self.db.execute("INSERT INTO pair_codes VALUES (?, ?)", (_hash(code), now + PAIR_TTL))
        self.db.commit()
        return code

    def pair(self, code: str, name: str) -> tuple[str, str] | None:
        """Bytt en gyldig engangskode mot (enhets-id, nøkkel). Koden kan bare brukes én gang."""
        h = _hash(code or "")
        row = self.db.execute("SELECT expires FROM pair_codes WHERE code_hash = ?", (h,)).fetchone()
        self.db.execute("DELETE FROM pair_codes WHERE code_hash = ?", (h,))
        self.db.commit()
        if row is None or row["expires"] < time.time():
            return None
        device_id, token = secrets.token_hex(6), secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute("INSERT INTO devices VALUES (?, ?, ?, ?, ?, 0)", (device_id, (name or "telefon")[:60],
                                                                       _hash(token), now, now))
        self.db.commit()
        return device_id, token

    def device_for(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        row = self.db.execute("SELECT * FROM devices WHERE token_hash = ? AND revoked = 0", (_hash(token),)).fetchone()
        if row is None:
            return None
        if time.time() - (row["last_seen"] or 0) > 60:
            self.db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time(), row["id"]))
            self.db.commit()
        return {"id": row["id"], "name": row["name"]}

    def devices(self) -> list[dict[str, Any]]:
        return [{"id": r["id"], "name": r["name"], "created": r["created"], "last_seen": r["last_seen"]}
                for r in self.db.execute("SELECT * FROM devices WHERE revoked = 0 ORDER BY created")]

    def revoke(self, device_id: str) -> bool:
        cur = self.db.execute("UPDATE devices SET revoked = 1 WHERE id = ? AND revoked = 0", (device_id,))
        self.db.commit()
        return cur.rowcount > 0


def open_lab(home: Path) -> Lab:
    return Lab(home / "lab.db")


def describe(ev: dict[str, Any]) -> str:
    """Én linje for Zeus og godkjenningskort: «lør 10.10 14:00 · Trening»."""
    days = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]
    try:
        d = datetime.fromisoformat(ev["start"])
        when = f"{days[d.weekday()]} {d.day}.{d.month}" + ("" if ev.get("all_day") else f" {d:%H:%M}")
    except (ValueError, KeyError):
        when = str(ev.get("start", "?"))
    return f"{when} · {ev.get('title', '')}"


def to_ics(events: list[dict[str, Any]], now: datetime | None = None) -> str:
    """Planen som iCalendar (.ics) til andre kalendere (Google, Apple, Thunderbird). Tidene er norsk lokaltid
    (TZID=Europe/Oslo); heldagsavtaler som DATE. Hver avtale har fast UID, så en ny import oppdaterer i stedet for
    å lage dubletter."""
    def esc(t: str) -> str:
        # \r og andre kontrolltegn ut (ellers kunne en tittel lage egne linjer i fila), linjeskift som \n
        t = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", t.replace("\r\n", "\n").replace("\r", "\n"))
        return t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    def fold(line: str) -> str:  # maks 75 oktetter per linje (RFC 5545)
        out, cur = [], ""
        for ch in line:
            if len((cur + ch).encode()) > 74:
                out.append(cur)
                cur = " " + ch
            else:
                cur += ch
        return "\r\n".join([*out, cur])

    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Olympus//Zeus plan//NO", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Zeus – planen", "X-WR-TIMEZONE:Europe/Oslo"]
    for e in events:
        start = e["start"]
        all_day = e.get("all_day") or len(start) == 10
        lines += ["BEGIN:VEVENT", f"UID:{e['id']}@olympus", f"DTSTAMP:{stamp}", f"SUMMARY:{esc(e['title'])}"]
        if all_day:
            d = datetime.fromisoformat(start[:10])
            lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
        else:
            s0 = datetime.fromisoformat(start)
            s1 = datetime.fromisoformat(e["end"]) if e.get("end") and len(e["end"]) > 10 else s0 + timedelta(hours=1)
            lines += [f"DTSTART;TZID=Europe/Oslo:{s0:%Y%m%dT%H%M%S}", f"DTEND;TZID=Europe/Oslo:{s1:%Y%m%dT%H%M%S}"]
        if e.get("note"):
            lines.append(f"DESCRIPTION:{esc(e['note'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in lines) + "\r\n"
