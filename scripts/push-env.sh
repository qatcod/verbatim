#!/usr/bin/env bash
# Push this shell's tokens to the production RunPod's /opt/verbatim/.env,
# then restart the daemons so they pick up the new values. Idempotent — safe
# to run any time you rotate a token or want to recover from a wiped .env.
#
# Tokens never appear in stdout or in chat. If a variable is unset in your
# current shell, the script prompts you silently for it (input is hidden).
#
# Usage:
#   ./scripts/push-env.sh

set -e

POD_USER=root
POD_HOST=103.196.86.88
POD_PORT=27009
POD_KEY="$HOME/.ssh/id_ed25519"
POD_DB_PATH="/opt/verbatim/state.db"

prompt_if_unset() {
  local var=$1
  local label=$2
  if [ -z "${!var}" ]; then
    echo "  $var not set in shell — paste it now (input hidden):"
    printf "    %s: " "$label"
    IFS= read -rs val
    echo
    if [ -z "$val" ]; then
      echo "  $var still empty. Aborting." >&2
      exit 1
    fi
    export "$var=$val"
  fi
}

echo "--- shell env check ---"
for v in ANTHROPIC_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
  val="${!v}"
  if [ -n "$val" ]; then
    printf "  OK: %s (%d chars, prefix %s...)\n" "$v" "${#val}" "${val:0:5}"
  fi
done

prompt_if_unset ANTHROPIC_API_KEY "ANTHROPIC_API_KEY (sk-ant-...)"
prompt_if_unset SLACK_BOT_TOKEN   "SLACK_BOT_TOKEN (xoxb-...)"
prompt_if_unset SLACK_APP_TOKEN   "SLACK_APP_TOKEN (xapp-...)"

echo
echo "--- pushing .env to ${POD_USER}@${POD_HOST} + restarting daemons ---"
printf 'ANTHROPIC_API_KEY=%s\nSLACK_BOT_TOKEN=%s\nSLACK_APP_TOKEN=%s\nSLACK_TOKEN=%s\nVERBATIM_DB_PATH=%s\n' \
  "$ANTHROPIC_API_KEY" "$SLACK_BOT_TOKEN" "$SLACK_APP_TOKEN" "$SLACK_BOT_TOKEN" "$POD_DB_PATH" \
  | ssh -p "$POD_PORT" -i "$POD_KEY" "${POD_USER}@${POD_HOST}" \
    'cat > /opt/verbatim/.env \
      && chmod 600 /opt/verbatim/.env \
      && echo "--- .env shape on pod ---" \
      && awk -F= "{ printf \"  %-22s = %d chars (prefix %s...)\\n\", \$1, length(\$0)-length(\$1)-1, substr(\$2,1,5) }" /opt/verbatim/.env \
      && echo "--- restarting daemons ---" \
      && supervisorctl restart all \
      && supervisorctl status'

echo
echo "Done. Daemons should be RUNNING. To trigger the email-intel backfill:"
echo "  ssh -p ${POD_PORT} -i ${POD_KEY} ${POD_USER}@${POD_HOST} '/opt/verbatim/run.sh ingest-slack-api --channel email-intelligence-service --since 2026-04-01 --include-loose --dry-run'"
