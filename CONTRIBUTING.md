# Contributing

1. Create a focused branch.
2. Keep personal data, API keys, cookies, logs, and model files out of commits.
3. Add or update tests for behavior changes.
4. Run:

```powershell
uv sync --frozen --no-dev
.venv\Scripts\python.exe -m compileall -q app scripts
.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

5. Open a pull request describing the change and validation performed.

By contributing, you agree that your contribution is licensed under the MIT License.
