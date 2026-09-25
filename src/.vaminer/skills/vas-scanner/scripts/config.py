"""Small, user-editable configuration for the standalone VAS scanner."""

# Command name on PATH or an executable path. Set to None to let preflight
# auto-install a private ast-grep CLI under <skill-dir>/.tool/ast_grep.
AST_GREP_CLI_PATH = None

# Optional pip index, e.g. Tsinghua: https://pypi.tuna.tsinghua.edu.cn/simple
AST_GREP_INDEX_URL = None

# A file needs at least one Anchor at this weight to become a task.
ADMISSION_QUERY_WEIGHT = 2

# The main Agent schedules at most this many candidate tasks/files in one run.
MAX_CANDIDATES = 50

# The main Agent may run this many task subagents concurrently.
CONCURRENCY = 3

# None writes runs under <repository>/.vas. Set a path to keep scanner runs elsewhere.
WORKSPACE_DIR = None
