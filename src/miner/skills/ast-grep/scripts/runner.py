#!/usr/bin/env python3
"""CLI compatibility wrapper for VAMiner's typed ast-grep runner."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from src.miner.tools.ast_grep import AstGrepRunnerError, main, run_ast_grep

__all__ = ["AstGrepRunnerError", "main", "run_ast_grep"]


if __name__ == "__main__":
    main()
