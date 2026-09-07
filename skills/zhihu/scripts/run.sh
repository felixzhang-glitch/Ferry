#!/bin/sh
set -eu

fail_package() {
  printf '{"ok":false,"error":{"code":"INVALID_PACKAGE","message":"%s"}}\n' "$1"
  exit 7
}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SKILL_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
MANIFEST="$SKILL_DIR/manifest.json"
[ -f "$MANIFEST" ] || fail_package "manifest.json is missing"
SKILL_VERSION=$(sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST" | head -n 1)
MIN_VERSION=$(sed -n 's/.*"min_version":[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST" | head -n 1)
[ -n "$SKILL_VERSION" ] || fail_package "manifest version is empty"
[ -n "$MIN_VERSION" ] || fail_package "manifest cli.min_version is empty"
if [ -n "${ZHIHU_CLI_HOME:-}" ]; then
  CLI_HOME=$ZHIHU_CLI_HOME
  if [ "$(uname -s)" = Linux ]; then
    case "$CLI_HOME" in /*) ;; *) fail_package "ZHIHU_CLI_HOME must be an absolute path" ;; esac
  fi
else
  case "$(uname -s)" in
    Darwin)
      [ -n "${HOME:-}" ] || fail_package "HOME is required"
      CLI_HOME="$HOME/Library/Application Support/zhihu-cli"
      ;;
    Linux)
      [ -n "${HOME:-}" ] || fail_package "HOME is required"
      DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
      case "$DATA_HOME" in /*) ;; *) fail_package "XDG_DATA_HOME must be an absolute path" ;; esac
      CLI_HOME="$DATA_HOME/zhihu-cli"
      ;;
    *) fail_package "unsupported operating system" ;;
  esac
fi
BINARY="$CLI_HOME/current/zhihu-cli"

installed_version() {
  "$BINARY" version 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'
}

version_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    split(a, x, /[.-]/); split(b, y, /[.-]/)
    for (i=1; i<=3; i++) {
      if ((x[i]+0) > (y[i]+0)) exit 0
      if ((x[i]+0) < (y[i]+0)) exit 1
    }
    if (a ~ /-/ && b !~ /-/) exit 1
    exit 0
  }'
}

is_ready() {
  [ -x "$BINARY" ] || return 1
  current=$(installed_version)
  [ -n "$current" ] && version_ge "$current" "$MIN_VERSION"
}

if [ "${1:-}" = "status" ]; then
  if is_ready; then
    exec "$BINARY" status --skill-version "$SKILL_VERSION" --min-cli-version "$MIN_VERSION"
  else
    printf '{"ok":true,"installed":false,"skill":{"current_version":"%s","min_cli_version":"%s"},"auth":{"configured":false},"update_check":{"status":"not_applicable"},"next_action":"request_install_consent"}\n' "$SKILL_VERSION" "$MIN_VERSION"
  fi
  exit 0
fi

if [ "${1:-}" = "setup" ]; then
  shift
  exec "$SCRIPT_DIR/setup.sh" "$@"
fi

if ! is_ready; then
  printf '%s\n' '{"ok":false,"error":{"code":"CLI_NOT_INSTALLED","message":"Run scripts/setup.sh after the user approves installation"}}'
  exit 7
fi

exec "$BINARY" "$@"
