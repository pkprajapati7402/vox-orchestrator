# GitHub Actions workflows

These two workflows are the project's CI/CD. They live here rather than in
`.github/workflows/` because the bot account that opened the pull request does not hold
the GitHub App `workflows` permission, so a push containing files under
`.github/workflows/` is rejected by GitHub.

**To activate them**, a repository maintainer runs:

```bash
mkdir -p .github/workflows
git mv ci/github-actions/ci.yml     .github/workflows/ci.yml
git mv ci/github-actions/deploy.yml .github/workflows/deploy.yml
git commit -m "Activate CI/CD workflows"
```

Nothing inside the files needs to change.

## `ci.yml` — on every push and pull request

| Job | What it does |
|---|---|
| `lint` | `ruff check` and `ruff format --check` over `app eval tests` |
| `test` | pytest with coverage on Python 3.11 and 3.12, then `alembic upgrade head` → `alembic downgrade base` |
| `eval` | the persona suite as a **gate**: fails below 90% task completion, 90% correct tool sequence, or above 5% hallucination; also diffs against the committed `eval/reports/baseline.json`, writes the report to the job summary and uploads it as an artifact |
| `docker` | builds the production image with layer caching |

Everything runs with `LLM_PRIMARY_PROVIDER=mock` and SQLite, so CI needs no secrets.

## `deploy.yml` — on merge to `main`

Two independent paths; each skips itself when its secrets are absent, so a fork without
credentials still gets a green pipeline.

| Target | Required repository secrets |
|---|---|
| Koyeb | `KOYEB_API_TOKEN`, `KOYEB_SERVICE` |
| Google Cloud Run | `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `GCP_PROJECT_ID`, `GCP_REGION` |

Cloud Run authenticates through Workload Identity Federation — no service-account JSON
key is ever stored. Deployment details and the reasoning behind `--min-instances 1` are in
[`../../docs/operations.md`](../../docs/operations.md#7-deployment).
