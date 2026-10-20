# Contributing to Beacon

Thanks for helping. A few rules keep pull requests easy to merge.

## Open pull requests against `dev`, not `main`

All work lands on `dev` first. `main` only receives `dev` when a release is
cut, and after that `dev` is moved forward to match `main` automatically. A
pull request merged straight into `main` breaks that step, so it will be
retargeted to `dev` before it is merged.

When you open the pull request, set **base** to `dev`. If you already opened
it against `main`, click **Edit** next to the title and change the base branch.

Start your branch from the current `dev`:

```bash
git fetch origin
git switch -c my-fix origin/dev
```

Code on `dev` can differ a lot from `main`. Check that what you are fixing
still looks the same on `dev`.

## Before you open it

From the `web/` folder, with Python 3.12:

```bash
pip install -r requirements.txt -r requirements-dev.txt ruff
ruff check .
python -m pytest -ra
```

CI runs the same checks, plus a Docker build and smoke test.

- Add a test for a bug fix that fails without the fix.
- Add user-facing changes to the **Unreleased** section of
  [CHANGELOG.md](CHANGELOG.md), in the style of the entries already there.
- Keep one change per pull request. If a fix closes an issue, write
  `Fixes #<number>` in the description.
