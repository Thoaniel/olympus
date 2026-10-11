"""Korte beskrivelser av modeller, så det er lettere å velge modell for Zeus (/modell).

Lokale modeller beskrives i config (`description`, `good_for`). Skymodellene endrer seg
ofte, så de gjenkjennes på navnet (modellfamilie) i stedet for å listes én og én.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from olympus.config import Config


@dataclass(frozen=True)
class ModelInfo:
    description: str
    good_for: frozenset[str] = field(default_factory=frozenset)
    chat: bool = True  # False: tale, embedding, moderering … kan ikke brukes til samtale


ALL = frozenset({"code", "linux", "write"})

# (mønster, beskrivelse, good_for). Første treff vinner, så det spesifikke står først.
FAMILIES: list[tuple[str, str, frozenset[str]]] = [
    # ikke chatmodeller (bilde, video, musikk, tale, sanntid, embedding, sikkerhet …)
    (r"whisper|tts|speech|native-audio|transcri|embed|guard|moderation|safeguard|content-safety|"
     r"imagen|veo|image|nano-banana|lyria|robotics|computer-use|-live|live-|orpheus|playai|aqa|rerank|deep-research",
     "Ikke en chatmodell (bilde, video, musikk, tale, sanntid, embedding eller sikkerhet). Kan ikke brukes til samtale.",
     frozenset()),
    # Claude via Claude Code
    (r"^claude-code/opus", "Claude Opus. Sterkest på vanskelig kode, store endringer og lang resonnering. Bruker mest av abonnementskvoten.", ALL),
    (r"^claude-code/sonnet", "Claude Sonnet. Rask og svært god på både kode og tekst. Godt standardvalg som sparer kvote.", ALL),
    (r"^claude-code/haiku", "Claude Haiku. Raskest og bruker minst kvote. Enkle spørsmål og korte tekster.", frozenset({"write", "linux"})),
    # kode-spesialister
    (r"qwen.*coder|coder.*qwen", "Qwen Coder. Laget for programmering og verktøybruk.", frozenset({"code"})),
    (r"poolside|laguna", "Poolside Laguna. Laget spesielt for programmering.", frozenset({"code"})),
    (r"devstral|codestral", "Mistral sin kodemodell. Laget for programmering.", frozenset({"code"})),
    (r"coder|-code\b|-code:", "Kodemodell. Laget for programmering.", frozenset({"code"})),
    (r"kimi|\bk2\b", "Moonshot Kimi K2. Sterk på agentisk koding og verktøybruk.", frozenset({"code", "linux"})),
    (r"glm", "Zhipu GLM. God på kode og agentoppgaver.", frozenset({"code", "linux"})),
    # resonnering
    (r"deepseek.*r1|r1-distill", "DeepSeek R1. Tenker lenge før den svarer. Sterk på vanskelige problemer, men treg.", frozenset({"code", "linux"})),
    (r"deepseek", "DeepSeek V3. Sterk allrounder, god på kode.", ALL),
    (r"gpt-oss-120b", "OpenAI gpt-oss 120B (åpen). Sterk resonnering og kode. Et av de beste gratisvalgene.", frozenset({"code", "linux"})),
    (r"gpt-oss", "OpenAI gpt-oss 20B (åpen). Rask, grei på kode og systemspørsmål.", frozenset({"code", "linux"})),
    (r"nemotron.*(ultra|super)", "NVIDIA Nemotron Super/Ultra. Stor modell med sterk resonnering og kode.", frozenset({"code", "linux"})),
    (r"nemotron", "NVIDIA Nemotron (liten). Rask, trent for resonnering og instruksjoner.", frozenset({"linux"})),
    # Google
    (r"gemini.*omni", "Gemini Omni. Multimodal allrounder (tekst, bilde og lyd inn).", frozenset({"write"})),
    (r"gemini.*flash-lite|flash-lite", "Gemini Flash-Lite. Rask og har den største gratiskvoten. Enkle oppgaver og oppsummering.", frozenset({"write"})),
    (r"gemini.*flash", "Gemini Flash. Rask allrounder med godt norsk, men begrenset gratiskvote. Nyere versjonsnummer er bedre.", ALL),
    (r"gemini.*pro", "Gemini Pro. Den sterkeste Gemini-modellen, men svært lav gratiskvote (få forespørsler per dag).", ALL),
    (r"antigravity", "Google Antigravity (forhåndsvisning). Ser ut til å være laget for agentisk koding, men er uprøvd her.", frozenset()),
    (r"gemma", "Google Gemma 4 (åpen). God på tekst og grei på kode. 26B-A4B er raskest (MoE).", frozenset({"write"})),
    # Meta
    (r"llama-?4.*maverick", "Meta Llama 4 Maverick. Stor allrounder, god på tekst og kode.", ALL),
    (r"llama-?4.*scout", "Meta Llama 4 Scout. Rask allrounder med lang kontekst.", frozenset({"write", "linux"})),
    (r"llama.*(70b|405b)", "Meta Llama 70B+. God allrounder med solid engelsk og middels norsk.", frozenset({"write", "linux"})),
    (r"llama.*(8b|3b|1b)|instant", "Liten Llama. Veldig rask, men bare for enkle spørsmål.", frozenset()),
    # Qwen generelt
    (r"qwen.*(235b|480b|max|plus)", "Stor Qwen. Sterk på kode, resonnering og flere språk.", ALL),
    (r"qwen", "Qwen. God på kode og flere språk, også norsk.", frozenset({"code", "write"})),
    # Mistral
    (r"mistral.*(large|medium)|magistral", "Mistral Large/Medium. God europeisk allrounder.", frozenset({"write", "linux"})),
    (r"mistral|mixtral|ministral", "Mistral (liten). Rask, grei på tekst.", frozenset({"write"})),
    # små/spesielle
    (r"allam", "ALLaM. Spesialisert på arabisk, lite nyttig på norsk.", frozenset()),
    (r"lfm|liquid", "Liquid LFM (svært liten). Rask, men bare for enkle oppgaver.", frozenset()),
    (r"phi", "Microsoft Phi (liten). Enkle oppgaver.", frozenset()),
]


def describe(config: Config, ref: str) -> ModelInfo:
    """Beskrivelse av en modellreferanse (lokal id eller provider/modell)."""
    local = ref.removeprefix("local/")
    spec = config.get("models", {}).get(local)
    if spec is not None:
        return ModelInfo(
            spec.get("description", "Lokal modell."),
            frozenset(spec.get("good_for", [])),
        )
    name = ref.lower()
    for pattern, text, good in FAMILIES:
        if re.search(pattern, name):
            return ModelInfo(text, good, chat="Ikke en chatmodell" not in text)
    return ModelInfo("Ukjent modell. Prøv den, og bytt hvis svarene ikke holder mål.")
