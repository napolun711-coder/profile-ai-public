# Security Policy

## Supported versions

Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities or exposed credentials.
Contact the maintainers privately through the repository's Security advisory
feature.

Include the affected version, reproduction steps, and impact. Do not include
real personal documents, API keys, device cookies, or access tokens.

## Local deployment boundary

The default `APP_HOST=127.0.0.1` is intentional. Enabling `0.0.0.0` exposes the
service to the local network and requires device pairing. Do not port-forward
the application to the public internet.
