#!/usr/bin/env bash
# Assemble l'installeur autonome : en-tête + marqueur + musitag.py
#   bash installer/build.sh musitag.py SORTIE
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
[ $# -eq 2 ] || { echo "usage : bash installer/build.sh musitag.py SORTIE" >&2; exit 2; }
src="$1"
out="$2"
mkdir -p "$(dirname "$out")"
version="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$src" | head -n1)"
[ -n "$version" ] || { echo "VERSION introuvable dans $src" >&2; exit 1; }
sha="$(sha256sum "$src" | cut -d' ' -f1)"
{
  sed -e "s/@VERSION@/$version/" -e "s/@SHA256@/$sha/" "$here/installer-head.sh"
  printf '__MUSITAG_PY__\n'
  cat "$src"
} > "$out"
chmod 755 "$out"
echo "$out  (musitag $version, $(wc -c < "$out") octets)"
