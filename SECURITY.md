# Security policy

Repository: [Liceron/nexura](https://github.com/Liceron/nexura).

## Reporting a vulnerability

Email **info@liceron.in** with the subject line `SECURITY: nexura-runtime`.
Please include the affected version, a description, reproduction steps and the
impact you expect. Do not open public issues for suspected vulnerabilities.

We aim to acknowledge reports within 3 business days and to agree on a disclosure
date with you before publishing a fix.

## Scope

In scope: the Python package (`src/nexura_runtime`), the REST service, the
TypeScript client and Studio web.

Known boundaries (documented behavior, not vulnerabilities):

- The `local-owner` service profile is single-user and intended for `127.0.0.1`.
  It is not safe to expose on a network.
- The `authenticated-shared-service` profile has not had an external security review.
- Handlers and modules you register (including `--handlers` sources) run with your
  process's privileges.
- Mock-transport tests do not prove the behavior of live provider APIs.

## Supported versions

Only the latest `0.x` release receives fixes.
