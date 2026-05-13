# Development
## Daily Workflow

```bash
# Add a dependency
uv add <package>

# Add a dev dependency
uv add --dev <package>

# Remove a dependency
uv remove <package>

# Run a script
uv run python <script.py>

# Run tests
uv run pytest

# Lint & format
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy .
```

## After pulling changes

```bash
uv sync
```
