# ADR-0014: Docker Compose project isolation

**Status:** Accepted

## Context

Every repository with a `docker-compose.yml` or `compose.yml` at its worktree root needs a unique
`COMPOSE_PROJECT_NAME` per repo+worktree. Without this, Docker Compose derives the project name
from the current directory's **basename**, and since every repository's primary worktree is
conventionally named `main` (per ADR-0010), two unrelated repos' `main` worktrees collide under
the same Compose project.

This is not theoretical. `docker compose up -d postgres --remove-orphans` run from one
repository's `main` worktree once deleted a healthy, running database container belonging to a
completely different repository's `main` worktree — because Compose believed they were the same
project. This is comparable in shape and blast radius to ADR-0010/0011/0012's other "every repo
must do X" conventions, which is why it warrants its own ADR rather than living only in prose.

## Decision

Every repository with a `docker-compose.yml` or `compose.yml` at its worktree root must add a
`COMPOSE_PROJECT_NAME` export to `worktrees/.envrc`, computed as `<repo-name>-<worktree-name>`
(both lowercased) via `$OLDPWD` (not `$PWD` — direnv evaluates `.envrc` with `$PWD` set to the
`.envrc`'s own directory, not the invoking worktree).

The canonical recipe and implementation details are documented in `prompts/worktree-reference.md`'s
"Docker Compose Project Isolation" section; refer to that doc for the shell snippet and rationale
rather than duplicating it here.

## Consequences

- **Per-repo opt-in:** This is a convention applied when a repository is set up or migrated, not
  enforced by any tooling. Every repo with a compose file must manually add this export to its
  `worktrees/.envrc`.

- **Migration required for existing data:** Migrating an existing repository that already has
  real development data in named volumes (e.g. a live Postgres database under the old, un-namespaced
  project name) requires the volume-migration recipe documented in `prompts/worktree-reference.md`,
  Section "If migrating an existing repo with real dev data in a named volume." The migration
  captures a baseline count from the old database before stopping it, copies data into the new,
  empty volume, and verifies row counts match before cleaning up the orphaned old volume.

- **Depends on worktree layout:** Correct `COMPOSE_PROJECT_NAME` computation depends on the
  worktree-per-branch layout established by ADR-0010 (the `.envrc` computes repo name from the
  worktree parent's parent, and worktree name from `$OLDPWD`). Repositories not following that
  layout will need to adapt the recipe.

- **Bind-mounted storage unaffected:** Repositories using host-path bind-mounts instead of
  named Docker volumes are not affected by project-name changes and need no migration.
