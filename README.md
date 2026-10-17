# musitag

Éditeur de métadonnées audio dans le terminal, pensé pour une bibliothèque Jellyfin
« propre » : toutes les infos **dans** les fichiers, rien à côté.

- tags (un fichier ou tout un dossier, récursif ou non), renommage, numérotation
- paroles synchronisées depuis **LRCLIB**, mention « ♪ Musique sans paroles ♪ » pour les instrumentaux
- pochettes depuis **Cover Art Archive**, avec aperçu des éditions dans le terminal
- découpage d'images CD + `.cue` en FLAC, au sample près
- nettoyage des dossiers : tout ce qui n'est pas de la musique part (après sauvetage de la pochette et des `.lrc`)

Formats : FLAC, MP3, WAV, AIFF, OGG, Opus, M4A (AAC/ALAC), WavPack, APE, WMA…

Systèmes : Fedora (et RHEL, Rocky, Alma…), Debian, Ubuntu (et Mint, Pop!_OS…).

## Installer

Il faut juste `git` (souvent déjà là) :

```bash
sudo dnf install git          # Fedora
sudo apt install git          # Debian / Ubuntu
```

Puis :

```bash
git clone https://github.com/pierretavu/musitag.git ~/musitag
cd ~/musitag
./install.sh
```

L'installation prend une minute : Python, rich, mutagen, Pillow et ffmpeg depuis les dépôts
officiels de ta distribution, puis la commande `musitag` dans `/usr/local/bin`. Le cache des
paquets est vidé à la fin, rien d'autre n'est laissé sur le système. Le dossier `~/musitag`
sert seulement aux mises à jour.

Pour choisir le dossier musique ouvert au démarrage : `./install.sh --musique /chemin/vers/Musique`
(ou plus tard dans musitag : va dans le dossier, touche **p**).

## Utiliser

```bash
musitag                 # ouvre ton dossier musique
musitag .               # le dossier où tu te trouves
musitag ~/Musique/Album # un dossier précis
musitag -r Pixies       # avec les sous-dossiers
```

Tout se fait au clavier, avec des menus. Rien n'est écrit dans les fichiers sans un aperçu
avant/après et ta confirmation.

## Mettre à jour

```bash
cd ~/musitag && git pull && ./install.sh
```

Les nouveautés de chaque version : [CHANGELOG.md](CHANGELOG.md).

## Désinstaller

```bash
cd ~/musitag && ./install.sh --desinstaller
rm -rf ~/musitag
```

Python, ffmpeg et les bibliothèques restent : ce sont des paquets standards de la distribution.
