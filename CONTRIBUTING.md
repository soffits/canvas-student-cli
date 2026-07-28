# Contributing

Thank you for improving `canvas-student-cli`.

Use `uv` for development:

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run python -m build
```

Keep runtime dependencies in the Python standard library unless there is a strong reason to add one. Preserve compact JSON success output for existing commands. Do not add Canvas write commands, course audit features, live Canvas tests, tokens, deployment details, or institution-specific URLs.
