#!/usr/bin/env sh

resolve_storage_endpoint() {
  endpoint="${1:-}"
  case "$endpoint" in
    http://*)
      scheme="http"
      rest="${endpoint#http://}"
      ;;
    https://*)
      scheme="https"
      rest="${endpoint#https://}"
      ;;
    *)
      scheme="https"
      rest="$endpoint"
      ;;
  esac

  host="${rest%%/*}"
  host="${host%%\?*}"
  host="${host%%#*}"
  if [ -z "$host" ]; then
    echo "storage endpoint host is empty" >&2
    return 64
  fi

  printf '%s://%s\n' "$scheme" "$host"
}
