# Changelog

## 0.5.3 - 2026-10-25

- Fix GitHub source archives missing the `app.models` package
- Anchor ignored runtime directories to the repository root so source packages are never excluded
- Fail `setup.cmd` immediately if the downloaded source package cannot import
- Add an application import gate to CI

## 0.5.2 - 2026-10-25

- Fix first-start false failure at the launcher's 92% progress ceiling
- Use a dependency-free `/health/live` readiness probe and verify the app identity
- Move inbox, archive, memory, worker, and local-model startup maintenance off the HTTP critical path
- Detect an exited app process immediately instead of waiting for the full timeout
- Raise the app readiness timeout to 180 seconds and allow `APP_READY_TIMEOUT` overrides
- Write `startup-diagnostic.txt` with process state, port listeners, and relevant log tails on failure

## 0.5.1 - 2026-10-25

- Fix uv not being recognized after installation on a clean Windows machine
- Locate uv by absolute path and fall back to the official Windows package when winget/PATH is unavailable
- Add SHA256 verification and automatic user PATH registration for the fallback

## 0.5.0 - 2026-10-25

- Clean-room open-source extraction
- Simplified settings with editable providers, models, chunking, and retrieval
- User data moved to `%LOCALAPPDATA%\ProfileAI`
- One-click installer for app dependencies, LightRAG 1.5.6, official env template, and Ollama
- Persistent LightRAG port selection with Windows reserved-port repair
- Browser-only read-aloud; no bundled speech models
- Simplified six-section settings page
- MIT license and Windows CI
