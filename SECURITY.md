# Security policy

MPC Forge is a local application: by default it only listens on `127.0.0.1`
and rejects requests with an unexpected `Host` header (DNS rebinding) or from
another origin (CSRF).

## Opening it to your network

`--host 0.0.0.0` (or any non-loopback address) exposes the app to your
network. In that mode:

- an access key is required. One is generated at startup and printed as a
  link (`http://host:port/?token=…`); pass your own with `--token` or
  `MPC_FORGE_ACCESS_TOKEN`, or disable it with `--token off` only on a network
  you fully trust;
- the key is stored in an `HttpOnly`, `SameSite=Strict` cookie and can be sent
  by API clients as `Authorization: Bearer <key>`;
- the `/api/debug` endpoints (log download) are not served;
- the host names other devices use must be listed in
  `MPC_FORGE_ALLOWED_HOSTS`.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/AdrianJimenezSantiago/mtg-forge/security/advisories/new)
with the steps to reproduce and the version affected. You will get an answer
within a week.
