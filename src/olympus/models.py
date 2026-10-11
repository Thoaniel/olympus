"""`olympus models list|pull|rm` — modellfiler på disk."""

from __future__ import annotations

import shutil

from olympus.config import Config


def status_rows(config: Config) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for mid, m in config.get("models", {}).items():
        present = config.model_path(mid).exists()
        rows.append(
            (
                mid,
                m.get("kind", "?"),
                f"{m.get('size_gb', 0):.2f} GB",
                m.get("license", "?"),
                "✓ lastet ned" if present else "– mangler",
            )
        )
    return rows


def pull(config: Config, model_id: str) -> str:
    """Last ned én GGUF-fil fra Hugging Face. Kaller skal ha spurt brukeren først."""
    from huggingface_hub import hf_hub_download

    m = config.model(model_id)
    target_dir = config.model_path(model_id).parent
    target_dir.mkdir(parents=True, exist_ok=True)
    return hf_hub_download(repo_id=m["repo"], filename=m["file"], local_dir=target_dir)


def remove(config: Config, model_id: str) -> bool:
    path = config.model_path(model_id)
    if not path.exists():
        return False
    path.unlink()
    meta = path.parent / ".cache"
    if meta.is_dir() and not any(p.exists() for p in path.parent.glob("*.gguf")):
        shutil.rmtree(meta, ignore_errors=True)
    return True
