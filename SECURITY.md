# Security policy

## Supported versions

Security updates are provided for the latest release only.

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** form in the repository Security
tab. Do not open a public issue for a vulnerability.

Do not include live Subsonic credentials, signed YouTube media URLs, cookies,
public IP addresses, or a copy of your `.env` file in a report. A minimal
reproduction with placeholder credentials is sufficient.

Mirasonic is designed to bind to loopback and run behind Tailscale, another VPN,
or a TLS-terminating reverse proxy. Publishing its HTTP port directly to the
internet is outside the supported deployment model.
