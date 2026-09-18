"""Small, user-editable configuration for the standalone VAS scanner."""

# Command name on PATH or an executable path. Set to None to let preflight
# install a private ast-grep CLI under <skill-dir>/.tool/ast_grep.
AST_GREP_CLI_PATH = "ast-grep"

# A file needs at least one Anchor at this weight to become a task.
ADMISSION_QUERY_WEIGHT = 2

# The main Agent schedules at most this many candidate tasks in one run.
MAX_CANDIDATES = 20

# The main Agent may run this many task subagents concurrently.
CONCURRENCY = 3

# None writes runs under <repository>/.vas. Set a path to keep scanner runs elsewhere.
WORKSPACE_DIR = None
