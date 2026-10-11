"""Olympus — lokal, selvhostet AI-assistent i terminalen."""

__version__ = "0.1.0"


def main() -> None:
    from olympus.cli import main as cli_main

    raise SystemExit(cli_main())
