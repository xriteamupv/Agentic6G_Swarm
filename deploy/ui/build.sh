#!/usr/bin/env bash
# Production build of the operator chat UI: agent-chat-ui as a self-contained
# Next.js server in var/chat-ui, served by deploy/ui/a6g-chat-ui.service.
#
#   deploy/ui/build.sh                                   # API URL / assistant id from the UI's .env
#   NEXT_PUBLIC_API_URL=http://edge:2024 deploy/ui/build.sh
#
# NEXT_PUBLIC_* values are baked into the browser bundle at build time (the environment
# overrides the UI's .env). Secrets never go into the bundle: the .env Next copies into
# the standalone output is removed; runtime secrets (only needed for the /api passthrough)
# come from ~/.config/a6g/chat-ui.env.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UI_DIR="${UI_DIR:-$REPO/chat-ui}"
OUT="${OUT:-$REPO/var/chat-ui}"
NODE_BIN="${NODE_BIN:-$(command -v node)}"

cd "$UI_DIR"
[ -d node_modules ] || pnpm install --frozen-lockfile
pnpm build

STAGE="$OUT.new"
rm -rf "$STAGE"
mkdir -p "$STAGE/.next"
cp -a .next/standalone/. "$STAGE/"
rm -f "$STAGE"/.env*                       # keep secrets out of the deployed bundle
cp -a .next/static "$STAGE/.next/static"   # standalone output does not include these
[ -d public ] && cp -a public "$STAGE/public"
cat > "$STAGE/start.sh" <<START
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec "$NODE_BIN" server.js
START
chmod +x "$STAGE/start.sh"

# Swap in the new build (the old one is kept until the new one is in place).
rm -rf "$OUT.old"
[ -d "$OUT" ] && mv "$OUT" "$OUT.old"
mv "$STAGE" "$OUT"
rm -rf "$OUT.old"

if grep -rqE 'lsv2_[a-z]+_[0-9a-f]{12,}' "$OUT/.next" "$OUT/server.js"; then
  echo "ERROR: a LangSmith key ended up in the build ($OUT); remove it before serving" >&2
  exit 1
fi
echo "chat UI built in $OUT (start: $OUT/start.sh, PORT/HOSTNAME from the environment)"
