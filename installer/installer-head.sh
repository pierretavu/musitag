#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2329  # (source dynamique, fonctions appelées via run/trap)
# ─────────────────────────────────────────────────────────────────────────────
#  Installeur de musitag — Fedora (et dérivés RHEL) · Debian (et Ubuntu, Mint…)
#
#  Installer :      sudo bash installer-musitag.sh
#  Options :        --musique /chemin   dossier musique ouvert au démarrage
#                   --garder            ne pas supprimer cet installeur à la fin
#                   --desinstaller      tout retirer
#
#  Ce que ça fait : installe Python + les bibliothèques (rich, mutagen, Pillow)
#  et ffmpeg depuis les dépôts officiels, pose la commande « musitag » dans
#  /usr/local/bin, vide le cache des paquets et supprime ses fichiers temporaires
#  (et ce fichier). Relancer l'installeur = mettre à jour musitag.
#  Le programme lui-même est inclus à la fin de ce fichier (lisible en clair).
# ─────────────────────────────────────────────────────────────────────────────
# lancé avec « sh » au lieu de « bash » : on se relance avec bash
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -Eeuo pipefail

MUSITAG_VERSION="@VERSION@"
PAYLOAD_SHA256="@SHA256@"
BIN=/usr/local/bin/musitag
APP_DIR=/opt/musitag                        # seulement si la distribution n'a pas les bibliothèques
LIB_DIR=$APP_DIR/lib                        # (musitag regarde ce dossier tout seul)
CONF=/etc/musitag.conf
OS_RELEASE="${MUSITAG_OS_RELEASE:-/etc/os-release}"

ARGS=("$@")
SELF="$(readlink -f -- "$0" 2>/dev/null || printf '%s' "$0")"

# ── affichage ────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  B=$'\e[1m'; D=$'\e[2m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; M=$'\e[35m'; C=$'\e[36m'; N=$'\e[0m'
else
  B=; D=; R=; G=; Y=; M=; C=; N=
fi
step() { printf '\n%s›%s %s%s%s\n' "$M" "$N" "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
note() { printf '  %s%s%s\n' "$D" "$*" "$N"; }
die()  { printf '\n  %s✗ %s%s\n\n' "$R" "$*" "$N" >&2; exit 1; }

usage() {
  sed -n '4,10p' "$SELF" | sed -e 's/^#  \{0,1\}//' -e 's/^#$//'
}

# ── options ──────────────────────────────────────────────────────────────────
MUSIC=""; KEEP=0; UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --musique)        [ $# -ge 2 ] || die "--musique attend un chemin"; MUSIC="$2"; shift 2 ;;
    --musique=*)      MUSIC="${1#*=}"; shift ;;
    --garder)         KEEP=1; shift ;;
    --desinstaller|--uninstall) UNINSTALL=1; shift ;;
    -h|--help|--aide) usage; exit 0 ;;
    *)                die "option inconnue : $1  (essaie --aide)" ;;
  esac
done

# ── root ─────────────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    if [ "$UNINSTALL" -eq 1 ]; then what="se désinstaller"; else what="s'installer"; fi
    printf '%s♫ musitag%s a besoin des droits administrateur pour %s.\n' "$M" "$N" "$what"
    exec sudo -- bash "$SELF" ${ARGS[@]+"${ARGS[@]}"}
  fi
  die "Lance l'installeur en root :  su -c 'bash $SELF'"
fi

# ── fichiers temporaires : tout dans un dossier effacé quoi qu'il arrive ─────
TMP="$(mktemp -d /tmp/musitag-install.XXXXXX)"
LOG="$TMP/install.log"
: > "$LOG"
cleanup() { rm -rf -- "$TMP"; }
on_error() {
  local rc=$?
  printf '\n  %s✗ Échec de l'"'"'installation (ligne %s).%s\n' "$R" "$1" "$N" >&2
  if [ -s "$LOG" ]; then
    printf '  %sDernières lignes du journal :%s\n' "$D" "$N" >&2
    tail -n 15 "$LOG" | sed 's/^/    /' >&2
  fi
  printf '\n  Rien n'"'"'a été laissé en route ; tu peux corriger puis relancer.\n\n' >&2
  exit "$rc"
}
trap cleanup EXIT
trap 'on_error $LINENO' ERR

# exécute une commande longue avec une petite animation, sortie dans le journal
run() {
  local msg="$1"; shift
  if [ -t 1 ]; then
    "$@" >>"$LOG" 2>&1 &
    local pid=$! i=0
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r  %s%s%s %s' "$C" "${frames[i++ % 10]}" "$N" "$msg"
      sleep 0.12
    done
    printf '\r\033[K'
    wait "$pid"
  else
    "$@" >>"$LOG" 2>&1
  fi
}

# ── désinstallation ──────────────────────────────────────────────────────────
if [ "$UNINSTALL" -eq 1 ]; then
  printf '\n%s♫ musitag%s — désinstallation\n' "$M" "$N"
  step "Suppression"
  removed=0
  for p in "$BIN" "$APP_DIR" "$CONF"; do
    if [ -e "$p" ]; then rm -rf -- "$p"; ok "$p"; removed=1; fi
  done
  for h in /root "$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6 || true)"; do
    if [ -n "$h" ] && [ -d "$h/.config/musitag" ]; then rm -rf -- "$h/.config/musitag"; ok "$h/.config/musitag"; removed=1; fi
  done
  [ "$removed" -eq 1 ] || note "musitag n'était pas installé."
  note "Python, rich, mutagen, Pillow et ffmpeg restent installés : ce sont des paquets standards"
  note "que d'autres programmes peuvent utiliser."
  printf '\n'
  exit 0
fi

printf '\n%s♫ musitag %s%s — installation\n' "$M" "$MUSITAG_VERSION" "$N"

# ── le programme, inclus à la fin de ce fichier ──────────────────────────────
step "Vérification du fichier"
MARK_LINE="$(grep -n -m1 -x '__MUSITAG_PY__' "$SELF" | cut -d: -f1 || true)"
[ -n "$MARK_LINE" ] || die "Fichier incomplet : le programme inclus est introuvable. Récupère l'installeur à nouveau."
tail -n +"$((MARK_LINE + 1))" "$SELF" > "$TMP/musitag.py"
if [ "$(sha256sum "$TMP/musitag.py" | cut -d' ' -f1)" != "$PAYLOAD_SHA256" ]; then
  die "Fichier abîmé (somme de contrôle incorrecte) : il a dû être tronqué ou modifié pendant le transfert."
fi
ok "intact (musitag $MUSITAG_VERSION)"

# ── distribution ─────────────────────────────────────────────────────────────
[ -r "$OS_RELEASE" ] || die "Impossible de reconnaître le système (/etc/os-release absent)."
OS_NAME="$(. "$OS_RELEASE"; printf '%s' "${PRETTY_NAME:-${NAME:-?}}")"
OS_IDS="$(. "$OS_RELEASE"; printf ' %s %s ' "${ID:-}" "${ID_LIKE:-}")"
case "$OS_IDS" in
  *" fedora "*|*" rhel "*|*" centos "*) FAMILY=fedora ;;
  *" debian "*|*" ubuntu "*)            FAMILY=debian ;;
  *) die "Système non pris en charge : $OS_NAME (il faut Fedora ou Debian/Ubuntu)." ;;
esac

if [ "$FAMILY" = fedora ]; then
  if command -v dnf >/dev/null 2>&1; then PM=dnf; elif command -v yum >/dev/null 2>&1; then PM=yum
  else die "Gestionnaire de paquets dnf introuvable."; fi
  CORE=(python3 less python3-rich python3-mutagen python3-pillow)
  FFMPEG=(ffmpeg-free ffmpeg)
  is_installed() { rpm -q "$1" >/dev/null 2>&1; }
  pkg_install()  { "$PM" install -y "$@"; }
  pkg_clean()    { "$PM" clean packages; }
else
  PM=apt-get
  export DEBIAN_FRONTEND=noninteractive
  CORE=(python3 ca-certificates less python3-rich python3-mutagen python3-pil)
  FFMPEG=(ffmpeg)
  is_installed() { [ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null || true)" = installed ]; }
  pkg_install()  { apt-get install -y --no-install-recommends "$@"; }
  pkg_clean()    { apt-get clean; }
fi
step "Système : $OS_NAME"
ok "gestionnaire de paquets : $PM"

# ── paquets ──────────────────────────────────────────────────────────────────
step "Paquets système"
if [ "$FAMILY" = debian ]; then
  run "mise à jour de la liste des paquets…" apt-get update || warn "liste des paquets pas entièrement à jour (on continue)"
fi
MISSING=()
for p in "${CORE[@]}"; do
  if is_installed "$p"; then
    ok "$p ${D}(déjà là)${N}"
  elif run "installation de $p…" pkg_install "$p"; then
    ok "$p"
  else
    MISSING+=("$p")
    warn "$p indisponible dans les dépôts"
  fi
done
command -v python3 >/dev/null 2>&1 || die "Python 3 n'a pas pu être installé."

FF_OK=0
if command -v ffmpeg >/dev/null 2>&1; then
  FF_OK=1; ok "ffmpeg ${D}(déjà là)${N}"
else
  for p in "${FFMPEG[@]}"; do
    if run "installation de $p (découpage des CUE)…" pkg_install "$p"; then FF_OK=1; ok "$p"; break; fi
  done
  [ "$FF_OK" -eq 1 ] || warn "ffmpeg indisponible : tout marche sauf le découpage des images CD (.cue)."
fi

# ── Python : bibliothèques de la distribution, sinon environnement dédié ─────
step "Python"
PY="$(command -v python3)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python $("$PY" -c 'import platform; print(platform.python_version())') trop ancien : il faut 3.9 ou plus."
ok "Python $("$PY" -c 'import platform; print(platform.python_version())')"

# Quelles bibliothèques manquent (absentes, ou rich trop ancien) ?
missing_libs() {
  PYTHONDONTWRITEBYTECODE=1 "$PY" - <<'PYCHECK' 2>>"$LOG"
import sys
out = []
try:
    import rich
    from importlib.metadata import version
    if int(version("rich").split(".")[0]) < 11:
        raise ImportError("rich trop ancien")
except Exception:
    out += ["rich", "markdown-it-py", "mdurl", "pygments"]
    if sys.version_info < (3, 11):
        out.append("typing-extensions")
try:
    import mutagen
except Exception:
    out.append("mutagen")
print(" ".join(out))
PYCHECK
}

# Télécharge depuis PyPI les dernières versions compatibles (bibliothèques 100 % Python),
# vérifie leur somme de contrôle et les dépose dans $LIB_DIR. Pas de pip ni de venv nécessaires.
fetch_libs() {
  "$PY" - "$LIB_DIR" "$@" <<'PYFETCH'
import hashlib, io, json, re, sys, urllib.request, zipfile

dest, names = sys.argv[1], sys.argv[2:]
here = sys.version_info[:3]

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "musitag-installer"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()

def vtuple(v):
    parts = []
    for x in v.split("."):
        if not x.isdigit():
            return None                       # pré-version (rc, b, dev…) : ignorée
        parts.append(int(x))
    return tuple(parts)

def py_ok(spec):
    """Évalue un requires_python simple (>=3.8, <4, !=3.0.*…)."""
    if not spec:
        return True
    for clause in spec.split(","):
        m = re.match(r"\s*(>=|<=|==|!=|~=|>|<)\s*([\d.]+)(\.\*)?\s*$", clause)
        if not m:
            continue
        op, ver, star = m.groups()
        want = tuple(int(x) for x in ver.split("."))
        cur = here[:len(want)] if star or op in ("==", "!=") else here
        cur = tuple(cur) + (0,) * max(0, len(want) - len(cur))
        ok = {">=": cur >= want, "<=": cur <= want, ">": cur > want, "<": cur < want,
              "==": cur == want, "!=": cur != want, "~=": cur >= want}[op]
        if not ok:
            return False
    return True

for name in names:
    meta = json.loads(get(f"https://pypi.org/pypi/{name}/json"))
    best = None
    for ver, files in meta["releases"].items():
        vt = vtuple(ver)
        if vt is None:
            continue
        for f in files:
            if (f["packagetype"] == "bdist_wheel" and f["filename"].endswith("none-any.whl")
                    and "py3" in f["filename"].split("-")[-3] and not f.get("yanked")
                    and py_ok(f.get("requires_python"))):
                if best is None or vt > best[0]:
                    best = (vt, ver, f)
                break
    if best is None:
        sys.exit(f"aucune version de {name} compatible avec Python {'.'.join(map(str, here))}")
    _, ver, f = best
    data = get(f["url"])
    if hashlib.sha256(data).hexdigest() != f["digests"]["sha256"]:
        sys.exit(f"{name} {ver} : somme de contrôle incorrecte")
    zf = zipfile.ZipFile(io.BytesIO(data))
    # le code et ses métadonnées seulement (pas les pages de manuel ni les scripts *.data/)
    zf.extractall(dest, [m for m in zf.namelist() if ".data/" not in m.split("/")[0] + "/"])
    print(f"{name} {ver}")
PYFETCH
}

NEED="$(missing_libs || true)"
if [ -z "$NEED" ]; then
  ok "rich et mutagen fournis par $OS_NAME"
  rm -rf -- "$APP_DIR"                          # reste éventuel d'une ancienne installation
else
  note "Pas fournies (ou trop anciennes) par $OS_NAME : $NEED"
  rm -rf -- "$APP_DIR"
  mkdir -p "$LIB_DIR"
  # shellcheck disable=SC2086  # liste de noms séparés par des espaces
  run "téléchargement depuis PyPI…" fetch_libs $NEED \
    || die "Téléchargement des bibliothèques impossible (pas d'accès à pypi.org ?)."
  ok "installées dans $LIB_DIR : $(grep -E '^[a-z-]+ [0-9.]+$' "$LOG" | tail -n "$(wc -w <<<"$NEED")" | paste -sd, - | sed 's/,/, /g')"
  find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
  chmod -R a+rX "$APP_DIR"
fi
if PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import PIL' >/dev/null 2>&1; then ok "Pillow (aperçu des pochettes)"
else warn "Pillow absent : les pochettes s'afficheront sans aperçu"; fi

# ── musitag ──────────────────────────────────────────────────────────────────
step "Installation de musitag"
{ printf '#!%s\n' "$PY"; tail -n +2 "$TMP/musitag.py"; } > "$TMP/musitag"
install -D -m 755 "$TMP/musitag" "$BIN"
INSTALLED_VERSION="$(PYTHONDONTWRITEBYTECODE=1 "$BIN" --version 2>>"$LOG" || true)"
[ "$INSTALLED_VERSION" = "musitag $MUSITAG_VERSION" ] || die "musitag ne démarre pas (voir le journal ci-dessus)."
ok "$BIN ${D}($INSTALLED_VERSION)${N}"

# dossier musique ouvert au démarrage
write_conf() {
  printf '# Réglages de musitag (modifiables, ou touche « p » dans musitag)\nMUSIC_ROOT=%s\n' "$1" > "$CONF"
  chmod 644 "$CONF"
  ok "dossier musique : $1"
}
if [ -n "$MUSIC" ]; then
  MUSIC="$(readlink -f -- "$MUSIC" 2>/dev/null || printf '%s' "$MUSIC")"
  [ -d "$MUSIC" ] || die "Dossier introuvable : $MUSIC"
  write_conf "$MUSIC"
elif [ -f "$CONF" ]; then
  ok "dossier musique : $(sed -n 's/^MUSIC_ROOT=//p' "$CONF" | head -n1) ${D}(réglage existant conservé)${N}"
else
  USER_HOME="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6 || true)"
  FOUND=""
  for d in "$USER_HOME/Musique" "$USER_HOME/Music" "$USER_HOME/musique" "$USER_HOME/music"; do
    if [ -d "$d" ]; then FOUND="$d"; break; fi
  done
  if [ -n "$FOUND" ]; then write_conf "$FOUND"
  else note "Dossier musique : à choisir au premier lancement (va dedans, puis touche « p »)."; fi
fi

# ── ménage ───────────────────────────────────────────────────────────────────
step "Nettoyage"
run "vidage du cache des paquets…" pkg_clean || true
ok "cache des paquets vidé, fichiers temporaires supprimés"
if [ "$KEEP" -eq 0 ] && [ -f "$SELF" ]; then
  rm -f -- "$SELF" && ok "installeur supprimé ${D}($SELF)${N}"
fi

printf '\n  %s✓ musitag %s est installé.%s  Lance-le avec : %smusitag%s\n' "$G$B" "$MUSITAG_VERSION" "$N" "$B" "$N"
[ "$FF_OK" -eq 1 ] || printf '  %s(sans ffmpeg : pas de découpage des images CD)%s\n' "$D" "$N"
printf '  %sPour tout retirer plus tard : relance cet installeur avec --desinstaller%s\n\n' "$D" "$N"
exit 0
