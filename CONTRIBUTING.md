# Contributing to this fork

This fork of [segwaynavimow/NavimowHA](https://github.com/segwaynavimow/NavimowHA)
maintains a patched build of the integration for personal use, while
tracking upstream and preparing potential contributions back. The layout
mirrors the sibling S2000 fork ([raouldekezel/dolphin-robot](https://github.com/raouldekezel/dolphin-robot))
so the same conventions apply here.

## Branches

- **`main`** — strict mirror of `upstream/main`. Fast-forward only. Never
  a place to commit personal changes. Used to detect patches absorbed
  upstream via `git cherry main deploy`.
- **`deploy`** — cut from an upstream tag (`UPSTREAM_BASE`, currently
  `NavimowHA-v1.1.0`). Carries the linear sequence of squash commits from
  merged PRs. HACS points to tags on this branch.
- **`patches/<id>-<slug>`** — one patch per branch, cut from
  `origin/deploy`. Ephemeral (deleted at squash-merge).

## Nomenclature

Every issue and branch uses one of these prefixes:

| Prefix   | Meaning                                                             |
| -------- | ------------------------------------------------------------------- |
| **SEC**  | Security — secret leak in logs, credential handling                 |
| **BUG**  | Bug fix (auth, MQTT, coordinator, staleness, races)                 |
| **HARD** | Hardening — robustness, edge cases, missing guards                  |
| **MAP**  | Data mapping — incomplete enum / label / translation                |
| **FEAT** | Feature — new user-visible capability, no underlying bug            |
| **CHORE**| Repo/CI/tests/docs infrastructure                                   |
| **SPIKE**| Time-boxed investigation with an artefact                           |

Issue titles: `<ID>: <short description>`. Branch names:
`patches/<id-lowercase>-<slug-en>`.

## Language

| Location                                                | Language |
| ------------------------------------------------------- | -------- |
| GitHub (issue/PR titles, bodies, comments, labels)      | English  |
| Commit messages, branch names, slugs                    | English  |
| Source code (comments, docstrings, identifiers)         | English  |
| Personal doc IT (Gitea), conversation with the operator | French   |

## Merges

Squash-only. `delete_branch_on_merge=true`. Squash commits inherit `(#NN)`
from GitHub; they ARE the atomic patch that will be replayed by
`git rebase --onto NEW_BASE OLD_BASE deploy` at each upstream sync.

## Tags & releases

- Tag format: `<upstream-tag>-raoul.<n>` (upstream uses release-please
  tags shaped `NavimowHA-vX.Y.Z`, so our tags look like
  `NavimowHA-v1.1.0-raoul.1`).
- Releases on GitHub: `gh release create --prerelease` (mandatory — HACS
  reads `/releases`, not raw tags; a tag pushed without a Release is
  invisible to HACS).

## Tests

Docker-ephemeral, never `pip install` on the host:

```
docker run --rm -v ~/NavimowHA:/work -w /work python:3.12-slim bash -c "
  pip install -q -r requirements-test.txt
  python -m pytest tests/ -v
"
```

Then clean up the `__pycache__` created as root:

```
docker run --rm -v ~/NavimowHA:/work -w /work alpine sh -c \
  "find /work -name __pycache__ -type d -exec rm -rf {} +; chown -R $(id -u):$(id -g) /work"
```

Test naming: `tests/test_<id>_<slug>.py`. Each test should be red against
the unpatched code and green after the patch.

**No source tests.** A test asserts observable behaviour (return values,
log records, `hasattr`, entity state changes), not the text of a source
file. Forbidden: `inspect.getsource(...) + re.search(...)`,
`Path(src).read_text() + re.findall`, any `open("...py").read()` grep.

| Invariant to pin | Correct alternative |
| --- | --- |
| Attribute / method removed | `assert not hasattr(Cls, "X")` |
| SDK called with the right value | `unittest.mock.patch(...)` + `.assert_called_with(...)` |
| Log emitted | `caplog.records` filtered by level/message |
| Data structure (translations, manifests, YAML) | read the **data file** (JSON/YAML) — that's not source, it's the contract |
| Structural import (« symbol no longer referenced anywhere ») | AST tooling (`ast.parse` + walk), not a textual grep |

Source-level greps lock the *syntax* rather than the *semantics*: a
whitespace-equivalent refactor breaks the test, and a semantically
equivalent workaround (rename, aliased constant) passes the test
without preserving the behaviour. Neither is what we want.

Rare exception: an audit-secrets tripwire, filed as its own decision
with a clear rationale (see the sibling S2000 fork's `test_sec_debug_secrets.py`
kept under CHORE-02 / issue #77).

## Diagnostic sessions

Raw evidence lives under `docs/diag/<date>_<id>_<topic>/`. See
[`docs/diag/README.md`](docs/diag/README.md) for the required structure
of `findings.md`, PII redaction rules, and the drift-proof index.
