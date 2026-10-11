"""Laster og skriver Olympus-konfigurasjon (~/.olympus/config.toml).

Brukerens config legges oppå standardverdiene, slik at nye nøkler i senere
versjoner alltid har en verdi selv om brukerfilen er gammel.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import tomlkit

# (fil i ~/.olympus, innebygd mal). config.toml er bare overstyringer; standardverdiene
# leses alltid fra pakken slik at nye nøkler slår gjennom ved oppdatering.
DEFAULT_FILES = (
    ("config.toml", "user_config.toml"),
    ("theme.toml", "theme.toml"),
    ("SOUL.md", "SOUL.md"),
)


def olympus_home() -> Path:
    return Path(os.environ.get("OLYMPUS_HOME", "~/.olympus")).expanduser()


def _default_text(name: str) -> str:
    return resources.files("olympus.defaults").joinpath(name).read_text(encoding="utf-8")


def ensure_home() -> Path:
    """Opprett ~/.olympus med standardfiler hvis de mangler. Overskriver aldri."""
    home = olympus_home()
    for sub in ("memory", "skills", "models", "logs", "agents"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    for name, template in DEFAULT_FILES:
        target = home / name
        if not target.exists():
            target.write_text(_default_text(template), encoding="utf-8")
    return home


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


ZEUS = "zeus"  # Olympus er Zeus: én assistent, hjernen på PC-en; bare modellen som svarer kan byttes


@dataclass
class Config:
    data: dict[str, Any]
    theme: dict[str, Any]
    home: Path = field(default_factory=olympus_home)

    # --- oppslag ---------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, key: str) -> Path:
        return Path(self.get(f"paths.{key}")).expanduser()

    def agent(self, name: str) -> dict[str, Any]:
        agents = self.get("agents", {})
        if name not in agents:
            raise KeyError(f"Ukjent agent: {name}")
        return {"name": name, **agents[name]}

    def model(self, model_id: str) -> dict[str, Any]:
        models = self.get("models", {})
        if model_id not in models:
            raise KeyError(f"Ukjent modell i config: {model_id!r}")
        return {"id": model_id, **models[model_id]}

    def agent_prompt(self, name: str) -> str:
        user = self.home / "agents" / f"{name}.md"
        if user.exists():
            return user.read_text(encoding="utf-8")
        builtin = resources.files("olympus.defaults").joinpath("agents", f"{name}.md")
        return builtin.read_text(encoding="utf-8") if builtin.is_file() else ""

    def is_downloaded(self, model_id: str) -> bool:
        try:
            return self.model_path(model_id).exists()
        except KeyError:
            return False

    def model_path(self, model_id: str) -> Path:
        m = self.model(model_id)
        return self.path("models_dir") / m["repo"].replace("/", "__") / m["file"]

    def agent_color(self, name: str) -> str:
        return self.theme.get("agents", {}).get(name, self.theme["ui"]["accent"])

    def soul(self) -> str:
        p = self.home / "SOUL.md"
        return p.read_text(encoding="utf-8") if p.exists() else _default_text("SOUL.md")


def load() -> Config:
    home = ensure_home()
    defaults = tomllib.loads(_default_text("config.toml"))
    user = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    theme = deep_merge(
        tomllib.loads(_default_text("theme.toml")),
        tomllib.loads((home / "theme.toml").read_text(encoding="utf-8")),
    )
    return Config(data=deep_merge(defaults, user), theme=theme, home=home)


def parse_value(raw: str) -> Any:
    """Tolk en CLI-verdi som TOML (tall, bool, liste), ellers som streng."""
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def set_value(dotted: str, raw: str) -> Any:
    """Sett en nøkkel i brukerens config.toml og behold kommentarer."""
    value = parse_value(raw)
    set_values({dotted: value})
    return value


def set_values(values: dict[str, Any], home: Path | None = None) -> None:
    """Skriv ferdig tolkede verdier (punktnøkler) til brukerens config.toml og behold kommentarer.
    Skrives atomisk (tmp + rename), så en samtidig leser aldri ser en halv fil."""
    home = home or ensure_home()
    path = home / "config.toml"
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    for dotted, value in values.items():
        *parents, leaf = dotted.split(".")
        node: Any = doc
        for part in parents:
            if part not in node:
                node[part] = tomlkit.table()
            node = node[part]
        node[leaf] = value
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    tmp.replace(path)


def saved_value(home: Path, dotted: str) -> Any:
    """Verdien brukeren har lagret i config.toml (None hvis den ikke er satt der)."""
    try:
        node: Any = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# Mediemapper: norsk og engelsk navn. Den første som finnes brukes; finnes ingen, velges språket systemet bruker.
MEDIA_DIRS = {
    "music": ("~/Musikk", "~/Music"),
    "film": ("~/Filmer", "~/Videos/Movies"),
    "show": ("~/Serier", "~/Videos/TV"),
    "audiobook": ("~/Lydbøker", "~/Audiobooks"),
    "ebook": ("~/Bøker", "~/Books"),
    "podcast": ("~/Podkaster", "~/Podcasts"),
}


def media_dir(c: Config, kind: str) -> Path:
    """media_server.<kind>_dir fra config, ellers standardmappa for denne maskinen."""
    import locale
    import os

    v = c.get(f"media_server.{kind}_dir", "")
    if v:
        return Path(v[0] if isinstance(v, list) else v).expanduser()
    nb, en = (Path(p).expanduser() for p in MEDIA_DIRS[kind])
    if nb.exists():
        return nb
    if en.exists():
        return en
    lang = (os.environ.get("LANG") or locale.getlocale()[0] or "").lower()[:2]
    return nb if lang in ("nb", "no", "nn") else en
