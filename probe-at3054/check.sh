#!/bin/bash
# AT-3054 probe. Prints "<name><TAB><result>" per target, nothing else.
# Results are readable|denied|absent for paths, present|absent for variable
# names. No target's content or any variable's value is ever printed: a file
# is opened and closed without a read, a directory listing goes to
# /dev/null, and a variable is tested with ${!v+set}, which expands to a
# fixed word, never to the value.
#
# usage: check.sh --env NAME... --path PATH... [--write PATH]
set -u

check_path() {
  local p=$1
  if [ ! -e "$p" ] && [ ! -L "$p" ]; then
    echo absent
  elif [ -d "$p" ]; then
    if ls -A -- "$p" >/dev/null 2>&1; then echo readable; else echo denied; fi
  else
    if { : < "$p"; } 2>/dev/null; then echo readable; else echo denied; fi
  fi
}

mode=
for arg in "$@"; do
  case "$arg" in
    --env|--path|--write) mode=$arg; continue ;;
  esac
  case "$mode" in
    --env)
      if [ -n "${!arg+set}" ]; then r=present; else r=absent; fi
      printf 'env %s\t%s\n' "$arg" "$r"
      ;;
    --path)
      printf '%s\t%s\n' "$arg" "$(check_path "$arg")"
      ;;
    --write)
      # Control: a write outside the workspace must be denied when the
      # sandbox is engaged. The file is empty and removed if it was made.
      if : > "$arg" 2>/dev/null; then r=allowed; rm -f -- "$arg"; else r=denied; fi
      printf 'write %s\t%s\n' "$arg" "$r"
      ;;
  esac
done
