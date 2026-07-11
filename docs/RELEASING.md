# Releasing colabapi to PyPI

Once this is set up, `pip install colabapi` and `pipx install colabapi` work for
everyone, and cutting a new version is a two-step ritual.

There are two ways to publish. **Trusted Publishing (recommended)** stores no
secrets. **Manual upload** is the quickest way to get the very first release out.

---

## Option A — Trusted Publishing (recommended, no tokens)

### One-time setup
1. Create a free account at <https://pypi.org/account/register/> and verify your email.
2. Go to <https://pypi.org/manage/account/publishing/> and add a **pending publisher**
   (this pre-authorizes the project before it exists on PyPI):
   - **PyPI Project Name:** `colabapi`
   - **Owner:** `lil-limbo`
   - **Repository name:** `colabapi`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
3. (Optional but recommended) In the GitHub repo, create an **Environment** named
   `pypi` under *Settings → Environments* to gate releases.

### Cutting a release
```bash
# 1. Bump the version in pyproject.toml (e.g. 0.1.0 -> 0.1.1) and commit.
# 2. Tag and push:
git tag v0.1.1
git push origin v0.1.1
# 3. On GitHub: Releases -> Draft a new release -> pick the tag -> Publish.
```
Publishing the GitHub Release triggers `.github/workflows/publish.yml`, which
builds and uploads to PyPI automatically. You can also trigger it by hand from the
Actions tab (**Run workflow**).

---

## Option B — Manual upload (fastest first release)

### One-time setup
1. Create a PyPI account (as above).
2. Create an API token at <https://pypi.org/manage/account/token/> (scope: entire
   account for the first upload; you can narrow it to the project afterwards).

### Upload
```bash
python -m pip install --upgrade build twine
python -m build                     # creates dist/*.whl and dist/*.tar.gz
twine upload dist/*                 # username: __token__   password: <the API token>
```

After the first successful upload you can switch to Option A for everything else.

---

## Version bumps
The single source of truth for the version is `pyproject.toml` (`project.version`).
Keep `colabapi/__init__.py`'s `__version__` in sync. PyPI refuses to re-upload an
existing version, so always bump before publishing.

## Verifying a release
```bash
pipx install colabapi        # or: pip install --user colabapi
colabapi --version
colabapi doctor
```
