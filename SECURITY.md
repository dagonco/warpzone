# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub security advisories: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue for them.

Include what you found, how to reproduce it and the version or commit. You can expect an acknowledgement within a week.

## Scope

`warpzone` handles Syncthing API keys and changes Syncthing configuration. Issues that could leak a key (logs, error messages, dry-run output), make it talk to the wrong host, or make it change or delete data outside the folders it manages are in scope.

## Supported versions

Only the latest release receives fixes.
