"""Modellvalget: hvilken modell Zeus svarer med.

Zeus er alltid hjernen på PC-en (minne, verktøy, plan, godkjenninger og økter blir her). Brukeren velger bare hvilken
modell som skriver svarene: en nedlastet lokal modell, eller en skymodell fra en provider som er koblet til. Valget
lagres som agents.zeus.model i ~/.olympus/config.toml og gjelder til det byttes tilbake.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from olympus.backends.llamacpp import BackendError
from olympus.config import ZEUS, Config, saved_value, set_values
from olympus.model_info import describe
from olympus.providers import LOCAL, Backends, list_models, parse_ref

LOCAL_ALIASES = ("lokal", "local")


class ModelCatalog:
    """Valgene: lokale modeller som er lastet ned + skymodeller fra tilkoblede providers. Skymodellene i config
    (providers.<p>.models) vises alltid; i tillegg hentes providerens egne lister (mellomlagret en time)."""

    TTL = 3600.0

    def __init__(self, config: Config, backends: Backends) -> None:
        self.config = config
        self.backends = backends
        self.cache: dict[str, list[str]] = {}
        self.errors: dict[str, str] = {}
        self.fetched = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self, provider: str | None = None) -> None:
        if provider:
            self.cache.pop(provider, None)
            self.errors.pop(provider, None)
        else:
            self.cache.clear()
            self.errors.clear()
        self.fetched = 0.0

    async def refresh(self, force: bool = False) -> None:
        """Hent modellistene fra OpenAI-kompatible providers med nøkkel (bare listene, ingen samtale sendes)."""
        async with self._lock:
            if not force and self.cache and time.monotonic() - self.fetched < self.TTL:
                return
            for prov, spec in self.config.get("providers", {}).items():
                if spec.get("type") != "openai" or spec.get("cost") == "betalt":
                    continue
                key = self.backends.api_key(prov)
                if not key:
                    continue
                try:
                    self.cache[prov] = await list_models(spec, key)
                    self.errors.pop(prov, None)
                except (BackendError, OSError, ValueError) as e:
                    self.errors[prov] = str(e)
                except Exception as e:  # noqa: BLE001 – nettverksfeil fra httpx o.l.
                    self.errors[prov] = f"{type(e).__name__}: {e}"
            self.fetched = time.monotonic()

    def _cloud_names(self, prov: str, spec: dict[str, Any]) -> list[str]:
        names = list(spec.get("models", []))
        if spec.get("type") == "openai":
            names += self.cache.get(prov, [])
        names = [n for n in dict.fromkeys(names) if describe(self.config, f"{prov}/{n}").chat]
        flt = spec.get("model_filter")
        return [n for n in names if not flt or flt in n]

    def choices(self) -> list[dict[str, Any]]:
        """[{id, label, cloud, provider, cost, size?, note}] – lokale først, så skyen gruppert per provider."""
        out: list[dict[str, Any]] = []
        for mid, m in self.config.get("models", {}).items():
            if m.get("kind") == "llm" and self.config.is_downloaded(mid):
                item = {"id": mid, "label": f"{mid} · lokal", "cloud": False, "provider": "lokal", "cost": "gratis",
                        "note": describe(self.config, mid).description.split(". ")[0]}
                if m.get("size_gb"):
                    item["size"] = f"{float(m['size_gb']):.1f} GB"
                out.append(item)
        for prov, spec in self.config.get("providers", {}).items():
            cost = spec.get("cost", "betalt")
            if cost == "betalt" or not self.backends.available(f"{prov}/x")[0]:
                continue  # betalt per token vises ikke; providers uten nøkkel/program heller ikke
            label = spec.get("label", prov)
            for name in self._cloud_names(prov, spec):
                ref = f"{prov}/{name}"
                note = cost + (" · kan trene på det du sender" if spec.get("may_train") else "")
                out.append({"id": ref, "label": f"{label}: {name}", "cloud": True, "provider": label, "cost": cost,
                            "note": note})
        cur = current(self.config)
        if cur and cur not in {c["id"] for c in out} and self.backends.available(cur)[0]:
            cloud = self.backends.is_cloud(cur)
            out.insert(0 if not cloud else len(out), {
                "id": cur, "label": cur, "cloud": cloud, "provider": self.backends.provider_label(cur) if cloud else "lokal",
                "cost": self.backends.cost(cur), "note": "valgt nå"})
        return out

    def find(self, ref: str) -> dict[str, Any] | None:
        return next((c for c in self.choices() if c["id"] == ref), None)


def current(config: Config) -> str:
    return str(config.get(f"agents.{ZEUS}.model", "") or "")


def local_model(config: Config) -> str:
    return str(config.get("general.local_model", "") or "")


def resolve_choice(catalog: ModelCatalog, wanted: str) -> str | None:
    """«lokal» → den lokale modellen; ellers id-en hvis den finnes blant valgene (ukjent = None)."""
    wanted = wanted.strip()
    if wanted.lower() in LOCAL_ALIASES:
        wanted = local_model(catalog.config)
    return wanted if catalog.find(wanted) is not None else None


def set_model(config: Config, ref: str) -> None:
    """Bytt modellen Zeus svarer med, og lagre valget i brukerens config.toml (overlever omstart).
    Velges en lokal modell, blir den også den lokale «tilbake til»-modellen."""
    values: dict[str, Any] = {f"agents.{ZEUS}.model": ref}
    is_local = parse_ref(ref, config.get("providers", {}))[0] == LOCAL
    if is_local:
        values["general.local_model"] = ref
    set_values(values, config.home)
    config.data.setdefault("agents", {}).setdefault(ZEUS, {})["model"] = ref
    if is_local:
        config.data.setdefault("general", {})["local_model"] = ref


def sync_saved(config: Config) -> bool:
    """Ta inn et modellbytte som er lagret av en annen prosess (TUI-en i lokal modus, `olympus config set`).
    Bare verdier brukeren faktisk har lagret, tas inn. True = noe endret seg."""
    changed = False
    for dotted in (f"agents.{ZEUS}.model", "general.local_model"):
        v = saved_value(config.home, dotted)
        if isinstance(v, str) and v and config.get(dotted) != v:
            head, leaf = dotted.rsplit(".", 1)
            node = config.data
            for part in head.split("."):
                node = node.setdefault(part, {})
            node[leaf] = v
            changed = True
    return changed


def cloud_warning(config: Config, backends: Backends, ref: str) -> str:
    """Én linje som sier hva som forlater PC-en når samtalen går til en skymodell (tom for lokale modeller)."""
    if not backends.is_cloud(ref):
        return ""
    who = backends.provider_label(ref)
    memory = " sammen med minnet ditt (USER.md/MEMORY.md)" if config.get("memory.share_with_cloud", True) else ""
    train = f" {who} kan bruke det til å trene modeller." if backends.trains_on_data(ref) else ""
    return (f"☁ Sky: samtalen sendes nå til {who} ({ref}){memory}. Verktøy, plan og godkjenninger blir på PC-en, og "
            f"det som leses fra PC-en krever oftere ja.{train} Tilbake til lokalt: /modell lokal.")


def describe_model(config: Config, backends: Backends, ref: str) -> str:
    """«qwen36-27b» eller «☁ groq/openai/gpt-oss-120b · sky – samtalen sendes ut»."""
    return f"☁ {ref} · sky – samtalen sendes ut" if backends.is_cloud(ref) else ref
