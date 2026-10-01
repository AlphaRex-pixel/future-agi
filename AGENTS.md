# Repository Guidelines

## Project Structure & Module Organization

`futureagi/` contains the Django backend and app-local tests. `frontend/src/` contains the React/Vite UI and tests. `agentcc-gateway/` is the Go gateway. Playwright flows live in `e2e/flows/`; deployment files, generated contracts, and utilities live in `deploy/`, `api_contracts/`, and `scripts/`.

## Build, Test, and Development Commands

- `cp futureagi/.env.example futureagi/.env && docker compose up -d` starts the local stack (API on port 8000; UI on 3031). Use the root Compose file for local services.
- `cd frontend && yarn dev` runs Vite; `yarn build` creates a production build.
- `cd frontend && yarn check-all` runs linting, type checks, and Vitest.
- `cd futureagi && make check-all` runs formatting checks, mypy, and pytest.
- `bin/e2e up && bin/e2e test` starts the isolated stack and executes Playwright flows; use `bin/e2e down` afterward.
- `yarn contracts:check` verifies generated API contracts are current.

## Coding Style & Naming Conventions

Python uses Black, Ruff, isort, mypy, and an 88-character limit. Add type hints where applicable. Frontend code uses ESLint (Airbnb) and Prettier. Run `cd futureagi && make format`, or `cd frontend && yarn lint:fix && yarn prettier`. Use `snake_case` for Python, `PascalCase` for React components, and `camelCase` for JavaScript variables.

Keep comments and docstrings brief; prefer self-explanatory code. Never reference Linear tickets or AI tools anywhere ( code comments , PR description , branch name , issues etc).

Mark work that must be revisited with `# TODO:`.

Only do what is told and keep changes minimal .

## Testing Guidelines

Backend tests use pytest (`test_*.py`); frontend tests use Vitest and React Testing Library (`*.test.jsx` or `__tests__/`). Use `frontend/src/utils/test-utils.jsx` for provider-aware rendering. Frontend global coverage must remain at least 70%. Run relevant tests before and after changes, separating pre-existing failures from regressions. Features and fixes require meaningful behavioral coverage; add Playwright flows for cross-service user behavior.

## Branch, Commit & Pull Request Guidelines

Follow `BRANCH_NAMING_CONVENTION.md`. Branch from `dev` and target PRs to `dev`. Use `<type>/<ticket-id>-<short-description>`, with the ticket ID optional but recommended. Allowed types are `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, and `perf`; use kebab-case and keep the description under 50 characters. Examples: `feat/AUTH-123-user-login` and `fix/session-list-pagination`.

Use short commit subjects in the form `[feat/fix/chore] message`. Do not mention Linear tickets in commits or code comments. Do not mention AI tools in commits, PR descriptions, or comments, and do not add AI `Co-Authored-By` trailers. Keep commits and PRs focused.

Use `.github/PULL_REQUEST_TEMPLATE.md` and target `dev`. Explain what and why, note tests, and include UI screenshots. Link a Linear ticket with `Fixed TH-XXXX`. Update user-facing changelog documentation when applicable; first-time contributors must complete the CLA.

## Agent-Specific Instructions

Keep responses concise unless detail is requested. Do not commit or push unless explicitly asked. Preserve modular design and use type hints or equivalent language features where applicable.

## Security & Configuration

Never commit credentials, tokens, PII, or a populated `.env`. Start from checked-in example configuration and review `SECURITY.md` before reporting vulnerabilities.
