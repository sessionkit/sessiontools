#!/bin/bash
# Claude Code status line, from sessiontools (src/sessiontools/statusline/).
# `session statusline install` copies this file into the sessionkit claude-code
# layer, ~/.sessionkit/tools/settings/statusline.sh, and points statusLine
# there at it; `session start` passes that to claude. The copy is yours to edit;
# `session statusline check --diff` shows your edits against this source.
# Doc: docs/status-line-script.md in the sessiontools repo.
#
# Shows: [agent] | repo | used/max (pct%) | $cost | 5h:X% wk:Y% | model
#
# Reads Claude Code's JSON session data on stdin, and local files only: no
# network. Every helper fails soft: a missing, slow or failing
# helper still leaves a line, and the script exits 0.
#
# Fields read:
#   .agent.name                              (optional, only with --agent)
#   .model.display_name / .model.id
#   .workspace.current_dir (fallback .cwd)
#   .context_window.context_window_size      (200000 or 1000000)
#   .context_window.used_percentage          (pre-calculated, may be null early)
#   .context_window.current_usage            (null before first API call)
#   .context_window.total_input_tokens / .total_output_tokens  (log only)
#   .cost.total_cost_usd
#   .rate_limits.{five_hour,seven_day}.used_percentage (subscribers only)
#   .session_id                              (log only)

# The switches. Set them in the environment Claude Code runs in, or edit the
# defaults here. off, 0, false and no mean off.
#   TAP: feed .rate_limits to ccusage-limits-tap, when it is on PATH
#   LOG: append one record per render to ~/.config/session/statusline-log.jsonl,
#        only when ~/.config/session already exists (never created here)
#   LOG_MAX: bytes; past it the log moves to statusline-log.jsonl.1 (one generation)
TAP=${SESSIONTOOLS_STATUSLINE_TAP:-on}
LOG=${SESSIONTOOLS_STATUSLINE_LOG:-on}
LOG_MAX=${SESSIONTOOLS_STATUSLINE_LOG_MAX:-26214400}

is_on() {
  case $1 in
    [Oo][Ff][Ff] | 0 | [Ff][Aa][Ll][Ss][Ee] | [Nn][Oo]) return 1 ;;
  esac
  return 0
}

input=$(cat)

# Feed vendor rate-limits to the tap. Backgrounded, and the whole
# group's streams go to /dev/null: a backgrounded child holding stdout keeps
# the pipe open, and a reader waiting for EOF would wait for the tap.
if is_on "$TAP" && command -v ccusage-limits-tap >/dev/null 2>&1; then
  { printf '%s' "$input" | ccusage-limits-tap; } >/dev/null 2>&1 &
fi

# Without jq, or with input that is not a JSON object, say so instead of
# rendering a wrong line.
if ! command -v jq >/dev/null 2>&1; then
  printf '%s\n' "Claude | statusline: jq not found"
  exit 0
fi
if ! printf '%s' "$input" | jq -e 'type == "object"' >/dev/null 2>&1; then
  printf '%s\n' "Claude | statusline: bad input"
  exit 0
fi

AGENT=$(echo "$input" | jq -r '.agent.name // empty')
MODEL=$(echo "$input" | jq -r '.model.display_name // "Claude"')
CWD=$(echo "$input" | jq -r '.workspace.current_dir // .cwd // empty')
MAX=$(echo "$input" | jq -r '.context_window.context_window_size // 200000')
PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)
COST=$(echo "$input" | jq -r '.cost.total_cost_usd // 0')

# Compute tokens in context = input + cache_creation + cache_read
# (matches the formula used by used_percentage)
# A count that is not a number counts as 0, so the sum never errors.
USED=$(echo "$input" | jq -r '
  def n: if type == "number" then . else 0 end;
  (.context_window.current_usage // {}) as $u
  | if ($u | type) != "object" then 0 else
      ($u.input_tokens | n)
      + ($u.cache_creation_input_tokens | n)
      + ($u.cache_read_input_tokens | n)
    end
')

# A value that is not a whole number would break the tests below; read it as 0.
case $PCT in '' | *[!0-9]*) PCT=0 ;; esac

# Format numbers as K / M (e.g., 45234 -> "45K", 1234567 -> "1.2M")
fmt_tokens() {
  local n=$1
  case $n in '' | *[!0-9]*) n=0 ;; esac
  if [ "$n" -ge 1000000 ]; then
    awk -v n="$n" 'BEGIN { printf "%.1fM", n/1000000 }'
  elif [ "$n" -ge 1000 ]; then
    awk -v n="$n" 'BEGIN { printf "%.0fK", n/1000 }'
  else
    printf "%d" "$n"
  fi
}

USED_FMT=$(fmt_tokens "$USED")
MAX_FMT=$(fmt_tokens "$MAX")

# Color the percentage by threshold
# 0-49% green, 50-79% yellow, 80%+ red
if [ "$PCT" -ge 80 ]; then
  COLOR=$'\033[31m' # red
elif [ "$PCT" -ge 50 ]; then
  COLOR=$'\033[33m' # yellow
else
  COLOR=$'\033[32m' # green
fi
RESET=$'\033[0m'
DIM=$'\033[2m'

# Repo name: the nearest directory up from cwd holding .git (a directory, or
# the .git FILE of a worktree or submodule), else cwd's own name. Builtins
# only: no git fork, so a slow repo cannot stall the line.
REPO=""
if [ -n "$CWD" ] && [ -d "$CWD" ]; then
  d=$CWD
  while [ "${d%/}" != "$d" ] && [ "$d" != "/" ]; do d=${d%/}; done
  REPO=${d##*/}
  [ "$d" = "/" ] && REPO="/"
  while [ -n "$d" ] && [ "$d" != "/" ]; do
    if [ -e "$d/.git" ]; then
      REPO=${d##*/}
      break
    fi
    case $d in
      */*) d=${d%/*} ;;
      *) break ;;
    esac
  done
fi

# Format cost: $0.00 up to $9.99, then $10 / $123
fmt_cost() {
  awk -v c="$1" 'BEGIN {
    if (c < 10) printf "$%.2f", c
    else printf "$%.0f", c
  }'
}
COST_FMT=$(fmt_cost "$COST")

# Build output: [agent] | repo | used/max (pct%) | $cost | model
# Order: agent name most prominent on left, model on far right (least important)
SEP="${DIM}|${RESET}"

if [ -n "$AGENT" ]; then
  agent_part="[${AGENT}]"
else
  agent_part="Claude"
fi

# Session (5h) and weekly plan-% from vendor .rate_limits, present
# only for subscribers after the first API response; hide segment when absent
FH_PCT=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty' | cut -d. -f1)
WK_PCT=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty' | cut -d. -f1)

repo_part=""
if [ -n "$REPO" ]; then
  repo_part=" ${SEP} ${REPO}"
fi

limits_part=""
if [ -n "$FH_PCT" ] || [ -n "$WK_PCT" ]; then
  limits_part=" ${SEP} ${DIM}5h:${FH_PCT:-?}% wk:${WK_PCT:-?}%${RESET}"
fi

# The per-render log. Other tools read it (a seat's running model, by sid), so
# the record schema is fixed. Skipped unless ~/.config/session exists. The
# group is backgrounded with every stream silenced, so a slow jq never holds
# the status line's stdout. Past LOG_MAX the file moves to .1 before the append.
LOG_DIR="$HOME/.config/session"
if is_on "$LOG" && [ -d "$LOG_DIR" ]; then
  L="$LOG_DIR/statusline-log.jsonl"
  {
    if [ -f "$L" ] && [ "$(wc -c <"$L")" -gt "$LOG_MAX" ]; then mv -f "$L" "$L.1"; fi
    printf '%s' "$input" | jq -c --arg t "$(date +%s)" '{t:($t|tonumber), sid:.session_id, model:.model.id, cost:.cost.total_cost_usd, ctx:.context_window.current_usage, pct:.context_window.used_percentage, size:.context_window.context_window_size, tin:.context_window.total_input_tokens, tout:.context_window.total_output_tokens}' >>"$L"
  } >/dev/null 2>&1 &
fi

# The line is data, never a printf format: a % or \ in a repo or model name
# prints as itself.
printf '%s\n' "${agent_part}${repo_part} ${SEP} ${USED_FMT}/${MAX_FMT} ${COLOR}(${PCT}%)${RESET} ${SEP} ${DIM}${COST_FMT}${RESET}${limits_part} ${SEP} ${DIM}${MODEL}${RESET}"
