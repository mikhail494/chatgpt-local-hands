# Security

Local Hands gives a browser-based AI conversation a privileged execution path onto your computer. Treat broad permissions accordingly.

## Recommended defaults

- Start with `mode: "safe"`.
- Keep `allowed_roots` as narrow as practical.
- Leave shell and process control disabled until you actually need them.
- Do not commit your real `config.json`, tokens, cookies, browser profiles, logs, runtime PID files, or generated output.

## Security properties

- The bridge binds only to `127.0.0.1`.
- Restricted filesystem modes validate canonical resolved paths.
- Shell and process-control capabilities can be disabled independently.
- Request IDs are deduplicated to reduce accidental re-execution.
- The extension parses completed assistant turns and does not execute tool blocks from user messages.
- Emergency STOP pauses extension-side processing and clears the pending queue.

## Important limitation

The bridge is intentionally unauthenticated. Loopback binding prevents remote machines from connecting directly, but **other software already running on the same computer can potentially reach it**.

Use the narrowest permission mode that works for your task.

## Reporting a security issue

Please do not post secrets or working exploit payloads in a public issue. Contact the repository owner privately through GitHub instead.
