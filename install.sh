#!/usr/bin/env bash
# Installe ou met à jour musitag depuis ce dossier (cloné avec git).
#
#   ./install.sh                     installer, ou mettre à jour après un « git pull »
#   ./install.sh --musique /chemin   + dossier musique ouvert au démarrage
#   ./install.sh --desinstaller      tout retirer
#
# Le mot de passe sudo est demandé au moment voulu. Rien n'est écrit dans ce dossier.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

here="$(cd "$(dirname "$(readlink -f -- "$0")")" && pwd)"

uninstall=0
for a in "$@"; do
  case "$a" in
    -h|--help|--aide) sed -n '2,8p' "$here/install.sh" | sed -e 's/^# \{0,1\}//'; exit 0 ;;
    --desinstaller|--uninstall) uninstall=1 ;;
  esac
done

# l'installeur autonome est assemblé dans un dossier temporaire, lancé, puis effacé
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
bash "$here/installer/build.sh" "$here/musitag.py" "$tmp/installer-musitag.sh" >/dev/null
bash "$tmp/installer-musitag.sh" --garder "$@"

if [ "$uninstall" -eq 0 ]; then
  printf '  Mettre à jour plus tard :  cd %q && git pull && ./install.sh\n\n' "$here"
fi
