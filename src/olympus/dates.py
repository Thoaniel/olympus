"""Norske datoer slik folk skriver dem: «i morgen 18», «fre 10:30», «12.10 20:00». Brukes av Lab's-skjemaet og
av plan-verktøyet – språkmodellen regner ofte feil på ukedager, så den kan sende brukerens ord rett hit."""

from __future__ import annotations

import datetime as dt
import re

DAYS = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]
MONTHS = ["jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"]


def parse_when(text: str, today: dt.date | None = None) -> str | None:
    """«i morgen 18», «man 20», «fre 10:30», «12.10 20:00», «12.10», «2026-10-12 18:00», «i dag» → ISO."""
    today = today or dt.date.today()
    t = text.strip().lower().replace("kl.", " ").replace(" kl ", " ").replace("hele dagen", "")
    t = re.sub(r"(\d{1,2}\.\d{1,2})\.(?=\s|$)", r"\1", t)  # «12.10. 20» – punktum etter dato
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(på|neste|nå på|til) ", "", t)  # «på fredag», «neste tirsdag» (= førstkommende)
    t = re.sub(r"^(man|tirs|ons|tors|fre|lør|søn)\w* (?=\d{1,2}\.\d{1,2})", "", t)  # «fredag 16.10 11:00»: datoen gjelder
    # «17. mai», «3. mars 2027», «1 januar 18:00» → «17.5», «3.3.2027», «1.1 18:00»
    t = re.sub(r"\b(\d{1,2})\.? ?(" + "|".join(MONTHS) + r")\w*(?: (\d{4}))?",
               lambda m: f"{m.group(1)}.{MONTHS.index(m.group(2)[:3]) + 1}" + (f".{m.group(3)}" if m.group(3) else ""), t)
    day: dt.date | None = None
    rest = t
    if m := re.match(r"(\d{4}-\d{2}-\d{2})(?:[ t](\d{1,2}:\d{2}))?$", t):
        iso = m.group(1) + (f"T{int(m.group(2).split(':')[0]):02}:{m.group(2).split(':')[1]}" if m.group(2) else "")
        try:
            dt.datetime.fromisoformat(iso)  # «2026-13-45» og «25:99» er ikke datoer
        except ValueError:
            return None
        return iso
    if t.startswith("i dag"):
        day, rest = today, t[5:]
    elif t.startswith("i morgen"):
        day, rest = today + dt.timedelta(days=1), t[8:]
    elif m := re.match(r"(man|tir|ons|tor|fre|lør|søn)(dag|sdag|ag)?\b", t):
        delta = (DAYS.index(m.group(1)) - today.weekday()) % 7 or 7
        day, rest = today + dt.timedelta(days=delta), t[m.end():]
    elif m := re.match(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", t):
        year = int(m.group(3)) if m.group(3) else today.year
        year += 2000 if year < 100 else 0
        try:
            day = dt.date(year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        if not m.group(3) and day < today:
            try:
                day = day.replace(year=day.year + 1)
            except ValueError:  # 29.2 og neste år er ikke skuddår
                return None
        rest = t[m.end():]
    if day is None:
        return None
    rest = rest.strip()
    if not rest:
        return day.isoformat()
    time_m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", rest)
    if not time_m or int(time_m.group(1)) > 23 or int(time_m.group(2) or 0) > 59:
        return None
    return f"{day.isoformat()}T{int(time_m.group(1)):02}:{int(time_m.group(2) or 0):02}"
