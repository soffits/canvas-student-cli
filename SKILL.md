---
name: canvas-student-cli
description: Read student Canvas LMS data on demand with the canvas-student CLI.
version: 0.1.0
author: Sakina
license: AGPL-3.0-only
platforms: [linux, macos, windows]
prerequisites:
  env_vars: [CANVAS_BASE_URL, CANVAS_API_TOKEN_FILE]
---

# Canvas Student CLI

Use `canvas-student` when a student needs to read their own Canvas courses, assignments, grades, files metadata, groups, discussions, pages, calendar events, conversations, peer reviews, or activity streams.

Successful output is compact JSON on stdout. Errors are compact structured JSON on stderr. Authenticated Canvas API calls are GET-only.

Examples:

```bash
canvas-student auth_check
canvas-student list_courses --enrollment-state active
canvas-student list_assignments 123 --fields id,name,due_at --sort due_at
canvas-student get_group_discussion_view 456 789
canvas-student download_file 555 ./handout.pdf --max-bytes 50000000
```
