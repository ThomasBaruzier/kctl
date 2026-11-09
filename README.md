# kctl

Save your Keimyung University CTL courses for offline reading and search.

kctl downloads course pages and attachments, extracts supported files, runs
English OCR, and creates one text file per course. Original files are kept.

## Setup

Requires Linux or another supported POSIX system, Python 3.12 or 3.13, and
[uv](https://docs.astral.sh/uv/).

From the project directory:

```sh
uv sync
cp .env.example .env
chmod 600 .env
```

The example includes a public Sectigo intermediate certificate and its SHA-256
fingerprint for CTL. Normally, only these optional login fields need editing:

- `KCTL_STUDENT_ID`: your student ID.
- `KCTL_PASSWORD`: your password.

A saved session is reused when possible. Otherwise, kctl uses configured
credentials or prompts in an interactive terminal.

If CTL's certificate chain changes, update `KCTL_INTERMEDIATE_PEM` and
`KCTL_INTERMEDIATE_SHA256` together using a trusted source. Existing `.env` files
are not updated automatically.

## Run

```sh
uv run kctl
```

This downloads, processes, and exports your courses into `dump/`.
Run it again to update the archive. Unchanged results are reused.

Submission files are included by default. To exclude them:

```sh
uv run kctl sync --no-submissions
```

## Find your files

- `dump/extracted/`: one searchable TXT file per course.
- `dump/documents/index.md`: browse course pages and original files.
- `dump/.ctl/`: internal state; do not edit.

Each course TXT ends with Extraction notes explaining known limitations.

## Useful commands

| Command | Task |
| --- | --- |
| `uv run kctl sync` | Update everything |
| `uv run kctl process` | Process downloaded files offline |
| `uv run kctl export` | Rebuild TXT exports without conversion |
| `uv run kctl status` | Check login status |
| `uv run kctl --help` | Show options |

Use another archive directory or select a course by its CTL ID:

```sh
uv run kctl sync ./semester
uv run kctl sync --course COURSE_ID
```

Directory and course selection also work with `process` and `export`.
Local commands do not need credentials or certificate settings.

## Limits and recovery

OCR can misread text. Diagrams, layouts, SmartArt, and interactive content may
not be fully captured. Check originals for important details.

A partial result means available content was kept with limitations. Older usable
output may be retained when a new conversion is incomplete. Missing or damaged
inputs can prevent an export from being replaced.

After an interruption, rerun the command. Do not delete `dump/` or use `--force`
just to clear warnings.

## Privacy

Keep `.env`, `dump/`, and saved login state private. They can contain passwords,
grades, feedback, and submissions.

Use a separate archive for each account. `uv run kctl forget` removes saved
local authentication state, not your archive or `.env`.
