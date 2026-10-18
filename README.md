# musitag

Éditeur de métadonnées audio en ligne de commande, avec une interface interactive pilotée au
clavier. musitag est conçu pour les bibliothèques musicales servies par Jellyfin ou tout autre
lecteur : les informations (tags, paroles, pochettes) sont écrites **dans les fichiers eux-mêmes**,
sans fichier annexe.

### Fonctionnalités

- **Tags** : édition d'un fichier ou d'un dossier entier (récursif ou non), champs communs,
  numérotation des pistes, titres déduits des noms de fichiers, rechercher / remplacer,
  renommage des fichiers d'après les tags, suppression de champs parasites.
- **Paroles** : récupération depuis [LRCLIB](https://lrclib.net), synchronisées lorsqu'elles
  existent, et mention « ♪ Musique sans paroles ♪ » pour les morceaux instrumentaux.
- **Pochettes** : recherche sur [MusicBrainz](https://musicbrainz.org) et
  [Cover Art Archive](https://coverartarchive.org), avec aperçu des différentes éditions
  directement dans le terminal.
- **Images CD** : découpage d'une image (FLAC, APE, WavPack, WAV…) et de son `.cue` en pistes
  FLAC taguées, au sample près, avec détection automatique de l'encodage du `.cue`.
- **Nettoyage** : suppression des fichiers annexes d'un album (scans, `.cue`, `.m3u`, `.log`…),
  après intégration de la pochette et des paroles `.lrc` dans les morceaux.
- **Scripts** : enchaînement d'actions défini sans code, depuis musitag, puis rejoué sur un
  album, un dossier entier ou un seul morceau avec `musitag -s NOM`.
- **Sécurité des données** : aucune modification n'est écrite sans aperçu avant / après et
  confirmation.

**Formats pris en charge** : FLAC, MP3, Ogg Vorbis, Opus, M4A / M4B (AAC, ALAC), WAV, AIFF,
WavPack, APE, Musepack, WMA, TTA, DSF, Speex.

**Systèmes pris en charge** : Fedora, RHEL et dérivées (Rocky Linux, AlmaLinux) ; Debian, Ubuntu
et dérivées (Linux Mint, Pop!_OS…), y compris dans un conteneur (LXC, Docker…).

---

## Installation

**Prérequis** : `git`, et un compte root ou disposant de `sudo`.

```bash
sudo dnf install git        # Fedora / RHEL
sudo apt install git        # Debian / Ubuntu
```

**Installation** :

```bash
git clone https://github.com/pierretavu/musitag.git ~/musitag
cd ~/musitag
./install.sh
```

Le dossier `~/musitag` est conservé : il sert aux mises à jour. Il peut être placé ailleurs,
mais pas dans `/usr/local/bin`, où s'installe la commande `musitag`.

Le script :

1. installe les dépendances depuis les dépôts officiels de la distribution : Python 3, rich,
   mutagen, Pillow et ffmpeg ;
2. installe la commande `musitag` dans `/usr/local/bin` ;
3. vide le cache des paquets et supprime ses fichiers temporaires.

Les droits administrateur sont demandés au besoin. En root (dans un conteneur, par exemple),
aucune autre manipulation n'est nécessaire.

| Option | Effet |
|---|---|
| `--musique CHEMIN` | définit le dossier ouvert au démarrage, pour tous les utilisateurs |
| `--aide` | affiche l'aide |

Si la distribution ne fournit pas une version suffisante de rich ou de mutagen, ces
bibliothèques sont téléchargées depuis PyPI, vérifiées par somme de contrôle et installées dans
`/opt/musitag`. Sans ffmpeg, toutes les fonctions restent disponibles sauf le découpage des
images CD.

---

## Utilisation

### Lancement

| Commande | Effet |
|---|---|
| `musitag` | ouvre le dossier musique (voir ci-dessous) |
| `musitag .` | ouvre le dossier courant |
| `musitag CHEMIN` | ouvre le dossier indiqué |
| `musitag -r CHEMIN` | inclut les sous-dossiers (mode récursif) |
| `musitag -n CHEMIN` | ce dossier uniquement (comportement par défaut) |
| `musitag -s NOM [CHEMIN]` | lance un script (voir [Scripts](#scripts)) |
| `musitag --version` | affiche la version installée |
| `musitag --help` | affiche l'aide |

**Dossier musique.** Sans argument, musitag ouvre le dossier musique défini, dans cet ordre de
priorité :

1. la variable d'environnement `MUSITAG_ROOT` ;
2. le réglage de l'utilisateur, enregistré avec la touche **p** (`~/.config/musitag/config`) ;
3. le réglage du système, défini à l'installation avec `--musique` (`/etc/musitag.conf`).

Lancé depuis un dossier situé dans le dossier musique, musitag ouvre ce dossier. Si aucun
dossier musique n'est trouvé, il ouvre le dossier personnel et l'indique au démarrage.

### Menu principal

| Touche | Action |
|---|---|
| `1` | Afficher les métadonnées de tous les fichiers, sous forme de tableau |
| `2` | Voir un fichier en détail : tous les champs et les informations techniques |
| `3` | Modifier un fichier : champs principaux, champs libres, renommage, pochette, paroles |
| `4` | Modifier tous les fichiers : outils en lot (voir ci-dessous) |
| `5` | Découper une image CD à l'aide de son `.cue` |
| `6` | Nettoyer le dossier : supprimer tout ce qui n'est pas de la musique |
| `s` | Scripts : créer, modifier, lancer (voir [Scripts](#scripts)) |
| `o` | Ouvrir un dossier : navigation Musique → Artiste → Album |
| `d` | Saisir un chemin (complétion avec Tab) |
| `p` | Définir le dossier courant comme dossier musique |
| `r` | Activer ou désactiver le mode récursif |
| `0` | Quitter |

`Ctrl+D` annule la saisie en cours et revient au menu.

### Outils en lot (menu 4)

| Touche | Outil |
|---|---|
| `1` | Champs communs : artiste, album, date, genre, disque… |
| `2` | Numérotation des pistes, d'après les noms de fichiers ou dans l'ordre |
| `3` | Titres déduits des noms de fichiers (`01 - Loser.flac` → `Loser`) |
| `4` | Rechercher / remplacer dans un champ (ex. : retirer « (2016 Remaster) ») |
| `5` | Renommage des fichiers d'après les tags, selon un modèle prédéfini ou personnalisé |
| `6` | Suppression de champs (COMMENT, ENCODER…) |
| `7` | Pochettes : recherche en ligne, image du dossier, suppression |
| `8` | Paroles : recherche sur LRCLIB, mention instrumentale, suppression |
| `9` | Effacement de tous les tags |

### Paroles

La recherche sur LRCLIB privilégie les paroles synchronisées. Les correspondances exactes
(titre, artiste, durée) sont retenues d'office ; les correspondances approchantes sont
proposées au choix. Un aperçu précède toute écriture, et les paroles déjà présentes sont
remplacées. Les morceaux reconnus comme instrumentaux reçoivent la mention « ♪ Musique sans
paroles ♪ », qui peut aussi être ajoutée manuellement. LRCLIB est un service public et gratuit :
aucun compte ni clé n'est nécessaire.

### Pochettes

Les éditions de l'album sont recherchées sur MusicBrainz et Cover Art Archive, puis affichées en
aperçu dans le terminal. La pochette choisie est intégrée à tous les fichiers de l'album.

### Images CD

Le découpage produit des pistes FLAC identiques au sample près à l'image d'origine (une image MP3
est découpée sans réencodage). Les informations de l'album et des pistes peuvent être corrigées
avant le découpage. L'image d'origine peut ensuite être rangée dans un sous-dossier `_source`,
ignoré par Jellyfin et par musitag, supprimée, ou laissée en place.

### Nettoyage

Le menu 6 liste tous les fichiers qui ne sont pas de la musique. Ils peuvent être supprimés en
totalité, au choix ou par type. Avant suppression, la pochette du dossier et les paroles `.lrc`
sont intégrées aux morceaux qui n'en ont pas.

### Scripts

Un script enchaîne des actions sur un album, sans intervention : par exemple chercher les
paroles exactes, intégrer la pochette du dossier ou, à défaut, le premier résultat en ligne,
retirer « (Remaster) » des titres, renommer les fichiers puis nettoyer le dossier.

**Créer un script.** Touche **s** du menu principal, puis **Nouveau script**. Les étapes
s'ajoutent une par une en choisissant une action dans une liste, puis ses réglages dans de
petits menus ; elles peuvent ensuite être modifiées, déplacées ou supprimées. Chaque script est
enregistré dans `~/.config/musitag/scripts/NOM.mst`, un fichier texte lisible qui peut aussi
être modifié à la main ou partagé.

**Lancer un script.**

| Commande | Effet |
|---|---|
| `musitag -s NOM` | l'album du dossier courant |
| `musitag -s NOM CHEMIN` | l'album indiqué, ou un seul morceau si `CHEMIN` est un fichier |
| `musitag -s NOM -r CHEMIN` | chaque album situé sous `CHEMIN`, l'un après l'autre |
| `musitag -s NOM -v` | mode verbeux : chaque action est décrite en temps réel |
| `musitag -s NOM --simulation` | affiche le bilan sans rien écrire |

`NOM` peut aussi être le chemin d'un fichier `.mst`. Un script se lance également depuis le menu
**s**, sur le dossier ouvert ou sur un morceau choisi.

**Déroulement.** Toutes les étapes sont d'abord préparées, sans rien écrire. Un bilan présente
ensuite l'ensemble des modifications, suivi d'**une seule confirmation** avant l'écriture. Sur
plusieurs albums, le bilan se limite à un résumé par album, et le détail complet reste
consultable. Un avertissement s'affiche avant tout lancement sur au moins dix albums ou sur la
racine de la bibliothèque.

| Touche, pendant l'exécution | Effet |
|---|---|
| `s` | passe l'étape en cours (une recherche trop longue, par exemple) ; ce qui était déjà trouvé est conservé |
| `q` | arrête la préparation et passe au bilan de ce qui est prêt ; pendant l'écriture, s'arrête après le fichier en cours |
| `v` | active ou désactive le mode verbeux |

**Actions disponibles.**

| Action | Réglages |
|---|---|
| Chercher les paroles | correspondances exactes seulement, ou la première trouvée ; paroles existantes remplacées ou conservées ; mention instrumentale |
| Marquer comme instrumental | paroles existantes remplacées ou conservées |
| Supprimer les paroles | — |
| Intégrer une pochette | sources classées par priorité : pochette déjà intégrée, image du dossier, 1er à 5e résultat en ligne |
| Supprimer les pochettes intégrées | — |
| Fixer des champs | un ou plusieurs champs et leur valeur |
| Supprimer des champs | liste de champs (`comment`, `encodeur`, ou un nom exact) |
| Numéroter les pistes | d'après les noms de fichiers ou dans l'ordre ; total des pistes |
| Titres d'après les noms de fichiers | — |
| Rechercher / remplacer | champ, texte recherché, remplacement, expression régulière |
| Effacer tous les tags | — |
| Renommer les fichiers | modèle (`{n} - {title}`, etc.) |
| Nettoyer le dossier | tout, ou certains types de fichiers |
| Découper l'image CD | image d'origine rangée dans `_source`, supprimée ou laissée en place |

Chaque étape qui peut échouer (aucune parole retenue, aucune pochette trouvée…) a sa propre
règle : passer à l'étape suivante, ou arrêter le script pour cet album. Les actions propres à un
album entier (numérotation, nettoyage, découpage) sont ignorées lorsque le script vise un seul
morceau.

### Droits d'accès

Si les fichiers ne sont pas modifiables par l'utilisateur courant (partage réseau, NAS…),
musitag propose de se relancer avec `sudo`.

### Réglages

| Variable d'environnement | Effet |
|---|---|
| `MUSITAG_ROOT` | dossier musique (prioritaire sur les réglages enregistrés) |
| `MUSITAG_CONTACT` | adresse de contact transmise à MusicBrainz, comme le recommande le service |
| `MUSITAG_TRUECOLOR=0` | aperçus des pochettes en 256 couleurs, pour les terminaux anciens |

---

## Mise à jour

```bash
cd ~/musitag
git pull
./install.sh
```

Le réglage du dossier musique est conservé. `musitag --version` affiche la version installée.

---

## Désinstallation

```bash
cd ~/musitag
./install.sh --desinstaller
cd ~ && rm -rf ~/musitag
```

Sont supprimés : la commande `/usr/local/bin/musitag`, le dossier `/opt/musitag`, les réglages
`/etc/musitag.conf` et `~/.config/musitag`. Python, ffmpeg et les bibliothèques sont conservés :
ce sont des paquets standards de la distribution, susceptibles d'être utilisés par d'autres
programmes.

---

## Versions

| Version | Principales nouveautés |
|---|---|
| 1.0 | Première version : édition des tags, outils en lot, découpage des images CD |
| 1.1 | Paroles synchronisées depuis LRCLIB, intégrées aux fichiers |
| 1.2 | Gestion des limitations de LRCLIB : rythme adaptatif, nouvelles tentatives |
| 1.3 | Nettoyage des dossiers, pochettes en ligne avec aperçu, navigation entre dossiers |
| 1.4 | Mention « ♪ Musique sans paroles ♪ » pour les instrumentaux |
| 1.5 | Dossier musique configurable, installeur Fedora / Debian / Ubuntu |
| 1.5.1 | Détection plus fiable de l'encodage des `.cue` |
| 1.5.2 | Correctifs de sécurité, installation et mise à jour par `git` |
| 1.5.3 | Installation : détection d'un dépôt cloné dans `/usr/local/bin` |
| 1.6 | Scripts : enchaînements d'actions sans code, lancés avec `musitag -s` |

Le détail de chaque version figure dans [CHANGELOG.md](CHANGELOG.md).
