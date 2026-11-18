# Sandbox and threat model

The session sandbox is a deterministic tool harness. It is **not** a secure sandbox for arbitrary code, and it is not described as one.

What it does:

- Starts one MCP server process per session, with a minimal environment that drops parent credential variables.
- Gives that process its own workspace directory. `reset()` deletes the workspace contents and starts a new process.
- Writes a replay log of tool name, arguments, status, and payload.
- Caps framed messages at one megabyte and tool calls at a per-session budget.
- Applies the client read timeout. A unit test uses a short timeout against a slow debug tool that is not on the default allowlist.
- Injects scripted faults: timeout, error, empty, malformed, stale, and conflict.
- Refuses tools that a skill did not declare, and refuses a shell.

What it does not do:

- It does not apply a container, cgroup, seccomp, or memory cap.
- It does not execute code supplied by a user, a document body, or a tool payload.
- It does not make a purchase or open a network connection from the tool server.
- Parallel sessions are isolated from each other by process and directory. They are not isolated from the host user account.

Document and tool text is data. The reply path reads structured facts. A care-note body that says to ignore instructions is returned in the trace as `untrusted_text` and is not used to choose a route or a number.

The in-process pipe server used by one transport test is only for framing. The agent runtime uses the subprocess sandbox.
