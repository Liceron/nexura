# Project templates: init, dev and deploy

Three commands take you from an empty folder to a running local Studio and a Docker recipe:

```bash
nexura-runtime init my-agent --template agent   # write a small, runnable project
cd my-agent && pytest                            # its offline tests
nexura-runtime dev                               # local service + Studio, project registered
nexura-runtime deploy --target docker            # write Dockerfile, .dockerignore, DEPLOY.md
```

Every generated project runs offline on a scripted `FakeModel` until you configure a model, and
ships tests that prove it. Related: [Quickstart in 10 minutes](quickstart-10-minutes.md),
[Service and Studio](studio-and-service.md), [Starter templates](templates.md) (workflow
templates you instantiate inside Studio, a different thing).

## `nexura-runtime init`

```bash
nexura-runtime init --list
nexura-runtime init DIR [--template agent|workflow|rag] [--name PACKAGE] [--force] [--no-env-example]
```

| Template | What you get |
| --- | --- |
| `agent` (default) | A support agent with a read tool (`lookup_order`), a write tool (`issue_refund`) that pauses for approval before every call, and a token and cost budget. A terminal loop asks you to approve or reject. |
| `workflow` | A two-step workflow: an agent drafts a reply, the run pauses before `send`, and a person approves or rejects. Checkpoints are in SQLite, so a paused run resumes from a new process (`python -m PACKAGE start` / `approve`). |
| `rag` | Ingests `docs/` into the hybrid knowledge index and answers with numbered citations; replies "I could not find this in the documents." when nothing matches. |

Each project has:

- `pyproject.toml` depending on `nexura-runtime[studio]`, `nexura-studio` and `nexura-agents`.
  Nexura is not on PyPI yet; a comment there and the README explain how to install from the
  repository's wheels (`uv build --all-packages`, then `pip install --find-links DIST -e ".[dev]"`).
- The package (named after the directory: `my-agent` gives `my_agent`; `--name` overrides
  it), a `__main__.py` you can run with `python -m`, and `studio.py`, which `dev` registers.
- `tests/` with offline tests on the scripted model. Nexura's own test suite generates every
  template and runs these tests in a subprocess.
- `README.md` with next steps, `.gitignore` (keeps `.env` and `.nexura/` out of Git) and
  `.env.example` with empty values (skip it with `--no-env-example`).

`init` refuses a directory that is not empty. With `--force` it adds the template's files next
to what is there, but an existing file is never overwritten: it is listed as kept on stderr.
Files are created exclusively, and nothing is written through a symbolic link that leaves the
project.

The same thing from Python:

```python
import tempfile
from pathlib import Path

from nexura_runtime.project import TEMPLATES, scaffold_project

print(sorted(TEMPLATES))
with tempfile.TemporaryDirectory() as tmp:
    result = scaffold_project(Path(tmp) / "refund-bot", "workflow")
    assert result.package == "refund_bot"
    assert (result.root / "refund_bot" / "workflow.py").is_file()
    assert (result.root / "tests" / "test_workflow.py").is_file()
```

### The RAG template's embedder

The `rag` project uses Nexura's deterministic **test embedder** unless `NEXURA_EMBEDDINGS` is
set. It is labelled non-semantic: reproducible for tests, but it does not understand meaning,
so the project's search tool uses keyword (BM25) ranking only while it is in use. Set
`NEXURA_EMBEDDINGS` (for example `openai:text-embedding-3-small` with `OPENAI_API_KEY`) for
hybrid keyword and semantic search; see [Knowledge](knowledge.md). An index remembers its
embedder, so build a fresh index after changing it.

## `nexura-runtime dev`

Run it in the project directory (or pass `--project DIR`). It starts, in one process:

- the REST service from `nexura-runtime serve` on `http://127.0.0.1:8765` (`--port`), which
  `studio-web` and the TypeScript client call, and
- the bundled Studio from the `nexura-studio` package on `http://127.0.0.1:8000`
  (`--studio-port`); open this one in your browser. `--no-studio` starts the service alone,
  and without `nexura-studio` installed only the service starts, with a note.

Both are bound to `127.0.0.1`; `dev` has no `--host` option. Data lives in `.nexura/` in the
project (`--data-dir`). Ctrl-C stops both.

When `pyproject.toml` has a `[tool.nexura]` table (every template writes one), `dev` imports the
handler sources and workflows it names and registers them in both servers:

```toml
[tool.nexura]
template = "workflow"
handlers = ["my_app.studio:registry"]     # same format as --handlers
workflows = ["my_app.studio:workflows"]   # dict of workflow id -> WorkflowSpec
```

A workflow is created under its id the first time and updated when its spec changes, so the
Studio list never fills with copies. These entries name the project's own trusted code;
workflow JSON never chooses what is imported. Without the table, `dev` starts with the
built-in demo modules and says so.

The agent template's agent appears in Studio as a two-step workflow: `propose` runs the agent
until it asks to call `issue_refund` (nothing is refunded), and `refund` performs exactly that
call after you approve the step.

## `nexura-runtime deploy --target docker`

Writes `Dockerfile`, `.dockerignore` and `DEPLOY.md` into the project. It never runs Docker.
Other targets (Kubernetes, Helm, serverless) print the supported list and exit with code 2:
the open-source edition is single user on one machine. Existing files are not replaced
without `--force`, and each replaced file is listed.

The image installs the project as an unprivileged user and runs
`nexura-runtime dev --container`: the bundled Studio alone, listening on `0.0.0.0` inside the
container. That mode refuses to start without `NEXURA_STUDIO_TOKEN`, and every request must
then carry the token. The REST service is not started in the container, because the
open-source service has no authentication. `DEPLOY.md` repeats the caveats from
`SECURITY_MODEL.md`: publish the port on `127.0.0.1` only
(`docker run -p 127.0.0.1:8000:8000 -e NEXURA_STUDIO_TOKEN ...`), never add
`--insecure-allow-unauthenticated-network`, and treat the token as one shared secret with
full authority. Until Nexura is on PyPI, build with the wheels copied into `./wheels/` and
`--build-arg PIP_FIND_LINKS=/app/wheels`. The generated Dockerfile has not been built in this
project's tests (they check its content only).

## With the `nexura` command

The `nexura` command belongs to the Nexura Cloud client, whose own `init` and `deploy`
always win. With both installed, these commands are `nexura project init|dev|deploy` and
`nexura dev` (the same parser and options as `nexura-runtime`).
