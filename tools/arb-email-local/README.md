# ARB Email local MCP

`arb-email-local-mcp` starts a local stdio MCP server exposing `email_send`.

Required environment:

- `ARB_EMAIL_POSTMARK_TOKEN`
- `ARB_EMAIL_SEND_ENABLED=1`

Optional environment:

- `ARB_EMAIL_DEFAULT_TO`
- `ARB_EMAIL_TO_ALLOWLIST`
- `ARB_EMAIL_SEAT_ID`
