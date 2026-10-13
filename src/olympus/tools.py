"""Verktøy agentene kan bruke, med godkjenning.

Regler (fra specen):
- Alt som endrer systemet krever ja fra brukeren: filskriving, `rm`, `sudo`, pacman/AUR …
- `sudo` og andre farlige kommandoer spør hver gang; de kan ikke godkjennes for hele økten.
- Rene lesekommandoer (ls, cat, git status, journalctl, pacman -Q …) kjøres uten spørsmål.
- Skymodeller må få ja før de leser filer utenfor arbeidsmappen (innholdet sendes ut).
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_OUTPUT = 12_000  # tegn som sendes tilbake til modellen


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # rå JSON fra modellen

    @property
    def args(self) -> dict[str, Any]:
        try:
            data = json.loads(self.arguments or "{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}


@dataclass
class Approval:
    """Hva brukeren må ta stilling til før et kall kjøres."""

    needed: bool
    reason: str = ""
    danger: bool = False  # farlig: spør hver gang, rød visning
    preview: str = ""  # diff, kommando eller sti
    preview_lang: str = "text"
    reject: str = ""  # ikke-tom: kallet avvises med denne teksten uten å kjøres (fant ikke, ugyldig tid)
    bind: dict[str, Any] = field(default_factory=dict)  # argumenter som låses til det brukeren så (id-en på kortet)


def _when(value: Any) -> str:
    """«fredag 10» → «fre 16.10 10:00» (som plan-verktøyet vil tolke det); ellers uendret."""
    from olympus.dates import parse_when
    from olympus.lab import describe

    iso = parse_when(str(value)) if isinstance(value, str) else None
    if not iso:
        return str(value)
    return describe({"title": "", "start": iso, "end": None, "all_day": len(iso) == 10}).rstrip(" ·")


@dataclass
class ToolResult:
    ok: bool
    output: str
    summary: str
    denied: bool = False
    undo: int | None = None  # endringen i planen (changes.seq) som «↶ angre» angrer


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    writes: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Valgfrie felt godtar null: modeller sender ofte `null` for felt de ikke bruker,
    og providers som validerer strengt (Groq) avviser da hele kallet."""
    out = {}
    for name, spec in props.items():
        if name not in required and isinstance(spec.get("type"), str):
            spec = {**spec, "type": [spec["type"], "null"]}
        out[name] = spec
    return {"type": "object", "properties": out, "required": required}


SPECS: dict[str, ToolSpec] = {
    s.name: s
    for s in [
        ToolSpec(
            "read_file",
            "Les en tekstfil. Relative stier er relative til arbeidsmappen. Bruk offset/limit (linjenummer) for store filer.",
            _obj(
                {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "første linje (1-basert)"},
                    "limit": {"type": "integer", "description": "maks antall linjer"},
                },
                ["path"],
            ),
        ),
        ToolSpec(
            "list_dir",
            "List filer og mapper i en mappe (ikke rekursivt).",
            _obj({"path": {"type": "string", "description": "standard: arbeidsmappen"}}, []),
        ),
        ToolSpec(
            "search_files",
            "Søk etter tekst (regulært uttrykk) i filer under en mappe. Returnerer fil:linje:innhold.",
            _obj({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
        ),
        ToolSpec(
            "media_search",
            "Søk i brukerens eget bibliotek i Olympus: musikk (sanger, album), film og serier, lydbøker og e-bøker. "
            "Svaret har id-er i [klammer] som kan brukes i playlist, pc (play) og media (info, edit, delete …). "
            "Tomt query gir en oversikt over hele biblioteket (antall, album, filmer, bøker).",
            _obj({"query": {"type": "string"},
                  "kind": {"type": "string", "enum": ["all", "music", "video", "book"], "description": "standard all"}},
                 ["query"]),
        ),
        ToolSpec(
            "playlist",
            "Spillelister i biblioteket (de samme som i Zeus-appen). action=list, show (id), create (name + query "
            "eller tracks=[id…]), add (id + query/tracks), delete (id). Endringer må brukeren godkjenne.",
            _obj({"action": {"type": "string", "enum": ["list", "show", "create", "add", "delete"]},
                  "id": {"type": "string"}, "name": {"type": "string"}, "query": {"type": "string"},
                  "tracks": {"type": "array", "items": {"type": "string"}}}, ["action"]),
            writes=True,
        ),
        ToolSpec(
            "audio",
            "Lyden på PC-en: action=status (utganger, volum, Bluetooth), volume (percent 0–100), mute, unmute, "
            "output (sink-navn fra status), connect/disconnect (mac fra status – Bluetooth-høyttaleren). "
            "Endringer må brukeren godkjenne.",
            _obj({"action": {"type": "string", "enum": ["status", "volume", "mute", "unmute", "output", "connect", "disconnect"]},
                  "percent": {"type": "integer"}, "sink": {"type": "string"}, "mac": {"type": "string"}}, ["action"]),
            writes=True,
        ),
        ToolSpec(
            "media_find",
            "Finn musikk, filmer, serier, lydbøker og e-bøker å laste ned fra kildene i olympus/sources (Internet "
            "Archive, LibriVox, Project Gutenberg, Wikimedia Commons …). kind = music | film | show | audiobook | ebook. "
            "Svaret har id-er i [klammer] til media_get.",
            _obj({"query": {"type": "string"}, "kind": {"type": "string", "enum": ["music", "film", "show", "audiobook", "ebook"]}},
                 ["query"]),
        ),
        ToolSpec(
            "media_get",
            "Last ned et treff fra media_find (id). Torrents går via Transmission; alt sorteres inn i biblioteket med "
            "cover når det er ferdig. Brukeren godkjenner hver nedlasting. (En lenke brukeren har gitt: media add.)",
            _obj({"id": {"type": "string"}}, ["id"]),
            writes=True,
        ),
        ToolSpec(
            "dato",
            "Regn med datoer (aldri regn selv – modellen tar ofte feil): action=dager (antall dager fra «fra» til «til»; "
            "standard fra = i dag), ukedag (hvilken ukedag «dato» er), pluss (datoen «dager» etter «dato»). Datoer kan "
            "være ISO eller norsk: «i morgen», «fredag», «24.12», «12.10.2027», «julaften», «nyttårsaften».",
            _obj({"action": {"type": "string", "enum": ["dager", "ukedag", "pluss"]}, "fra": {"type": "string"},
                  "til": {"type": "string"}, "dato": {"type": "string"}, "dager": {"type": "integer"}}, ["action"]),
        ),
        ToolSpec(
            "plan",
            "Brukerens kalender (planen i Zeus-appen og Lab's på PC-en). action=list (from/to, ISO-dato, to er til og "
            "med; standard: i dag og 7 dager fram), add (title, start, valgfritt end, note). start kan være brukerens egne ord – «fredag 10», «i morgen 18», «tir 12:30», «24.12» (hele dagen), «12.10 20:00» – eller ISO «2026-10-14T10:00»; send helst ordene, så regnes ukedagen ut riktig, "
            "Gjentatt avtale: add med repeat (daily, weekdays, weekly, biweekly, monthly) og valgfritt until (siste dato) "
            "eller count – ALDRI legg inn hver forekomst for seg. "
            "move (id, start, valgfritt end), edit (id + felt), delete (id; series=true sletter også resten av serien). "
            "I stedet for id kan du sende match = "
            "avtalens tittel (f.eks. «tannlege»). Alt som endrer, må brukeren godkjenne. Si aldri at en avtale ikke "
            "finnes uten å ha sjekket med list.",
            _obj({"action": {"type": "string", "enum": ["list", "add", "move", "edit", "delete"]},
                  "id": {"type": "string"}, "match": {"type": "string"}, "title": {"type": "string"},
                  "start": {"type": "string"}, "end": {"type": "string"}, "note": {"type": "string"},
                  "from": {"type": "string"}, "to": {"type": "string"},
                  "repeat": {"type": "string", "enum": ["daily", "weekdays", "weekly", "biweekly", "monthly"]},
                  "until": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 366},
                  "series": {"type": "boolean"}}, ["action"]),
            writes=True,
        ),
        ToolSpec(
            "memory",
            "Varig minne på tvers av økter (som et notatark som alltid er med deg). target=user: fakta om brukeren "
            "(preferanser, stil, faste opplysninger). target=memory: din egen lærdom (miljø, konvensjoner, hva som "
            "virket). action=add (content), replace (old_text som treffer én oppføring + hele nye content) eller remove "
            "(old_text eller id). list viser oppføringene med id (det samme brukeren ser i appen). Lagre bare det som vil være "
            "nyttig i senere økter – ikke midlertidige detaljer. Blir det fullt: slå sammen eller fjern før du legger til.",
            _obj({"action": {"type": "string", "enum": ["add", "replace", "remove", "list"]},
                  "target": {"type": "string", "enum": ["memory", "user"]},
                  "content": {"type": "string"}, "old_text": {"type": "string"},
                  "id": {"type": "string", "description": "id fra list (i stedet for old_text)"}}, ["action"]),
        ),
        ToolSpec(
            "session_search",
            "Søk i alle tidligere samtaler (fulltekst). Bruk når brukeren viser til noe fra før («som vi snakket om», "
            "«forrige gang») eller du trenger detaljer som ikke er i denne samtalen. Returnerer økt, hvem og utdrag. "
            "action=list (uten query): de siste samtalene.",
            _obj({"query": {"type": "string"}, "action": {"type": "string", "enum": ["search", "list"]}}, []),
        ),
        ToolSpec(
            "library_search",
            "Søk i Olympus' lokale bibliotek. Kilder: arch-wiki (engelsk), hyprland-wiki (engelsk, Lua-config), "
            "lovdata-lover og lovdata-forskrifter (gjeldende norske lover/forskrifter), wikipedia-nb og "
            "wikipedia-nn (norsk Wikipedia), docs (brukerens egne). Velg source når du vet hvor svaret står. "
            "Returnerer treff med id, artikkel › seksjon og utdrag.",
            _obj({"query": {"type": "string"}, "source": {"type": "string", "description": "valgfritt: én av kildene over"}}, ["query"]),
        ),
        ToolSpec(
            "library_read",
            "Les fra biblioteket: en seksjon (id fra library_search) eller en hel artikkel (tittel, f.eks. «Pacman»). "
            "section= begrenser til en seksjon i artikkelen.",
            _obj({"ref": {"type": "string", "description": "id eller artikkeltittel"}, "section": {"type": "string"},
                  "source": {"type": "string", "description": "valgfritt: kilde, hvis tittelen finnes i flere"}}, ["ref"]),
        ),
        ToolSpec(
            "snl_search",
            "Søk i Store norske leksikon (snl.no) på nettet. Bare søkeordene sendes; ingenting lagres. "
            "Bruk for norske faktaspørsmål (geografi, historie, samfunn, biografier …).",
            _obj({"query": {"type": "string"}}, ["query"]),
        ),
        ToolSpec(
            "snl_read",
            "Les en artikkel fra Store norske leksikon (permalink fra snl_search, f.eks. «Galdhøpiggen»).",
            _obj({"permalink": {"type": "string"}}, ["permalink"]),
        ),
        ToolSpec(
            "write_file",
            "Skriv en hel fil (oppretter eller overskriver). Brukeren må godkjenne.",
            _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
            writes=True,
        ),
        ToolSpec(
            "edit_file",
            "Erstatt nøyaktig tekst i en fil. old_text må finnes nøyaktig én gang. Brukeren må godkjenne.",
            _obj(
                {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
                ["path", "old_text", "new_text"],
            ),
            writes=True,
        ),
        ToolSpec(
            "shell",
            "Kjør en shell-kommando i arbeidsmappen (bash). Lesekommandoer kjøres direkte; alt som endrer "
            "systemet må brukeren godkjenne. Bruk ikke interaktive programmer.",
            _obj({"command": {"type": "string"}}, ["command"]),
            writes=True,
        ),
    ]
}

# Kommandoer som bare leser. Første ord (eventuelt med underkommando) må stå her.
# Bare kommandoer som ikke kan endre noe uansett argumenter (eller som sjekkes nærmere under).
READONLY = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "stat", "which", "whereis", "type",
    "uname", "whoami", "id", "pwd", "uptime", "df", "du", "free", "lsblk", "lscpu", "lspci", "lsusb",
    "lsmod", "printenv", "echo", "tree", "diff", "sort", "cut", "realpath", "basename",
    "dirname", "journalctl", "ss", "ps", "pgrep", "nproc", "locale", "vulkaninfo", "rocminfo",
    "glxinfo", "sensors", "true", "man", "apropos", "whatis",
}
READONLY_SUB = {
    # diff/show/blame kan kjøre textconv/diff.external fra repoets config: de krever ja.
    "git": {"status", "log", "rev-parse", "ls-files", "describe"},
    "systemctl": {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files", "show", "cat"},
    "pacman": {"-Q", "-Qi", "-Ql", "-Qo", "-Qs", "-Qm", "-Qe", "-Qdt", "-Qdtq", "-Qk", "-Si", "-Ss", "-Sl", "-F", "-Fl", "-Fx"},
    "paru": {"-Q", "-Qi", "-Qm", "-Si", "-Ss", "-Qua"},
    "hyprctl": {"version", "clients", "monitors", "workspaces", "activewindow", "activeworkspace", "devices", "getoption", "layers", "binds", "systeminfo"},
    "uv": {"--version"},
    "python": {"--version"},
    "python3": {"--version"},
    "docker": {"ps", "images", "version"},  # ikke inspect/info: viser miljøvariabler (passord) og oppsett
    "tailscale": {"status", "ip", "version"},
}
# Argumenter som gjør en ellers lesende kommando skrivende eller kjørende.
WRITE_ARGS = re.compile(r"^(-delete|-exec|-execdir|-ok|-okdir|-fprint\w*|-fls|-o|--output(=.*)?|--output-file(=.*)?)$")
# Kommandoer (basenavn) som alltid er farlige, også bak /usr/bin/, command, env, xargs …
DANGER_CMDS = {
    "sudo", "doas", "pkexec", "su", "rm", "rmdir", "dd", "shred", "wipefs", "fdisk", "parted", "sfdisk",
    "chmod", "chown", "chattr", "reboot", "shutdown", "poweroff", "halt", "kill", "killall", "pkill",
    "yay", "mkfs", "mkswap", "mount", "umount", "crontab", "eval", "truncate", "mv",
}
WRAPPERS = {"command", "env", "exec", "nice", "nohup", "time", "xargs", "timeout", "stdbuf", "builtin", "unbuffer"}
SHELLS = {"bash", "sh", "zsh", "fish", "dash", "python", "python3", "perl", "ruby", "node"}
DANGER = re.compile(
    r"pacman\s+-[SRUD]|paru\s+-[SRU]|makepkg\s+-i"
    r"|systemctl\s+(--user\s+)?(start|stop|restart|enable|disable|mask|daemon-reload|kill)"
    r"|git\s+(push|reset\s+--hard|clean|checkout\s+--|rebase|filter-branch|branch\s+-[dD]|tag\s+-d)"
    r"|curl[^|]*\|\s*\w*sh|wget[^|]*\|\s*\w*sh"
    r"|>\s*/(etc|usr|boot|var|sys|dev)/|mkfs"
)
# Skall-syntaks som kan skjule kommandoer: linjeskift, bakgrunn (&), substitusjon, omdirigering.
HIDDEN = re.compile(r"[\n\r`]|\$\(|<\(|>\(|(?<![&|])&(?![&>])")


def _segments(command: str) -> list[list[str]] | None:
    """Del kommandoen i ledd og ord. None hvis den ikke kan tolkes trygt."""
    if HIDDEN.search(command):
        return None
    # Del på | || && ; bare utenfor anførselstegn: `grep -E 'a|b'` er én lesende kommando, ikke to.
    lex = shlex.shlex(command, posix=True, punctuation_chars="|&;")
    lex.whitespace_split = True
    lex.commenters = ""  # som bash midt i ord: «a#b; rm x» er to kommandoer – aldri skjul resten som «kommentar»
    out: list[list[str]] = [[]]
    try:
        for tok in lex:
            if tok in ("|", "||", "&&", ";"):
                out.append([])
            elif set(tok) <= set("|&;"):
                return None  # f.eks. «|&» eller «;;»: ikke tolkbart trygt
            else:
                out[-1].append(tok)
    except ValueError:
        return None
    return [w for w in out if w]


def _real_cmd(words: list[str]) -> tuple[str, list[str]]:
    """Kommandoen som faktisk kjøres (forbi env, command, nice …), og argumentene."""
    i = 0
    while i < len(words):
        w = os.path.basename(words[i])
        if "=" in words[i] and not words[i].startswith("-") and i == 0:
            i += 1  # VAR=verdi foran kommandoen
            continue
        if w in WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            continue
        return w, words[i + 1:]
    return "", []


def is_dangerous(command: str) -> bool:
    if DANGER.search(command):
        return True
    segs = _segments(command)
    if segs is None:
        return True  # skjult syntaks: behandles som farlig
    for words in segs:
        if os.path.basename(words[0]) in WRAPPERS and any(os.path.basename(w) in DANGER_CMDS for w in words):
            return True  # nice -n 5 rm …, timeout 10 rm …: ikke prøv å tolke flaggene, vær forsiktig
        cmd, args = _real_cmd(words)
        if cmd in DANGER_CMDS or cmd.startswith("mkfs"):
            return True
        if cmd in SHELLS and any(a in ("-c", "-e") for a in args):
            return True  # bash -c "…" kan skjule hva som helst
    return False


# Lesekommandoer som har flagg som kjører andre programmer. GNU godtar forkortede
# langflagg (--comp = --compress-program), så det sjekkes på prefiks.
EXEC_FLAGS = {
    "rg": ("--pre", "-z", "--search-z"),                # --pre=<program>, --pre-glob, dekomprimering
    "sort": ("--com", "-T"),                            # --compress-program=<program>
    "man": ("-P", "-H", "--pa", "--ht", "-R", "--re"),  # pager, nettleser, rekoding
    "less": ("",),
}


GIT_LOG_OK = re.compile(r"^(--oneline|--graph|--decorate|--all|--reverse|--no-merges|-\d+|-n\d*|\d+|"
                        r"--(since|until|after|before|author|grep|format|pretty|max-count)(=.*)?)$")
JOURNAL_BAD = ("--v", "--ro", "--fl", "--sy", "--rel", "--se", "--up", "--cat", "--fi")  # vacuum, rotate, flush …


def _exec_flag(cmd: str, args: list[str]) -> bool:
    if cmd == "journalctl" and any(a.startswith(JOURNAL_BAD) for a in args):
        return True
    for a in args:
        # Forkortede langflagg (--out=, --outp …) og -o med verdi rett etter (-ofil, -nofil).
        # --o, --ou, --out … --output (med eller uten =verdi), men ikke --oneline o.l.
        if re.match(r"^--o(u(t(p(u(t(-file)?)?)?)?)?)?(=|$)", a) or (
            cmd in ("sort", "tree", "uniq", "diff") and re.match(r"^-[A-Za-z]*o", a)
        ):
            return True
    prefixes = EXEC_FLAGS.get(cmd)
    if prefixes is None:
        return False
    if cmd == "man":
        return any(a.startswith("-") for a in args)  # man: bare sidenavn er lesing
    return any(a.startswith(prefixes) for a in args if a.startswith("-"))


# Ingen skall-syntaks i det som kjøres uten spørsmål: da kan kommandoen kjøres som en
# argumentliste uten bash, og det som sjekkes er nøyaktig det som kjøres.
SHELL_CHARS = re.compile(r"""[|;&<>$`(){}*?\[\]~\\!#\n\r"']""")


def simple_argv(command: str) -> list[str] | None:
    """Argumentlisten for én enkel kommando uten skall-syntaks, ellers None."""
    if not command.strip() or SHELL_CHARS.search(command):
        return None
    words = command.split()
    return words or None


def _is_internal_host(host: str) -> bool:
    """Sant hvis vertsnavnet er (eller slås opp til) en lokal, privat, Tailscale- eller link-local-adresse."""
    from olympus.netsafe import is_internal_host

    return is_internal_host(host)


def is_readonly(command: str) -> bool:
    """Sant hvis kommandoen er én enkel lesekommando (uten skall-syntaks, rør eller
    argumenter som skriver eller kjører andre programmer)."""
    argv = simple_argv(command)
    if argv is None:
        return False
    for words in [argv]:
        cmd = os.path.basename(words[0])
        if "=" in words[0] or cmd in WRAPPERS:
            return False
        if any(WRITE_ARGS.match(w) for w in words[1:]) or _exec_flag(cmd, words[1:]):
            return False
        if cmd in READONLY:
            continue
        subs = READONLY_SUB.get(cmd)
        if subs is None or len(words) < 2 or words[1] not in subs:
            return False
        if cmd == "git" and words[1] == "log" and not all(GIT_LOG_OK.match(a) for a in words[2:]):
            return False  # -p/--stat o.l. bruker diff-maskineriet (textconv)
    return True


SENSITIVE = re.compile(
    r"(^|/)(\.ssh|\.gnupg|\.aws|\.kube|\.docker|\.password-store|\.netrc|\.pgpass|\.git-credentials|"
    r"\.claude\.json|\.claude/\.credentials|auth\.json|keyrings?|\.mozilla|\.config/(chromium|google-chrome|BraveSoftware)|"
    r"[\w.-]*\.env(\.[\w-]+)?$|id_[a-z0-9]+|[\w.-]*\.(pem|key|p12|pfx|kdbx)$|secrets?|credentials?|tokens?)(/|$)",
    re.I,
)


def is_sensitive(path: Path) -> bool:
    """Filer som aldri skal sendes til skyen uten at brukeren har sagt ja."""
    return bool(SENSITIVE.search(str(path)))


def mentions_sensitive(command: str, cwd: Path) -> bool:
    if SENSITIVE.search(command):
        return True
    for words in _segments(command) or []:
        for w in words[1:]:
            p = Path(os.path.expanduser(w.split("=", 1)[-1]))
            p = p if p.is_absolute() else cwd / p
            if (p.exists() or p.is_symlink()) and is_sensitive(Path(os.path.realpath(p))):
                return True
    return False


RECURSIVE = re.compile(r"(^|[\s;&|])(grep\s+(-\w*[rR]\w*|--recursive|-d\s*recurse)|rg|find|tree|du|ls\s+-\w*R)(\s|$)")


def is_recursive(command: str) -> bool:
    """Søk/lister som går gjennom hele kataloger (og dermed kan treffe hemmeligheter)."""
    return bool(RECURSIVE.search(command))


def drop_sensitive_lines(output: str, cwd: Path) -> tuple[str, int]:
    """Fjern søketreff (fil:linje:…) i filer som ser ut som hemmeligheter."""
    kept, dropped = [], 0
    for line in output.splitlines():
        path = line.split(":", 1)[0]
        if path and is_sensitive(Path(os.path.realpath(cwd / path))):
            dropped += 1
            continue
        kept.append(line)
    return "\n".join(kept), dropped


def mentions_outside(command: str, cwd: Path) -> bool:
    """Kan kommandoen lese filer utenfor arbeidsmappen? Konservativ: all skall-ekspansjon
    ($VAR, glob, {a,b}, escaping) regnes som mulig utenfor, siden den ikke kan avgjøres her."""
    if any(ch in command for ch in "$*?[{\\"):
        return True
    segs = _segments(command)
    if segs is None:
        return True
    root = cwd.resolve()
    for words in segs:
        for w in words[1:]:
            candidates = [w]
            if w.startswith("-"):
                if "=" in w:
                    candidates.append(w.split("=", 1)[1])  # --file=/etc/x
                if len(w) > 2 and w[1] != "-":
                    candidates.append(w[2:])  # -f/etc/x
            for c in candidates:
                if not c or (c.startswith("-") and c is w):
                    continue
                p = Path(os.path.expanduser(c))
                p = p if p.is_absolute() else root / p
                # Løs opp også relative stier: en symlenke i arbeidsmappen kan peke ut av den.
                if c.startswith(("/", "~")) or ".." in c.split("/") or p.exists() or p.is_symlink():
                    real = Path(os.path.realpath(p))
                    if real != root and root not in real.parents:
                        return True
    return False


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n… ({len(text) - limit} tegn utelatt) …\n{tail}"


# Verktøy/handlinger som sender data ut av maskinen (søkeord, steder, lenker): må godkjennes etter at Zeus har lest en
# nettside i samme runde (se Toolbox.approval).
OUTBOUND_AFTER_WEB = {"web:search", "web:read", "web:open", "media_find:", "snl_search:", "snl_read:", "media:inspect",
                      "media:add", "pc:place", "pc:weather"}


@dataclass
class Toolbox:
    cwd: Path
    cloud: bool = False  # modellen kjører i skyen: lesing utenfor arbeidsmappen krever ja
    timeout: float = 120.0
    session_allow: set[str] = field(default_factory=set)  # verktøy godkjent for resten av økten
    # Innhold utenfra er kommet inn i samtalen (nettside, søk, MCP-resultat uten «rent»-merke …). Gjelder resten av
    # samtalen: orkestratoren setter den fra historikken ved hver runde (_tainted på lagrede meldinger), så den
    # overlever lagring/åpning av økta. Se approval.
    web_tainted: bool = False
    undo_instead: bool = True  # tools.undo_instead: angrebare endringer (én avtale) uten kort, med «↶ angre»
    library: Any = None  # olympus.library.Library
    max_read_lines: int | None = None  # satt for gratis skymodeller (tokenkvote per minutt)
    approved_paths: dict[str, str] = field(default_factory=dict)  # kall-id → oppløst sti ved godkjenning
    direct_exec: dict[str, list[str]] = field(default_factory=dict)  # kall-id → argv kjørt uten bash
    memory: Any = None  # olympus.memory.MemoryStore
    sessions: Any = None  # olympus.sessions.SessionIndex
    session_id: str | None = None  # økten som pågår (utelates fra session_search)
    media_dirs: dict[str, Path] = field(default_factory=dict)  # music/audiobook/podcast → mappe
    lab: Any = None  # olympus.lab.Lab (planen)
    lib: Any = None  # olympus.medialib.Library (mediebiblioteket)
    dl: Any = None  # olympus.downloads.Downloads
    found: dict[str, Any] = field(default_factory=dict)  # treff fra media_find (id → Result)
    hub: Any = None  # olympus.web.server.Hub (modellbytte, økter, kringkasting) – bare i serveren
    mcp: Any = None  # olympus.mcp_client.McpHub (verktøy fra lokale MCP-servere, mcp_*)
    mcp_notice: Any = None  # kalles med en tekst til chatten mens et MCP-kall venter («venter på godkjenning …»)

    def ctx(self) -> Any:
        """Konteksten handlingene i olympus.actions kjører med når Zeus kaller dem."""
        from olympus.actions import Ctx

        return Ctx(lab=self.lab, lib=self.lib, dl=self.dl, hub=self.hub, memory=self.memory, zeus=True, who="zeus",
                   found=self.found)

    def specs(self, names: list[str]) -> list[dict[str, Any]]:
        return [SPECS[n].schema() for n in names if n in SPECS]

    def _path(self, raw: str | None) -> Path:
        p = Path(os.path.expanduser(raw or ".")).expanduser()
        return (p if p.is_absolute() else self.cwd / p).resolve()

    def _inside(self, p: Path) -> bool:
        return p == self.cwd.resolve() or self.cwd.resolve() in p.parents

    def short(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.cwd.resolve()))
        except ValueError:
            return str(p).replace(str(Path.home()), "~", 1)

    # --- godkjenning -----------------------------------------------------
    def allowed(self, key: str) -> bool:
        """«Alltid i økten» – men ikke når innhold utenfra er lest og modellen er i skyen (da kan en injisert ordre
        ellers lekke data ut uten spørsmål)."""
        return key in self.session_allow and not (self.web_tainted and self.cloud)

    def approval(self, call: ToolCall) -> Approval:
        appr = self._approval(call)
        # «Godkjenn alltid i denne samtalen» (appen/Lab's) gjelder akkurat denne handlingen (plan:add, ikke all plan),
        # aldri røde kall. Handlingene i olympus.actions sjekkes inne i _approval, før lenkekontrollen (enforce_egress).
        from olympus import actions

        key = f"{call.name}:{call.args.get('action') or ''}"
        # Etter en lest nettside kan Zeus være lurt (prompt-injeksjon): alt som sender noe ut av maskinen må da godkjennes
        # i resten av runden – ikke bare web, men søk i åpne kilder, leksikon, stedsoppslag og lenker.
        own_rules = {"shell", "read_file", "list_dir", "search_files", "write_file", "edit_file"}  # har egne, strengere regler
        # MCP etter innhold utenfra: ingen «alltid» (mcp_client.McpHub.approval spør på nytt hver gang)
        mcp_tainted = call.name.startswith("mcp_") and self.web_tainted
        if call.name not in actions.GENERATED and call.name not in own_rules and self.allowed(key) \
                and not mcp_tainted and appr.needed and not appr.danger and not appr.reject:
            appr.needed = False
        # Sjekkes etter «alltid»: når innhold fra nettet er lest, spør vi uansett hva som er godkjent tidligere.
        if self.web_tainted and not appr.needed and not appr.reject and key in OUTBOUND_AFTER_WEB:
            appr = Approval(True, "Zeus vil sende noe ut etter å ha lest innhold fra nettet",
                            preview=json.dumps(call.args, ensure_ascii=False)[:500])
        return appr

    def _approval(self, call: ToolCall) -> Approval:
        a = call.args
        name = call.name
        if name.startswith("mcp_"):  # verktøy fra en MCP-server (olympus.mcp_client): risikoen bestemmer kortet
            if self.mcp is None:
                return Approval(False, reject=f"Ukjent verktøy: {name}")
            return self.mcp.approval(name, a, self.cloud, self.session_allow, tainted=self.web_tainted)
        if name not in SPECS:
            return Approval(False)
        from olympus import actions

        if name in actions.GENERATED:
            appr = actions.zeus_approval(self.ctx(), name, a, self.cloud)
            # Etter at Zeus har lest en nettside i denne runden, kan siden ha lurt ham (prompt-injeksjon): da må nye søk
            # og sider godkjennes, så private data ikke kan lekke ut gjennom søkeord eller lenker
            # «Godkjenn alltid i denne samtalen» gjelder akkurat denne handlingen (plan:add, ikke all plan), aldri
            # røde kall – og lenker serveren skal kontakte, spør fortsatt (enforce_egress under)
            if self.allowed(f"{name}:{a.get('action') or ''}") and appr.needed and not appr.danger and not appr.reject:
                appr.needed = False
            # Etter innhold fra nettet gjelder ikke «alltid»: et lurt søk kunne ellers lekke private data uten spørsmål
            if name == "web" and self.web_tainted and not appr.needed:
                appr = Approval(True, "Zeus vil søke/lese videre etter innhold fra nettet – søket sendes ut",
                                preview=str(a.get("query") or a.get("url") or f"treff {a.get('n')}"))
            # Lenker Zeus har valgt: alltid ja først, med hele lenken på kortet (håndheves her for alle handlinger)
            return actions.enforce_egress(actions.for_tool(name, str(a.get("action") or "")), a, appr)
        if name == "media_search" and self.cloud:
            # Titlene i biblioteket ditt ville blitt sendt til en skymodell: spør først.
            return Approval(True, "Søk i mediabiblioteket – treffene sendes til skyen", preview=str(a.get("query", "")))
        if name == "memory" and a.get("action") == "list" and self.cloud:
            return Approval(True, "Les minnet – oppføringene sendes til skyen", preview="USER.md og MEMORY.md")
        if name == "dato":
            return Approval(False)  # ren utregning, ingenting endres eller forlater maskinen
        if name in ("library_search", "library_read", "memory", "media_search"):
            return Approval(False)  # lokalt bibliotek/minne, ingenting forlater maskinen
        if name == "session_search":
            if self.cloud and not self.allowed(name):
                # Utdrag fra tidligere (også helt lokale) samtaler ville blitt sendt til skyen: spør først.
                return Approval(True, "Søk i tidligere samtaler – utdragene sendes til skyen", preview=str(a.get("query", "")))
            return Approval(False)
        if name == "media_find":
            return Approval(False)
        if name == "media_get":
            res = self.found.get(str(a.get("id", "")))
            if res is None and a.get("id"):
                return Approval(True, "Zeus vil laste ned (ukjent treff – søk på nytt)", danger=True, preview=str(a.get("id")))
            if res is None:
                return Approval(False, reject="Gi id fra media_find (en lenke brukeren har gitt: media add).")
            size = f"{res.size / 1e9:.1f} GB" if res.size > 1e9 else f"{res.size / 1e6:.0f} MB" if res.size else "ukjent størrelse"
            text = (f"{res.title} ({res.year or '?'})\n  {res.source} · {res.license or 'ukjent lisens'} · {size}\n"
                    f"  {'torrent via Transmission' if res.torrent else 'direkte'} → {res.kind}\n"
                    f"Lenke: {res.torrent or res.direct or res.page}")
            return Approval(True, "Zeus vil laste ned", preview=text, preview_lang="text")
        if name == "audio":
            act = str(a.get("action", "status"))
            if act == "status":
                return Approval(False)
            detail = {"volume": f"volum {a.get('percent')} %", "output": f"utgang {a.get('sink', '')}",
                      "connect": f"koble til {a.get('mac', '')}", "disconnect": f"koble fra {a.get('mac', '')}"}.get(act, act)
            return Approval(True, "Zeus vil endre lyden på PC-en", preview=detail, preview_lang="text")
        if name == "playlist":
            act = str(a.get("action", "list"))
            if act in ("list", "show"):
                return Approval(False)
            pid = self._playlist_id(str(a.get("id") or "")) or self._playlist_id(str(a.get("name") or ""))
            known = next((p["name"] for p in (self.lib.playlists() if self.lib else []) if p["id"] == pid), "")
            if act in ("add", "delete") and not pid:
                return Approval(False, reject=f"Fant ingen spilleliste «{a.get('id') or a.get('name')}». Bruk action=list.")
            what = known or a.get("name") or a.get("id") or ""
            src = a.get("query") or (f"{len(a.get('tracks') or [])} spor" if a.get("tracks") else "")
            verb = {"create": "lage spillelisten", "add": "legge til i spillelisten", "delete": "slette spillelisten"}.get(act, act)
            return Approval(True, "Zeus vil endre spillelister", danger=act == "delete",
                            preview=f"{verb}  {what}" + (f"\n  ← {src}" if src else ""), preview_lang="text",
                            bind={"id": pid} if pid and act != "create" else {})
        if name == "plan":
            act = str(a.get("action", "list"))
            if act == "list":
                if self.cloud:  # kalenderen din ville blitt sendt til en skymodell: spør først (som øktsøk)
                    return Approval(True, "Les planen – avtalene sendes til skyen",
                                    preview=f"{a.get('from') or 'i dag'} – {a.get('to') or '+7 dager'}")
                return Approval(False)
            from olympus.lab import _check_order, _iso, describe

            try:  # ugyldige tidspunkter avvises før kortet – kallet kjøres da ikke i det hele tatt
                _check_order(_iso(a.get("start")), _iso(a.get("end")))
            except (ValueError, TypeError) as e:
                return Approval(False, reject=f"Ugyldig tidspunkt: {e}")
            eid = self._plan_ref(str(a.get("id") or ""), str(a.get("match") or "")) if act != "add" else ""
            if act != "add" and not eid:
                return Approval(False, reject=f"Fant ingen (eller flere) kommende avtaler som passer "
                                              f"«{a.get('id') or a.get('match')}». Bruk action=list og send id-en.")
            ev = self.lab.get(eid) if self.lab is not None and eid else None
            what = describe(ev) if ev else str(a.get("id", "?"))
            # Kortet viser nøyaktig det som vil skje: alle felt som endres, ikke bare hvilken avtale – og
            # tidspunktet slik det blir tolket («fredag 10» → «fre 16.10 10:00»), så feil dag synes før du sier ja.
            fields = [f"{k}: {a[k]}" + (f"  → {_when(a[k])}" if k in ("start", "end") and _when(a[k]) != a[k] else "")
                      for k in ("title", "start", "end", "note") if a.get(k)]
            if act == "add":
                from olympus.lab import REPEATS

                from olympus.lab import series_dates

                rep = str(a.get("repeat") or "")
                if rep:  # nøyaktig det som blir laget: samme utregning som add_series
                    try:
                        times = series_dates(str(a.get("start") or ""), a.get("end") or None, rep,
                                             a.get("until") or None, a.get("count"))
                    except ValueError as e:
                        return Approval(False, reject=str(e))
                    first = describe({"start": times[0][0], "title": ""}).split(" · ")[0]
                    last = describe({"start": times[-1][0], "title": ""}).split(" · ")[0]
                    fields.append(f"gjentas: {REPEATS[rep]} – {len(times)} avtaler, første {first}, siste {last}")
                text = "legge til\n  " + "\n  ".join(fields)
            elif act == "delete":
                ev_s = (ev or {}).get("series")
                n = (self.lab.db.execute("SELECT count(*) FROM events WHERE series = ? AND deleted = 0 AND start >= ?",
                                         (ev_s, ev["start"])).fetchone()[0] if a.get("series") and ev_s else 1)
                text = f"slette  {what}" + (f"\n  og resten av serien: {n} avtaler i alt" if n > 1 else "")
            else:
                text = f"{'flytte' if act == 'move' else 'endre'}  {what}\n  → " + ("\n  → ".join(fields) or "(ingen endring)")
            # Angre i stedet for å spørre: én avtale lagt til/flyttet/endret skjer med en gang, med «↶ angre» i chatten.
            # Serier, sletting og alt rødt spør fortsatt.
            # Bare lokalt: med en skymodell ville svaret (avtalen som ble funnet/endret) gått ut uten at du ble spurt.
            if self.undo_instead and not self.cloud and act in ("add", "move", "edit") and not a.get("repeat"):
                return Approval(False, bind={"id": eid, "match": ""} if eid else {})
            return Approval(True, "Zeus vil endre planen", danger=act == "delete", preview=text, preview_lang="text",
                            bind={"id": eid, "match": ""} if eid else {})
        if name in ("snl_search", "snl_read"):
            return Approval(False)  # bare søkeord/artikkelnavn sendes til snl.no; brukeren har slått det på
        if name in ("read_file", "list_dir", "search_files"):
            p = self._path(a.get("path"))
            if self.cloud and is_sensitive(p):
                return Approval(True, f"Les {self.short(p)}: ser ut som hemmeligheter/nøkler (sendes til skyen)",
                                danger=True, preview=str(p))
            if self.cloud and not self._inside(p) and not self.allowed(name):
                return Approval(True, f"Les {self.short(p)} utenfor arbeidsmappen (innholdet sendes til skyen)", preview=str(p))
            return Approval(False)
        if name == "shell":
            cmd = str(a.get("command", ""))
            if is_dangerous(cmd):
                return Approval(True, "Kommandoen kan endre eller skade systemet", danger=True, preview=cmd, preview_lang="bash")
            if self.cloud and mentions_sensitive(cmd, self.cwd):
                return Approval(True, "Kommandoen rører filer som ser ut som hemmeligheter (resultatet sendes til skyen)",
                                danger=True, preview=cmd, preview_lang="bash")
            if self.cloud and is_recursive(cmd):
                return Approval(True, "Rekursivt søk/liste (kan treffe private filer; resultatet sendes til skyen)",
                                preview=cmd, preview_lang="bash")
            if self.cloud and mentions_outside(cmd, self.cwd):
                # Lesekommandoer utenfor arbeidsmappen ville sendt innholdet ut av maskinen.
                return Approval(True, "Kommandoen leser utenfor arbeidsmappen (resultatet sendes til skyen)",
                                preview=cmd, preview_lang="bash")
            if is_readonly(cmd):
                self.direct_exec[call.id] = simple_argv(cmd) or []
                return Approval(False)
            argv = simple_argv(cmd)
            if name in self.session_allow and argv is not None:
                # «Ja for økten» gjelder bare enkle kommandoer, og de kjøres uten bash, så
                # faresjekken over så nøyaktig det som kjøres. Sammensatte kommandoer spør alltid.
                self.direct_exec[call.id] = argv
                return Approval(False)
            return Approval(True, "Kjør kommando", preview=cmd, preview_lang="bash")
        # write_file / edit_file
        p = self._path(a.get("path"))
        outside = not self._inside(p)
        danger = outside and str(p).startswith(("/etc", "/usr", "/boot", "/var", "/opt"))
        if name in self.session_allow and not outside:
            return Approval(False)
        self.approved_paths[call.id] = str(p)
        reason = ("Skriv" if name == "write_file" else "Endre") + f" {self.short(p)}"
        if outside:
            reason += " (utenfor arbeidsmappen)"
        return Approval(True, reason, danger=danger, preview=self._diff(call), preview_lang="diff")

    def _diff(self, call: ToolCall) -> str:
        a = call.args
        p = self._path(a.get("path"))
        try:
            old = p.read_text(encoding="utf-8") if p.exists() else ""
        except (OSError, UnicodeDecodeError):
            old = ""
        if call.name == "write_file":
            new = str(a.get("content", ""))
        else:
            o, n = str(a.get("old_text", "")), str(a.get("new_text", ""))
            new = old.replace(o, n, 1) if o and o in old else old
        diff = difflib.unified_diff(
            old.splitlines(), new.splitlines(), f"a/{self.short(p)}", f"b/{self.short(p)}", lineterm="", n=2
        )
        return _clip("\n".join(diff), 4000) or "(ingen endring)"

    # --- kjøring ---------------------------------------------------------
    async def run(self, call: ToolCall) -> ToolResult:
        from olympus import actions

        if call.name.startswith("mcp_") and self.mcp is not None:
            if call.arguments and call.arguments.strip() not in ("{}", "null") and not call.args:
                return ToolResult(False, f"Ugyldige argumenter (ikke JSON-objekt): {call.arguments[:200]}", "ugyldige argumenter")
            result = await self.mcp.call(call.name, call.args, self.mcp_notice)
            if self.mcp.taints(call.name) and not result.output.startswith("Ukjent MCP"):
                # Serverens tekst (også feil) er nå i konteksten: med mindre verktøyet er merket rent
                # (openWorldHint=false, ikke sensitivt, ikke filer/varsler/utklipp/nett) regnes den som utenfra
                self.web_tainted = True
            return result
        if call.name in actions.GENERATED:
            if call.arguments and call.arguments.strip() not in ("{}", "null") and not call.args:
                return ToolResult(False, f"Ugyldige argumenter (ikke JSON-objekt): {call.arguments[:200]}", "ugyldige argumenter")
            return await actions.zeus_run(self.ctx(), call.name, {k: v for k, v in call.args.items() if v is not None})
        try:
            fn = getattr(self, f"_t_{call.name}")
        except AttributeError:
            return ToolResult(False, f"Ukjent verktøy: {call.name}", "ukjent verktøy")
        if call.name in ("write_file", "edit_file"):
            # Stien kan ha blitt byttet ut (f.eks. med en symlenke) etter godkjenningen.
            now = str(self._path(call.args.get("path")))
            before = self.approved_paths.pop(call.id, now)
            if now != before:
                return ToolResult(False, f"Stien endret seg etter godkjenning ({before} → {now}). Avbrutt.", "sti endret")
        if call.arguments and call.arguments.strip() not in ("{}", "null") and not call.args:
            return ToolResult(False, f"Ugyldige argumenter (ikke JSON-objekt): {call.arguments[:200]}", "ugyldige argumenter")
        if call.name == "shell" and call.id in self.direct_exec:
            # Godkjent uten spørsmål som én enkel kommando: kjør nøyaktig den, uten bash.
            return await self._run_argv(self.direct_exec.pop(call.id))
        try:
            return await fn(**{k: v for k, v in call.args.items() if v is not None})
        except TypeError as e:
            return ToolResult(False, f"Feil argumenter: {e}", "feil argumenter")
        except OSError as e:
            return ToolResult(False, f"{type(e).__name__}: {e}", str(e)[:80])

    async def _t_read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
        p = self._path(path)
        if not p.is_file():
            return ToolResult(False, f"Finnes ikke eller er ikke en fil: {p}", "finnes ikke")
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return ToolResult(False, f"{p} er ikke en tekstfil", "binærfil")
        start = max(1, int(offset or 1))
        if self.max_read_lines and (not limit or int(limit) > self.max_read_lines):
            limit = self.max_read_lines
        end = len(lines) if not limit else min(len(lines), start - 1 + int(limit))
        body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
        if end < len(lines):
            body += f"\n… (filen har {len(lines)} linjer; les videre med offset={end + 1})"
        return ToolResult(True, _clip(body), f"{self.short(p)} · linje {start}–{end} av {len(lines)}")

    async def _t_list_dir(self, path: str | None = None) -> ToolResult:
        p = self._path(path)
        if not p.is_dir():
            return ToolResult(False, f"Ikke en mappe: {p}", "ikke en mappe")
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        out = [f"{x.name}/" if x.is_dir() else x.name for x in items[:500]]
        return ToolResult(True, "\n".join(out), f"{self.short(p)} · {len(items)} elementer")

    async def _t_search_files(self, pattern: str, path: str | None = None) -> ToolResult:
        p = self._path(path)
        rg = shutil.which("rg")
        cmd = [rg, "-n", "--no-heading", "-m", "20", "--", pattern, str(p)] if rg else [
            "grep", "-rnI", "-m", "20", "-E", "--", pattern, str(p)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=self.cwd
        )
        out, err = await asyncio.wait_for(proc.communicate(), 30)
        text = out.decode(errors="replace").replace(str(self.cwd.resolve()) + "/", "")
        if self.cloud:
            text, hidden = drop_sensitive_lines(text, self.cwd)
            if hidden:
                text += f"\n({hidden} treff i private filer er skjult)"
        hits = len(text.splitlines())
        if proc.returncode not in (0, 1):
            return ToolResult(False, err.decode(errors="replace")[:500], "søket feilet")
        return ToolResult(True, _clip(text) or "(ingen treff)", f"«{pattern}» · {hits} treff")

    async def _t_memory(self, action: str, target: str = "", content: str = "", old_text: str = "", id: str = "") -> ToolResult:
        if self.memory is None:
            return ToolResult(False, "Minnet er ikke tilgjengelig.", "ikke tilgjengelig")
        if action == "list":
            items = [(t, i) for t in ("user", "memory") for i in self.memory.items(t)]
            return ToolResult(True, "\n".join(f"- [{i['id']}] ({t}) {i['text']}" for t, i in items) or "(minnet er tomt)",
                              f"{len(items)} oppføringer")
        if not target and id and (hit := self.memory.find(id)):
            target = hit[0]
        r = self.memory.apply(action, target, content, old_text, old_id=id)
        if r.ok and self.hub is not None:
            await self.hub.send({"type": "memory_changed"})
        what = {"add": "lagret", "replace": "endret", "remove": "fjernet"}.get(action, action)
        return ToolResult(r.ok, r.message, f"{target}: {what}" if r.ok else r.message[:80])

    async def _t_session_search(self, query: str = "", action: str = "search") -> ToolResult:
        if self.sessions is None:
            return ToolResult(False, "Øktsøk er ikke tilgjengelig.", "ikke tilgjengelig")
        if action == "list" or not query.strip():
            rows = self.sessions.store.list(15) if hasattr(self.sessions, "store") else []
            return ToolResult(True, "\n".join(f"- [{r.id}] {r.title}" for r in rows) or "(ingen samtaler)", f"{len(rows)} samtaler")
        hits = await asyncio.to_thread(self.sessions.search, query, 8, self.session_id)
        if not hits:
            return ToolResult(True, "(ingen treff i tidligere økter)", f"«{query}» · 0 treff")
        out = "\n\n".join(f"[{h['title']}] {h['who']}: {h['snippet']}" for h in hits)
        return ToolResult(True, out, f"«{query}» · {len(hits)} treff i {len({h['sid'] for h in hits})} økter")

    async def _t_library_search(self, query: str, source: str | None = None) -> ToolResult:
        if self.library is None or not self.library.ready():
            return ToolResult(False, "Biblioteket er ikke indeksert. Kjør `olympus library index`.", "ikke indeksert")
        hits = await asyncio.to_thread(self.library.search, query, 8, source)
        if not hits:
            return ToolResult(True, "(ingen treff — prøv andre, gjerne engelske, søkeord)", f"«{query}» · 0 treff")
        out = "\n\n".join(f"[{h.id}] {h.title} › {h.section or '(innledning)'}  ({h.source})\n{h.snippet}" for h in hits)
        return ToolResult(True, out, f"«{query}» · {len(hits)} treff: {', '.join(dict.fromkeys(h.title for h in hits))[:80]}")

    async def _t_library_read(self, ref: str, section: str | None = None, source: str | None = None) -> ToolResult:
        if self.library is None:
            return ToolResult(False, "Biblioteket er ikke tilgjengelig.", "ikke tilgjengelig")
        got = await asyncio.to_thread(self.library.read, ref, section, 8000, source)
        if got is None:
            return ToolResult(False, f"Fant ikke «{ref}» i biblioteket. Søk med library_search først.", "fant ikke")
        title, text = got
        return ToolResult(True, f"# {title}\n\n{text}", f"{title}{' › ' + section if section else ''} · {len(text)} tegn")

    @staticmethod
    def _write_nofollow(p: Path, content: str) -> None:
        """Skriv uten å følge en symlenke i siste ledd (O_NOFOLLOW)."""
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)

    def _plan_ref(self, ref: str, match: str = "") -> str:
        """Avtalens id – eller en kommende avtale med den tittelen (modellen bruker ofte navnet). "" = ingen/flere."""
        if self.lab is None:
            return ""
        for r in (ref, match):
            if r and (ev := self.lab.get(r)) and not ev["deleted"]:
                return ev["id"]
        from datetime import date, timedelta

        # Tittelsøk bare med «match» (eller et navn i id-feltet) – aldri med en ekte id som har forsvunnet etter
        # godkjenningen, ellers kunne en annen avtale bli truffet.
        looks_like_id = bool(re.fullmatch(r"[0-9a-f]{12}", ref.strip()))
        want = (match or ("" if looks_like_id else ref)).strip().casefold()
        if not want:
            return ""
        evs = self.lab.events(date.today().isoformat(), (date.today() + timedelta(days=366)).isoformat())
        hits = [e for e in evs if e["title"].casefold() == want] or [e for e in evs if want in e["title"].casefold()]
        return hits[0]["id"] if len(hits) == 1 else ""

    async def _t_dato(self, action: str, fra: str = "", til: str = "", dato: str = "", dager: int = 0) -> ToolResult:
        from datetime import date, timedelta

        from olympus.dates import parse_when

        names = {"julaften": "24.12", "første juledag": "25.12", "nyttårsaften": "31.12", "nyttårsdag": "1.1",
                 "17. mai": "17.5", "grunnlovsdag": "17.5", "sankthans": "23.6", "halloween": "31.10"}
        full = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]

        def parse(text: str, default: date | None = None) -> date | None:
            t = (text or "").strip().lower()
            if not t:
                return default
            t = names.get(t, t)
            iso = parse_when(t)
            return date.fromisoformat(iso[:10]) if iso else None

        today = date.today()
        if action == "dager":
            a, b = parse(fra, today), parse(til)
            if a is None or b is None:
                return ToolResult(False, f"Forstår ikke datoen («{fra or 'i dag'}» / «{til}»).", "ugyldig dato")
            n = (b - a).days
            return ToolResult(True, f"{n} dager fra {full[a.weekday()]} {a:%d.%m.%Y} til {full[b.weekday()]} {b:%d.%m.%Y}"
                                    f" ({n // 7} uker og {n % 7} dager).", f"{n} dager")
        if action == "ukedag":
            d = parse(dato)
            if d is None:
                return ToolResult(False, f"Forstår ikke datoen «{dato}».", "ugyldig dato")
            return ToolResult(True, f"{d:%d.%m.%Y} er en {full[d.weekday()]} (uke {d.isocalendar().week}).", full[d.weekday()])
        if action == "pluss":
            d = parse(dato, today)
            if d is None:
                return ToolResult(False, f"Forstår ikke datoen «{dato}».", "ugyldig dato")
            r = d + timedelta(days=int(dager))
            return ToolResult(True, f"{d:%d.%m.%Y} + {int(dager)} dager = {full[r.weekday()]} {r:%d.%m.%Y}.", f"{r:%d.%m.%Y}")
        return ToolResult(False, f"Ukjent handling: {action}", "ukjent handling")

    def _last_change(self, event_id: str) -> int | None:
        row = self.lab.db.execute("SELECT max(seq) FROM changes WHERE event_id = ?", (event_id,)).fetchone()
        return row[0] if row else None

    async def _t_plan(self, action: str, id: str = "", title: str = "", start: str = "", end: str = "",
                      note: str = "", match: str = "", **kw: Any) -> ToolResult:
        from datetime import date, timedelta

        from olympus.lab import describe

        if self.lab is None:
            return ToolResult(False, "Planen er ikke tilgjengelig.", "ikke tilgjengelig")
        try:
            if action == "list":
                from olympus.lab import _iso

                # Datoene tolkes som i add («lørdag 17.10», «17.10.2026», «i morgen»): før måtte de være ISO, og
                # modellen brukte to–tre kall på å gjette formatet.
                start_d = (_iso(kw.get("from")) or date.today().isoformat())[:10]
                # «to» er TIL OG MED den dagen (modellen ba om man–fre og mistet fredagen da den var eksklusiv)
                end_d = ((date.fromisoformat(_iso(kw["to"])[:10]) + timedelta(days=1)).isoformat() if kw.get("to")
                         else (date.fromisoformat(start_d) + timedelta(days=8)).isoformat())
                evs = self.lab.events(start_d, end_d)
                body = "\n".join(f"- [{e['id']}] {describe(e)}" + (f" ({e['note']})" if e["note"] else "") for e in evs)
                last = date.fromisoformat(end_d) - timedelta(days=1)
                period = f"Planen {date.fromisoformat(start_d):%d.%m}–{last:%d.%m}:\n"
                return ToolResult(True, period + (body or "(ingenting i planen i den perioden)"), f"{len(evs)} avtaler")
            if action == "add" and kw.get("repeat"):
                from olympus.lab import REPEATS

                evs = self.lab.add_series(title, start, end or None, note=note, repeat=str(kw["repeat"]),
                                          until=kw.get("until") or None, count=kw.get("count"), who="zeus")
                last = describe(evs[-1]).split(" · ")[0]
                return ToolResult(True, f"La til {len(evs)} avtaler ({REPEATS[kw['repeat']]}, serie {evs[0]['series']}): "
                                        f"{describe(evs[0])} … siste {last}", f"la til {len(evs)} × {title}")
            if action == "add":
                ev = self.lab.add(title, start, end or None, note=note, who="zeus")
                return ToolResult(True, f"La til [{ev['id']}] {describe(ev)}", f"la til {describe(ev)}",
                                  undo=self._last_change(ev["id"]))
            if action in ("move", "edit", "delete"):
                ref = id or match
                id = self._plan_ref(id, match)
                if not id:
                    return ToolResult(False, f"Fant ingen (eller flere) kommende avtaler som passer «{ref}». Bruk action=list "
                                             "og send id-en.", "fant ikke")
            if action in ("move", "edit"):
                ev = self.lab.update(id, who="zeus", title=title or None, start=start or None, end=end or None,
                                     note=note or None)
                return ToolResult(True, f"Endret [{ev['id']}] {describe(ev)}", f"endret {describe(ev)}",
                                  undo=self._last_change(ev["id"]))
            if action == "delete" and kw.get("series"):
                gone = self.lab.delete_series(id, who="zeus")
                return ToolResult(True, f"Slettet {len(gone)} avtaler i serien fra {describe(gone[0])} (kan angres i loggen)",
                                  f"slettet {len(gone)} avtaler")
            if action == "delete":
                ev = self.lab.delete(id, who="zeus")
                return ToolResult(True, f"Slettet {describe(ev)} (kan angres i loggen)", f"slettet {describe(ev)}")
        except KeyError:
            return ToolResult(False, f"Fant ingen avtale med id «{id}». Bruk action=list først.", "fant ikke")
        except ValueError as e:
            return ToolResult(False, f"Ugyldig tidspunkt eller felt: {e}", "ugyldig")
        return ToolResult(False, f"Ukjent handling: {action}", "ukjent handling")

    async def _t_media_search(self, query: str, kind: str = "all") -> ToolResult:
        if self.lib is None:
            return ToolResult(False, "Mediebiblioteket er ikke tilgjengelig.", "ikke tilgjengelig")
        if not query.strip():
            # Tomt søk: en ærlig oversikt (antall + album/filmer/bøker), så Zeus aldri gjetter på hva som finnes.
            st = self.lib.stats()
            albums = self.lib.albums()[:25]
            v = self.lib.videos()
            books = self.lib.books()[:15]
            lines = [f"Biblioteket: {st['tracks']} sanger i {st['albums']} album, {st['films']} filmer, "
                     f"{st['episodes']} episoder, {st['books']} bøker, {st['playlists']} spillelister."]
            lines += [f"- album [{a['id']}] {a['title']} – {a['artist']} ({a['tracks']} spor)" for a in albums]
            lines += [f"- film [{f['id']}] {f['title']} ({f.get('year') or '?'})" for f in v["films"][:15]]
            lines += [f"- serie {s_['name']} ({len(s_['episodes'])} episoder)" for s_ in v["shows"][:10]]
            lines += [f"- bok [{b['id']}] {b['title']} – {b['author']}" for b in books]
            return ToolResult(True, "\n".join(lines), f"oversikt: {st['tracks']} sanger, {st['films']} filmer, {st['books']} bøker")
        r = await asyncio.to_thread(self.lib.search, query, 15)
        want = {"music": ("tracks", "albums"), "video": ("videos",), "book": ("books",)}.get(kind, ("tracks", "albums", "videos", "books"))
        lines = []
        if "albums" in want:
            for a in r["albums"]:
                _, files = self.lib.files_for(a["id"])
                where = f" · {self._rel(files[0].parent)}" if files else ""
                lines.append(f"- album [{a['id']}] {a['title']} – {a['artist']} ({a['tracks']} spor){where}")
        if "tracks" in want:
            for t in r["tracks"]:
                _, files = self.lib.files_for(t["id"])
                where = f" · {self._rel(files[0])}" if files else ""
                lines.append(f"- sang [{t['id']}] {t['title']} – {t['artist']} (album «{t['album']}» [{t['album_id']}]){where}")
        if "videos" in want:
            lines += [f"- {'episode' if v['kind'] == 'episode' else 'film'} [{v['id']}] "
                      + (f"{v['show']} S{v['season'] or 0:02}E{v['episode'] or 0:02} {v['title']}" if v["kind"] == "episode"
                         else f"{v['title']} ({v['year'] or '?'})") for v in r["videos"]]
        if "books" in want:
            lines += [f"- {'lydbok' if b['kind'] == 'audio' else 'e-bok'} [{b['id']}] {b['title']} – {b['author']}" for b in r["books"]]
        return ToolResult(True, "\n".join(lines) or "(ingen treff i biblioteket)", f"«{query}» · {len(lines)} treff")

    @staticmethod
    def _rel(p: Path) -> str:
        try:
            return "~/" + str(p.relative_to(Path.home()))
        except ValueError:
            return str(p)

    async def _t_media_find(self, query: str, kind: str | None = None) -> ToolResult:
        from olympus.sources import search_all

        hits, errors = await search_all(query, kind)
        lines = []
        for h in hits:
            self.found[h.id] = h
            size = f" · {h.size / 1e9:.1f} GB" if h.size > 1e9 else f" · {h.size / 1e6:.0f} MB" if h.size else ""
            lines.append(f"- [{h.id}] {h.title}{f' ({h.year})' if h.year else ''} – {h.creator[:40]} · {h.source} · "
                         f"{h.kind} · {'torrent' if h.torrent else 'direkte'}{size} · {h.license[:40]}")
        body = "\n".join(lines) or "(ingen treff)"
        if errors:
            body += "\n\nKilder som ikke svarte: " + ", ".join(errors)
        return ToolResult(True, body, f"«{query}» · {len(hits)} treff")

    async def _t_media_get(self, id: str = "", **_: Any) -> ToolResult:
        if self.dl is None:
            return ToolResult(False, "Nedlasting er ikke tilgjengelig her (bare i Olympus-serveren).", "ikke tilgjengelig")
        res = self.found.get(id) if id else None
        if res is None:
            return ToolResult(False, "Ukjent treff – kjør media_find på nytt.", "ukjent treff")
        try:
            # Akkurat det godkjenningskortet viste: treffets egen tittel, type og år.
            d = await asyncio.to_thread(self.dl.start, res.kind, res.title, res.torrent, "" if res.torrent else res.direct,
                                        res.year, res.source, res.page, res.creator, res.extra.get("identifier", ""))
        except (ValueError, RuntimeError, OSError) as e:
            return ToolResult(False, f"Fikk ikke startet nedlastingen: {e}", "feilet")
        except Exception as e:  # noqa: BLE001 – Transmission nede o.l.
            return ToolResult(False, f"Fikk ikke kontakt med Transmission: {type(e).__name__}", "feilet")
        if (run := self.dl.runner(d["id"])) is not None:
            asyncio.get_running_loop().run_in_executor(None, run, d["id"])
        return ToolResult(True, f"Startet: {d['title']} ({d['method']}). Jeg sier fra når den er i biblioteket.", f"laster ned {d['title']}")

    async def _t_audio(self, action: str, percent: int | None = None, sink: str = "", mac: str = "") -> ToolResult:
        from olympus import audio

        if action == "volume" and percent is not None:
            await audio.set_volume(int(percent), sink or None)
        elif action in ("mute", "unmute"):
            await audio.set_mute(action == "mute", sink or None)
        elif action == "output":
            if not await audio.set_default(sink):
                return ToolResult(False, f"Ukjent utgang «{sink}». Se action=status.", "ukjent utgang")
        elif action in ("connect", "disconnect"):
            if not await audio.bt_connect(mac, action == "connect"):
                return ToolResult(False, f"Fikk ikke {'koblet til' if action == 'connect' else 'koblet fra'} {mac}.", "feilet")
        st = await audio.status()
        lines = [f"- {'●' if o['default'] else '○'} {o['label']} [{o['name']}] {o['volume']} %{' (dempet)' if o['muted'] else ''}"
                 for o in st["outputs"]]
        lines += [f"- Bluetooth: {b['name']} [{b['mac']}] {'tilkoblet' if b['connected'] else 'ikke tilkoblet'}" for b in st["bluetooth"]]
        return ToolResult(True, "\n".join(lines) or "(ingen lydutganger funnet)", action)

    def _playlist_id(self, ref: str) -> str:
        """Modellen bruker ofte navnet i stedet for id-en: godta begge (navn uten hensyn til store/små bokstaver)."""
        if not ref or self.lib is None:
            return ""
        ps = self.lib.playlists()
        hit = next((p for p in ps if p["id"] == ref), None) or next(
            (p for p in ps if p["name"].strip().lower() == ref.strip().lower()), None)
        return hit["id"] if hit else ""

    async def _t_playlist(self, action: str, name: str = "", id: str = "", query: str = "",
                          tracks: list | None = None) -> ToolResult:
        if self.lib is None:
            return ToolResult(False, "Mediebiblioteket er ikke tilgjengelig.", "ikke tilgjengelig")
        lib = self.lib
        # Med id (også låst av godkjenningen) brukes bare den; navnet er bare reserve når id mangler.
        id = (self._playlist_id(id) if id else self._playlist_id(name)) if action != "create" else id
        if action == "list":
            ps = lib.playlists()
            return ToolResult(True, "\n".join(f"- [{p['id']}] {p['name']} ({p['tracks']} spor)" for p in ps) or "(ingen spillelister)",
                              f"{len(ps)} spillelister")
        if action == "show":
            ts = lib.playlist_tracks(id)
            return ToolResult(True, "\n".join(f"- [{t['id']}] {t['title']} – {t['artist']}" for t in ts) or "(tom)", f"{len(ts)} spor")
        ids = [str(t) for t in (tracks or [])]
        if query and not ids:
            ids = [t["id"] for t in await asyncio.to_thread(lib.songs_for, query, 40)]
        try:
            if action == "create":
                p = lib.playlist_create(name or query or "Ny spilleliste", ids)
                return ToolResult(True, f"Laget «{p['name']}» [{p['id']}] med {p['tracks']} spor.", f"laget {p['name']}")
            if action == "add":
                lib.playlist_add(id, ids)
                return ToolResult(True, f"La til {len(ids)} spor.", f"la til {len(ids)} spor")
            if action == "delete":
                gone = lib.playlist_delete(id)
                return ToolResult(gone, "Slettet." if gone else f"Fant ingen spilleliste «{id}».", "slettet" if gone else "fant ikke")
        except KeyError:
            return ToolResult(False, f"Fant ingen spilleliste med id «{id}». Bruk action=list.", "fant ikke")
        return ToolResult(False, f"Ukjent handling: {action}", "ukjent handling")

    async def _t_snl_search(self, query: str) -> ToolResult:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Olympus/0.1 (personlig assistent)"}) as c:
                r = await c.get("https://snl.no/api/v1/search", params={"query": query, "limit": 6})
            hits = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return ToolResult(False, f"SNL svarte ikke: {e}", "SNL utilgjengelig")
        if not hits:
            return ToolResult(True, "(ingen treff i SNL)", f"⊙ SNL «{query}» · 0 treff")
        out = []
        for h in hits:
            snippet = re.sub(r"</?mark>", "", h.get("snippet") or "")
            clar = f" ({h['clarification']})" if h.get("clarification") else ""
            import urllib.parse

            out.append(f"{h.get('headword')}{clar} — permalink: {urllib.parse.unquote(h.get('permalink') or '')}\n{snippet}")
        return ToolResult(True, "\n\n".join(out), f"⊙ SNL «{query}» · {len(hits)} treff")

    async def _t_snl_read(self, permalink: str) -> ToolResult:
        import urllib.parse

        import httpx

        from olympus.library import _clean, _WikiHTML

        name = urllib.parse.unquote(permalink.strip().removeprefix("https://snl.no/"))
        url = "https://snl.no/" + urllib.parse.quote(name)
        try:
            async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Olympus/0.1 (personlig assistent)"},
                                         follow_redirects=True) as c:
                d = (await c.get(url + ".json")).json()
        except (httpx.HTTPError, ValueError) as e:
            return ToolResult(False, f"Fant ikke artikkelen i SNL: {e}", "fant ikke")
        parser = _WikiHTML()
        parser.feed(d.get("xhtml_body", ""))
        text = "\n\n".join(f"## {n}\n{_clean(b)}" if n else _clean(b) for n, b in parser.sections if _clean(b))
        authors = ", ".join(a.get("full_name", "").strip() for a in d.get("authors", [])[:3])
        footer = (f"\n\nKilde: Store norske leksikon – «{d.get('title')}», {authors}. {d.get('url', url)} "
                  f"(lisens: {d.get('license_name', '?')}; siter kort og lenk, ikke gjengi hele artikkelen)")
        return ToolResult(True, _clip(f"# {d.get('title')}\n\n{text}", 7000) + footer,
                          f"⊙ SNL «{d.get('title')}» · {len(text)} tegn")

    async def _t_write_file(self, path: str, content: str) -> ToolResult:
        p = self._path(path)
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        self._write_nofollow(p, content)
        verb = "overskrev" if existed else "opprettet"
        return ToolResult(True, f"{verb} {p} ({len(content.splitlines())} linjer)", f"{verb} {self.short(p)}")

    async def _t_edit_file(self, path: str, old_text: str, new_text: str) -> ToolResult:
        p = self._path(path)
        if not p.is_file():
            return ToolResult(False, f"Finnes ikke: {p}", "finnes ikke")
        text = p.read_text(encoding="utf-8")
        count = text.count(old_text) if old_text else 0
        if count != 1:
            return ToolResult(False, f"old_text finnes {count} ganger i {p}; den må finnes nøyaktig én gang. Les filen og prøv igjen.", f"fant teksten {count} ganger")
        self._write_nofollow(p, text.replace(old_text, new_text, 1))
        return ToolResult(True, f"Endret {p}", f"endret {self.short(p)}")

    def _shell_env(self) -> dict[str, str]:
        return {**os.environ, "GIT_CONFIG_PARAMETERS": "'core.fsmonitor=false' 'core.hooksPath=/dev/null'",
                "GIT_PAGER": "cat", "PAGER": "cat"}

    async def _run_argv(self, argv: list[str]) -> ToolResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, cwd=self.cwd, env=self._shell_env(), start_new_session=True,
            )
        except FileNotFoundError:
            return ToolResult(False, f"Fant ikke programmet {argv[0]}", "fant ikke programmet")
        return await self._finish(proc)

    async def _finish(self, proc) -> ToolResult:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(False, f"Tidsavbrudd etter {self.timeout:.0f} s", "tidsavbrudd")
        except asyncio.CancelledError:
            proc.kill()
            raise
        text = out.decode(errors="replace")
        if self.cloud:
            text, hidden = drop_sensitive_lines(text, self.cwd)
            if hidden:
                text += f"\n({hidden} linjer fra private filer er skjult)"
        ok = proc.returncode == 0
        return ToolResult(ok, _clip(f"{text}\n[exit {proc.returncode}]"), f"exit {proc.returncode}")

    async def _t_shell(self, command: str) -> ToolResult:
        """Godkjent av brukeren: kjør i bash."""
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command, env=self._shell_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, cwd=self.cwd, start_new_session=True,
        )  # fmt: skip
        return await self._finish(proc)


# Verktøyene som lages fra handlingsregisteret (media, downloads, pc, model) – se olympus.actions.
from olympus import actions as _actions  # noqa: E402

SPECS.update(_actions.tool_specs())
