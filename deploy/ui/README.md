# Operator chat UI: production deployment (SPEC 10.4)

The operator talks to the coordinator through the customised
[agent-chat-ui](../../../agent-chat-ui). `next dev` is for UI development only: it renders
React in development mode and recompiles on demand, which is much slower on long mission
threads. In production the UI runs as a self-contained Next.js server.

## Build

```bash
deploy/ui/build.sh
```

- Builds `../agent-chat-ui` (`output: "standalone"`) and assembles `var/chat-ui/`
  (`server.js`, static assets, `start.sh`).
- `NEXT_PUBLIC_API_URL` (Agent Server, default `http://localhost:2024`) and
  `NEXT_PUBLIC_ASSISTANT_ID` are baked in at build time: from the UI's `.env`, or the
  environment (`NEXT_PUBLIC_API_URL=http://<edge-host>:2024 deploy/ui/build.sh`). The URL
  must be reachable from the operator's browser.
- No secret goes into the bundle. The `.env` that Next copies into the standalone output
  is removed, and the script fails if a LangSmith key appears in the result.
- Rebuild after changing the UI or its `NEXT_PUBLIC_*` settings.

## Run

```bash
PORT=3000 var/chat-ui/start.sh            # by hand
```

or as a user service next to the coordinator:

```bash
cp deploy/ui/a6g-chat-ui.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now a6g-chat-ui
journalctl --user -u a6g-chat-ui -f
```

Stop `next dev` first if it uses the same port. Behind the edge reverse proxy, forward
the UI's port; the Agent Server (`:2024`) must also be reachable at `NEXT_PUBLIC_API_URL`.

## Kept from the 2026-10-14 streaming fix

- `useStream({ fetchStateHistory: false, throttle: 100 })` and the memoised message
  components (`src/providers/Stream.tsx`, `src/components/thread/`).
- The branch switcher is removed: with history off it could not switch anything (the
  gateway runs every message on the thread's latest state). Edit and regenerate re-submit
  from the latest state.
