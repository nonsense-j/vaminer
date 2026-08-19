"""Run ``python -m src.miner.preflight``."""

from .cli import entrypoint


if __name__ == "__main__":
    raise SystemExit(entrypoint())
