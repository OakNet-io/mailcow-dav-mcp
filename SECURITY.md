# Security policy

## Supported versions

Only the latest release receives security fixes. Pin a release tag (see the README) and update when a new one is out.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report it privately through GitHub:
[Report a vulnerability](https://github.com/OakNet-io/mailcow-dav-mcp/security/advisories/new).

Include, if you can:

- affected version or commit;
- steps to reproduce, or a proof of concept with synthetic data;
- impact (for example credential exposure, unwanted writes, SSRF, denial of service).

Never include real passwords, app passwords, tokens or personal data from a mailbox.

You will get an answer within a few days. Fixes are released as a new tag and credited in the advisory, unless you prefer to stay anonymous.

## Scope

In scope: this server's code, its handling of DAV responses, local files, the HTTP transport and the documented configuration.

Out of scope: vulnerabilities in mailcow, SOGo, MCP clients or AI models themselves. Report those to their projects.
