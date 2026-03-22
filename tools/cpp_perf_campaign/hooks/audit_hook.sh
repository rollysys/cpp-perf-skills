#!/bin/bash
# Audit hook for Claude Code tool calls.
# Only active when CPP_PERF_AUDIT=1.
# Writes JSONL to $CPP_PERF_AUDIT_LOG (defaults to $CPP_PERF_CASE_DIR/audit.jsonl).

[ "$CPP_PERF_AUDIT" != "1" ] && exit 0

INPUT=$(cat)

LOG_PATH="${CPP_PERF_AUDIT_LOG:-${CPP_PERF_CASE_DIR:+${CPP_PERF_CASE_DIR}/audit.jsonl}}"
[ -z "$LOG_PATH" ] && exit 0

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

RECORD=$(echo "$INPUT" | jq -c --arg ts "$TIMESTAMP" '{
  ts: $ts,
  event: .hook_event_name,
  tool: .tool_name,
  agent_id: .agent_id,
  input: .tool_input,
  response: .tool_response
}')

echo "$RECORD" >> "$LOG_PATH"
exit 0
