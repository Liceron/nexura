# Third-Party Notices and Open Source Software Licenses

This file lists the third-party software that the open-source Nexura packages depend on or
bundle. The packages themselves are Liceron's own code under the Apache License 2.0
("Copyright (c) 2026 Liceron"); see `LICENSE`.

| Package | Where it lives | Licence |
| --- | --- | --- |
| `nexura-runtime` (Python) | `src/nexura_runtime` | Apache-2.0, Liceron |
| `nexura-agents` (Python) | `packages/nexura-agents` | Apache-2.0, Liceron |
| `nexura-connectors` (Python) | `packages/nexura-connectors` | Apache-2.0, Liceron |
| `nexura-guardrails` (Python) | `packages/nexura-guardrails` | Apache-2.0, Liceron |
| `nexura-postgres` (Python) | `packages/nexura-postgres` | Apache-2.0, Liceron |
| `nexura-qdrant` (Python) | `packages/nexura-qdrant` | Apache-2.0, Liceron |
| `nexura-sandbox` (Python) | `packages/nexura-sandbox` | Apache-2.0, Liceron |
| `nexura-studio` (Python) | `packages/nexura-studio` | Apache-2.0 AND OFL-1.1, Liceron and font authors |
| `nexura-vertex` (Python) | `packages/nexura-vertex` | Apache-2.0, Liceron |
| `@liceron/nexura-sdk` (TypeScript client) | `clients/typescript` | Apache-2.0, Liceron |
| Nexura Studio web app | `studio-web` | Apache-2.0, Liceron |

Licences below were read from the installed package metadata (`importlib.metadata` for Python,
each package's `package.json` and `LICENSE` file under `studio-web/node_modules` for npm) in this
environment on 2026-10-18, except where a row says otherwise. Versions are the ones locked in
`uv.lock` and `studio-web/package-lock.json` at that date. Each dependency remains governed by
its own licence and notices; this list does not replace them.

---

## Python: `nexura-runtime` core dependencies

| Dependency | Licence | Copyright / project | Repository | Use |
| --- | --- | --- | --- | --- |
| `nexura-agents` | Apache-2.0 | Liceron (first party) | this repository | Agent runtime library |
| `httpx` | BSD-3-Clause | Encode OSS Ltd. | https://github.com/encode/httpx | HTTP transport |
| `pydantic` | MIT | Samuel Colvin and contributors | https://github.com/pydantic/pydantic | Typed models and validation |
| `packaging` | Apache-2.0 OR BSD-2-Clause | PyPA | https://github.com/pypa/packaging | Version parsing |
| `cryptography` | Apache-2.0 OR BSD-3-Clause | Individual contributors to pyca/cryptography | https://github.com/pyca/cryptography | AES-GCM secrets store, trigger signing keys, RS256 token verification (core since 0.4.0) |

## Python: `nexura-runtime` optional extras

| Extra | Dependency | Licence | Copyright / project | Repository | Use |
| --- | --- | --- | --- | --- | --- |
| `studio` | `fastapi` | MIT | Sebastián Ramírez | https://github.com/fastapi/fastapi | Local service and Studio API |
| `studio` | `uvicorn` | BSD-3-Clause | Encode OSS Ltd. | https://github.com/encode/uvicorn | ASGI server |
| `knowledge` | `pypdf` | BSD-3-Clause | Mathieu Fenniak, Martin Thoma and contributors | https://github.com/py-pdf/pypdf | Bounded PDF text extraction |
| `sandbox` | `pydantic-monty` (with `pydantic-monty-client` and `pydantic-monty-runtime`) | MIT | Pydantic project (installed files carry no copyright line) | https://github.com/pydantic/monty | Default code-sandbox backend |
| Operator-installed runtime | CPython 3.13.15 WASI | PSF-2.0 (and bundled notices in the archive's `LICENSE`) | Python Software Foundation and contributors | https://github.com/python/cpython | Full pure-Python standard library; archive SHA256 `67e1c32a85d5e0600c0939f5fd4023ca0127f1a9e81f5499697467bda67af78d`; not included in the SDK wheel |
| Runtime build tooling | `brettcannon/cpython-wasi-build` | MIT | Brett Cannon and contributors | https://github.com/brettcannon/cpython-wasi-build | Upstream build/release source; no build-script source copied into the SDK |

`secrets` is an empty extra kept for compatibility; `cryptography` is now a core dependency.

`pydantic-monty` 1.1.0 installs about 10 MB of package files plus a ~32 MB `monty` worker binary
(measured in this environment, 2026-10-18). It is not part of the core install
(`pip install "nexura-runtime[sandbox]"`).

## Python: `nexura-agents` dependencies

| Kind | Dependency | Licence | Repository | Use |
| --- | --- | --- | --- | --- |
| core | `httpx` | BSD-3-Clause | https://github.com/encode/httpx | HTTP transport |
| core | `pydantic` | MIT | https://github.com/pydantic/pydantic | Typed models |
| core | `typing-extensions` | PSF-2.0 | https://github.com/python/typing_extensions | Typing backports |
| extra `litellm` | `litellm` | MIT (upstream `LICENSE`; the upstream repository's `enterprise/` directory is licensed separately) | https://github.com/BerriAI/litellm | Optional multi-provider model adapter |
| extra `otel` | `opentelemetry-api` | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python | Optional tracing |

`litellm` is not installed in this environment, so its licence was not read from local metadata.
Check the metadata of the version you install.

## Python: split integration packages

All split packages depend on the matching first-party runtime and/or agents version;
those shared dependencies are attributed above. Their additional dependencies are:

| Distribution / extra | Dependency | Licence | Repository | Use |
| --- | --- | --- | --- | --- |
| `nexura-connectors` | `httpx`, `pydantic` | BSD-3-Clause, MIT respectively | https://github.com/encode/httpx and https://github.com/pydantic/pydantic | Transport and validation |
| `nexura-connectors` | `httpcore` | BSD-3-Clause | https://github.com/encode/httpcore | HTTP connection handling |
| `nexura-connectors[postgres]`, `nexura-postgres` | `asyncpg` | Apache-2.0 | https://github.com/MagicStack/asyncpg | PostgreSQL driver |
| `nexura-qdrant` | `qdrant-client` | Apache-2.0 | https://github.com/qdrant/qdrant-client | Vector backend client |
| `nexura-vertex` | `httpx` | BSD-3-Clause | https://github.com/encode/httpx | Provider HTTP transport |
| `nexura-vertex` | `google-auth[requests]` | Apache-2.0 | https://github.com/googleapis/google-auth-library-python | Vertex AI credentials |
| `nexura-guardrails[yaml]` | `pyyaml` 6.0.3 (licence read 2026-10-22) | MIT | https://github.com/yaml/pyyaml | YAML policy files (optional) |
| `nexura-studio` | `fastapi`, `uvicorn`, `pydantic` | MIT, BSD-3-Clause, MIT respectively | https://github.com/fastapi/fastapi, https://github.com/encode/uvicorn and https://github.com/pydantic/pydantic | Studio service and validation |
| `nexura-sandbox[wasi]` | `wasmtime` 49.0.0 | Apache-2.0 WITH LLVM-exception | https://github.com/bytecodealliance/wasmtime-py | WASI interpreter; native runtime outside the service process |

`nexura-sandbox` core adds no third-party requirements beyond runtime, and `nexura-guardrails`
core none beyond `nexura-agents` (its `judge` extra is empty). SQLAlchemy
(MIT, https://github.com/sqlalchemy/sqlalchemy) belongs to the root development
group; it is not a dependency of the published Postgres package. `authlib`
(BSD-3-Clause, https://github.com/authlib/authlib) and `joserfc` (BSD-3-Clause,
https://github.com/authlib/joserfc) belong to the separate closed edition and are
not dependencies or extras of these open-source distributions.

`packaging/sbom.json` inventories the exact versions from `uv.lock` for all eight
published Python packages and all their extras, including transitive dependencies.
It unions platform marker branches and excludes development groups, npm and OS
packages. Regenerate offline with `python scripts/generate_sbom.py`.

## Bundled assets in the Python wheels and Studio web

### Noto Sans Devanagari font

- **Files**: `packages/nexura-studio/src/nexura_studio/static/fonts/NotoSansDevanagari.ttf`
  (shipped in the `nexura-studio` wheel), with
  `OFL.txt` and `PROVENANCE.md` beside it.
  The same unmodified font and notices are also bundled in `studio-web/public/fonts/`
  and copied into the OSS and enterprise Studio web distributions at build time.
- **Licence**: SIL Open Font License, Version 1.1 (full text in `OFL.txt`).
- **Copyright**: Copyright 2022 The Noto Project Authors (https://github.com/notofonts/devanagari).
- **Source**: Google Fonts repository, `ofl/notosansdevanagari`, obtained 2026-10-04, unmodified
  (`PROVENANCE.md` records the original filename).
- **Use**: Local Devanagari fallback for the bundled Studio page. No external font service is
  contacted.

The rest of the bundled Studio page (`nexura_studio/static/app.js`) is first-party code with no
third-party runtime libraries. (The embeddable chat widget moved to the closed edition with
publishing.)


### Manrope variable Latin font

- File: `studio-web/public/fonts/manrope-latin-wght-normal.woff2`.
- Source: Fontsource's `@fontsource-variable/manrope` 5.3.0 distribution,
  `files/manrope-latin-wght-normal.woff2`
  (https://www.npmjs.com/package/@fontsource-variable/manrope/v/5.3.0).
  Upstream font project: https://github.com/sharanda/manrope.
- Copyright 2019 The Manrope Project Authors.
- Licence: SIL Open Font License 1.1, retained in `OFL-Manrope.txt` beside the font.
- Copied unmodified from the existing enterprise web asset for the Studio
  wordmark in Round 2 (2026-10-27); no download or font modification in Round 3.
- SHA-256: `a30ddcd349703aff7464c34bef3fffdff405ee50c113440d7c8693c02d210972`.
- Served locally from `/fonts/`; no external font service is contacted. This
  asset belongs to the Studio web distribution, not the Python Studio wheel.

## TypeScript client: `@liceron/nexura-sdk`

No runtime dependencies. `typescript` (Apache-2.0, Microsoft Corporation) is a development
dependency used to compile it.

## Studio web app: `studio-web` runtime dependencies

These are bundled into `studio-web/dist` when you build Studio (`npm run build`). The Studio web
bundle is not shipped inside the Python wheel or sdist. Development-only tools (Vite, Vitest,
ESLint, Playwright, Testing Library, TypeScript, type packages) are not bundled and are not listed.

| Dependency | Version | Licence | Copyright (from its `LICENSE`) | Repository |
| --- | --- | --- | --- | --- |
| `react` | 18.3.1 | MIT | Facebook, Inc. and its affiliates | https://github.com/facebook/react |
| `react-dom` | 18.3.1 | MIT | Facebook, Inc. and its affiliates | https://github.com/facebook/react |
| `scheduler` | 0.23.2 | MIT | Facebook, Inc. and its affiliates | https://github.com/facebook/react |
| `loose-envify` | 1.4.0 | MIT | Andres Suarez | https://github.com/zertosh/loose-envify |
| `js-tokens` | 4.0.0 | MIT | Simon Lydell | https://github.com/lydell/js-tokens |
| `@xyflow/react` | 12.12.0 | MIT | webkid GmbH | https://github.com/xyflow/xyflow |
| `@xyflow/system` | 0.0.83 | MIT | webkid GmbH | https://github.com/xyflow/xyflow |
| `zustand` | 4.5.7 | MIT | Paul Henschel | https://github.com/pmndrs/zustand |
| `use-sync-external-store` | 1.7.0 | MIT | Meta Platforms, Inc. and affiliates | https://github.com/facebook/react |
| `classcat` | 5.0.5 | MIT | Jorge Bucaran | https://github.com/jorgebucaran/classcat |
| `d3-color`, `d3-dispatch`, `d3-drag`, `d3-interpolate`, `d3-selection`, `d3-timer`, `d3-transition`, `d3-zoom` | 3.x | ISC | Mike Bostock | https://github.com/d3 |
| `d3-ease` | 3.0.1 | BSD-3-Clause | Mike Bostock; Robert Penner (easing equations) | https://github.com/d3/d3-ease |
| `@liceron/nexura-sdk` | 0.5.0 | Apache-2.0 | Liceron (first party, `clients/typescript`) | this repository |

---

## Test infrastructure

The optional Linux `bubblewrap` backend invokes operator-installed `bwrap` and
`/usr/bin/prlimit` as separate programs; neither is embedded or linked into the SDK.
Local package evidence: `/usr/share/doc/bubblewrap/copyright` identifies bubblewrap as
LGPL-2+ (Alexander Larsson, Simon McVittie, Collabora); upstream source is
https://github.com/containers/bubblewrap/. The util-linux `prlimit` executable is GPL-2.0-or-later
according to `/usr/share/doc/util-linux/copyright`. No third-party source was copied.
Seccomp accepts an optional operator-generated native BPF file; no libseccomp Python
bindings, compiled policy or new Python dependency is included.

The gVisor runtime and Python OCI root filesystem used for sandbox integration verification are
operator-managed test infrastructure; they are not embedded in the SDK wheel. Their test
runtime/image provenance is recorded in the repair report.

## Upstream reference and provenance

- **Nexura V2** (`/home/ubuntu/projects/Nexura V2/src/nexura-sdk`): read-only reference, no code
  copied. No copied-file provenance entry exists for it in this repository. Zero Langflow, zero
  `lfx` and zero parent-workspace dependencies. If V2-derived code is ever added, its notice
  ("MIT License, Copyright (c) 2024 Langflow") must travel with that code.
- Studying upstream designs is not permission to copy source. Any
  future copied file must be recorded here with its upstream URL, commit, path, SHA-256 and full
  licence before it is merged.
