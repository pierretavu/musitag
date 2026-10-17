# Journal des versions

## 1.5.2 — 17 octobre 2026
- Sécurité : les caractères de contrôle contenus dans les tags, les noms de fichiers, les
  `.cue` ou les réponses en ligne sont neutralisés à l'affichage (un fichier piégé ne peut
  plus agir sur le terminal : titre de fenêtre, presse-papiers, faux liens, écran effacé).
- Sécurité : un `.cue` ne peut désigner qu'un fichier de son propre dossier.
- Les images démesurées ne sont plus décodées (aperçus, comparaison des pochettes).
- Réseau : réponses de taille bornée, redirections vers une adresse non chiffrée refusées.
- Installation depuis un clone git : `./install.sh`, mise à jour par `git pull && ./install.sh`.

## 1.5.1 — 17 octobre 2026
- Détection de l'encodage des `.cue` plus fiable, même sur des textes très courts
  (japonais, russe, français) : une note par caractère plutôt que par mot.

## 1.5 — 17 octobre 2026
- Dossier musique configurable : touche **p** « Définir comme dossier musique »
  (`~/.config/musitag/config`), ou `/etc/musitag.conf` pour tout le système.
- Installeur autonome Fedora / Debian / Ubuntu : paquets,
  repli PyPI vérifié si la distribution est trop ancienne, ménage complet, `--desinstaller`.
- musitag refuse proprement une bibliothèque `rich` trop ancienne (< 11).
- Les longues listes ne passent par `less` que s'il est installé.

## 1.4 — 15 octobre 2026
- « ♪ Musique sans paroles ♪ » écrit dans les morceaux instrumentaux : automatiquement
  quand LRCLIB le confirme, ou à la main (fichier ou album). Feishin l'affiche au lieu
  d'aller chercher des crédits en ligne.
- Menus Paroles : chercher sur LRCLIB, marquer comme instrumental, supprimer.

## 1.3 — 15 octobre 2026
- Démarrage dans `/mnt/nas_medias/Musiques` (ou l'album courant), récursivité désactivée
  par défaut, « Dossier musique non trouvé » sinon.
- Navigateur de dossiers (touche **o**).
- **Nettoyage** : liste tout ce qui n'est pas de la musique, suppression tout / au choix /
  par type, éléments protégés, intégration de la pochette et des `.lrc` avant suppression.
- **Pochettes en ligne** (MusicBrainz + Cover Art Archive) avec aperçu des éditions
  dans le terminal et choix au numéro.

## 1.2 — 15 octobre 2026
- LRCLIB : rythme limité et adaptatif, pause commune sur 429 / 503, réessais patients,
  menu « Relancer les erreurs », messages d'erreur justes.

## 1.1 — 15 octobre 2026
- Paroles depuis LRCLIB, synchronisées si possible, intégrées dans les fichiers
  (correspondance exacte, puis recherche élargie validée à la main).

## 1.0 — 13 octobre 2026
- Première version : affichage et édition des tags (fichier ou lot, récursif ou non),
  outils en lot (numérotation, titres depuis les noms, rechercher/remplacer, renommage,
  pochettes, nettoyage des champs), découpage CUE au sample près.
