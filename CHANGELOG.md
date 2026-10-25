# Changelog

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
