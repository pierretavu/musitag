# Journal des versions

## 1.7 — 27 octobre 2026
- Pochettes : le menu des pochettes indique d'emblée où elles en sont : combien de fichiers
  ont une pochette intégrée, ses dimensions, son format et son poids, si plusieurs images
  différentes cohabitent dans l'album, et quelle image se trouve dans le dossier.
- Pochettes : **Voir les pochettes intégrées** affiche la pochette en grand dans le terminal,
  ou chaque image différente côte à côte avec la liste des fichiers qui portent chacune (et
  ceux qui n'en ont pas). Depuis un bureau, l'image peut aussi s'ouvrir dans la visionneuse.
- Pochettes : **Extraire la pochette** l'enregistre en fichier image dans le dossier de
  l'album (`cover.jpg` ou `cover.png` par défaut, nom au choix) : au format d'origine, sans
  aucune perte, ou converti en JPEG ou en PNG. Sur plusieurs albums à la fois, une image par
  dossier. Un fichier existant n'est remplacé qu'après confirmation.
- Pochettes : quand un fichier contient plusieurs images (face avant, dos, livret…), c'est la
  face avant qui est retenue pour l'aperçu, l'extraction et les scripts.
- Fiche d'un fichier : dimensions de la pochette intégrée.

## 1.6.1 — 18 octobre 2026
- Paroles : musitag n'insiste plus quand LRCLIB sature. Un seul réessai court par requête ;
  après plusieurs échecs d'affilée, LRCLIB est mis en pause (1 minute, puis plus longtemps
  s'il sature encore) et les recherches restantes sont signalées comme reportées au lieu
  d'attendre. Un script continue ses autres étapes et rappelle dans le bilan qu'il suffit de
  le relancer plus tard.
- Paroles : bien moins de requêtes. La correspondance exacte (une requête par morceau), puis
  une seule recherche par album, partagée entre ses morceaux, avant toute recherche morceau
  par morceau. Exemple : un album de 10 titres absent de LRCLIB passe de 50 à 22 requêtes.
- Paroles : les morceaux introuvables sont mémorisés 30 jours
  (`~/.cache/musitag/lrclib-introuvables.json`) et ne sont pas recherchés de nouveau d'ici là
  (sauf recherche manuelle).
- Menus : « Relancer ces recherches » attend la fin de la pause de LRCLIB avant de réessayer.

## 1.6 — 18 octobre 2026
- **Scripts** : enchaînements d'actions créés sans code depuis musitag (touche **s**) et
  enregistrés dans `~/.config/musitag/scripts/NOM.mst`.
  - Lancement : `musitag -s NOM`, sur l'album du dossier courant, un autre album, un seul
    morceau, ou chaque album d'un dossier (`-r`).
  - 14 actions : paroles (exactes ou première trouvée), mention instrumentale, pochette par
    ordre de priorité (intégrée, dossier, résultats en ligne), champs, numérotation, titres,
    rechercher / remplacer, renommage, nettoyage, découpage d'image CD…
  - Tout est préparé avant d'écrire : bilan, puis une seule confirmation. Bilan résumé par
    album sur les gros lancements, avertissement avant un lancement sur au moins dix albums ou
    sur la racine de la bibliothèque.
  - Pendant l'exécution : `s` saute l'étape en cours, `q` arrête, `v` active le mode verbeux.
    Options `-v` (verbeux) et `--simulation` (bilan sans écriture).
  - Règle par étape en cas d'échec : passer à la suivante, ou arrêter le script pour cet album.

## 1.5.3 — 18 octobre 2026
- Installation : si le dépôt a été cloné dans `/usr/local/bin` (l'emplacement de la commande
  `musitag`), l'installeur s'arrête aussitôt et indique comment le déplacer ; la
  désinstallation ne supprime plus jamais un dossier à cet emplacement.
- Installation : en cas d'échec au démarrage de musitag, le journal est réellement affiché.

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
