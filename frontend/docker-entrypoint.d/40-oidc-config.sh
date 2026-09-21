#!/bin/sh
# Runtime OIDC configuration injection (Prompt 7; runs as an nginx entrypoint
# hook BEFORE nginx starts).
#
# The built bundle inlines config.js with a DEV_DEFAULTS authority of
# http://localhost:8090 (the compose-published provider port). In the
# container we want the SAME-ORIGIN path instead (/oidc proxied by nginx --
# no CORS, works on any host/port the frontend is published on), so the
# entrypoint rewrites that single literal in the built chunk:
#   OIDC_BROWSER_AUTHORITY  - provider URL as reachable from the BROWSER
#   OIDC_CLIENT_ID          - the OAuth client id
set -e

: "${OIDC_BROWSER_AUTHORITY:=/oidc}"
: "${OIDC_CLIENT_ID:=processguard-frontend}"

CONFIG_DIR="/usr/share/nginx/html/assets"
FILE=$(grep -rl '"http://localhost:8090"' "$CONFIG_DIR" 2>/dev/null | head -1)
if [ -n "$FILE" ] && [ "$OIDC_BROWSER_AUTHORITY" != "http://localhost:8090" ]; then
  sed -i "s|http://localhost:8090|${OIDC_BROWSER_AUTHORITY}|g" "$FILE"
  echo "OIDC runtime config: authority=${OIDC_BROWSER_AUTHORITY} (${FILE})"
else
  echo "OIDC runtime config: using built-in authority (dev default)"
fi
