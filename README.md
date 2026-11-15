# canvas-student-cli

`canvas-student` reads your own [Canvas LMS](https://www.instructure.com/canvas) account from the command line: courses, assignments, grades, submissions and feedback, files, modules, pages, discussions, announcements, the calendar and your inbox. It only ever sends `GET` requests to the Canvas API, prints results as compact JSON on stdout and errors as JSON on stderr. It's made for students, so there are no teacher or admin commands.

The installed executable is `canvas-student`. The import package is `canvas_student_cli`.

## Install

```bash
uv tool install git+https://github.com/soffits/canvas-student-cli.git
```

For local development:

```bash
uv sync --extra dev
uv run canvas-student --version
```

## Configuration

Create a Canvas access token in Canvas account settings. Prefer an owner-readable token file:

```bash
mkdir -p "$HOME/.config/canvas-student-cli"
printf '%s\n' 'your_token_here' > "$HOME/.config/canvas-student-cli/token"
chmod 600 "$HOME/.config/canvas-student-cli/token"
export CANVAS_API_TOKEN_FILE="$HOME/.config/canvas-student-cli/token"
export CANVAS_BASE_URL="https://canvas.example.edu"
```

`CANVAS_API_TOKEN` is also supported. The base URL must be an HTTPS Canvas origin, with plain HTTP accepted only for localhost tests.

## Output

Successful commands print compact JSON on stdout. Errors are compact JSON on stderr with stable categories: `usage`, `config`, `security`, `canvas_http`, `network`, and `internal`.

## Commands

Read commands: `auth_check`, `profile`, `dashboard_cards`, `todo`, `upcoming_events`, `missing_submissions`, `list_courses`, `get_course`, `get_syllabus`, `list_assignments`, `get_assignment`, `list_assignment_groups`, `get_submission`, `list_my_submissions`, `get_submission_feedback`, `get_grades`, `list_quizzes`, `get_quiz`, `list_files`, `get_file`, `list_groups`, `list_group_members`, `list_discussions`, `get_discussion`, `get_discussion_view`, `list_announcements`, `list_modules`, `get_module`, `list_module_items`, `course_structure`, `list_pages`, `get_page`, `calendar_events`, `list_conversations`, `get_conversation`, `conversation_unread_count`, `list_peer_reviews`, `get_submission_peer_reviews`, `user_activity_stream`, `course_activity_stream`, and `search_course_content`.

Group reads: `get_group`, `list_group_files`, `list_group_folders`, `list_group_discussions`, `get_group_discussion`, `get_group_discussion_view`, `list_group_pages`, and `get_group_page`.

Safe file download:

```bash
canvas-student download_file FILE_ID ./file.pdf --max-bytes 50000000
canvas-student download_file FILE_ID ./file.pdf --overwrite
```

The command first reads Canvas file metadata with bearer authentication, then downloads the Canvas-provided file URL without forwarding `Authorization` to that file host. It enforces bounded redirects, rejects unsafe local paths, streams to a private temporary file, enforces size limits, fsyncs, and atomically publishes.

## Filtering lists

List commands accept optional projection and filtering after bounded pagination completes:

```bash
canvas-student list_assignments 123 --fields id,name,due_at --match lab --sort due_at --descending
```

`--fields` keeps comma-separated top-level fields. `--match` searches scalar top-level values case-insensitively. `--sort FIELD` sorts by a top-level field. `--descending` requires `--sort`. Unknown fields and sort keys fail with deterministic usage errors.

`list_conversations` also supports `--context-code` and `--query` as client-side filters while preserving Canvas `--scope` and `--filter` request flags.

## Read-only

Every authenticated Canvas API request is a `GET`; the code has no path that sends anything else. There are no commands that change anything in Canvas, and none of the teacher or admin analytics.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run python -m build
```

Project URL: https://github.com/soffits/canvas-student-cli
