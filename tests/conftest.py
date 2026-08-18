"""Global pytest isolation from external observability services."""

from __future__ import annotations

import os


# Tests exercise the real workflow tracing paths with scripted runtimes. Keep
# those paths local even when a developer's repository .env has live keys.
os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
