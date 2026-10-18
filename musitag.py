#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
musitag — éditeur interactif de métadonnées audio, dans le terminal.

  • Affiche les tags de tous les fichiers d'un dossier (option récursive)
  • Modifie un fichier champ par champ, ou tous les fichiers d'un coup
  • Outils en lot : numérotation, titres depuis le nom de fichier,
    rechercher/remplacer, renommage depuis les tags, pochettes, nettoyage
  • Paroles (synchronisées si possible) depuis LRCLIB, intégrées dans les fichiers ;
    les instrumentaux reçoivent « ♪ Musique sans paroles ♪ »
  • Pochettes depuis Cover Art Archive, avec aperçu des éditions dans le terminal
  • Nettoyage : supprime tout ce qui n'est pas de la musique (cover.jpg, .cue, .m3u…)
  • Découpe une image CD + .cue en pistes FLAC taguées, au sample près

Dossier ouvert par défaut : /mnt/nas_medias/Musiques, ou celui choisi avec la touche p
(enregistré dans ~/.config/musitag/config ; /etc/musitag.conf pour tout le système).

Formats : FLAC, MP3, WAV, AIFF, OGG, Opus, M4A (AAC/ALAC), WavPack, APE,
          Musepack, WMA, TTA, DSF
Dépendances : python3-rich, python3-mutagen, ffmpeg (pour le découpage CUE)
Installation (Fedora) : sudo dnf install python3-rich python3-mutagen python3-pillow ffmpeg-free
"""
from __future__ import annotations

import argparse
import base64
import codecs
import glob
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import difflib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple

# bibliothèques déposées par l'installeur quand la distribution n'en fournit pas de récentes
_VENDOR_DIR = "/opt/musitag/lib"
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

VERSION = "1.5.3"


def install_cmd(*pkgs_by_distro: str) -> str:
    """Commande d'installation adaptée : dnf (Fedora) ou apt (Debian/Ubuntu)."""
    if shutil.which("dnf"):
        return "sudo dnf install " + pkgs_by_distro[0]
    return "sudo apt install " + pkgs_by_distro[1]


def _die_missing(what: str) -> None:
    sys.stderr.write(
        f"\nmusitag : le module Python « {what} » est introuvable.\n"
        "Installe les dépendances avec :\n\n    "
        + install_cmd("python3-rich python3-mutagen ffmpeg-free",
                      "python3-rich python3-mutagen ffmpeg") + "\n\n"
    )
    sys.exit(1)


try:
    from rich import box
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.progress import (BarColumn, Progress, SpinnerColumn, TextColumn,
                               TimeElapsedColumn)
    from rich.rule import Rule
    from rich.segment import Segment
    from rich.table import Table
    from rich.text import Text
except ImportError:
    _die_missing("rich")

try:
    from importlib.metadata import version as _pkg_version
    if int(_pkg_version("rich").split(".")[0]) < 11:
        sys.stderr.write(f"\nmusitag : la bibliothèque « rich » est trop ancienne ({_pkg_version('rich')}, "
                         "il faut la 11 ou plus).\nRelance l'installeur de musitag, il s'en occupe.\n\n")
        sys.exit(1)
except (ImportError, ValueError):
    pass
except Exception as _e:  # PackageNotFoundError : rich installé hors des métadonnées, on tente quand même
    if type(_e).__name__ != "PackageNotFoundError":
        raise

try:
    from rich.console import Group
except ImportError:  # vieilles versions de rich
    from rich.console import RenderGroup as Group  # type: ignore
try:
    from rich.progress import MofNCompleteColumn
except ImportError:
    MofNCompleteColumn = None  # type: ignore

try:
    import mutagen
    from mutagen.aiff import AIFF
    from mutagen.apev2 import BINARY as APE_BINARY
    from mutagen.apev2 import APEv2File, APEValue
    from mutagen.asf import ASF, ASFByteArrayAttribute
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, COMM, TCON, TXXX, USLT, ID3FileType
    from mutagen.id3 import Frames as ID3Frames
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    from mutagen.oggflac import OggFLAC
    from mutagen.oggopus import OggOpus
    from mutagen.oggspeex import OggSpeex
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE
except ImportError:
    _die_missing("mutagen")

try:
    from mutagen.dsf import DSF
except ImportError:
    DSF = None  # type: ignore

try:
    import readline
except ImportError:
    readline = None  # type: ignore


# ═════════════════════════════════ Constantes ═════════════════════════════════

AUDIO_EXTS = {".flac", ".mp3", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".wav",
              ".aif", ".aiff", ".aifc", ".wv", ".ape", ".mpc", ".wma", ".tta",
              ".dsf", ".spx"}
SYSTEM_CONFIG = "/etc/musitag.conf"
DEFAULT_MUSIC_ROOT = "/mnt/nas_medias/Musiques"


def _real_home() -> str:
    """Dossier perso du vrai utilisateur, même quand musitag tourne via sudo."""
    user = os.environ.get("SUDO_USER")
    if user and os.geteuid() == 0:
        try:
            import pwd
            return pwd.getpwnam(user).pw_dir
        except (KeyError, ImportError):
            pass
    return os.path.expanduser("~")


USER_CONFIG = os.path.join(_real_home(), ".config", "musitag", "config")


def _read_config(path: str) -> Dict[str, str]:
    conf: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    conf[k.strip().upper()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return conf


# priorité : variable d'environnement > ~/.config/musitag/config > /etc/musitag.conf > défaut
CONFIG = {**_read_config(SYSTEM_CONFIG), **_read_config(USER_CONFIG)}
MUSIC_ROOT = (os.environ.get("MUSITAG_ROOT") or CONFIG.get("MUSIC_ROOT")
              or DEFAULT_MUSIC_ROOT).rstrip("/") or "/"


def save_user_config(key: str, value: str) -> str:
    """Écrit KEY=valeur dans ~/.config/musitag/config (du vrai utilisateur). Renvoie le chemin."""
    conf = _read_config(USER_CONFIG)
    conf[key.upper()] = value
    os.makedirs(os.path.dirname(USER_CONFIG), exist_ok=True)
    with open(USER_CONFIG, "w", encoding="utf-8") as fh:
        fh.write("# Réglages de musitag\n")
        for k, v in conf.items():
            fh.write(f"{k}={v}\n")
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:          # lancé via sudo : le fichier reste à l'utilisateur
        for path in (os.path.dirname(os.path.dirname(USER_CONFIG)), os.path.dirname(USER_CONFIG), USER_CONFIG):
            try:
                os.chown(path, int(uid), int(gid))
            except OSError:
                pass
    return USER_CONFIG
# dossiers techniques des NAS (Synology, QNAP…) : jamais listés ni supprimés
SYSTEM_DIR_NAMES = {"@eaDir", ".@__thumb", "@Recycle", "#recycle", "#snapshot", ".snapshot",
                    "lost+found", ".Trash-1000", ".streams"}
# audio que musitag ne sait pas taguer, mais qu'il ne faut surtout pas supprimer par erreur
AUDIO_LIKE_EXTS = {".aac", ".ac3", ".dts", ".mka", ".shn", ".tak", ".mid", ".midi", ".mod", ".xm",
                   ".it", ".s3m", ".amr", ".ra", ".rm", ".au", ".snd", ".caf", ".w64", ".rf64",
                   ".ofr", ".wvc", ".dff", ".mp2", ".m4p"}
SPLIT_SOURCE_EXTS = (".flac", ".ape", ".wv", ".wav", ".tta", ".aiff", ".aif",
                     ".tak", ".m4a", ".mp3", ".ogg", ".opus")
LOSSLESS_CODECS = {"flac", "ape", "wavpack", "tta", "alac", "tak", "shorten", "mlp"}
COVER_NAMES = ("cover", "folder", "front", "album", "pochette")
IMAGE_EXTS = (".jpg", ".jpeg", ".png")

FIELDS: List[Tuple[str, str]] = [
    ("title", "Titre"),
    ("artist", "Artiste"),
    ("albumartist", "Artiste de l'album"),
    ("album", "Album"),
    ("date", "Date"),
    ("genre", "Genre"),
    ("tracknumber", "N° de piste"),
    ("tracktotal", "Total pistes"),
    ("discnumber", "N° de disque"),
    ("disctotal", "Total disques"),
    ("composer", "Compositeur"),
    ("comment", "Commentaire"),
]
LABEL = dict(FIELDS)
LABEL["lyrics"] = "Paroles"
TEXT_FIELDS = ["title", "artist", "albumartist", "album", "date", "genre",
               "composer", "comment"]
NUM_FIELDS = ("tracknumber", "tracktotal", "discnumber", "disctotal")
BATCH_FIELDS = ["artist", "albumartist", "album", "date", "genre", "discnumber",
                "disctotal", "composer", "comment"]

FORMAT_NAMES = {
    "FLAC": "FLAC", "MP3": "MP3", "EasyMP3": "MP3", "OggVorbis": "OGG",
    "OggOpus": "OPUS", "OggFLAC": "OGG-FLAC", "OggSpeex": "SPEEX", "MP4": "M4A",
    "WAVE": "WAV", "AIFF": "AIFF", "WavPack": "WV", "MonkeysAudio": "APE",
    "Musepack": "MPC", "ASF": "WMA", "TrueAudio": "TTA", "DSF": "DSF",
    "OptimFROG": "OFR",
}
LOSSLESS_NAMES = {"FLAC", "WAV", "AIFF", "WV", "APE", "ALAC", "TTA", "OFR",
                  "OGG-FLAC", "DSF"}

CUE_ENCODINGS = [
    ("utf-8", "UTF-8"),
    ("cp1252", "Windows-1252 (français, Europe de l'Ouest)"),
    ("cp1251", "Windows-1251 (russe, cyrillique)"),
    ("cp932", "Shift-JIS (japonais)"),
    ("cp1250", "Windows-1250 (Europe centrale)"),
    ("koi8-r", "KOI8-R (ancien russe)"),
    ("gb18030", "GB18030 (chinois simplifié)"),
    ("big5", "Big5 (chinois traditionnel)"),
    ("latin-1", "ISO-8859-1"),
]

# Caractères qui ne doivent jamais partir tels quels vers le terminal quand ils viennent des
# DONNÉES (tags, noms de fichiers, .cue, réponses de LRCLIB / MusicBrainz) : codes de contrôle
# (Échap → titre de fenêtre, presse-papiers, faux liens, écran effacé…), contrôles C1, et
# inversions de sens d'écriture (texte affiché à l'envers pour tromper).
UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


class SafeConsole(Console):
    """Console rich qui remplace ces caractères par « \ufffd » dans le texte affiché.
    Les codes que rich produit lui-même (couleurs, curseur) ne sont pas concernés."""

    def _render_buffer(self, buffer) -> str:
        return super()._render_buffer(
            seg if seg.control or not UNSAFE_CHARS.search(seg.text)
            else Segment(UNSAFE_CHARS.sub("\ufffd", seg.text), seg.style, seg.control)
            for seg in buffer)


console = SafeConsole(highlight=False)


class TagError(Exception):
    """Erreur lisible renvoyée à l'utilisateur."""


class Cancel(Exception):
    """Ctrl+D : retour au menu."""


# ═════════════════════════════════ Utilitaires ════════════════════════════════

def split_values(s: str) -> List[str]:
    """« Rock ; Pop » → ['Rock', 'Pop']"""
    return [v.strip() for v in s.split(";") if v.strip()]


def join_values(vals) -> str:
    return " ; ".join(str(v) for v in vals)


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def path_sort_key(p: str):
    return (natural_key(os.path.dirname(p)), natural_key(os.path.basename(p)))


def split_num(s: str) -> Tuple[str, str]:
    """« 3/12 » → ('3', '12')"""
    s = (s or "").strip()
    if "/" in s:
        a, b = s.split("/", 1)
        return a.strip(), b.strip()
    return s, ""


def fmt_dur(sec: float) -> str:
    sec = int(round(sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(n: int) -> str:
    for unit, div in (("Go", 1 << 30), ("Mo", 1 << 20), ("Ko", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f}".replace(".", ",") + " " + unit
    return f"{n} o"


def safe_name(s: str) -> str:
    """Nom de fichier valide sur Linux ET sur un partage SMB/NAS."""
    s = s.replace("/", "-").replace("\\", "-").replace("|", "-").replace(":", " -")
    s = s.replace('"', "'")
    s = re.sub(r"[<>?*]", "", s)
    s = UNSAFE_CHARS.sub("", s.replace("\t", " ").replace("\n", " "))
    s = re.sub(r"\s+", " ", s).strip().rstrip(".").strip()
    return s[:180] or "sans titre"


def image_mime(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def picture_desc(mime: str, size: int) -> str:
    return f"[image {mime or '?'} · {human_size(size)}]"


def parse_selection(s: str, n: int) -> List[int]:
    """« 1,3-5 » → [0, 2, 3, 4] (indices base 0)."""
    s = s.strip().lower()
    if s in ("*", "tout", "tous", "all"):
        return list(range(n))
    out = set()
    for part in re.split(r"[,\s]+", s):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            for i in range(min(a, b), max(a, b) + 1):
                if 1 <= i <= n:
                    out.add(i - 1)
        elif part.isdigit() and 1 <= int(part) <= n:
            out.add(int(part) - 1)
    return sorted(out)


_TRK_LEAD = re.compile(r"^\s*(?:(\d{1,2})[-.](?=\d))?([A-Da-d])?(\d{1,3})(?=\s|[-._)\]])")
_TRK_MID = re.compile(r"_(\d{1,3})_")


def parse_track_from_name(stem: str):
    """(disque, face vinyle, numéro) d'après le nom de fichier, ou None."""
    m = _TRK_LEAD.match(stem)
    if m:
        disc = int(m.group(1)) if m.group(1) else None
        return disc, (m.group(2) or "").upper(), int(m.group(3))
    mids = _TRK_MID.findall(stem)
    if mids:
        return None, "", int(mids[-1])
    return None


def title_from_filename(stem: str, artists: List[str]) -> str:
    s = stem
    m = re.match(r"^.*_(\d{1,3})_(.+)$", s)
    if m and " " in s:                                  # Artiste_Album_01_Titre
        s = m.group(2)
    else:                                               # 01 - Titre / A1. Titre / 1-01 Titre
        s = re.sub(r"^\s*(?:\d{1,2}[-.](?=\d))?[A-Da-d]?\d{1,3}(?:\s*[-._)\]]+\s*|\s+)",
                   "", s, count=1)
    if "_" in s and " " not in s:
        s = s.replace("_", " ")
    for a in artists:
        if not a:
            continue
        for pat in (r"^\s*" + re.escape(a) + r"\s+-\s+", r"\s+-\s+" + re.escape(a) + r"\s*$"):
            s2 = re.sub(pat, "", s, flags=re.I)
            if s2.strip():
                s = s2
    s = re.sub(r"\s{2,}", " ", s).strip(" -_")
    return s or stem


# ══════════════════════════ Accès aux tags par format ══════════════════════════

class Backend:
    kind = "?"

    def __init__(self, mf):
        self.mf = mf

    def ensure_tags(self):
        if self.mf.tags is None:
            self.mf.add_tags()
        return self.mf.tags

    def get(self, f: str) -> List[str]:
        raise NotImplementedError

    def set(self, f: str, values: List[str]) -> None:   # [] = supprimer
        raise NotImplementedError

    def raw_items(self) -> List[Tuple[str, str]]:
        raise NotImplementedError

    def raw_set(self, key: str, values: List[str]) -> None:
        raise NotImplementedError

    def raw_delete(self, key: str) -> None:
        raise NotImplementedError

    def pictures(self) -> List[Tuple[str, int]]:
        return []

    def clear_pictures(self) -> None:
        pass

    def add_picture(self, data: bytes, mime: str) -> None:
        raise TagError("pochette non prise en charge pour ce format")

    def save(self) -> None:
        self.mf.save()


class VorbisBackend(Backend):
    """FLAC, OGG Vorbis, Opus, Speex : commentaires Vorbis (clé=valeur)."""
    kind = "Vorbis comments"
    MAP = {
        "title": ["TITLE"], "artist": ["ARTIST"],
        "albumartist": ["ALBUMARTIST", "ALBUM ARTIST", "ALBUM_ARTIST"],
        "album": ["ALBUM"], "date": ["DATE", "YEAR"], "genre": ["GENRE"],
        "composer": ["COMPOSER"], "comment": ["COMMENT", "DESCRIPTION"],
        "tracktotal": ["TRACKTOTAL", "TOTALTRACKS"],
        "disctotal": ["DISCTOTAL", "TOTALDISCS"],
        "lyrics": ["LYRICS", "UNSYNCEDLYRICS", "UNSYNCED LYRICS"],
    }
    NUMKEY = {"tracknumber": "TRACKNUMBER", "discnumber": "DISCNUMBER"}
    PAIR = {"tracknumber": "tracktotal", "discnumber": "disctotal",
            "tracktotal": "tracknumber", "disctotal": "discnumber"}
    PIC_KEYS = ("METADATA_BLOCK_PICTURE", "COVERART", "COVERARTMIME")

    def _vals(self, key):
        t = self.mf.tags
        if t is None:
            return []
        try:
            return [v for v in t[key] if v != ""]
        except KeyError:
            return []

    def _del(self, key):
        t = self.mf.tags
        if t is not None and key in t:
            del t[key]

    def get(self, f):
        if f in self.NUMKEY:
            v = self._vals(self.NUMKEY[f])
            n = split_num(v[0])[0] if v else ""
            return [n] if n else []
        if f in ("tracktotal", "disctotal"):
            for k in self.MAP[f]:
                v = self._vals(k)
                if v:
                    return v[:1]
            v = self._vals(self.NUMKEY[self.PAIR[f]])
            tot = split_num(v[0])[1] if v else ""
            return [tot] if tot else []
        for k in self.MAP[f]:
            v = self._vals(k)
            if v:
                return v
        return []

    def set(self, f, values):
        t = self.ensure_tags()
        if f in self.NUMKEY:
            key, totf = self.NUMKEY[f], self.PAIR[f]
            total = self.get(totf)
            self._del(key)
            if values:
                t[key] = [values[0]]
            if total and not any(self._vals(k) for k in self.MAP[totf]):
                t[self.MAP[totf][0]] = total          # « 3/12 » → TRACKTOTAL=12
            return
        if f in ("tracktotal", "disctotal"):
            nkey = self.NUMKEY[self.PAIR[f]]
            num = self._vals(nkey)
            if num and "/" in num[0]:
                n = split_num(num[0])[0]
                self._del(nkey)
                if n:
                    t[nkey] = [n]
            for k in self.MAP[f]:
                self._del(k)
            if values:
                t[self.MAP[f][0]] = [values[0]]
            return
        for k in self.MAP[f]:
            self._del(k)
        if values:
            t[self.MAP[f][0]] = list(values)

    def raw_items(self):
        out = []
        t = self.mf.tags
        if t is not None:
            grouped: Dict[str, List[str]] = {}
            for k, v in t:
                K = k.upper()
                if K in ("METADATA_BLOCK_PICTURE", "COVERART"):
                    v = picture_desc("", len(v) * 3 // 4)
                grouped.setdefault(K, []).append(v)
            out = [(k, join_values(v)) for k, v in grouped.items()]
        for i, p in enumerate(getattr(self.mf, "pictures", None) or [], 1):
            out.append((f"PICTURE#{i}", picture_desc(p.mime, len(p.data))))
        return out

    def raw_set(self, key, values):
        self.ensure_tags()[key.strip().upper()] = list(values)

    def raw_delete(self, key):
        m = re.fullmatch(r"PICTURE#(\d+)", key)
        if m and hasattr(self.mf, "pictures"):
            pics = list(self.mf.pictures)
            del pics[int(m.group(1)) - 1]
            self.mf.clear_pictures()
            for p in pics:
                self.mf.add_picture(p)
            return
        self._del(key)

    def pictures(self):
        out = [(p.mime, len(p.data)) for p in (getattr(self.mf, "pictures", None) or [])]
        for v in self._vals("METADATA_BLOCK_PICTURE"):
            try:
                p = Picture(base64.b64decode(v))
                out.append((p.mime, len(p.data)))
            except Exception:
                out.append(("?", len(v) * 3 // 4))
        for v in self._vals("COVERART"):
            out.append(("?", len(v) * 3 // 4))
        return out

    def clear_pictures(self):
        if hasattr(self.mf, "clear_pictures"):
            self.mf.clear_pictures()
        for k in self.PIC_KEYS:
            self._del(k)

    def add_picture(self, data, mime):
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.desc = "Cover"
        pic.data = data
        if isinstance(self.mf, FLAC):
            self.mf.add_picture(pic)
        else:
            self.ensure_tags()["METADATA_BLOCK_PICTURE"] = [
                base64.b64encode(pic.write()).decode("ascii")]


class ID3Backend(Backend):
    """MP3, WAV, AIFF, TTA, DSF : ID3v2.4 en UTF-8."""
    kind = "ID3v2"
    TEXT = {"title": "TIT2", "artist": "TPE1", "albumartist": "TPE2", "album": "TALB",
            "date": "TDRC", "genre": "TCON", "composer": "TCOM"}

    def get(self, f):
        t = self.mf.tags
        if t is None:
            return []
        if f in self.TEXT:
            fr = t.get(self.TEXT[f])
            if fr is None:
                return []
            if f == "genre":
                return [g for g in fr.genres if g]
            return [str(x) for x in fr.text if str(x)]
        if f == "comment":
            for fr in t.getall("COMM"):
                if fr.desc == "":
                    return [str(x) for x in fr.text if str(x)]
            return []
        if f == "lyrics":
            return [fr.text for fr in t.getall("USLT") if fr.text][:1]
        fr = t.get("TRCK" if f in ("tracknumber", "tracktotal") else "TPOS")
        if fr is None or not fr.text:
            return []
        n, tot = split_num(str(fr.text[0]))
        v = n if f in ("tracknumber", "discnumber") else tot
        return [v] if v else []

    def set(self, f, values):
        t = self.ensure_tags()
        if f in self.TEXT:
            fid = self.TEXT[f]
            t.delall(fid)
            if f == "date":
                t.delall("TYER")
                t.delall("TDAT")
            if values:
                t.add(ID3Frames[fid](encoding=3, text=list(values)))
            return
        if f == "comment":
            for k in [k for k, fr in t.items()
                      if k.startswith("COMM") and getattr(fr, "desc", "") == ""]:
                del t[k]
            if values:
                t.add(COMM(encoding=3, lang="eng", desc="", text=list(values)))
            return
        if f == "lyrics":
            t.delall("USLT")
            if values:
                t.add(USLT(encoding=3, lang="eng", desc="", text="\n".join(values)))
            return
        if f in ("tracknumber", "tracktotal"):
            fid, numf, totf = "TRCK", "tracknumber", "tracktotal"
        else:
            fid, numf, totf = "TPOS", "discnumber", "disctotal"
        n, tot = self.get(numf), self.get(totf)
        if f == numf:
            n = values[:1]
        else:
            tot = values[:1]
        t.delall(fid)
        if n:
            t.add(ID3Frames[fid](encoding=3, text=[n[0] + ("/" + tot[0] if tot else "")]))
        elif tot:
            raise TagError(f"en ID3, « {LABEL[totf]} » exige un « {LABEL[numf]} »")

    def raw_items(self):
        t = self.mf.tags
        if t is None:
            return []
        out = []
        for key, fr in sorted(t.items(), key=lambda kv: kv[0]):
            fid = getattr(fr, "FrameID", key[:4])
            if fid == "APIC":
                val = picture_desc(fr.mime, len(fr.data))
            elif fid == "TCON":
                val = join_values(fr.genres)
            elif isinstance(getattr(fr, "text", None), list):
                val = join_values(str(x) for x in fr.text)
            elif isinstance(getattr(fr, "text", None), str):
                val = fr.text
            elif hasattr(fr, "url"):
                val = fr.url
            else:
                val = "[données binaires]"
            out.append((key, val))
        return out

    def raw_set(self, key, values):
        t = self.ensure_tags()
        K = key.strip()
        U = K.upper()
        if U.startswith("TXXX:"):
            t.add(TXXX(encoding=3, desc=K[5:], text=list(values)))
        elif len(U) == 4 and U in ID3Frames and U.startswith("T") and U != "TXXX":
            t.delall(U)
            t.add(ID3Frames[U](encoding=3, text=list(values)))
        else:
            t.add(TXXX(encoding=3, desc=K, text=list(values)))

    def raw_delete(self, key):
        t = self.mf.tags
        if t is None:
            return
        if key in t:
            del t[key]
        else:
            t.delall(key)

    def pictures(self):
        t = self.mf.tags
        return [(fr.mime, len(fr.data)) for fr in t.getall("APIC")] if t is not None else []

    def clear_pictures(self):
        if self.mf.tags is not None:
            self.mf.tags.delall("APIC")

    def add_picture(self, data, mime):
        self.ensure_tags().add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))

    def save(self):
        if isinstance(self.mf, ID3FileType):
            self.mf.save(v1=0)        # supprime au passage les vieux tags ID3v1
        else:
            self.mf.save()            # WAV / AIFF / DSF


class MP4Backend(Backend):
    """M4A / ALAC / AAC : atomes iTunes."""
    kind = "MP4 (iTunes)"
    TEXT = {"title": "\xa9nam", "artist": "\xa9ART", "albumartist": "aART",
            "album": "\xa9alb", "date": "\xa9day", "genre": "\xa9gen",
            "composer": "\xa9wrt", "comment": "\xa9cmt", "lyrics": "\xa9lyr"}

    def get(self, f):
        t = self.mf.tags
        if t is None:
            return []
        if f in self.TEXT:
            v = [str(x) for x in t.get(self.TEXT[f], []) if str(x)]
            if f == "genre" and not v and "gnre" in t:
                v = [TCON.GENRES[g - 1] for g in t["gnre"] if 0 < g <= len(TCON.GENRES)]
            return v
        key = "trkn" if f in ("tracknumber", "tracktotal") else "disk"
        if not t.get(key):
            return []
        n, tot = t[key][0]
        v = n if f in ("tracknumber", "discnumber") else tot
        return [str(v)] if v else []

    def set(self, f, values):
        t = self.ensure_tags()
        if f in self.TEXT:
            k = self.TEXT[f]
            if k in t:
                del t[k]
            if f == "genre" and "gnre" in t:
                del t["gnre"]
            if values:
                t[k] = list(values)
            return
        if f in ("tracknumber", "tracktotal"):
            key, numf, totf = "trkn", "tracknumber", "tracktotal"
        else:
            key, numf, totf = "disk", "discnumber", "disctotal"

        def as_int(vals):
            if not vals:
                return 0
            if not vals[0].isdigit():
                raise TagError(f"« {vals[0]} » n'est pas un nombre")
            return int(vals[0])

        n, tot = as_int(self.get(numf)), as_int(self.get(totf))
        if f == numf:
            n = as_int(values)
        else:
            tot = as_int(values)
        if key in t:
            del t[key]
        if n or tot:
            t[key] = [(n, tot)]

    def raw_items(self):
        t = self.mf.tags
        if t is None:
            return []
        out = []
        for k, v in sorted(t.items(), key=lambda kv: kv[0]):
            if k == "covr":
                for c in v:
                    mime = "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
                    out.append((k, picture_desc(mime, len(c))))
                continue
            if k.startswith("----"):
                val = join_values(bytes(x).decode("utf-8", "replace") for x in v)
            elif k in ("trkn", "disk"):
                val = join_values(f"{a}/{b}" if b else str(a) for a, b in v)
            elif isinstance(v, list):
                val = join_values(v)
            else:
                val = str(v)
            out.append((k, val))
        return out

    def raw_set(self, key, values):
        t = self.ensure_tags()
        if key.startswith("----:") or len(key) != 4:
            k = key if key.startswith("----:") else "----:com.apple.iTunes:" + key
            t[k] = [MP4FreeForm(v.encode("utf-8")) for v in values]
        else:
            t[key] = list(values)

    def raw_delete(self, key):
        t = self.mf.tags
        if t is not None and key in t:
            del t[key]

    def pictures(self):
        t = self.mf.tags
        return [("image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg", len(c))
                for c in (t.get("covr", []) if t is not None else [])]

    def clear_pictures(self):
        t = self.mf.tags
        if t is not None and "covr" in t:
            del t["covr"]

    def add_picture(self, data, mime):
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        self.ensure_tags()["covr"] = [MP4Cover(data, imageformat=fmt)]


class APEBackend(Backend):
    """APE, WavPack, Musepack : tags APEv2."""
    kind = "APEv2"
    TEXT = {"title": "Title", "artist": "Artist", "albumartist": "Album Artist",
            "album": "Album", "date": "Year", "genre": "Genre",
            "composer": "Composer", "comment": "Comment", "lyrics": "Lyrics"}

    def _vals(self, key):
        t = self.mf.tags
        if t is None or key not in t or t[key].kind == APE_BINARY:
            return []
        return [x for x in str(t[key]).split("\x00") if x]

    def _put(self, key, values):
        t = self.ensure_tags()
        if key in t:
            del t[key]
        if values:
            t[key] = "\x00".join(values)

    def get(self, f):
        if f in self.TEXT:
            return self._vals(self.TEXT[f])
        v = self._vals("Track" if f in ("tracknumber", "tracktotal") else "Disc")
        if not v:
            return []
        n, tot = split_num(v[0])
        r = n if f in ("tracknumber", "discnumber") else tot
        return [r] if r else []

    def set(self, f, values):
        if f in self.TEXT:
            self._put(self.TEXT[f], list(values))
            return
        if f in ("tracknumber", "tracktotal"):
            key, numf, totf = "Track", "tracknumber", "tracktotal"
        else:
            key, numf, totf = "Disc", "discnumber", "disctotal"
        n, tot = self.get(numf), self.get(totf)
        if f == numf:
            n = values[:1]
        else:
            tot = values[:1]
        if n:
            self._put(key, [n[0] + ("/" + tot[0] if tot else "")])
        elif tot:
            raise TagError(f"en APEv2, « {LABEL[totf]} » exige un « {LABEL[numf]} »")
        else:
            self._put(key, [])

    def raw_items(self):
        t = self.mf.tags
        if t is None:
            return []
        out = []
        for k in sorted(t.keys(), key=str.lower):
            v = t[k]
            if v.kind == APE_BINARY:
                size = len(v.value)
                val = picture_desc("", size) if k.lower().startswith("cover art") else "[données binaires]"
            else:
                val = join_values(x for x in str(v).split("\x00") if x)
            out.append((k, val))
        return out

    def raw_set(self, key, values):
        self._put(key.strip(), list(values))

    def raw_delete(self, key):
        t = self.mf.tags
        if t is not None and key in t:
            del t[key]

    def _pic_keys(self):
        t = self.mf.tags
        if t is None:
            return []
        return [k for k in t.keys() if k.lower().startswith("cover art") and t[k].kind == APE_BINARY]

    def pictures(self):
        t = self.mf.tags
        out = []
        for k in self._pic_keys():
            raw = t[k].value
            data = raw.split(b"\x00", 1)[1] if b"\x00" in raw[:256] else raw
            out.append((image_mime(data) or "?", len(data)))
        return out

    def clear_pictures(self):
        for k in self._pic_keys():
            del self.mf.tags[k]

    def add_picture(self, data, mime):
        name = b"cover.png" if mime == "image/png" else b"cover.jpg"
        self.ensure_tags()["Cover Art (Front)"] = APEValue(name + b"\x00" + data, APE_BINARY)


def _asf_picture(blob: bytes) -> Tuple[str, bytes]:
    """WM/Picture : type (1 o) + taille (4 o) + mime UTF-16 + description UTF-16 + données."""
    try:
        pos, strings = 5, []
        for _ in range(2):
            end = pos
            while blob[end:end + 2] != b"\x00\x00":
                end += 2
            strings.append(blob[pos:end].decode("utf-16-le"))
            pos = end + 2
        return strings[0], blob[pos:]
    except Exception:
        return "?", blob[5:]


class ASFBackend(Backend):
    """WMA."""
    kind = "ASF (WMA)"
    TEXT = {"title": "Title", "artist": "Author", "albumartist": "WM/AlbumArtist",
            "album": "WM/AlbumTitle", "date": "WM/Year", "genre": "WM/Genre",
            "composer": "WM/Composer", "comment": "Description", "lyrics": "WM/Lyrics"}

    def _vals(self, key):
        t = self.mf.tags
        if t is None or key not in t:
            return []
        return [str(x) for x in t[key] if str(x)]

    def _put(self, key, values):
        t = self.mf.tags
        if key in t:
            del t[key]
        if values:
            t[key] = list(values)

    def get(self, f):
        if f in self.TEXT:
            return self._vals(self.TEXT[f])
        if f == "tracknumber":
            return self._vals("WM/TrackNumber")[:1]
        if f == "tracktotal":
            return []
        v = self._vals("WM/PartOfSet")
        if not v:
            return []
        n, tot = split_num(v[0])
        r = n if f == "discnumber" else tot
        return [r] if r else []

    def set(self, f, values):
        if f in self.TEXT:
            self._put(self.TEXT[f], list(values))
        elif f == "tracknumber":
            self._put("WM/TrackNumber", list(values[:1]))
        elif f == "tracktotal":
            if values:
                raise TagError("le WMA ne stocke pas le total de pistes")
        else:
            n, tot = self.get("discnumber"), self.get("disctotal")
            if f == "discnumber":
                n = values[:1]
            else:
                tot = values[:1]
            self._put("WM/PartOfSet", [n[0] + ("/" + tot[0] if tot else "")] if n else [])

    def raw_items(self):
        t = self.mf.tags
        out = []
        for k in sorted(t.keys()):
            if k == "WM/Picture":
                for v in t[k]:
                    mime, data = _asf_picture(v.value)
                    out.append((k, picture_desc(mime, len(data))))
                continue
            vals = []
            for v in t[k]:
                vals.append("[données binaires]" if isinstance(v, ASFByteArrayAttribute) else str(v))
            out.append((k, join_values(vals)))
        return out

    def raw_set(self, key, values):
        self._put(key.strip(), list(values))

    def raw_delete(self, key):
        if key in self.mf.tags:
            del self.mf.tags[key]

    def pictures(self):
        out = []
        for v in self.mf.tags.get("WM/Picture", []):
            mime, data = _asf_picture(v.value)
            out.append((mime, len(data)))
        return out

    def clear_pictures(self):
        self.raw_delete("WM/Picture")

    def add_picture(self, data, mime):
        blob = (struct.pack("<bi", 3, len(data)) + (mime + "\0").encode("utf-16-le")
                + "\0".encode("utf-16-le") + data)
        self.mf.tags["WM/Picture"] = [ASFByteArrayAttribute(blob)]


def make_backend(mf) -> Backend:
    if isinstance(mf, (FLAC, OggVorbis, OggOpus, OggFLAC, OggSpeex)):
        return VorbisBackend(mf)
    if isinstance(mf, MP4):
        return MP4Backend(mf)
    if isinstance(mf, ASF):
        return ASFBackend(mf)
    if isinstance(mf, APEv2File):
        return APEBackend(mf)
    id3_types = [ID3FileType, WAVE, AIFF] + ([DSF] if DSF else [])
    if isinstance(mf, tuple(id3_types)):
        return ID3Backend(mf)
    raise TagError("format sans tags modifiables")


class AudioFile:
    def __init__(self, path: str):
        self.path = path
        self.load()

    def load(self):
        mf = mutagen.File(self.path)
        if mf is None:
            raise TagError("format non reconnu")
        self.mf = mf
        self.backend = make_backend(mf)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def dir(self) -> str:
        return os.path.dirname(self.path)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1]

    def get(self, f: str) -> List[str]:
        try:
            return self.backend.get(f)
        except Exception:
            return []

    def first(self, f: str) -> str:
        v = self.get(f)
        return v[0] if v else ""

    def length(self) -> float:
        return getattr(self.mf.info, "length", 0) or 0

    def fmt_name(self) -> str:
        name = FORMAT_NAMES.get(type(self.mf).__name__, type(self.mf).__name__.upper())
        if name == "M4A":
            codec = getattr(self.mf.info, "codec", "") or ""
            name = "ALAC" if codec.startswith("alac") else "AAC"
        return name

    def fmt_short(self) -> str:
        name, info = self.fmt_name(), self.mf.info
        if name in LOSSLESS_NAMES:
            bits = getattr(info, "bits_per_sample", 0)
            sr = getattr(info, "sample_rate", 0)
            return f"{name} {bits}/{sr / 1000:g}" if bits and sr else name
        br = getattr(info, "bitrate", 0)
        return f"{name} {round(br / 1000)}k" if br else name

    def tech_line(self) -> str:
        i = self.mf.info
        parts = [self.fmt_name()]
        bits = getattr(i, "bits_per_sample", 0)
        if bits:
            parts.append(f"{bits} bits")
        sr = getattr(i, "sample_rate", 0)
        if sr:
            parts.append(f"{sr / 1000:g} kHz".replace(".", ","))
        ch = getattr(i, "channels", 0)
        if ch:
            parts.append({1: "mono", 2: "stéréo"}.get(ch, f"{ch} canaux"))
        br = getattr(i, "bitrate", 0)
        if br:
            parts.append(f"{round(br / 1000)} kb/s")
        parts.append(fmt_dur(self.length()))
        try:
            parts.append(human_size(os.path.getsize(self.path)))
        except OSError:
            pass
        parts.append(f"tags {self.backend.kind}")
        return "  ·  ".join(parts)


# ═══════════════════════════ Recherche des fichiers ═══════════════════════════

def _walk(folder: str):
    """os.walk qui saute les dossiers cachés et ceux contenant un .ignore."""
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted((d for d in dirs if not d.startswith(".")
                          and not os.path.exists(os.path.join(root, d, ".ignore"))),
                         key=natural_key)
        yield root, files


def find_files(folder: str, recursive: bool, exts) -> List[str]:
    out = []
    if recursive:
        for root, files in _walk(folder):
            out += [os.path.join(root, f) for f in files
                    if not f.startswith(".") and os.path.splitext(f)[1].lower() in exts]
    else:
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        for f in names:
            p = os.path.join(folder, f)
            if not f.startswith(".") and os.path.splitext(f)[1].lower() in exts and os.path.isfile(p):
                out.append(p)
    out.sort(key=path_sort_key)
    return out


class _NullProgress:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add_task(self, *a, **k):
        return 0

    def advance(self, *a, **k):
        pass

    def update(self, *a, **k):
        pass


def make_progress(transient: bool = True):
    cols = [SpinnerColumn(style="magenta"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None, complete_style="magenta", finished_style="green")]
    cols.append(MofNCompleteColumn() if MofNCompleteColumn else TextColumn("{task.completed}/{task.total}"))
    cols.append(TimeElapsedColumn())
    return Progress(*cols, console=console, transient=transient)


def load_files(paths: List[str]):
    files, skipped = [], []
    prog = make_progress() if len(paths) > 30 and console.is_terminal else _NullProgress()
    with prog:
        task = prog.add_task("Lecture des tags", total=len(paths))
        for p in paths:
            try:
                files.append(AudioFile(p))
            except Exception as e:
                skipped.append((p, str(e) or type(e).__name__))
            prog.advance(task)
    return files, skipped


def group_by_dir(files):
    groups: Dict[str, list] = {}
    for af in files:
        groups.setdefault(af.dir, []).append(af)
    return list(groups.items())


# ═══════════════════════════════ Paroles : LRCLIB ═════════════════════════════
# ─── accès réseau ───
# Réponses de taille bornée, et jamais de redirection d'une adresse https vers http
# (le contenu pourrait alors être modifié en route).
MAX_JSON_BYTES = 16 << 20
MAX_IMAGE_BYTES = 64 << 20


class _NoDowngrade(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.full_url.startswith("https://") and not newurl.startswith("https://"):
            raise urllib.error.URLError("redirection vers une adresse non chiffrée refusée")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_NoDowngrade)


def url_open(req, timeout: float):
    return _OPENER.open(req, timeout=timeout)


def read_limited(r, limit: int) -> bytes:
    data = r.read(limit + 1)
    if len(data) > limit:
        raise urllib.error.URLError(f"réponse trop grosse (plus de {limit >> 20} Mo)")
    return data


# API publique et gratuite : https://lrclib.net — pas de clé, juste un User-Agent.

LRCLIB_URL = os.environ.get("MUSITAG_LRCLIB_URL", "https://lrclib.net/api").rstrip("/")
USER_AGENT = f"musitag/{VERSION} (outil perso de tags audio)"
LRCLIB_WORKERS = 3                # recherches en parallèle
LRCLIB_INTERVAL = 0.25            # écart minimum entre deux requêtes, tous threads confondus
MAX_DURATION_GAP = 15             # au-delà, ce n'est pas la même version du morceau

_LRC_TS = r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]"
LRC_LINE = re.compile(r"^\s*((?:" + _LRC_TS + r")+)(.*)$")
LRC_TS_ALL = re.compile(_LRC_TS)
LRC_META = re.compile(r"^\s*\[(?:ar|ti|al|au|by|re|ve|offset|length|#)\s*:", re.I)
_VARIANT_WORDS = ("live", "remix", "acoustic", "acoustique", "instrumental", "karaoke",
                  "demo", "cover", "sped up", "slowed", "nightcore", "reprise")


def is_synced(text: str) -> bool:
    return any(LRC_LINE.match(l) for l in text.splitlines())


def lyric_lines(text: str) -> List[str]:
    out = []
    for l in text.splitlines():
        if LRC_META.match(l):
            continue
        t = LRC_TS_ALL.sub("", l).strip()
        if t:
            out.append(t)
    return out


INSTRUMENTAL_TEXT = "♪ Musique sans paroles ♪"


def is_instrumental_mark(text: str) -> bool:
    return [l.strip() for l in lyric_lines(text or "")] == [INSTRUMENTAL_TEXT]


def lyrics_summary(text: str) -> str:
    if not text or not text.strip():
        return ""
    if is_instrumental_mark(text):
        return "♪ instrumental"
    n = len(lyric_lines(text))
    kind = "synchronisées" if is_synced(text) else "texte"
    return f"{kind} · {n} ligne{'s' if n > 1 else ''}"


def get_lyrics(af) -> str:
    return "\n".join(af.get("lyrics")).strip()


def oneline(v: str, limit: int = 300) -> str:
    """Valeur sur une ligne pour les tableaux (les paroles font des dizaines de lignes)."""
    if "\n" in v:
        lines = [l for l in v.splitlines() if l.strip()]
        v = (lines[0] if lines else "") + f"  … ({len(lines)} lignes)"
    return v if len(v) <= limit else v[:limit] + "…"


class LrclibError(Exception):
    pass


_RATE_LOCK = threading.Lock()
_next_slot = 0.0
_interval = LRCLIB_INTERVAL


def _throttle() -> None:
    """Espace les requêtes de tous les threads (LRCLIB renvoie 429 si on va trop vite)."""
    global _next_slot
    with _RATE_LOCK:
        now = time.monotonic()
        wait = _next_slot - now
        _next_slot = max(now, _next_slot) + _interval
    if wait > 0:
        time.sleep(wait)


def _slow_down(pause: float) -> None:
    """LRCLIB freine (429 / 503) : tout le monde fait une pause, puis on ralentit le rythme."""
    global _next_slot, _interval
    with _RATE_LOCK:
        _interval = min(_interval * 1.5, 3.0)
        _next_slot = max(_next_slot, time.monotonic() + pause)


def _speed_up() -> None:
    """Ça passe : on réaccélère doucement, sans descendre sous le rythme de base."""
    global _interval
    with _RATE_LOCK:
        _interval = max(LRCLIB_INTERVAL, _interval * 0.95)


def _http_error_message(code: int) -> str:
    if code == 429:
        return "LRCLIB limite les requêtes (HTTP 429)"
    if code in (502, 503, 504):
        return f"LRCLIB surchargé (HTTP {code})"
    return f"erreur LRCLIB (HTTP {code})"


def lrclib_request(path: str, params: Dict[str, str]):
    """GET sur l'API LRCLIB. Renvoie None si 404 (rien trouvé).
    Réessaie tout seul : patiemment sur 429 / 503 (serveur qui freine), plus vite sur
    les autres erreurs serveur ou réseau."""
    url = f"{LRCLIB_URL}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last = "erreur inconnue"
    attempts = 6
    for attempt in range(attempts):
        wait = 2 ** attempt                      # 1, 2, 4, 8, 16 s
        _throttle()
        try:
            with url_open(req, timeout=20) as r:
                data = json.loads(read_limited(r, MAX_JSON_BYTES).decode("utf-8"))
            _speed_up()
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _speed_up()
                return None
            last = _http_error_message(e.code)
            if e.code not in (429, 500, 502, 503, 504):
                break
            if e.code in (429, 503):
                pause = (2, 4, 8, 15, 25, 30)[attempt]
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra and ra.strip().isdigit():
                    pause = max(pause, min(int(ra), 60))
                _slow_down(pause)            # l'attente se fait dans _throttle()
                continue
        except ValueError:
            last = "réponse illisible de LRCLIB"
        except OSError as e:                     # URLError, timeout, connexion coupée…
            last = "LRCLIB injoignable (" + (str(getattr(e, "reason", "") or e) or type(e).__name__) + ")"
        if attempt < attempts - 1:
            time.sleep(wait)
    raise LrclibError(last)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", s or "").casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ").replace("’", "'")
    s = re.sub(r"[^\w']+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def base_title(title: str) -> str:
    """« Loser (2016 Remaster) » → « Loser », « Song - Radio Edit » → « Song »."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "")
    t = re.sub(r"\s+-\s+.*\b(remaster\w*|version|edit|mix|live|mono|stereo|single|radio)\b.*$",
               "", t, flags=re.I)
    t = re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", "", t, flags=re.I)
    return t.strip() or (title or "").strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def _artist_sim(ours: List[str], theirs: str) -> float:
    t = _norm(theirs)
    best = 0.0
    for a in ours:
        n = _norm(a)
        if not n or not t:
            continue
        if n == t:
            return 1.0
        # « Sickmode » dans « Rooler, Sickmode & Paul Elstak »
        if re.search(r"(?:^| )" + re.escape(n) + r"(?: |$)", t) or re.search(r"(?:^| )" + re.escape(t) + r"(?: |$)", n):
            best = max(best, 0.9)
        best = max(best, difflib.SequenceMatcher(None, n, t).ratio())
    return best


@dataclass
class LyricsMatch:
    rec: dict
    exact: bool
    title_sim: float
    artist_sim: float
    diff: Optional[float]
    score: float

    @property
    def text(self) -> str:
        return (self.rec.get("syncedLyrics") or self.rec.get("plainLyrics") or "").strip()

    @property
    def kind(self) -> str:
        if self.rec.get("syncedLyrics"):
            return "synced"
        return "plain" if self.rec.get("plainLyrics") else ""

    def describe(self) -> str:
        s = f"{self.rec.get('artistName', '?')} – {self.rec.get('trackName', '?')}"
        if self.diff is not None and self.diff >= 1:
            s += f" · Δ {self.diff:.0f} s"
        return s


@dataclass
class LyricsResult:
    af: object
    status: str                    # exact | approx | instrumental | notfound | skip | error
    match: Optional[LyricsMatch] = None
    candidates: List[LyricsMatch] = dc_field(default_factory=list)
    message: str = ""


def _evaluate(rec: dict, title: str, artists: List[str], dur: float, exact: bool = False) -> LyricsMatch:
    theirs = rec.get("trackName") or rec.get("name") or ""
    ts = max(_sim(title, theirs), _sim(base_title(title), base_title(theirs)))
    asim = _artist_sim(artists, rec.get("artistName") or "")
    try:
        diff = abs(float(rec.get("duration") or 0) - dur) if dur and rec.get("duration") else None
    except (TypeError, ValueError):
        diff = None
    ours_n, theirs_n = _norm(title), _norm(theirs)
    penalty = sum(1.0 for w in _VARIANT_WORDS
                  if re.search(r"\b" + re.escape(w) + r"\b", theirs_n)
                  and not re.search(r"\b" + re.escape(w) + r"\b", ours_n))
    score = (ts * 3 + asim * 2 - (diff / 5 if diff is not None else 1.0)
             + (0.5 if rec.get("syncedLyrics") else 0.0) - penalty)
    return LyricsMatch(rec, exact, ts, asim, diff, score)


def _acceptable(m: LyricsMatch, strict: bool = True) -> bool:
    if strict and m.diff is not None and m.diff > MAX_DURATION_GAP:
        return False
    return m.title_sim >= 0.85 and m.artist_sim >= 0.5


def lrclib_lookup(af, title: Optional[str] = None, artist: Optional[str] = None) -> LyricsResult:
    """Cherche les paroles d'un fichier : correspondance exacte d'abord
    (titre + artiste + durée à ±2 s), puis recherche élargie.
    Avec un titre/artiste tapé à la main, l'écart de durée n'élimine plus rien
    (il reste affiché pour que tu juges)."""
    strict = title is None and artist is None
    title = (af.first("title") if title is None else title).strip()
    artists = [artist] if artist else af.get("artist") + af.get("albumartist")
    artists = [a.strip() for a in dict.fromkeys(artists) if a and a.strip()]
    album, dur = af.first("album"), af.length()
    if not title or not artists:
        return LyricsResult(af, "skip", message="titre ou artiste manquant")
    main = artists[0]
    try:
        # 1) correspondance exacte
        rec = None
        if dur:
            params = {"track_name": title, "artist_name": main, "duration": str(round(dur))}
            if album:
                rec = lrclib_request("get", {**params, "album_name": album})
            if rec is None:
                rec = lrclib_request("get", params)
        exact_m = None
        if isinstance(rec, dict):
            if rec.get("instrumental"):
                m = _evaluate(rec, title, artists, dur, exact=True)
                return LyricsResult(af, "instrumental", m, [m])
            exact_m = _evaluate(rec, title, artists, dur, exact=True)
            if exact_m.kind == "synced":
                return LyricsResult(af, "exact", exact_m, [exact_m])
            if not exact_m.kind:
                exact_m = None

        # 2) recherche élargie (ou version synchronisée d'un résultat « texte seul »)
        bt = base_title(title)
        queries = [{"track_name": bt, "artist_name": main}, {"q": f"{main} {bt}"}, {"track_name": bt}]
        seen = {exact_m.rec.get("id")} if exact_m else set()
        found: List[LyricsMatch] = []
        instrumental = None
        for q in queries:
            res = lrclib_request("search", q) or []
            for r in res if isinstance(res, list) else []:
                if not isinstance(r, dict) or r.get("id") in seen:
                    continue
                seen.add(r.get("id"))
                m = _evaluate(r, title, artists, dur)
                if not _acceptable(m, strict):
                    continue
                if r.get("instrumental") or not m.kind:
                    if r.get("instrumental") and m.diff is not None and m.diff <= 3:
                        instrumental = instrumental or m
                    continue
                found.append(m)
            if found:
                break
        found.sort(key=lambda m: -m.score)

        if exact_m:
            for m in found:     # même morceau, même durée, mais synchronisé : on prend
                if (m.kind == "synced" and m.diff is not None and m.diff <= 2
                        and m.title_sim >= 0.95 and m.artist_sim >= 0.8):
                    m.exact = True
                    return LyricsResult(af, "exact", m, [m, exact_m] + [x for x in found if x is not m][:6])
            return LyricsResult(af, "exact", exact_m, [exact_m] + found[:7])
        if found:
            return LyricsResult(af, "approx", found[0], found[:8])
        if instrumental:
            return LyricsResult(af, "instrumental", instrumental, [instrumental])
        return LyricsResult(af, "notfound")
    except LrclibError as e:
        return LyricsResult(af, "error", message=str(e))
    except Exception as e:
        return LyricsResult(af, "error", message=str(e) or type(e).__name__)


def lrclib_ping() -> Optional[str]:
    """None si LRCLIB répond, sinon le message d'erreur (test rapide, sans réessai)."""
    req = urllib.request.Request(f"{LRCLIB_URL}/search?" + urllib.parse.urlencode({"q": "musitag"}),
                                 headers={"User-Agent": USER_AGENT})
    try:
        with url_open(req, timeout=10):
            return None
    except urllib.error.HTTPError:
        return None                    # le serveur a répondu : il est joignable
    except OSError as e:
        return str(getattr(e, "reason", "") or e) or type(e).__name__


def lyrics_fetch_all(files) -> List[LyricsResult]:
    results: List[Optional[LyricsResult]] = [None] * len(files)
    prog = make_progress() if console.is_terminal else _NullProgress()
    ex = ThreadPoolExecutor(max_workers=LRCLIB_WORKERS)
    streak, aborted = 0, False
    try:
        with prog:
            task = prog.add_task("Recherche sur LRCLIB", total=len(files))
            futs = {ex.submit(lrclib_lookup, af): i for i, af in enumerate(files)}
            for fut in as_completed(futs):
                i = futs[fut]
                if fut.cancelled():
                    results[i] = LyricsResult(files[i], "error", message="recherche interrompue")
                else:
                    try:
                        results[i] = fut.result()
                    except Exception as e:
                        results[i] = LyricsResult(files[i], "error", message=str(e) or type(e).__name__)
                # LRCLIB ne répond plus : on arrête au lieu d'attendre chaque fichier
                streak = streak + 1 if results[i].status == "error" else 0
                if streak >= 8 and not aborted:
                    aborted = True
                    for f in futs:
                        f.cancel()
                prog.advance(task)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return results  # type: ignore


# ══════════════════════════════ Interface terminal ════════════════════════════

def _rl_prompt(markup: str) -> str:
    with console.capture() as cap:
        console.print(markup, end="")
    s = cap.get()
    if readline is not None:      # séquences de couleur invisibles pour readline
        s = re.sub(r"(\x1b\[[0-9;]*m)", "\x01\\1\x02", s)
    return s


def ask(markup: str, prefill: str = "") -> str:
    """Saisie avec édition (flèches, historique). `prefill` = texte déjà tapé."""
    prompt = _rl_prompt(markup)
    if readline is None:
        if prefill:
            console.print(f"   [dim](actuel : {escape(prefill)} — Entrée = garder, « - » = vider)[/]")
        try:
            r = input(prompt)
        except EOFError:
            console.print()
            raise Cancel()
        if prefill:
            if r == "":
                return prefill
            if r.strip() == "-":
                return ""
        return r
    if prefill:
        readline.set_startup_hook(lambda: readline.insert_text(prefill))
    try:
        return input(prompt)
    except EOFError:
        console.print()
        raise Cancel()
    finally:
        readline.set_startup_hook(None)


def confirm(msg: str, default: bool = True) -> bool:
    hint = "O/n" if default else "o/N"
    while True:
        r = ask(f"{msg} [dim]\\[{hint}][/] ").strip().lower()
        if not r:
            return default
        if r in ("o", "oui", "y", "yes"):
            return True
        if r in ("n", "non", "no"):
            return False


def pause(msg: str = "Entrée pour revenir…") -> None:
    try:
        ask(f"[dim]{msg}[/]")
    except Cancel:
        pass


def menu(title: str, options, default: Optional[str] = None) -> str:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(style="dim")
    keys = []
    for opt in options:
        if opt is None:
            grid.add_row("", "", "")
            continue
        k, label, hint = opt
        keys.append(k.lower())
        badge = Text(f" {k} ", style="bold black on grey62" if k == "0" else "bold black on cyan")
        grid.add_row(badge, Text(label), Text(hint))
    console.print(Panel(grid, title=Text(f" {title} ", style="bold"), title_align="left",
                        border_style="cyan", box=box.ROUNDED, padding=(0, 1), expand=False))
    while True:
        r = ask("[bold magenta]›[/] ").strip().lower()
        if not r and default is not None:
            return default
        if r == "q" and "0" in keys:
            r = "0"
        if r in keys:
            return r
        console.print(f"[red]Choix invalide[/] : {escape(r) or '(vide)'}")


def info(msg: str, style: str = "yellow") -> None:
    console.print(Panel(Text.from_markup(msg), border_style=style, box=box.ROUNDED, expand=False))


def track_label(af: AudioFile) -> str:
    tr, disc, dt = af.first("tracknumber"), af.first("discnumber"), af.first("disctotal")
    trs = tr.zfill(2) if tr.isdigit() else tr
    multi = (dt.isdigit() and int(dt) > 1) or (disc.isdigit() and int(disc) > 1)
    return f"{disc}-{trs}" if disc and multi else trs


def files_table(fl, start: int = 1, title: Optional[str] = None, compact: bool = False,
                total: Optional[int] = None):
    """Colonnes à largeur fixe : les tableaux de plusieurs dossiers restent alignés."""
    wide = console.width >= 110
    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False,
              title=Text(title, style="bold cyan") if title else None, title_justify="left")
    t.add_column("#", style="dim", justify="right", no_wrap=True,
                 width=max(1, len(str(total or (start + len(fl) - 1)))))
    t.add_column("N°", justify="right", no_wrap=True, style="cyan", width=4)
    t.add_column("Titre", ratio=3, no_wrap=True, overflow="ellipsis")
    if compact:
        t.add_column("Fichier", ratio=3, no_wrap=True, overflow="ellipsis", style="dim")
    else:
        t.add_column("Artiste", ratio=2, no_wrap=True, overflow="ellipsis")
        t.add_column("Album", ratio=2, no_wrap=True, overflow="ellipsis")
        t.add_column("Date", no_wrap=True, style="green", width=10)
        if wide:
            t.add_column("Genre", ratio=2, no_wrap=True, overflow="ellipsis", style="yellow")
            t.add_column("♪", no_wrap=True, width=1)
    t.add_column("Durée", justify="right", no_wrap=True, style="dim", width=7)
    if wide and not compact:
        t.add_column("Format", no_wrap=True, style="dim", width=12)
    for i, af in enumerate(fl, start):
        tv = af.get("title")
        tcell = Text(join_values(tv)) if tv else Text(af.name, style="dim italic")
        row = [str(i), track_label(af), tcell]
        if compact:
            row.append(Text(af.name))
        else:
            row += [Text(join_values(af.get("artist"))), Text(join_values(af.get("album"))),
                    Text(af.first("date"))]
            if wide:
                row.append(Text(join_values(af.get("genre"))))
                lyr = get_lyrics(af)
                row.append(Text("♪", style="magenta") if lyr and is_instrumental_mark(lyr)
                           else Text("●", style="green") if lyr and is_synced(lyr)
                           else Text("○", style="cyan") if lyr else Text(""))
        row.append(fmt_dur(af.length()))
        if wide and not compact:
            row.append(af.fmt_short())
        t.add_row(*row)
    return t


def detail_panel(af: AudioFile, pos: Optional[int] = None, total: Optional[int] = None):
    g = Table.grid(padding=(0, 2))
    g.add_column(style="bold cyan", justify="right", no_wrap=True)
    g.add_column(overflow="fold")
    for f, label in FIELDS:
        v = af.get(f)
        g.add_row(label, Text(join_values(v)) if v else Text("—", style="dim"))
    pics = af.backend.pictures()
    g.add_row("Pochette", Text(", ".join(f"{m} · {human_size(s)}" for m, s in pics)) if pics
              else Text("aucune intégrée", style="dim"))
    lyr = get_lyrics(af)
    g.add_row("Paroles", Text(lyrics_summary(lyr), style="green" if is_synced(lyr) else "")
              if lyr else Text("aucune", style="dim"))

    items = af.backend.raw_items()
    raw = Table(box=box.SIMPLE_HEAD, header_style="dim", expand=True, pad_edge=False, show_edge=False)
    raw.add_column("Champ brut", style="cyan", no_wrap=True)
    raw.add_column("Valeur", overflow="fold")
    for k, v in items:
        raw.add_row(Text(k), Text(oneline(v)))
    parts = [g, Text(""), Rule(Text("tous les champs du fichier", style="dim"), style="dim"),
             raw if items else Text("  (aucun tag)", style="dim"),
             Rule(style="dim"), Text(af.tech_line(), style="dim")]
    return Panel(Group(*parts), title=Text(f" {af.name} ", style="bold"), title_align="left",
                 subtitle=Text(f" {pos}/{total} ", style="dim") if pos else None,
                 subtitle_align="right", border_style="magenta", box=box.ROUNDED, padding=(1, 2))


def print_long(renderables, wait: bool = True) -> None:
    """Affiche ; passe par un pager (less) si ça dépasse l'écran."""
    with console.capture() as cap:
        for r in renderables:
            console.print(r)
    lines = cap.get().count("\n")
    if console.is_terminal and lines > console.height - 3 and shutil.which("less"):
        pager = os.environ.get("PAGER", "")
        if not pager or pager.strip() == "less":
            os.environ["PAGER"] = "less -R"
        info("Liste longue : elle s'ouvre dans [bold]less[/] (flèches pour défiler, "
             "[bold]q[/] pour quitter).", "cyan")
        time.sleep(1.0)
        with console.pager(styles=True):
            for r in renderables:
                console.print(r)
    else:
        for r in renderables:
            console.print(r)
        if wait:
            pause()


# ═════════════════════════ Modifications : aperçu + écriture ══════════════════

@dataclass
class Op:
    af: AudioFile
    kind: str                  # set | raw_set | raw_delete | pics_clear | pic_add | wipe | rename
    key: str = ""
    values: List[str] = dc_field(default_factory=list)
    before: str = ""
    after: str = ""
    label: str = ""
    data: bytes = b""
    mime: str = ""


def normalize(f: str, values: List[str]) -> List[str]:
    values = [v.strip() for v in values if v and v.strip()]
    if f in NUM_FIELDS:
        values = values[:1]
        values = [str(int(v)) if v.isdigit() else v for v in values]
    return values


def op_set(af: AudioFile, f: str, values: List[str]) -> Optional[Op]:
    old = af.get(f)
    new = normalize(f, values)
    if old == new:
        return None
    return Op(af, "set", f, new, join_values(old), join_values(new), LABEL[f])


def preview(ops: List[Op], limit: int = 60) -> None:
    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False)
    t.add_column("Fichier", ratio=3, no_wrap=True, overflow="ellipsis", style="dim")
    t.add_column("Champ", no_wrap=True, style="cyan")
    t.add_column("Avant", ratio=3, overflow="fold")
    t.add_column("", no_wrap=True, style="dim")
    t.add_column("Après", ratio=3, overflow="fold")
    last = None
    for op in ops[:limit]:
        name = Text(op.af.name) if op.af is not last else Text("")
        last = op.af
        before = Text(op.before, style="red") if op.before else Text("—", style="dim")
        after = Text(op.after, style="bold green") if op.after else Text("(supprimé)", style="dim red")
        t.add_row(name, Text(op.label), before, "→", after)
    console.print(t)
    if len(ops) > limit:
        console.print(f"[dim]… et {len(ops) - limit} autres modifications[/]")
    nfiles = len({id(o.af) for o in ops})
    console.print(f"[bold]{len(ops)}[/] modification(s) sur [bold]{nfiles}[/] fichier(s)\n")


def _apply_file(af: AudioFile, fops: List[Op]) -> None:
    tag_ops = [o for o in fops if o.kind != "rename"]
    if any(o.kind == "wipe" for o in tag_ops):
        try:
            af.mf.delete()
        except (NotImplementedError, AttributeError):
            af.mf.tags.clear()
            af.mf.save()
        af.load()
        if isinstance(af.mf, FLAC) and af.mf.pictures:
            af.mf.clear_pictures()
            af.mf.save()
        tag_ops = [o for o in tag_ops if o.kind != "wipe"]
    if tag_ops:
        b = af.backend
        for o in tag_ops:
            if o.kind == "set":
                b.set(o.key, o.values)
            elif o.kind == "raw_set":
                b.raw_set(o.key, o.values)
            elif o.kind == "raw_delete":
                b.raw_delete(o.key)
            elif o.kind == "pics_clear":
                b.clear_pictures()
            elif o.kind == "pic_add":
                b.clear_pictures()
                b.add_picture(o.data, o.mime)
        b.save()
    for o in fops:
        if o.kind == "rename":
            new = os.path.join(af.dir, o.after)
            if os.path.exists(new) and not os.path.samefile(new, af.path):
                raise TagError(f"« {o.after} » existe déjà")
            os.rename(af.path, new)
            af.path = new


def friendly_error(e: Exception) -> str:
    if isinstance(e, PermissionError) or "Errno 13" in str(e) or "Permission denied" in str(e):
        return "permission refusée — relance musitag avec sudo"
    if "Errno 28" in str(e):
        return "disque plein"
    return str(e) or type(e).__name__


def apply_ops(ops: List[Op]):
    order, groups = [], {}
    for o in ops:
        if id(o.af) not in groups:
            groups[id(o.af)] = []
            order.append(o.af)
        groups[id(o.af)].append(o)
    ok, errors = 0, []
    prog = make_progress() if len(order) > 8 and console.is_terminal else _NullProgress()
    with prog:
        task = prog.add_task("Écriture", total=len(order))
        for af in order:
            try:
                _apply_file(af, groups[id(af)])
                ok += 1
            except Exception as e:
                errors.append((af.name, friendly_error(e)))
            finally:
                try:
                    af.load()          # relit l'état réel du fichier
                except Exception:
                    pass
            prog.advance(task)
    return ok, errors


def show_result(ok: int, errors) -> None:
    if ok:
        console.print(Panel(Text(f"✓ {ok} fichier(s) enregistré(s)", style="bold green"),
                            border_style="green", box=box.ROUNDED, expand=False))
    if errors:
        t = Table(box=box.SIMPLE_HEAD, header_style="bold red", title=Text("Erreurs", style="bold red"),
                  title_justify="left", expand=True)
        t.add_column("Fichier", ratio=1, overflow="fold")
        t.add_column("Problème", ratio=2, overflow="fold", style="red")
        for name, err in errors:
            t.add_row(Text(name), Text(err))
        console.print(t)


def run_ops(ops, title: str = "Aperçu des modifications") -> bool:
    ops = [o for o in ops if o]
    rank: Dict[int, int] = {}
    for o in ops:
        rank.setdefault(id(o.af), len(rank))
    ops.sort(key=lambda o: rank[id(o.af)])          # regroupe l'aperçu par fichier
    if not ops:
        info("Rien à modifier : tout est déjà comme demandé.")
        pause()
        return False
    console.print()
    console.print(Rule(Text(title, style="bold magenta"), style="magenta"))
    preview(ops)
    if not confirm("Appliquer ces modifications ?", True):
        console.print("[yellow]Rien n'a été modifié.[/]")
        pause()
        return False
    ok, errors = apply_ops(ops)
    show_result(ok, errors)
    pause()
    return True


# ═══════════════════════════════ Outils en lot ════════════════════════════════

def field_prompt(label: str) -> str:
    return f"  [bold cyan]{escape(label):>20}[/] [magenta]›[/] "


def batch_form(files) -> None:
    console.print(Panel(Text.from_markup(
        "Chaque champ affiche la valeur commune : [bold]modifie-la directement[/] (flèches, effacer…).\n"
        "Entrée sans rien changer = on ne touche pas. Tout effacer = supprimer le champ.\n"
        "Si les fichiers ont des valeurs différentes : Entrée = ne rien changer, "
        "[bold]-[/] = supprimer partout.\n"
        "Plusieurs valeurs dans un champ : sépare-les par [bold];[/]  (ex : Rock ; Pop)"),
        title=Text(" Champs communs ", style="bold"), title_align="left",
        border_style="cyan", box=box.ROUNDED))
    ops = []
    for f in BATCH_FIELDS:
        label = LABEL[f]
        uniq = {tuple(af.get(f)) for af in files}
        if len(uniq) == 1:
            common = join_values(next(iter(uniq)))
            r = ask(field_prompt(label), prefill=common).strip()
            if r == common or (r in ("", "-") and not common):
                continue
            new = [] if r in ("", "-") else (split_values(r) if f not in NUM_FIELDS else [r])
        else:
            samples = sorted(join_values(u) or "∅" for u in uniq)
            sample = ", ".join(samples[:3]) + (" …" if len(samples) > 3 else "")
            console.print(f"  [bold cyan]{escape(label):>20}[/] [dim]{len(uniq)} valeurs : "
                          f"{escape(sample)}[/]")
            r = ask(field_prompt("")).strip()
            if r == "":
                continue
            new = [] if r == "-" else (split_values(r) if f not in NUM_FIELDS else [r])
        ops += [op_set(af, f, new) for af in files]
    run_ops(ops)


def batch_numbering(files) -> None:
    mode = menu("Numéroter les pistes", [
        ("1", "D'après le numéro au début du nom", "« 01 - Titre », « A1. Titre », « 1-03 Titre »"),
        ("2", "Dans l'ordre des fichiers", "1, 2, 3… selon l'ordre alphabétique"),
        ("0", "Retour", ""),
    ])
    if mode == "0":
        return
    with_total = confirm("Écrire aussi le total de pistes ?", True)
    ops, problems = [], []
    for d, fl in group_by_dir(files):
        assigned = []
        if mode == "1":
            parsed = [(af, parse_track_from_name(af.stem)) for af in fl]
            for af, p in parsed:
                if p is None:
                    problems.append(af.name)
            ok = [(af, p) for af, p in parsed if p is not None]
            if any(p[1] for _, p in ok):          # faces de vinyle A1, B2… → séquentiel
                ok.sort(key=lambda x: (x[1][1], x[1][2]))
                assigned = [(af, i, p[0]) for i, (af, p) in enumerate(ok, 1)]
            else:
                assigned = [(af, p[2], p[0]) for af, p in ok]
        else:
            assigned = [(af, i, None) for i, af in enumerate(fl, 1)]
        total = max((n for _, n, _ in assigned), default=0)
        for af, n, disc in assigned:
            ops.append(op_set(af, "tracknumber", [str(n)]))
            if with_total:
                ops.append(op_set(af, "tracktotal", [str(total)]))
            if disc:
                ops.append(op_set(af, "discnumber", [str(disc)]))
    if problems:
        info("Pas de numéro trouvé dans : " + ", ".join(escape(p) for p in problems[:10])
             + (" …" if len(problems) > 10 else ""))
    run_ops(ops)


def batch_titles(files) -> None:
    ops = []
    for af in files:
        new = title_from_filename(af.stem, af.get("artist") + af.get("albumartist"))
        ops.append(op_set(af, "title", [new]))
    run_ops(ops, "Titres déduits des noms de fichiers")


def batch_replace(files) -> None:
    opts = [(str(i), LABEL[f], "") for i, f in enumerate(TEXT_FIELDS, 1)]
    opts += [("t", "Tous ces champs", ""), ("0", "Retour", "")]
    k = menu("Rechercher / remplacer — dans quel champ ?", opts)
    if k == "0":
        return
    fields = TEXT_FIELDS if k == "t" else [TEXT_FIELDS[int(k) - 1]]
    search = ask("  [bold]Rechercher[/] [magenta]›[/] ")
    if not search:
        return
    repl = ask("  [bold]Remplacer par[/] [dim](vide = supprimer)[/] [magenta]›[/] ")
    regex = confirm("  Expression régulière ?", False)
    try:
        pat = re.compile(search if regex else re.escape(search))
    except re.error as e:
        info(f"Expression invalide : {escape(str(e))}", "red")
        pause()
        return
    ops = []
    for af in files:
        for f in fields:
            old = af.get(f)
            if not old:
                continue
            new = [re.sub(r"\s{2,}", " ", pat.sub(repl if regex else (lambda m: repl), v)).strip()
                   for v in old]
            ops.append(op_set(af, f, [v for v in new if v]))
    run_ops(ops)


def batch_rename(files) -> None:
    k = menu("Renommer les fichiers d'après les tags", [
        ("1", "01 - Titre", "{n} - {title}"),
        ("2", "01. Titre", "{n}. {title}"),
        ("3", "1-01 - Titre", "{disc}-{n} - {title}   (multi-disques)"),
        ("4", "01 - Artiste - Titre", "{n} - {artist} - {title}"),
        ("5", "Modèle personnalisé", "{n} {title} {artist} {album} {year} {disc}"),
        ("0", "Retour", ""),
    ])
    presets = {"1": "{n} - {title}", "2": "{n}. {title}", "3": "{disc}-{n} - {title}",
               "4": "{n} - {artist} - {title}"}
    if k == "0":
        return
    tpl = presets.get(k) or ask("  [bold]Modèle[/] [magenta]›[/] ", prefill="{n} - {title}").strip()
    if not tpl:
        return
    ops, problems, planned = [], [], {}
    for af in files:
        tr, tot = af.first("tracknumber"), af.first("tracktotal")
        width = 3 if tot.isdigit() and int(tot) >= 100 else 2
        vals = {"n": tr.zfill(width) if tr.isdigit() else tr, "title": af.first("title"),
                "artist": af.first("artist"), "album": af.first("album"),
                "year": af.first("date")[:4], "disc": af.first("discnumber"),
                "albumartist": af.first("albumartist") or af.first("artist")}
        missing = [m for m in re.findall(r"\{(\w+)\}", tpl) if not vals.get(m)]
        if missing:
            problems.append((af.name, ", ".join(missing)))
            continue
        name = tpl
        for key, v in vals.items():
            name = name.replace("{" + key + "}", v.replace("/", "-"))
        name = safe_name(name) + af.ext.lower()
        if name == af.name:
            continue
        dest = os.path.join(af.dir, name)
        if dest in planned:
            problems.append((af.name, f"même nom que {planned[dest]}"))
            continue
        planned[dest] = af.name
        ops.append(Op(af, "rename", before=af.name, after=name, label="Nom"))
    if problems:
        t = Table(box=box.SIMPLE_HEAD, header_style="bold yellow", expand=True,
                  title=Text("Ignorés", style="bold yellow"), title_justify="left")
        t.add_column("Fichier", overflow="fold")
        t.add_column("Raison (champ manquant)", style="yellow")
        for n, why in problems:
            t.add_row(Text(n), Text(why))
        console.print(t)
    run_ops(ops, "Renommage")


def batch_delete_fields(files) -> None:
    counts: Dict[str, int] = {}
    examples: Dict[str, str] = {}
    for af in files:
        for key, v in af.backend.raw_items():
            counts[key] = counts.get(key, 0) + 1
            examples.setdefault(key, oneline(v, 120))
    if not counts:
        info("Aucun tag dans ces fichiers.")
        pause()
        return
    keys = sorted(counts, key=str.lower)
    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False)
    t.add_column("#", style="dim", justify="right")
    t.add_column("Champ", style="cyan", no_wrap=True)
    t.add_column("Fichiers", justify="right")
    t.add_column("Exemple de valeur", ratio=1, no_wrap=True, overflow="ellipsis", style="dim")
    for i, key in enumerate(keys, 1):
        t.add_row(str(i), Text(key), str(counts[key]), Text(examples[key]))
    console.print(t)
    sel = parse_selection(ask("  Numéros à supprimer [dim](ex : 2,5-7)[/] [magenta]›[/] "), len(keys))
    if not sel:
        return
    chosen = [keys[i] for i in sel]
    ops = []
    for af in files:
        present = dict(af.backend.raw_items())
        for key in chosen:
            if key in present:
                ops.append(Op(af, "raw_delete", key, before=oneline(present[key], 120), after="", label=key))
    run_ops(ops)


def find_cover(folder: str) -> Optional[str]:
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    imgs = [n for n in names if os.path.splitext(n)[1].lower() in IMAGE_EXTS]
    for base in COVER_NAMES:
        for n in sorted(imgs):
            if os.path.splitext(n)[0].lower() == base:
                return os.path.join(folder, n)
    return os.path.join(folder, imgs[0]) if len(imgs) == 1 else None


def read_image(path: str):
    with open(path, "rb") as fh:
        data = fh.read()
    mime = image_mime(data)
    if mime is None:
        raise TagError("seuls les JPEG et PNG peuvent être intégrés")
    return data, mime


def batch_pictures(files) -> None:
    k = menu("Pochettes intégrées", [
        ("1", "Chercher en ligne", "Cover Art Archive : aperçus et choix de l'édition"),
        ("2", "Intégrer l'image du dossier", "cover.jpg, folder.jpg, front.jpg…"),
        ("3", "Intégrer une image précise", "chemin vers un .jpg ou .png"),
        ("4", "Supprimer les pochettes intégrées", ""),
        ("0", "Retour", ""),
    ])
    if k == "0":
        return
    if k == "1":
        covers_online(files)
        return
    ops = []
    if k == "4":
        for af in files:
            pics = af.backend.pictures()
            if pics:
                ops.append(Op(af, "pics_clear", before=f"{len(pics)} image(s)", after="", label="Pochette"))
        run_ops(ops)
        return
    if k == "2":
        missing = []
        for d, fl in group_by_dir(files):
            cover = find_cover(d)
            if not cover:
                missing.append(d)
                continue
            try:
                data, mime = read_image(cover)
            except (OSError, TagError) as e:
                missing.append(f"{d} ({e})")
                continue
            for af in fl:
                ops.append(Op(af, "pic_add", before=f"{len(af.backend.pictures())} image(s)",
                              after=f"{os.path.basename(cover)} · {human_size(len(data))}",
                              label="Pochette", data=data, mime=mime))
        if missing:
            info("Pas d'image trouvée dans : " + ", ".join(escape(m) for m in missing[:8]))
    else:
        path = os.path.expanduser(ask("  [bold]Chemin de l'image[/] [magenta]›[/] ").strip().strip("'\""))
        try:
            data, mime = read_image(path)
        except (OSError, TagError) as e:
            info(f"Impossible de lire l'image : {escape(str(e))}", "red")
            pause()
            return
        for af in files:
            ops.append(Op(af, "pic_add", before=f"{len(af.backend.pictures())} image(s)",
                          after=f"{os.path.basename(path)} · {human_size(len(data))}",
                          label="Pochette", data=data, mime=mime))
    if any(len(o.data) > 1_500_000 for o in ops):
        console.print("[yellow]Image lourde (> 1,5 Mo) : elle sera copiée dans chaque fichier.[/]")
    run_ops(ops)


def batch_wipe(files) -> None:
    info(f"Tous les tags et pochettes de [bold]{len(files)}[/] fichier(s) vont être effacés.", "red")
    if not confirm("Vraiment tout effacer ?", False):
        return
    ops = [Op(af, "wipe", before=f"{len(af.backend.raw_items())} champ(s)", after="", label="Tous les tags")
           for af in files]
    run_ops(ops)


# ═══════════════════════════════ Paroles : écrans ═════════════════════════════

LYR_LABELS = {
    "synced": ("◷ synchronisées", "green"),
    "plain": ("≡ texte", "cyan"),
    "instrumental": ("♪ instrumental", "magenta"),
    "notfound": ("✗ introuvable", "dim"),
    "skip": ("– ignoré", "dim"),
    "error": ("⚠ erreur", "red"),
}


def render_lyrics(text: str, max_lines: Optional[int] = None) -> Text:
    lines = [l for l in text.splitlines() if not LRC_META.match(l)]
    shown = lines if max_lines is None else lines[:max_lines]
    out = Text()
    for l in shown:
        m = LRC_LINE.match(l)
        if m:
            out.append(m.group(1).strip() + "  ", style="dim magenta")
            out.append(m.group(2).strip())
        else:
            out.append(l.rstrip())
        out.append("\n")
    if max_lines is not None and len(lines) > max_lines:
        out.append(f"… {len(lines) - max_lines} lignes de plus", style="dim italic")
    out.rstrip()
    return out


def lyrics_panel(text: str, title: str, subtitle: str = "", max_lines: Optional[int] = None):
    return Panel(render_lyrics(text, max_lines), title=Text(f" {title} ", style="bold"), title_align="left",
                 subtitle=Text(f" {subtitle} ", style="dim") if subtitle else None, subtitle_align="right",
                 border_style="green", box=box.ROUNDED, padding=(1, 2))


def show_lyrics(af) -> None:
    text = get_lyrics(af)
    if not text:
        info("Pas de paroles dans ce fichier.")
        pause()
        return
    print_long([lyrics_panel(text, af.first("title") or af.name, lyrics_summary(text))])


def result_cells(r: LyricsResult):
    key = r.match.kind if r.status in ("exact", "approx") and r.match else r.status
    label, style = LYR_LABELS.get(key, (key, ""))
    res = Text(label, style=style)
    if r.status == "exact" or (r.status == "instrumental" and r.match and r.match.exact):
        corr = Text("exacte", style="green")
    elif r.status == "approx" or (r.status == "instrumental" and r.match):
        corr = Text("approx · ", style="bold yellow")
        corr.append(r.match.describe())
    elif r.status in ("skip", "error"):
        corr = Text(r.message, style="red" if r.status == "error" else "dim")
    else:
        corr = Text("")
    return res, corr


def lyrics_tables(results: List[LyricsResult], only: Optional[set] = None):
    groups: Dict[str, List[Tuple[int, LyricsResult]]] = {}
    for i, r in enumerate(results, 1):
        if only is None or (i - 1) in only:
            groups.setdefault(r.af.dir, []).append((i, r))
    base = os.path.commonpath(list(groups)) if len(groups) > 1 else None
    renders = []
    width = len(str(len(results)))
    for d, items in groups.items():
        title = None
        if base is not None:
            rel = os.path.relpath(d, base)
            title = "." if rel == "." else rel
        t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False,
                  title=Text(title, style="bold cyan") if title else None, title_justify="left")
        t.add_column("#", style="dim", justify="right", no_wrap=True, width=width)
        t.add_column("N°", justify="right", no_wrap=True, style="cyan", width=4)
        t.add_column("Titre", ratio=3, no_wrap=True, overflow="ellipsis")
        t.add_column("Résultat", no_wrap=True, width=15)
        t.add_column("Correspondance", ratio=4, no_wrap=True, overflow="ellipsis")
        t.add_column("Actuelles", no_wrap=True, style="dim", width=22)
        for i, r in items:
            af = r.af
            res, corr = result_cells(r)
            cur = lyrics_summary(get_lyrics(af))
            t.add_row(str(i), track_label(af), Text(af.first("title") or af.name), res, corr,
                      Text(cur or "—"))
        renders.append(t)
    return renders


def lyrics_counts(results: List[LyricsResult]) -> Text:
    c: Dict[str, int] = {}
    for r in results:
        key = r.status
        if r.status in ("exact", "approx"):
            key += "_" + r.match.kind
        c[key] = c.get(key, 0) + 1
    exact = c.get("exact_synced", 0) + c.get("exact_plain", 0)
    approx = c.get("approx_synced", 0) + c.get("approx_plain", 0)
    t = Text()
    t.append(f"{exact} exacte(s)", style="bold green")
    t.append(f" ({c.get('exact_synced', 0)} synchronisées, {c.get('exact_plain', 0)} texte)", style="green")
    t.append("  ·  ")
    t.append(f"{approx} approx", style="bold yellow")
    t.append("  ·  ")
    t.append(f"{c.get('instrumental', 0)} instrumental(aux)  ·  {c.get('notfound', 0)} introuvable(s)", style="dim")
    if c.get("skip"):
        t.append(f"  ·  {c['skip']} ignoré(s)", style="dim")
    if c.get("error"):
        t.append(f"  ·  {c['error']} erreur(s)", style="bold red")
    return t


def lyrics_op(af, text: str) -> Optional[Op]:
    old = get_lyrics(af)
    if old == text.strip():
        return None
    return Op(af, "set", "lyrics", [text.strip()], before=lyrics_summary(old),
              after=lyrics_summary(text), label="Paroles")


def batch_lyrics(files) -> None:
    info(f"Recherche des paroles de [bold]{len(files)}[/] fichier(s) sur LRCLIB.\n"
         "[dim]Les paroles trouvées remplacent celles déjà présentes ; les instrumentaux reçoivent "
         f"« {INSTRUMENTAL_TEXT} ». Rien n'est écrit sans ta validation.[/]", "cyan")
    with console.status("[magenta]Connexion à LRCLIB…[/]", spinner="dots"):
        down = lrclib_ping()
    if down:
        info(f"LRCLIB est injoignable ({escape(down)}).\n"
             "Vérifie que la VM a accès à Internet (lrclib.net).", "red")
        pause()
        return
    results = lyrics_fetch_all(files)
    print_long(lyrics_tables(results) + [lyrics_counts(results)], wait=False)

    while True:                      # LRCLIB a freiné ou calé : on propose de relancer
        err_idx = [i for i, r in enumerate(results) if r.status == "error"]
        if not err_idx:
            break
        k = menu(f"{len(err_idx)} recherche(s) en erreur", [
            ("1", "Relancer ces recherches", "LRCLIB était surchargé : ça passe en général au 2e essai"),
            ("2", "Continuer sans elles", "ces fichiers ne seront pas modifiés"),
            ("0", "Annuler", "rien n'est écrit"),
        ])
        if k == "0":
            return
        if k == "2":
            break
        with console.status("[magenta]Petite pause pour laisser souffler LRCLIB…[/]", spinner="dots"):
            time.sleep(5)
        retry = lyrics_fetch_all([results[i].af for i in err_idx])
        for i, r in zip(err_idx, retry):
            results[i] = r
        console.print()
        print_long(lyrics_tables(results, only=set(err_idx)) + [lyrics_counts(results)], wait=False)

    approx = [i for i, r in enumerate(results)
              if r.status == "approx" or (r.status == "instrumental" and r.match and not r.match.exact)]
    keep = set()
    if approx:
        k = menu(f"Correspondances approchantes ({len(approx)})", [
            ("1", "Les garder toutes", ""),
            ("2", "N'en garder aucune", "seulement les exactes"),
            ("3", "Choisir lesquelles", "numéros de la colonne #"),
            ("0", "Annuler", "rien n'est écrit"),
        ])
        if k == "0":
            return
        if k == "1":
            keep = set(approx)
        elif k == "3":
            nums = ", ".join(str(i + 1) for i in approx[:15]) + (" …" if len(approx) > 15 else "")
            console.print(f"[dim]Approx : {nums}[/]")
            sel = parse_selection(ask("  Numéros à garder [dim](ex : 3,7-9)[/] [magenta]›[/] "), len(results))
            keep = {i for i in sel if i in approx}

    ops = []
    for i, r in enumerate(results):
        if r.status == "exact" or (r.status == "approx" and i in keep):
            ops.append(lyrics_op(r.af, r.match.text))
        elif r.status == "instrumental" and r.match and (r.match.exact or i in keep):
            ops.append(lyrics_op(r.af, INSTRUMENTAL_TEXT))
    ops = [o for o in ops if o]
    if not ops:
        info("Aucune parole à écrire.")
        pause()
        return
    replaced = sum(1 for o in ops if o.before)
    msg = f"Écrire les paroles dans [bold]{len(ops)}[/] fichier(s)"
    if replaced:
        msg += f" [dim](dont {replaced} qui en {'avaient' if replaced > 1 else 'avait'} déjà)[/]"
    if not confirm(msg + " ?", True):
        console.print("[yellow]Rien n'a été modifié.[/]")
        pause()
        return
    ok, errors = apply_ops(ops)
    show_result(ok, errors)
    pause()


def mark_instrumental(files) -> None:
    """Écrit « ♪ Musique sans paroles ♪ » : Feishin (paroles locales prioritaires) l'affiche
    au lieu d'aller chercher des crédits ou n'importe quoi en ligne."""
    run_ops([lyrics_op(af, INSTRUMENTAL_TEXT) for af in files], "Marquer comme instrumental")


def remove_lyrics(files) -> None:
    ops = []
    for af in files:
        old = get_lyrics(af)
        if old:
            ops.append(Op(af, "set", "lyrics", [], before=lyrics_summary(old), after="", label="Paroles"))
    run_ops(ops, "Supprimer les paroles")


def lyrics_menu(files) -> None:
    """Menu Paroles en lot (menu 4 → 8)."""
    with_lyr = sum(1 for af in files if get_lyrics(af))
    k = menu("Paroles", [
        ("1", "Chercher sur LRCLIB", "synchronisées si possible ; instrumentaux marqués"),
        ("2", "Marquer comme instrumental", f"« {INSTRUMENTAL_TEXT} » dans les {len(files)} fichier(s)"),
        ("3", "Supprimer les paroles", f"{with_lyr} fichier(s) en ont"),
        ("0", "Retour", ""),
    ])
    if k == "1":
        batch_lyrics(files)
    elif k == "2":
        mark_instrumental(files)
    elif k == "3":
        remove_lyrics(files)


def file_lyrics_menu(af) -> None:
    """Menu Paroles d'un fichier (menu Fichier → 6)."""
    lyr = get_lyrics(af)
    opts = [("1", "Chercher sur LRCLIB", "synchronisées si possible")]
    if is_instrumental_mark(lyr):
        opts.append(("2", "Retirer la mention instrumental", ""))
    else:
        opts.append(("2", "Marquer comme instrumental", f"« {INSTRUMENTAL_TEXT} »"))
    if lyr:
        opts += [("3", "Supprimer les paroles", lyrics_summary(lyr)), ("l", "Lire les paroles", "")]
    k = menu("Paroles", opts + [("0", "Retour", "")])
    if k == "1":
        file_lyrics(af)
    elif k == "2":
        if is_instrumental_mark(lyr):
            remove_lyrics([af])
        else:
            mark_instrumental([af])
    elif k == "3":
        remove_lyrics([af])
    elif k == "l":
        show_lyrics(af)


def file_lyrics(af) -> None:
    title, artist = None, None
    while True:
        with console.status("[magenta]Recherche sur LRCLIB…[/]", spinner="dots"):
            r = lrclib_lookup(af, title, artist)
        if r.status == "error":
            info(escape(r.message), "red")
            pause()
            return
        if r.status == "skip":
            info("Il faut un titre et un artiste pour chercher : renseigne-les d'abord (menu 1).")
            pause()
            return
        if r.status == "instrumental":
            how = "correspondance exacte" if r.match.exact else "correspondance approchante, vérifie"
            info(f"LRCLIB indique que « {escape(r.match.describe())} » est un instrumental ({how}).")
            mark_instrumental([af])
            return
        if r.status == "notfound":
            t0 = af.first("title") if title is None else title
            a0 = (af.first("artist") or af.first("albumartist")) if artist is None else artist
            info(f"Rien trouvé sur LRCLIB pour « {escape(a0)} – {escape(t0)} » ({fmt_dur(af.length())}).")
            k = menu("Et maintenant ?", [
                ("1", "Chercher avec un autre titre ou artiste", ""),
                ("2", "Marquer comme instrumental", f"« {INSTRUMENTAL_TEXT} »"),
                ("0", "Retour", ""),
            ])
            if k == "2":
                mark_instrumental([af])
            if k != "1":
                return
            title = ask(field_prompt("Titre"), prefill=t0).strip()
            artist = ask(field_prompt("Artiste"), prefill=a0).strip()
            continue
        break

    cands = r.candidates or [r.match]
    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False,
              title=Text("Résultats LRCLIB", style="bold"), title_justify="left")
    t.add_column("#", style="dim", justify="right", no_wrap=True)
    t.add_column("", no_wrap=True, width=7)
    t.add_column("Artiste", ratio=2, no_wrap=True, overflow="ellipsis")
    t.add_column("Titre", ratio=3, no_wrap=True, overflow="ellipsis")
    t.add_column("Album", ratio=2, no_wrap=True, overflow="ellipsis", style="dim")
    t.add_column("Durée", justify="right", no_wrap=True, width=12)
    t.add_column("Paroles", no_wrap=True, width=15)
    for i, m in enumerate(cands, 1):
        dur = m.rec.get("duration") or 0
        dcell = Text(fmt_dur(dur))
        if m.diff is not None and m.diff >= 1:
            dcell.append(f" Δ{m.diff:.0f}s", style="yellow")
        label, style = LYR_LABELS[m.kind or "notfound"]
        t.add_row(str(i), Text("exacte", style="green") if m.exact else Text("approx", style="yellow"),
                  Text(m.rec.get("artistName") or ""), Text(m.rec.get("trackName") or ""),
                  Text(m.rec.get("albumName") or ""), dcell, Text(label, style=style))
    console.print(t)
    console.print(f"[dim]Ton fichier : {escape(af.first('artist'))} – {escape(af.first('title'))} "
                  f"· {fmt_dur(af.length())}[/]")
    while True:
        k = ask("  Numéro du résultat [dim](Entrée = 1)[/] [magenta]›[/] ").strip() or "1"
        if k.isdigit() and 1 <= int(k) <= len(cands):
            break
        console.print("[red]Numéro hors liste.[/]")
    m = cands[int(k) - 1]
    console.print(lyrics_panel(m.text, m.describe(), lyrics_summary(m.text), max_lines=30))
    op = lyrics_op(af, m.text)
    if op is None:
        info("Le fichier contient déjà exactement ces paroles.")
        pause()
        return
    if not confirm("Remplacer les paroles actuelles ?" if op.before else "Écrire ces paroles dans le fichier ?", True):
        return
    ok, errors = apply_ops([op])
    show_result(ok, errors)
    pause()


# ═════════════════════════════ Fichier individuel ═════════════════════════════

def edit_form(af: AudioFile) -> None:
    console.print(Text.from_markup(
        "[dim]Modifie le texte directement · tout effacer = supprimer le champ · "
        "plusieurs valeurs : sépare par « ; »[/]\n"))
    ops = []
    for f, label in FIELDS:
        cur = join_values(af.get(f))
        r = ask(field_prompt(label), prefill=cur).strip()
        if r == cur:
            continue
        ops.append(op_set(af, f, (split_values(r) if f not in NUM_FIELDS else [r]) if r else []))
    run_ops(ops)


def edit_free_field(af: AudioFile) -> None:
    hints = {"Vorbis comments": "LABEL, ISRC, REPLAYGAIN_TRACK_GAIN, MUSICBRAINZ_ALBUMID…",
             "ID3v2": "TXXX:LABEL, TBPM, TSRC (ISRC), TPUB (label)…",
             "MP4 (iTunes)": "LABEL (champ libre iTunes), ©grp…",
             "APEv2": "Label, ISRC, Publisher…", "ASF (WMA)": "WM/Publisher, WM/ISRC…"}
    console.print(f"[dim]Exemples pour ce format : {escape(hints.get(af.backend.kind, ''))}[/]")
    key = ask("  [bold]Nom du champ[/] [magenta]›[/] ").strip()
    if not key:
        return
    present = {k.lower(): (k, v) for k, v in af.backend.raw_items()}
    cur_key, cur = present.get(key.lower(), (key, ""))
    if "\n" in cur:
        info("Champ sur plusieurs lignes (paroles…) : utilise le menu [bold]Paroles[/] "
             "ou [bold]Supprimer des champs[/].")
        pause()
        return
    if cur.startswith("[image") or cur == "[données binaires]":
        info("Ce champ contient des données binaires : utilise le menu Pochette ou Supprimer.")
        pause()
        return
    r = ask(field_prompt(cur_key), prefill=cur).strip()
    if r == cur:
        return
    if r:
        run_ops([Op(af, "raw_set", cur_key, split_values(r), before=cur, after=join_values(split_values(r)),
                    label=cur_key)])
    elif cur:
        run_ops([Op(af, "raw_delete", cur_key, before=cur, after="", label=cur_key)])


def edit_delete_fields(af: AudioFile) -> None:
    items = af.backend.raw_items()
    if not items:
        info("Ce fichier n'a aucun tag.")
        pause()
        return
    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False)
    t.add_column("#", style="dim", justify="right")
    t.add_column("Champ", style="cyan", no_wrap=True)
    t.add_column("Valeur", ratio=1, overflow="ellipsis", no_wrap=True)
    for i, (k, v) in enumerate(items, 1):
        t.add_row(str(i), Text(k), Text(oneline(v, 120)))
    console.print(t)
    sel = parse_selection(ask("  Numéros à supprimer [dim](ex : 1,3-4)[/] [magenta]›[/] "), len(items))
    run_ops([Op(af, "raw_delete", items[i][0], before=oneline(items[i][1], 120), after="", label=items[i][0])
             for i in sel])


def edit_rename(af: AudioFile) -> None:
    new = ask("  [bold]Nouveau nom[/] [dim](sans extension)[/] [magenta]›[/] ", prefill=af.stem).strip()
    if not new or new == af.stem:
        return
    name = safe_name(new) + af.ext
    run_ops([Op(af, "rename", before=af.name, after=name, label="Nom")], "Renommage")


def pick_file(app) -> Optional[int]:
    files = app.files
    if not files:
        info("Aucun fichier audio ici.")
        pause()
        return None
    renders = []
    idx = 1
    groups = group_by_dir(files)
    for d, fl in groups:
        title = os.path.relpath(d, app.folder) if len(groups) > 1 else None
        renders.append(files_table(fl, idx, title=title, compact=True, total=len(files)))
        idx += len(fl)
    for r in renders:
        console.print(r)
    while True:
        r = ask("  [bold]Numéro du fichier[/] [dim](ou un mot du nom · Entrée = retour)[/] [magenta]›[/] ").strip()
        if not r:
            return None
        if r.isdigit():
            if 1 <= int(r) <= len(files):
                return int(r) - 1
            console.print("[red]Numéro hors liste.[/]")
            continue
        hits = [i for i, af in enumerate(files) if r.lower() in af.path.lower()
                or r.lower() in af.first("title").lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            console.print("[red]Aucun fichier ne correspond.[/]")
            continue
        for i in hits[:30]:
            console.print(f"  [dim]{i + 1:>4}[/]  {escape(files[i].name)}")


def file_screen(app, idx: int, edit: bool) -> None:
    while app.files:
        idx = max(0, min(idx, len(app.files) - 1))
        af = app.files[idx]
        app.clear()
        console.print(detail_panel(af, idx + 1, len(app.files)))
        nav = [None, ("s", "Fichier suivant", ""), ("p", "Fichier précédent", "")]
        read_lyr = [("l", "Lire les paroles", "")] if get_lyrics(af) else []
        if edit:
            opts = [("1", "Modifier les champs principaux", "titre, artiste, album, n°…"),
                    ("2", "Ajouter / modifier un champ libre", "ISRC, LABEL, REPLAYGAIN…"),
                    ("3", "Supprimer des champs", ""),
                    ("4", "Renommer le fichier", ""),
                    ("5", "Pochette", "en ligne, depuis le dossier, supprimer"),
                    ("6", "Paroles", "LRCLIB, marquer instrumental, supprimer")] + read_lyr \
                + nav + [("0", "Retour", "")]
        else:
            opts = [("m", "Modifier ce fichier", "")] + read_lyr + nav + [("0", "Retour", "Entrée")]
        key = menu("Fichier", opts, default=None if edit else "0")
        try:
            if key == "0":
                return
            if key == "s":
                idx = (idx + 1) % len(app.files)
            elif key == "p":
                idx = (idx - 1) % len(app.files)
            elif key == "m":
                edit = True
            elif key == "1":
                edit_form(af)
            elif key == "2":
                edit_free_field(af)
            elif key == "3":
                edit_delete_fields(af)
            elif key == "4":
                edit_rename(af)
                app.resort()
                idx = app.files.index(af)
            elif key == "5":
                batch_pictures([af])
            elif key == "6":
                file_lyrics_menu(af)
            elif key == "l":
                show_lyrics(af)
        except (KeyboardInterrupt, Cancel):
            console.print("\n[yellow]Annulé.[/]")
            time.sleep(0.6)


# ═══════════════════════════════ Découpage CUE ════════════════════════════════

@dataclass
class CueTrack:
    num: int
    title: str = ""
    performer: str = ""
    songwriter: str = ""
    isrc: str = ""
    file: str = ""
    index0: Optional[int] = None
    index1: Optional[int] = None


@dataclass
class CueSheet:
    path: str
    encoding: str
    performer: str = ""
    title: str = ""
    songwriter: str = ""
    date: str = ""
    genre: str = ""
    disc: str = ""
    totaldiscs: str = ""
    tracks: List[CueTrack] = dc_field(default_factory=list)
    force_artist: str = ""


def _script_of(c: str) -> str:
    n = unicodedata.name(c, "")
    if n.startswith(("CJK", "HIRAGANA", "KATAKANA", "FULLWIDTH", "HALFWIDTH", "IDEOGRAPHIC")):
        return "CJK"
    return n.split(" ")[0]


# Pour deviner l'encodage d'un .cue, chaque décodage possible reçoit une note :
# les lettres « normales » d'une langue rapportent, les caractères typiques d'un
# mauvais décodage (« ƒnƒ‹ƒq », « Ïðèâåò », zone privée Unicode…) coûtent cher.
_RU_LETTERS = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯіїєґІЇЄҐў")
_WEST_LETTERS = set("àâäáãåæçéèêëíìîïñóòôöõøœúùûüýÿßÀÂÄÁÃÅÆÇÉÈÊËÍÌÎÏÑÓÒÔÖÕØŒÚÙÛÜÝ")
_MOJIBAKE = set("ƒ„†‡ˆ‰‹›˜™Ÿ¤¦¨¯´¸ЂЃѓЉЊЌЋЏђљњќћџ")


def _char_score(c: str) -> int:
    if c < "\x80":
        return 0
    if c in _RU_LETTERS or c in _WEST_LETTERS:
        return 2
    if c in _MOJIBAKE:
        return -3
    cat = unicodedata.category(c)
    if cat == "Co" or cat == "Cc":                    # zone privée, caractères de contrôle
        return -5
    if "\uff61" <= c <= "\uff9f":                   # katakana demi-chasse
        return -1
    if "\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff" or "\uff01" <= c <= "\uff5e":
        return 2                                     # hiragana, katakana, kanji, pleine chasse
    return -1 if cat.startswith(("S", "P")) and c not in "«»’“”…–—€°·" else 0


def _plausibility(txt: str) -> int:
    """Note d'un décodage : lettres plausibles + pénalité pour les mots qui mélangent
    les alphabets (« Cafй » : du français lu comme du russe)."""
    score = sum(_char_score(c) for c in txt)
    for word in re.findall(r"\w+", txt):
        non = [c for c in word if ord(c) > 127]
        if not non:
            continue
        scripts = {_script_of(c) for c in non}
        if len(scripts) > 1:                          # alphabets mélangés dans un même mot
            score -= 3
            continue
        has_ascii = any("a" <= c.lower() <= "z" for c in word)
        if "CYRILLIC" in scripts and has_ascii and len(non) <= 2:
            score -= 4                                # « crиme » : du français lu en cyrillique
        for run in re.findall("[\u00c0-\u00ff\u0152\u0153]{3,}", word):
            score -= 4 * len(run)                     # « Ïðèâåò » : du russe lu en Windows-1252
    return score


def decode_cue(raw: bytes, forced: Optional[str] = None) -> Tuple[str, str]:
    if forced:
        return raw.decode(forced, errors="replace"), forced
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    best = None
    for enc in ("cp1252", "cp1251", "cp932"):
        try:
            txt = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        sc = _plausibility(txt)
        if best is None or sc > best[0]:
            best = (sc, txt, enc)
    if best:
        return best[1], best[2]
    return raw.decode("latin-1"), "latin-1"


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s.endswith('"'):
        return s[1:-1]
    return s.lstrip('"')


def _msf(s: str) -> int:
    m = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2})\s*", s)
    if not m:
        raise TagError(f"position invalide dans le .cue : {s}")
    return (int(m.group(1)) * 60 + int(m.group(2))) * 75 + int(m.group(3))


def parse_cue(path: str, forced: Optional[str] = None) -> CueSheet:
    with open(path, "rb") as fh:
        text, enc = decode_cue(fh.read(), forced)
    cs = CueSheet(path, enc)
    cur_file, tr, in_track = "", None, False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        cmd, rest = parts[0].upper(), (parts[1] if len(parts) > 1 else "")
        if cmd == "REM":
            sub = rest.split(None, 1)
            if not sub or in_track:
                continue
            key, val = sub[0].upper(), _unquote(sub[1]) if len(sub) > 1 else ""
            attr = {"GENRE": "genre", "DATE": "date", "DISCNUMBER": "disc",
                    "TOTALDISCS": "totaldiscs", "DISCTOTAL": "totaldiscs"}.get(key)
            if attr:
                setattr(cs, attr, val)
        elif cmd == "FILE":
            m = re.match(r'^"(.*)"\s+(\S+)$', rest) or re.match(r"^(.*\S)\s+(\S+)$", rest)
            cur_file = m.group(1) if m else _unquote(rest)
        elif cmd == "TRACK":
            sub = rest.split()
            in_track = True
            if len(sub) >= 2 and sub[1].upper() != "AUDIO":
                tr = None                                  # piste de données : ignorée
                continue
            tr = CueTrack(num=int(sub[0]), file=cur_file)
            cs.tracks.append(tr)
        elif cmd == "INDEX" and tr is not None:
            sub = rest.split()
            if len(sub) == 2:
                n, pos = int(sub[0]), _msf(sub[1])
                if n == 0:
                    tr.index0 = pos
                elif n == 1:
                    tr.index1 = pos
                    tr.file = cur_file          # cas EAC « gaps appended »
        elif cmd in ("TITLE", "PERFORMER", "SONGWRITER"):
            val = _unquote(rest)
            if not in_track:
                setattr(cs, cmd.lower() if cmd != "TITLE" else "title", val)
            elif tr is not None:
                setattr(tr, cmd.lower(), val)
        elif cmd == "ISRC" and tr is not None:
            tr.isrc = rest.strip()
    cs.tracks = [t for t in cs.tracks if t.index1 is not None]
    return cs


def resolve_source(cs: CueSheet, name: str) -> Optional[str]:
    """Le fichier audio d'une ligne FILE du .cue — toujours DANS le dossier du .cue :
    un .cue piégé (« ../../ailleurs.flac », « /chemin/absolu », lien symbolique vers
    l'extérieur) ne peut pas faire lire ni déplacer un fichier d'ailleurs."""
    d = os.path.dirname(cs.path) or "."
    root = os.path.realpath(d)

    def inside(path: str) -> Optional[str]:
        real = os.path.realpath(path)
        return path if real.startswith(root + os.sep) and os.path.isfile(real) else None

    name = name.replace("\\", "/")                       # chemins Windows (EAC…)
    found = inside(os.path.join(d, name.lstrip("/")))
    if found:
        return found
    name = os.path.basename(name)
    try:
        low = {e.lower(): e for e in os.listdir(d)}
    except OSError:
        return None
    if name.lower() in low:
        found = inside(os.path.join(d, low[name.lower()]))
        if found:
            return found
    for base in (os.path.splitext(name)[0], os.path.splitext(os.path.basename(cs.path))[0]):
        for ext in SPLIT_SOURCE_EXTS:
            if (base + ext).lower() in low:
                found = inside(os.path.join(d, low[(base + ext).lower()]))
                if found:
                    return found
    return None


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,sample_rate,channels,bits_per_raw_sample,bits_per_sample,"
         "sample_fmt,duration_ts,time_base,duration", "-of", "json", path],
        capture_output=True, text=True)
    try:
        s = json.loads(out.stdout)["streams"][0]
    except (ValueError, KeyError, IndexError):
        raise TagError(f"ffprobe ne lit pas {os.path.basename(path)} : {out.stderr.strip()[:200]}")
    sr = int(s["sample_rate"])
    codec = s.get("codec_name", "")
    total, exact = None, False
    try:
        mf = mutagen.File(path)
        if isinstance(mf, FLAC):
            total, exact = mf.info.total_samples, True
    except Exception:
        pass
    if total is None and s.get("duration_ts") not in (None, "N/A") and s.get("time_base") == f"1/{sr}":
        total, exact = int(s["duration_ts"]), True
    if total is None and s.get("duration") not in (None, "N/A"):
        total = round(float(s["duration"]) * sr)
    bits = 0
    for k in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            bits = bits or int(s.get(k) or 0)
        except ValueError:
            pass
    if not bits:
        bits = 16 if s.get("sample_fmt", "").startswith("s16") else 24
    lossless = codec in LOSSLESS_CODECS or codec.startswith("pcm_")
    return {"sr": sr, "codec": codec, "total": total, "exact": exact, "bits": bits,
            "lossless": lossless, "channels": int(s.get("channels") or 2)}


def frames_to_samples(frames: int, sr: int) -> int:
    return (frames * sr * 2 + 75) // 150          # arrondi au plus proche


@dataclass
class SplitItem:
    track: CueTrack
    src: str
    sr: int
    start: int
    end: Optional[int]
    expected: Optional[int]
    tolerance: int
    out_path: str
    lossless: bool
    bits: int


def disc_prefix_needed(cs: CueSheet) -> bool:
    return (cs.totaldiscs.isdigit() and int(cs.totaldiscs) > 1) or (cs.disc.isdigit() and int(cs.disc) > 1)


def build_plan(cs: CueSheet, sources: Dict[str, str], probes: Dict[str, dict]) -> List[SplitItem]:
    outdir = os.path.dirname(cs.path)
    prefix = f"{int(cs.disc)}-" if disc_prefix_needed(cs) and cs.disc.isdigit() else ""
    by_file: Dict[str, List[CueTrack]] = {}
    for t in cs.tracks:
        by_file.setdefault(t.file, []).append(t)
    items = []
    for fname, tracks in by_file.items():
        src, pr = sources[fname], probes[fname]
        sr = pr["sr"]
        tracks = sorted(tracks, key=lambda t: t.index1)
        for i, t in enumerate(tracks):
            start = frames_to_samples(t.index1, sr)
            if i + 1 < len(tracks):
                end = frames_to_samples(tracks[i + 1].index1, sr)
                expected, tol = end - start, 0
            else:
                end = None
                if pr["total"] is not None:
                    expected = pr["total"] - start
                    tol = 0 if pr["exact"] else max(1, sr // 75)
                else:
                    expected, tol = None, 0
            ext = ".flac" if pr["lossless"] else os.path.splitext(src)[1].lower()
            title = t.title or f"Piste {t.num:02d}"
            name = safe_name(f"{prefix}{t.num:02d} - {title}") + ext
            items.append(SplitItem(t, src, sr, start, end, expected, tol,
                                   os.path.join(outdir, name), pr["lossless"], pr["bits"]))
    items.sort(key=lambda it: it.track.num)
    return items


def ffmpeg_cmd(item: SplitItem, tmp: str) -> List[str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y"]
    if item.start > 0:
        cmd += ["-ss", "%.6f" % (item.start / item.sr)]
    cmd += ["-i", item.src, "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1"]
    if item.lossless:
        if item.end is not None:
            cmd += ["-af", f"atrim=end_sample={item.end - item.start}"]
        cmd += ["-c:a", "flac", "-compression_level", "8", "-fflags", "+bitexact", "-flags:a", "+bitexact"]
        if item.bits <= 16:
            cmd += ["-sample_fmt", "s16"]
        elif item.bits <= 24:
            cmd += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
        else:                                   # 32 bits : FLAC 32 bits (ffmpeg ≥ 6.1)
            cmd += ["-sample_fmt", "s32", "-strict", "experimental"]
    else:
        if item.end is not None:
            cmd += ["-t", "%.6f" % ((item.end - item.start) / item.sr)]
        cmd += ["-c", "copy"]
    cmd += ["-progress", "pipe:1", "-nostats", tmp]
    return cmd


def run_ffmpeg(cmd: List[str], on_time=None) -> Tuple[int, str]:
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace")
    for line in p.stdout:
        if line.startswith(("out_time_us=", "out_time_ms=")) and on_time:
            try:
                on_time(int(line.split("=", 1)[1]) / 1e6)
            except ValueError:
                pass
    err = p.stderr.read()
    return p.wait(), err


def output_samples(path: str) -> Optional[int]:
    try:
        mf = mutagen.File(path)
        if isinstance(mf, FLAC):
            return mf.info.total_samples
        return round(mf.info.length * mf.info.sample_rate)
    except Exception:
        return None


def track_tags(cs: CueSheet, t: CueTrack) -> Dict[str, List[str]]:
    artist = cs.force_artist or t.performer or cs.performer
    one = lambda v: [v] if v else []  # noqa: E731
    return {
        "title": [t.title or f"Piste {t.num:02d}"],
        "artist": one(artist),
        "albumartist": one(cs.performer or artist),
        "album": one(cs.title),
        "date": one(cs.date),
        "genre": split_values(cs.genre),
        "tracknumber": [str(t.num)],
        "tracktotal": [str(len(cs.tracks))],
        "discnumber": one(cs.disc),
        "disctotal": one(cs.totaldiscs),
        "composer": one(t.songwriter or cs.songwriter),
    }


def split_one(item: SplitItem, cs: CueSheet, on_time=None) -> Optional[str]:
    """Découpe + vérifie + tague une piste. Renvoie None si OK, sinon l'erreur."""
    ext = os.path.splitext(item.out_path)[1]
    tmp = os.path.join(os.path.dirname(item.out_path), f".musitag-{os.getpid()}-{item.track.num:02d}{ext}")
    try:
        rc, err = run_ffmpeg(ffmpeg_cmd(item, tmp), on_time)
        if rc != 0:
            return "ffmpeg : " + (err.strip().splitlines() or ["erreur inconnue"])[-1]
        got = output_samples(tmp)
        if item.lossless and item.expected is not None and got is not None:
            if abs(got - item.expected) > item.tolerance:
                return f"longueur inattendue : {got} échantillons au lieu de {item.expected}"
        if item.lossless:
            out_bits = getattr(mutagen.File(tmp).info, "bits_per_sample", item.bits)
            if out_bits < min(item.bits, 32):
                return f"profondeur réduite ({item.bits} → {out_bits} bits) : ffmpeg trop ancien ?"
        af = AudioFile(tmp)
        for f, vals in track_tags(cs, item.track).items():
            af.backend.set(f, vals)
        if item.track.isrc:
            if isinstance(af.backend, VorbisBackend):
                af.backend.raw_set("ISRC", [item.track.isrc])
            elif isinstance(af.backend, ID3Backend):
                af.backend.raw_set("TSRC", [item.track.isrc])
        af.backend.save()
        os.replace(tmp, item.out_path)
        return None
    except Exception as e:
        return friendly_error(e)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def cue_overview(cs: CueSheet, sources, probes) -> None:
    g = Table.grid(padding=(0, 2))
    g.add_column(style="bold cyan", justify="right", no_wrap=True)
    g.add_column(overflow="fold")
    dash = Text("—", style="dim")
    g.add_row("Artiste de l'album", Text(cs.performer) if cs.performer else dash)
    g.add_row("Album", Text(cs.title) if cs.title else dash)
    g.add_row("Date", Text(cs.date) if cs.date else dash)
    g.add_row("Genre", Text(cs.genre) if cs.genre else dash)
    disc = cs.disc + (f" / {cs.totaldiscs}" if cs.totaldiscs else "")
    g.add_row("Disque", Text(disc) if cs.disc else dash)
    if cs.force_artist:
        g.add_row("Artiste des pistes", Text(f"{cs.force_artist}  (forcé)", style="yellow"))
    src_txt = Text()
    for fname, path in sources.items():
        pr = probes[fname]
        src_txt.append(os.path.basename(path))
        detail = f"  {pr['codec']} · {pr['bits']} bits · {pr['sr'] / 1000:g} kHz"
        if pr["total"]:
            detail += f" · {fmt_dur(pr['total'] / pr['sr'])}"
        src_txt.append(detail.replace(".", ","), style="dim")
        if not pr["lossless"]:
            src_txt.append("  (source compressée : découpe sans réencodage)", style="yellow")
        src_txt.append("\n")
    g.add_row("Source", src_txt)
    g.add_row("Encodage du .cue", Text(cs.encoding, style="dim"))

    plan = build_plan(cs, sources, probes)
    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False)
    t.add_column("N°", justify="right", style="cyan", no_wrap=True)
    t.add_column("Titre", ratio=3, overflow="fold")
    t.add_column("Artiste", ratio=2, overflow="fold")
    t.add_column("Début", justify="right", style="dim", no_wrap=True)
    t.add_column("Durée", justify="right", no_wrap=True)
    t.add_column("Fichier créé", ratio=3, style="dim", overflow="fold")
    for it in plan:
        tt = track_tags(cs, it.track)
        dur = it.expected / it.sr if it.expected is not None else 0
        t.add_row(f"{it.track.num:02d}", Text(tt["title"][0]), Text(join_values(tt["artist"])),
                  fmt_dur(it.start / it.sr), fmt_dur(dur) if dur else "?", Text(os.path.basename(it.out_path)))
    console.print(Panel(Group(g, t), title=Text(f" {os.path.basename(cs.path)} ", style="bold"),
                        title_align="left", border_style="magenta", box=box.ROUNDED, padding=(1, 2)))
    first = cs.tracks[0] if cs.tracks else None
    if first and first.index1 and first.index1 > 2 * 75:
        console.print(f"[yellow]Note : {fmt_dur(first.index1 / 75)} avant la piste 1 (piste cachée "
                      "ou silence) ne seront pas exportées.[/]")


def cue_album_form(cs: CueSheet) -> None:
    console.print("[dim]Modifie directement · ces infos s'appliqueront à toutes les pistes[/]\n")
    cs.performer = ask(field_prompt("Artiste de l'album"), prefill=cs.performer).strip()
    cs.title = ask(field_prompt("Album"), prefill=cs.title).strip()
    cs.date = ask(field_prompt("Date"), prefill=cs.date).strip()
    cs.genre = ask(field_prompt("Genre"), prefill=cs.genre).strip()
    cs.disc = ask(field_prompt("N° de disque"), prefill=cs.disc).strip()
    cs.totaldiscs = ask(field_prompt("Total disques"), prefill=cs.totaldiscs).strip()
    performers = {t.performer for t in cs.tracks if t.performer}
    if performers - {cs.performer}:
        console.print("\n[dim]Les pistes ont des artistes différents. Laisse vide pour les garder, "
                      "ou tape un nom pour le mettre sur toutes les pistes.[/]")
    cs.force_artist = ask(field_prompt("Artiste des pistes"), prefill=cs.force_artist).strip()


def cue_track_form(cs: CueSheet) -> None:
    r = ask("  [bold]Numéro de la piste[/] [magenta]›[/] ").strip()
    hits = [t for t in cs.tracks if r.isdigit() and t.num == int(r)]
    if not hits:
        return
    t = hits[0]
    t.title = ask(field_prompt("Titre"), prefill=t.title).strip()
    t.performer = ask(field_prompt("Artiste"), prefill=t.performer).strip()


def cue_flow(app) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        info("Le découpage CUE a besoin de ffmpeg :\n\n    [bold]"
             + install_cmd("ffmpeg-free", "ffmpeg") + "[/]", "red")
        pause()
        return
    cues = find_files(app.folder, app.recursive, {".cue"})
    if not cues:
        info("Aucun fichier .cue ici" + (" ni dans les sous-dossiers." if app.recursive else
             " (active le mode récursif avec [bold]r[/] pour chercher dans les sous-dossiers)."))
        pause()
        return
    if len(cues) == 1:
        path = cues[0]
    else:
        t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False)
        t.add_column("#", style="dim", justify="right")
        t.add_column("Fichier .cue", overflow="fold")
        for i, c in enumerate(cues, 1):
            t.add_row(str(i), Text(os.path.relpath(c, app.folder)))
        console.print(t)
        sel = parse_selection(ask("  [bold]Numéro du .cue[/] [magenta]›[/] "), len(cues))
        if not sel:
            return
        path = cues[sel[0]]

    cs = parse_cue(path)
    if not cs.tracks:
        info("Ce .cue ne contient aucune piste audio exploitable.", "red")
        pause()
        return
    # n° de disque deviné depuis le nom (« CD2 », « Disc 2 »…) et nombre de .cue du dossier
    if not cs.disc:
        m = re.search(r"(?:cd|disc|disk|disque)\s*[_.-]?\s*(\d{1,2})",
                      os.path.basename(path) + " " + " ".join(t.file for t in cs.tracks), re.I)
        if m:
            cs.disc = str(int(m.group(1)))
    same_dir = [c for c in cues if os.path.dirname(c) == os.path.dirname(path)]
    if cs.disc and not cs.totaldiscs and len(same_dir) > 1:
        cs.totaldiscs = str(len(same_dir))

    sources: Dict[str, str] = {}
    for fname in dict.fromkeys(t.file for t in cs.tracks):
        src = resolve_source(cs, fname)
        if src is None:
            d = os.path.dirname(path)
            cands = sorted((p for p in find_files(d, False, set(SPLIT_SOURCE_EXTS))),
                           key=lambda p: -os.path.getsize(p))
            console.print(f"[yellow]Le .cue cite « {escape(fname)} », introuvable.[/]")
            if not cands:
                info("Aucun fichier audio à côté du .cue.", "red")
                pause()
                return
            for i, c in enumerate(cands, 1):
                console.print(f"  [dim]{i:>3}[/]  {escape(os.path.basename(c))}  "
                              f"[dim]{human_size(os.path.getsize(c))}[/]")
            sel = parse_selection(ask("  [bold]Quel fichier utiliser ?[/] [magenta]›[/] "), len(cands))
            if not sel:
                return
            src = cands[sel[0]]
        sources[fname] = src
    try:
        probes = {f: probe(p) for f, p in sources.items()}
    except TagError as e:
        info(escape(str(e)), "red")
        pause()
        return

    while True:
        app.clear()
        cue_overview(cs, sources, probes)
        key = menu("Découpage CUE", [
            ("1", "Lancer le découpage", "FLAC au sample près, tags écrits"),
            ("2", "Modifier les infos de l'album", "artiste, album, date, genre, disque"),
            ("3", "Corriger une piste", "titre, artiste"),
            ("4", "Changer l'encodage du .cue", "si les accents / le cyrillique sont cassés"),
            ("0", "Annuler", ""),
        ])
        if key == "0":
            return
        if key == "2":
            cue_album_form(cs)
        elif key == "3":
            cue_track_form(cs)
        elif key == "4":
            opts = [(str(i), label, "") for i, (_, label) in enumerate(CUE_ENCODINGS, 1)]
            k = menu("Encodage du .cue", opts + [("0", "Retour", "")])
            if k != "0":
                enc = CUE_ENCODINGS[int(k) - 1][0]
                keep = (cs.disc, cs.totaldiscs, cs.force_artist)
                new = parse_cue(path, enc)
                if [t.file for t in new.tracks] == [t.file for t in cs.tracks]:
                    cs = new
                    if not cs.disc:
                        cs.disc, cs.totaldiscs = keep[0], cs.totaldiscs or keep[1]
                    cs.force_artist = keep[2]
        elif key == "1":
            if run_split(app, cs, sources, probes):
                return


def run_split(app, cs: CueSheet, sources, probes) -> bool:
    plan = build_plan(cs, sources, probes)
    existing = [it.out_path for it in plan if os.path.exists(it.out_path)]
    if existing:
        info(f"[bold]{len(existing)}[/] fichier(s) de destination existent déjà, ex. « "
             f"{escape(os.path.basename(existing[0]))} ».\nSi c'est un autre disque du même album, "
             "renseigne « N° de disque » et « Total disques » (menu 2) : les noms seront préfixés "
             "(1-01, 2-01…).")
        if not confirm("Les écraser ?", False):
            return False
    total_sec = sum((it.expected or 0) / it.sr for it in plan) or 1.0
    results = []
    with Progress(SpinnerColumn(style="magenta"), TextColumn("{task.description}"),
                  BarColumn(bar_width=None, complete_style="magenta", finished_style="green"),
                  TextColumn("{task.percentage:>3.0f} %"), TimeElapsedColumn(),
                  console=console, transient=True) as prog:
        task = prog.add_task("Découpage", total=total_sec)
        done = 0.0
        for it in plan:
            sec = (it.expected or 0) / it.sr
            label = f"Piste {it.track.num:02d}/{len(plan)}"
            prog.update(task, description=label, completed=done)
            base = done
            err = split_one(it, cs, lambda s, b=base, m=sec: prog.update(task, completed=b + min(s, m or s)))
            done += sec
            prog.update(task, completed=done)
            mark = "[green]✓[/]" if err is None else "[red]✗[/]"
            prog.console.print(f"  {mark} {escape(os.path.basename(it.out_path))}"
                               + (f"  [red]{escape(err)}[/]" if err else ""))
            results.append((it, err))
    failed = [r for r in results if r[1]]
    if failed:
        info(f"{len(failed)} piste(s) en erreur sur {len(results)}. L'image CD d'origine n'a pas été touchée.", "red")
        pause()
        app.rescan()
        return True
    console.print(Panel(Text(f"✓ {len(results)} pistes créées, durées vérifiées au sample près et taguées",
                             style="bold green"), border_style="green", box=box.ROUNDED, expand=False))
    handle_originals(cs, list(dict.fromkeys(sources.values())))
    app.rescan()
    return True


def handle_originals(cs: CueSheet, srcs: List[str]) -> None:
    key = menu("Que faire de l'image CD d'origine ?", [
        ("1", "La ranger dans « _source »", "sous-dossier ignoré par Jellyfin et musitag (.ignore)"),
        ("2", "La supprimer", "le .cue et " + ", ".join(os.path.basename(s) for s in srcs)),
        ("3", "La laisser en place", "Jellyfin verra l'album en double"),
    ])
    files = srcs + [cs.path]
    try:
        if key == "1":
            dest = os.path.join(os.path.dirname(cs.path), "_source")
            os.makedirs(dest, exist_ok=True)
            open(os.path.join(dest, ".ignore"), "a").close()
            for f in files:
                shutil.move(f, os.path.join(dest, os.path.basename(f)))
            console.print("[green]✓ Rangé dans « _source/ ».[/]")
        elif key == "2":
            if confirm("Supprimer définitivement ces fichiers ?", False):
                for f in files:
                    os.remove(f)
                console.print("[green]✓ Supprimé.[/]")
    except PermissionError:
        console.print("[red]Permission refusée — relance musitag avec sudo.[/]")
    except OSError as e:
        console.print(f"[red]Erreur : {escape(str(e))}[/]")
    pause()


# ═════════════════════ Pochettes en ligne : MusicBrainz + Cover Art Archive ═════════════════════
# Même philosophie que LRCLIB : données ouvertes, pas de clé. MusicBrainz identifie
# l'album (et ses éditions), Cover Art Archive (hébergé par Internet Archive) fournit
# les images. Règle MusicBrainz : 1 requête / seconde et un User-Agent identifiable.

MB_URL = os.environ.get("MUSITAG_MB_URL", "https://musicbrainz.org/ws/2").rstrip("/")
CAA_URL = os.environ.get("MUSITAG_CAA_URL", "https://coverartarchive.org").rstrip("/")
_CONTACT = (os.environ.get("MUSITAG_CONTACT") or CONFIG.get("CONTACT") or "").strip()
MB_USER_AGENT = f"musitag/{VERSION} ( {_CONTACT or 'outil perso de tags audio'} )"
MAX_COVERS = 9

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None


class NetError(Exception):
    pass


class Throttle:
    """Espace les requêtes vers un service, tous threads confondus, et ralentit s'il freine."""

    def __init__(self, interval: float):
        self.base = self.interval = interval
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next - now
            self.next = max(now, self.next) + self.interval
        if delay > 0:
            time.sleep(delay)

    def slow_down(self, pause: float) -> None:
        with self.lock:
            self.interval = min(self.interval * 1.5, self.base * 4)
            self.next = max(self.next, time.monotonic() + pause)

    def speed_up(self) -> None:
        with self.lock:
            self.interval = max(self.base, self.interval * 0.95)


MB_THROTTLE = Throttle(1.1)
CAA_THROTTLE = Throttle(0.15)


def http_get(url: str, throttle: Throttle, tries: int = 4, accept: str = "application/json") -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": MB_USER_AGENT, "Accept": accept})
    last = "erreur inconnue"
    for attempt in range(tries):
        throttle.wait()
        try:
            with url_open(req, timeout=30) as r:
                data = read_limited(r, MAX_IMAGE_BYTES if accept.startswith("image") else MAX_JSON_BYTES)
            throttle.speed_up()
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = f"HTTP {e.code}"
            if e.code in (429, 503):                 # MusicBrainz répond 503 quand on va trop vite
                throttle.slow_down((2, 4, 8, 15)[attempt])
                continue
            if e.code not in (500, 502, 504):
                break
        except OSError as e:
            last = str(getattr(e, "reason", "") or e) or type(e).__name__
        if attempt < tries - 1:
            time.sleep(2 ** attempt)
    raise NetError(last)


def http_json(url: str, throttle: Throttle):
    data = http_get(url, throttle)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        raise NetError("réponse illisible")


def _caa_url(u: str) -> str:
    """Les URL de Cover Art Archive arrivent en http:// : on passe en https."""
    if u and u.startswith("http://") and CAA_URL.startswith("https://"):
        return "https://" + u[len("http://"):]
    return u


def _lucene(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mb_release_groups(artist: str, album: str) -> List[dict]:
    out, seen = [], set()
    queries = []
    for alb in dict.fromkeys([album, base_title(album)]):
        queries.append(f"releasegroup:{_lucene(alb)} AND artist:{_lucene(artist)}")
    queries.append(f"releasegroup:{_lucene(base_title(album))}")       # artiste crédité autrement
    for q in queries:
        js = http_json(f"{MB_URL}/release-group/?" + urllib.parse.urlencode(
            {"query": q, "fmt": "json", "limit": 15}), MB_THROTTLE) or {}
        for rg in js.get("release-groups", []):
            if rg.get("id") in seen:
                continue
            seen.add(rg.get("id"))
            credit = "".join((c.get("name") or "") + (c.get("joinphrase") or "")
                             for c in rg.get("artist-credit", []) if isinstance(c, dict))
            ts = max(_sim(album, rg.get("title", "")), _sim(base_title(album), base_title(rg.get("title", ""))))
            asim = _artist_sim([artist], credit)
            if ts >= 0.6 and asim >= 0.5:
                rg["_credit"] = credit
                rg["_rank"] = ts * 2 + asim + (rg.get("score") or 0) / 100
                out.append(rg)
        if out:
            break
    out.sort(key=lambda r: -r["_rank"])
    return out


def mb_releases_with_art(rg_id: str) -> List[dict]:
    js = http_json(f"{MB_URL}/release?" + urllib.parse.urlencode(
        {"release-group": rg_id, "inc": "media", "fmt": "json", "limit": 100}), MB_THROTTLE) or {}
    rels = [r for r in js.get("releases", []) if (r.get("cover-art-archive") or {}).get("front")]
    rels.sort(key=lambda r: (r.get("status") != "Official", r.get("date") or "9999"))
    return rels


@dataclass
class CoverCandidate:
    album: str
    artist: str
    date: str
    country: str
    media: str
    release_id: str
    urls: List[str]                 # du plus adapté au plus lourd : 1200 px, 500 px, original
    thumb: bytes = b""


def _fetch_candidate(rg: dict, rel: dict) -> Optional[CoverCandidate]:
    try:
        js = http_json(f"{CAA_URL}/release/{rel['id']}", CAA_THROTTLE) or {}
        img = next((i for i in js.get("images", [])
                    if i.get("front") or "Front" in (i.get("types") or [])), None)
        if not img:
            return None
        th = img.get("thumbnails") or {}
        thumb_url = _caa_url(th.get("250") or th.get("small") or img.get("image") or "")
        urls = [_caa_url(u) for u in (th.get("1200"), th.get("500") or th.get("large"), img.get("image")) if u]
        thumb = (http_get(thumb_url, CAA_THROTTLE, accept="image/*") or b"") if thumb_url else b""
    except NetError:
        return None
    media = " + ".join(dict.fromkeys((m.get("format") or "?") for m in rel.get("media") or []))
    return CoverCandidate(rel.get("title") or rg.get("title", ""), rg.get("_credit", ""),
                          rel.get("date") or "", rel.get("country") or "", media, rel["id"], urls, thumb)


MAX_IMAGE_PIXELS = 40_000_000          # ~6300×6300 : bien au-delà d'une vraie pochette


def open_image(data: bytes):
    """Ouvre une image avec Pillow sans la décoder si elle est démesurée (ValueError)."""
    im = PILImage.open(io.BytesIO(data))
    w, h = im.size
    if w * h > MAX_IMAGE_PIXELS:
        raise ValueError(f"image trop grande ({w}×{h})")
    return im


def _fingerprint(data: bytes):
    """Empreinte d'image : structure (hash moyen 8×8) + couleur moyenne."""
    if PILImage is None or not data:
        return None
    try:
        im = open_image(data).convert("RGB").resize((8, 8))
        px = im.load()
        rgb = [px[x, y] for y in range(8) for x in range(8)]
        gray = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in rgb]
        avg = sum(gray) / 64
        h = sum(1 << i for i, p in enumerate(gray) if p > avg)
        mean = tuple(sum(px[c] for px in rgb) / 64 for c in range(3))
        return h, mean
    except Exception:
        return None


def _same_image(a, b) -> bool:
    return bin(a[0] ^ b[0]).count("1") <= 5 and max(abs(x - y) for x, y in zip(a[1], b[1])) < 18


def cover_candidates(artist: str, album: str) -> List[CoverCandidate]:
    pairs = []
    for rg in mb_release_groups(artist, album)[:3]:
        for rel in mb_releases_with_art(rg["id"])[:8]:
            pairs.append((rg, rel))
        if len(pairs) >= 16:
            break
    if not pairs:
        return []
    with ThreadPoolExecutor(max_workers=4) as ex:
        cands = [c for c in ex.map(lambda p: _fetch_candidate(*p), pairs) if c]
    # la même image publiée pour plusieurs éditions : on n'en garde qu'une
    kept, prints = [], []
    for c in cands:
        fp = _fingerprint(c.thumb)
        if fp is not None and any(_same_image(fp, o) for o in prints):
            continue
        if fp is not None:
            prints.append(fp)
        kept.append(c)
    return kept[:MAX_COVERS]


# ─── aperçu des images dans le terminal (blocs ▀ en couleurs vraies) ───

_IMG_CONSOLE = None


def img_console() -> Console:
    global _IMG_CONSOLE
    if _IMG_CONSOLE is None:
        if console.is_terminal and os.environ.get("MUSITAG_TRUECOLOR", "1") != "0":
            _IMG_CONSOLE = SafeConsole(color_system="truecolor", highlight=False)
        else:
            _IMG_CONSOLE = console
    return _IMG_CONSOLE


def image_preview(data: bytes, cols: int) -> Optional[Text]:
    """Une image en cols × cols/2 caractères : chaque « ▀ » affiche 2 pixels (haut/bas)."""
    if PILImage is None or not data:
        return None
    try:
        im = open_image(data).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    rows = max(2, round(cols * h / max(w, 1)))
    rows += rows % 2
    lanczos = getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS")
    im = im.resize((cols, rows), lanczos)
    px = im.load()
    from rich.color import Color
    from rich.style import Style
    t = Text()
    for y in range(0, rows, 2):
        for x in range(cols):
            top, bot = px[x, y], px[x, y + 1]
            t.append("▀", Style(color=Color.from_rgb(*top), bgcolor=Color.from_rgb(*bot)))
        if y + 2 < rows:
            t.append("\n")
    return t


def image_dims(data: bytes) -> str:
    if PILImage is not None:
        try:
            w, h = PILImage.open(io.BytesIO(data)).size
            return f"{w}×{h}"
        except Exception:
            pass
    return ""


def show_covers(entries: List[Tuple[str, bytes, str, str]]) -> None:
    """entries : (numéro, image, ligne 1, ligne 2)."""
    width, height = console.width, console.height
    # la plus grande taille d'aperçu qui tient dans l'écran
    for cell in (26, 22, 18, 16, 14, 12):
        per_row = max(1, (width - 2) // (cell + 3))
        lines = -(-len(entries) // per_row) * (cell // 2 + 3)
        if lines <= height - 8:
            break
    out = img_console()
    for start in range(0, len(entries), per_row):
        chunk = entries[start:start + per_row]
        grid = Table.grid(padding=(0, 3))
        for _ in chunk:
            grid.add_column(width=cell, no_wrap=True, overflow="ellipsis")
        pics = []
        for _, data, _, _ in chunk:
            p = image_preview(data, cell)
            pics.append(p if p is not None else Text("\n" * (cell // 4) + "  (pas d'aperçu)", style="dim"))
        grid.add_row(*pics)
        grid.add_row(*[Text.assemble((f" {num} ", "bold black on cyan" if num != "0" else "bold black on grey62"),
                                     " ", (l1, "bold")) for num, _, l1, _ in chunk])
        grid.add_row(*[Text(l2, style="dim") for _, _, _, l2 in chunk])
        out.print(grid)
        out.print()


def _most_common(values: List[str]) -> str:
    counts: Dict[str, int] = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def cover_album_flow(d: str, fl: List[AudioFile], pos: int, total: int) -> str:
    """Une album : recherche, aperçus, choix, intégration. Renvoie done / skip / quit."""
    artist = _most_common([af.first("albumartist") or af.first("artist") for af in fl])
    album = _most_common([af.first("album") for af in fl])
    current = next((p for p in (first_picture(af) for af in fl) if p), None)
    while True:
        if console.is_terminal:
            console.clear()
        console.print(Rule(Text(f" Pochette · album {pos}/{total} ", style="bold magenta"), style="magenta"))
        console.print(Text.assemble("  ", (artist or "?", "bold"), " – ", (album or "?", "bold"),
                                    (f"   {len(fl)} fichier(s) · {d}", "dim")))
        console.print()
        if not artist or not album:
            info("Il manque l'artiste ou l'album dans les tags : tape-les pour lancer la recherche.")
            artist = ask(field_prompt("Artiste"), prefill=artist).strip()
            album = ask(field_prompt("Album"), prefill=album).strip()
            if not artist or not album:
                return "skip"
            continue
        cands: List[CoverCandidate] = []
        try:
            with console.status("[magenta]Recherche sur MusicBrainz / Cover Art Archive…[/]", spinner="dots"):
                cands = cover_candidates(artist, album)
        except NetError as e:
            info(f"MusicBrainz / Cover Art Archive injoignable ({escape(str(e))}).", "red")
        entries = []
        if current:
            dims = image_dims(current)
            entries.append(("0", current, "actuelle", f"intégrée · {dims}" if dims else "intégrée"))
        for i, c in enumerate(cands, 1):
            year = c.date[:4] if c.date else "?"
            l2 = " · ".join(x for x in (year, c.country, c.media) if x)
            entries.append((str(i), c.thumb, c.album, l2))
        if entries:
            show_covers(entries)
        if not cands:
            info(f"Aucune pochette trouvée pour « {escape(artist)} – {escape(album)} ».\n"
                 "[dim]Essaie « r » avec un nom d'album plus simple, ou passe cet album.[/]")
        if PILImage is None and cands:
            console.print("[yellow]Aperçus indisponibles : installe Pillow pour voir les images dans le terminal "
                          f"([bold]{install_cmd('python3-pillow', 'python3-pil')}[/]).[/]")
        console.print("  [dim]numéro = choisir · [bold]r[/] = chercher avec d'autres mots · "
                      "[bold]Entrée[/] = passer cet album · [bold]q[/] = arrêter[/]")
        r = ask("[bold magenta]›[/] ").strip().lower()
        if r == "q":
            return "quit"
        if r in ("", "s", "0"):
            return "skip"
        if r == "r":
            artist = ask(field_prompt("Artiste"), prefill=artist).strip()
            album = ask(field_prompt("Album"), prefill=album).strip()
            continue
        if r.isdigit() and 1 <= int(r) <= len(cands):
            chosen = cands[int(r) - 1]
            break
        console.print("[red]Choix invalide.[/]")
        time.sleep(0.8)

    data = None
    with console.status("[magenta]Téléchargement de la pochette…[/]", spinner="dots"):
        for u in chosen.urls:
            try:
                data = http_get(u, CAA_THROTTLE, accept="image/*")
            except NetError:
                data = None
            if data and image_mime(data):
                break
    if not data or not image_mime(data):
        info("Téléchargement impossible (ou format d'image non pris en charge).", "red")
        pause()
        return "skip"
    mime = image_mime(data)
    dims = image_dims(data)
    desc = " · ".join(x for x in (dims, human_size(len(data))) if x)
    console.print(f"  Choisie : [bold]{escape(chosen.album)}[/] [dim]({escape(desc)})[/]")
    if not confirm(f"Intégrer cette pochette dans les {len(fl)} fichier(s) de l'album ?", True):
        return "skip"
    ops = [Op(af, "pic_add", before=f"{len(af.backend.pictures())} image(s)",
              after=f"Cover Art Archive · {desc}", label="Pochette", data=data, mime=mime) for af in fl]
    ok, errors = apply_ops(ops)
    show_result(ok, errors)
    if total == 1 or errors:
        pause()
    else:
        time.sleep(1.0)
    return "done"


def covers_online(files) -> None:
    groups = group_by_dir(files)
    if len(groups) > 1:
        missing = [(d, fl) for d, fl in groups if any(not af.backend.pictures() for af in fl)]
        k = menu(f"Pochettes en ligne · {len(groups)} albums", [
            ("1", "Tous les albums", "remplace aussi les pochettes existantes si tu en choisis une"),
            ("2", "Seulement ceux sans pochette", f"{len(missing)} album(s) avec au moins un fichier sans image"),
            ("0", "Retour", ""),
        ])
        if k == "0":
            return
        if k == "2":
            groups = missing
        if not groups:
            info("Tous les albums ont déjà une pochette intégrée.", "green")
            pause()
            return
    done = 0
    for pos, (d, fl) in enumerate(groups, 1):
        res = cover_album_flow(d, fl, pos, len(groups))
        done += res == "done"
        if res == "quit":
            break
    if len(groups) > 1:
        info(f"{done} album(s) mis à jour sur {len(groups)}.\n[dim]Jellyfin : « Actualiser les métadonnées » "
             "+ « Remplacer les images » pour qu'il reprenne les nouvelles pochettes.[/]", "green")
        pause()


# ═══════════════════════════════ Nettoyage ════════════════════════════════════
# Objectif : dans un dossier d'album, uniquement la musique. Tout le reste
# (pochette, cue, playlist, logs, scans…) est proposé à la suppression.

CLEAN_KINDS = [
    # (type affiché, extensions)
    ("Image", {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}),
    ("CUE", {".cue"}),
    ("Playlist", {".m3u", ".m3u8", ".pls", ".xspf", ".wpl", ".asx"}),
    ("Paroles .lrc", {".lrc", ".elrc"}),
    ("Texte / log", {".txt", ".log", ".nfo", ".md", ".rtf", ".doc", ".docx", ".pdf", ".htm", ".html",
                     ".url", ".json", ".xml", ".csv", ".ini", ".sfk", ".pk", ".reapeaks"}),
    ("Contrôle", {".accurip", ".ffp", ".md5", ".sfv", ".sha1", ".sha256", ".par2", ".crc", ".st5"}),
    ("Vidéo", {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".vob", ".ts"}),
    ("Archive", {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}),
    ("Temporaire", {".torrent", ".part", ".!qb", ".tmp", ".bak", ".crdownload", ".old"}),
]
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".directory", "ehthumbs.db", ".ignore"}


@dataclass
class CleanItem:
    path: str
    is_dir: bool
    kind: str
    size: int
    note: str = ""
    protected: bool = False


def _classify(path: str, name: str) -> Tuple[str, str, bool]:
    """(type, note, protégé) d'un fichier non audio."""
    low = name.lower()
    ext = os.path.splitext(low)[1]
    folder = os.path.dirname(path)
    if ext in AUDIO_LIKE_EXTS:
        return "Audio ?", "audio que musitag ne sait pas lire : vérifie avant de supprimer", True
    if low in JUNK_NAMES or low.startswith("._"):
        return "Système", "fichier caché (Mac/Windows/musitag)", False
    for kind, exts in CLEAN_KINDS:
        if ext in exts:
            if kind == "CUE":
                try:
                    cs = parse_cue(path)
                    srcs = {resolve_source(cs, t.file) for t in cs.tracks}
                    srcs.discard(None)
                except Exception:
                    srcs = set()
                if srcs:
                    return kind, "image CD pas encore découpée : découpe-la d'abord (menu 5)", True
            if kind == "Image" and find_cover(folder) == path:
                return kind, "pochette du dossier", False
            if kind == "Paroles .lrc":
                return kind, "paroles hors du fichier audio", False
            return kind, "", False
    return "Autre", "", False


def _dir_stats(path: str):
    """(contient de l'audio ?, nb de fichiers, taille, types présents)"""
    has_audio, n, size, kinds = False, 0, 0, {}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SYSTEM_DIR_NAMES]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXTS or ext in AUDIO_LIKE_EXTS:
                has_audio = True
            n += 1
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            k = next((kd for kd, exts in CLEAN_KINDS if ext in exts),
                     "audio" if ext in AUDIO_EXTS or ext in AUDIO_LIKE_EXTS else "autre")
            kinds[k] = kinds.get(k, 0) + 1
    return has_audio, n, size, kinds


def scan_clean(root: str, recursive: bool) -> List[CleanItem]:
    items: List[CleanItem] = []

    def visit(d: str) -> None:
        try:
            entries = sorted(os.scandir(d), key=lambda e: natural_key(e.name))
        except OSError:
            return
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if e.name in SYSTEM_DIR_NAMES:
                    continue
                has_audio, n, size, kinds = _dir_stats(e.path)
                desc = ", ".join(f"{v} {k.lower()}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:3])
                if os.path.exists(os.path.join(e.path, ".ignore")):
                    items.append(CleanItem(e.path, True, "Dossier", size,
                                           f"ignoré par Jellyfin (.ignore), ex. image CD déjà découpée · {desc}"))
                elif not has_audio:
                    items.append(CleanItem(e.path, True, "Dossier", size,
                                           f"pas de musique dedans · {n} fichier(s) : {desc}" if n else "dossier vide"))
                elif recursive:
                    visit(e.path)
                continue
            ext = os.path.splitext(e.name)[1].lower()
            if ext in AUDIO_EXTS:
                continue
            try:
                size = e.stat(follow_symlinks=False).st_size
            except OSError:
                size = 0
            kind, note, protected = _classify(e.path, e.name)
            items.append(CleanItem(e.path, False, kind, size, note, protected))

    visit(root)
    return items


def clean_table(items: List[CleanItem], base: str):
    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False)
    t.add_column("#", style="dim", justify="right", no_wrap=True, width=len(str(len(items))))
    t.add_column("Type", no_wrap=True, width=12)
    t.add_column("Élément", ratio=3, overflow="fold")
    t.add_column("Taille", justify="right", no_wrap=True, width=9, style="dim")
    t.add_column("Note", ratio=2, overflow="fold")
    for i, it in enumerate(items, 1):
        rel = os.path.relpath(it.path, base) + (os.sep if it.is_dir else "")
        kind_style = {"Dossier": "bold cyan", "Image": "magenta", "CUE": "yellow", "Audio ?": "bold red"}.get(it.kind, "")
        note = Text(("⚠ " if it.protected else "") + it.note,
                    style="yellow" if it.protected else "dim")
        t.add_row(str(i), Text(it.kind, style=kind_style), Text(rel), human_size(it.size), note)
    return t


def first_picture(af) -> Optional[bytes]:
    """Données de la première image intégrée (pour l'aperçu de la pochette actuelle)."""
    mf, b = af.mf, af.backend
    try:
        if isinstance(mf, FLAC) and mf.pictures:
            return mf.pictures[0].data
        if isinstance(b, VorbisBackend):
            for v in b._vals("METADATA_BLOCK_PICTURE"):
                return Picture(base64.b64decode(v)).data
        if isinstance(b, ID3Backend) and mf.tags is not None:
            apics = mf.tags.getall("APIC")
            if apics:
                return apics[0].data
        if isinstance(b, MP4Backend) and mf.tags is not None and mf.tags.get("covr"):
            return bytes(mf.tags["covr"][0])
        if isinstance(b, APEBackend):
            for k in b._pic_keys():
                raw = mf.tags[k].value
                return raw.split(b"\x00", 1)[1] if b"\x00" in raw[:256] else raw
        if isinstance(b, ASFBackend):
            for v in mf.tags.get("WM/Picture", []):
                return _asf_picture(v.value)[1]
    except Exception:
        return None
    return None


def _read_text(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def rescue_ops(selected: List[CleanItem]) -> List[Op]:
    """Avant de supprimer : récupère dans les fichiers audio la pochette et les paroles
    qui n'existent que dans les fichiers à jeter (cover.jpg, Scans/front.jpg, .lrc)."""
    ops: List[Op] = []
    audio_cache: Dict[str, List[AudioFile]] = {}

    def audio_in(d: str) -> List[AudioFile]:
        if d not in audio_cache:
            audio_cache[d], _ = load_files(find_files(d, False, AUDIO_EXTS))
        return audio_cache[d]

    covered = set()
    for it in selected:
        if it.protected:
            continue
        cover = None
        if it.kind == "Image" and it.note == "pochette du dossier":
            cover, d = it.path, os.path.dirname(it.path)
        elif it.is_dir:
            cover, d = find_cover(it.path), os.path.dirname(it.path)
        if cover and d not in covered:
            try:
                data, mime = read_image(cover)
            except (OSError, TagError):
                continue
            covered.add(d)
            for af in audio_in(d):
                if not af.backend.pictures():
                    ops.append(Op(af, "pic_add", before="aucune", label="Pochette", data=data, mime=mime,
                                  after=f"{os.path.relpath(cover, d)} · {human_size(len(data))}"))
        if it.kind == "Paroles .lrc":
            stem = os.path.splitext(os.path.basename(it.path))[0].lower()
            for af in audio_in(os.path.dirname(it.path)):
                if af.stem.lower() == stem and not get_lyrics(af):
                    try:
                        text = _read_text(it.path).strip()
                    except OSError:
                        continue
                    if text:
                        ops.append(Op(af, "set", "lyrics", [text], before="", label="Paroles",
                                      after=lyrics_summary(text)))
    return ops


def delete_items(items: List[CleanItem]):
    ok, errors = 0, []
    prog = make_progress() if len(items) > 8 and console.is_terminal else _NullProgress()
    with prog:
        task = prog.add_task("Suppression", total=len(items))
        for it in items:
            try:
                if it.is_dir:
                    shutil.rmtree(it.path)
                else:
                    os.remove(it.path)
                ok += 1
            except FileNotFoundError:
                ok += 1
            except Exception as e:
                errors.append((os.path.basename(it.path), friendly_error(e)))
            prog.advance(task)
    return ok, errors


def clean_flow(app) -> None:
    with console.status("[magenta]Analyse du dossier…[/]", spinner="dots"):
        items = scan_clean(app.folder, app.recursive)
    if not items:
        info("Rien à nettoyer : il n'y a que de la musique ici"
             + (" (sous-dossiers compris)." if app.recursive else
                ".\n[dim]Active le mode récursif (r) pour inspecter aussi les sous-dossiers.[/]"), "green")
        pause()
        return
    print_long([clean_table(items, app.folder)], wait=False)
    free = [i for i, it in enumerate(items) if not it.protected]
    prot = len(items) - len(free)
    total = sum(items[i].size for i in free)
    kinds: Dict[str, List[int]] = {}
    for i in free:
        kinds.setdefault(items[i].kind, []).append(i)
    summary = Text(f"{len(items)} élément(s) qui ne sont pas de la musique · {human_size(sum(it.size for it in items))}",
                   style="bold")
    if prot:
        summary.append(f"\n{prot} protégé(s) (⚠) : jamais inclus dans « tout », seulement si tu les choisis par numéro.",
                       style="yellow")
    console.print(summary)
    k = menu("Nettoyage", [
        ("1", f"Tout supprimer", f"{len(free)} élément(s) · {human_size(total)}"),
        ("2", "Choisir lesquels", "numéros, ex : 1,3-5"),
        ("3", "Par type", ", ".join(f"{kd} ({len(v)})" for kd, v in kinds.items())),
        ("0", "Retour", "rien n'est supprimé"),
    ])
    if k == "0":
        return
    if k == "1":
        sel = free
    elif k == "2":
        sel = parse_selection(ask("  Numéros à supprimer [magenta]›[/] "), len(items))
    else:
        names = list(kinds)
        for j, kd in enumerate(names, 1):
            console.print(f"  [cyan]{j:>2}[/]  {escape(kd)} [dim]({len(kinds[kd])})[/]")
        picked = parse_selection(ask("  Types à supprimer [dim](ex : 1,3)[/] [magenta]›[/] "), len(names))
        sel = sorted(i for j in picked for i in kinds[names[j]])
    chosen = [items[i] for i in sel]
    if not chosen:
        return
    console.print()
    for it in chosen[:15]:
        console.print(f"  [red]✗[/] {escape(os.path.relpath(it.path, app.folder))}{os.sep if it.is_dir else ''}")
    if len(chosen) > 15:
        console.print(f"  [dim]… et {len(chosen) - 15} autre(s)[/]")

    rescue = rescue_ops(chosen)
    if rescue:
        pics = sum(1 for o in rescue if o.kind == "pic_add")
        lyr = len(rescue) - pics
        parts = ([f"la pochette dans {pics} fichier(s) qui n'en ont pas"] if pics else []) + \
                ([f"les paroles .lrc dans {lyr} fichier(s)"] if lyr else [])
        console.print()
        if confirm("Avant de supprimer, intégrer " + " et ".join(parts) + " ?", True):
            ok, errors = apply_ops(rescue)
            show_result(ok, errors)
            if errors and not confirm("Des intégrations ont échoué. Supprimer quand même ?", False):
                return
    size = sum(it.size for it in chosen)
    console.print()
    if not confirm(f"[bold red]Supprimer définitivement {len(chosen)} élément(s)[/] ({human_size(size)}) ? "
                   "[dim]Pas de corbeille sur le NAS.[/]", False):
        console.print("[yellow]Rien n'a été supprimé.[/]")
        pause()
        return
    ok, errors = delete_items(chosen)
    if ok:
        console.print(Panel(Text(f"✓ {ok} élément(s) supprimé(s) · {human_size(size)} libérés", style="bold green"),
                            border_style="green", box=box.ROUNDED, expand=False))
    if errors:
        show_result(0, errors)
    if any(f.endswith(".jpg") or f.endswith(".png") for f in (it.path.lower() for it in chosen)):
        console.print("[dim]Jellyfin : « Actualiser les métadonnées » + « Remplacer les images » sur les albums "
                      "concernés pour qu'il reprenne la pochette intégrée.[/]")
    pause()
    app.rescan()


# ═════════════════════════════════ Application ════════════════════════════════

class App:
    def __init__(self, folder: str, recursive: bool, notice: str = ""):
        self.folder = folder
        self.recursive = recursive
        self.notice = notice
        self.files: List[AudioFile] = []
        self.skipped: List[Tuple[str, str]] = []
        self.can_write = True

    def rescan(self) -> None:
        self.files, self.skipped = load_files(find_files(self.folder, self.recursive, AUDIO_EXTS))
        self.can_write = self._check_write()

    def resort(self) -> None:
        self.files.sort(key=lambda af: path_sort_key(af.path))

    def _check_write(self) -> bool:
        if os.geteuid() == 0:
            return True
        targets = [self.folder] + [af.path for af in self.files[:20]]
        return all(os.access(p, os.W_OK) for p in targets)

    def clear(self) -> None:
        if console.is_terminal:
            console.clear()

    def banner(self) -> None:
        g = Table.grid(padding=(0, 2))
        g.add_column(style="dim", justify="right", no_wrap=True)
        g.add_column(overflow="fold")
        g.add_row("Dossier", Text(self.folder, style="bold"))
        if self.notice:
            g.add_row("Note", Text(self.notice, style="yellow"))
        g.add_row("Mode", Text("récursif (sous-dossiers inclus)", style="cyan") if self.recursive
                  else Text("ce dossier uniquement"))
        n = len(self.files)
        cnt = Text(f"{n} fichier{'s' if n > 1 else ''} audio")
        dirs = len({af.dir for af in self.files})
        if dirs > 1:
            cnt.append(f" dans {dirs} dossiers")
        if n:
            cnt.append(f"  ·  {fmt_dur(sum(af.length() for af in self.files))}", style="dim")
        if self.skipped:
            cnt.append(f"  ·  {len(self.skipped)} illisible(s)", style="yellow")
        g.add_row("Contenu", cnt)
        if os.geteuid() == 0:
            g.add_row("Droits", Text("root (sudo)", style="green"))
        elif not self.can_write:
            g.add_row("Droits", Text("lecture seule — relance avec sudo pour modifier", style="yellow"))
        title = Text.assemble(("♫ ", "magenta"), ("musitag", "bold magenta"), (f"  v{VERSION}", "dim"))
        console.print(Panel(g, title=title, title_align="left", border_style="magenta",
                            box=box.ROUNDED, padding=(0, 1)))

    def screen(self) -> None:
        self.clear()
        self.banner()

    def offer_sudo(self) -> None:
        if self.can_write or os.geteuid() == 0 or not shutil.which("sudo"):
            return
        self.screen()
        info("Ton utilisateur ne peut pas modifier ce dossier (NAS).\n"
             "Tu peux juste consulter, ou relancer musitag avec sudo.", "yellow")
        if confirm("Relancer avec sudo ?", True):
            # « sudo env » transmet les réglages MUSITAG_* (sudo efface l'environnement)
            keep = [f"{k}={v}" for k, v in os.environ.items() if k.startswith("MUSITAG_")]
            args = ["sudo", "env"] + keep + [sys.executable, os.path.abspath(__file__), self.folder] \
                + (["-r"] if self.recursive else [])
            os.execvp("sudo", args)

    def view_all(self) -> None:
        if not self.files:
            info("Aucun fichier audio ici.")
            pause()
            return
        renders, idx = [], 1
        groups = group_by_dir(self.files)
        for d, fl in groups:
            title = None
            if len(groups) > 1:
                rel = os.path.relpath(d, self.folder)
                title = "." if rel == "." else rel
            renders.append(files_table(fl, idx, title=title, total=len(self.files)))
            idx += len(fl)
        with_lyr = sum(1 for af in self.files if get_lyrics(af))
        no_title = sum(1 for af in self.files if not af.get("title"))
        summary = Text(f"{len(self.files)} fichier(s) · {fmt_dur(sum(af.length() for af in self.files))}",
                       style="dim")
        if no_title:
            summary.append(f" · {no_title} sans titre (nom de fichier en italique)", style="yellow")
        summary.append(f" · paroles : {with_lyr}/{len(self.files)}", style="dim")
        if with_lyr and console.width >= 110:
            summary.append("  (colonne ♪ : ", style="dim")
            summary.append("●", style="green")
            summary.append(" synchronisées · ", style="dim")
            summary.append("○", style="cyan")
            summary.append(" texte · ", style="dim")
            summary.append("♪", style="magenta")
            summary.append(" instrumental)", style="dim")
        if self.skipped:
            summary.append(f"\nIllisibles : " + ", ".join(os.path.basename(p) for p, _ in self.skipped[:10]),
                           style="yellow")
        print_long(renders + [summary])

    def batch(self) -> None:
        if not self.files:
            info("Aucun fichier audio ici.")
            pause()
            return
        files = self.choose_scope()
        if not files:
            return
        actions = {"1": batch_form, "2": batch_numbering, "3": batch_titles, "4": batch_replace,
                   "5": batch_rename, "6": batch_delete_fields, "7": batch_pictures,
                   "8": lyrics_menu, "9": batch_wipe}
        while True:
            self.screen()
            dirs = len({af.dir for af in files})
            console.print(f" [bold]{len(files)}[/] fichier(s) concerné(s)"
                          + (f" dans [bold]{dirs}[/] dossiers" if dirs > 1 else "") + "\n")
            key = menu("Modifier tous les fichiers", [
                ("1", "Champs communs", "artiste, album, date, genre, disque…"),
                ("2", "Numéroter les pistes", "depuis les noms ou dans l'ordre"),
                ("3", "Titres depuis les noms de fichiers", "« 01 - Loser.flac » → Loser"),
                ("4", "Rechercher / remplacer", "ex : retirer « (2016 Remaster) »"),
                ("5", "Renommer les fichiers", "depuis les tags : « 01 - Titre.flac »"),
                ("6", "Supprimer des champs", "COMMENT, ENCODER, champs parasites…"),
                ("7", "Pochettes", "en ligne (Cover Art Archive), cover.jpg, supprimer"),
                ("8", "Paroles", "LRCLIB, marquer instrumental, supprimer"),
                ("9", "Effacer tous les tags", ""),
                None,
                ("0", "Retour", ""),
            ])
            if key == "0":
                return
            try:
                actions[key](files)
            except (KeyboardInterrupt, Cancel):
                console.print("\n[yellow]Annulé.[/]")
                time.sleep(0.6)
            self.resort()

    def choose_scope(self):
        groups = group_by_dir(self.files)
        if len(groups) == 1:
            return self.files
        t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False,
                  title=Text("Quels dossiers ?", style="bold"), title_justify="left")
        t.add_column("#", style="dim", justify="right")
        t.add_column("Dossier", ratio=1, overflow="fold")
        t.add_column("Fichiers", justify="right")
        for i, (d, fl) in enumerate(groups, 1):
            t.add_row(str(i), Text(os.path.relpath(d, self.folder)), str(len(fl)))
        console.print(t)
        r = ask(f"  Numéros [dim](ex : 2,4-6 · Entrée = les {len(groups)} dossiers)[/] [magenta]›[/] ").strip()
        if not r:
            return self.files
        sel = parse_selection(r, len(groups))
        return [af for i in sel for af in groups[i][1]]

    def open_folder(self, path: str) -> None:
        self.folder = path
        self.notice = ""
        self.rescan()
        self.offer_sudo()

    def browse(self) -> None:
        """Navigateur : Musiques → Artiste → Album, au numéro ou par morceau de nom."""
        cur, flt = self.folder, ""
        while True:
            try:
                subs = sorted((e.name for e in os.scandir(cur)
                               if e.is_dir() and not e.name.startswith(".") and e.name not in SYSTEM_DIR_NAMES),
                              key=natural_key)
            except OSError as e:
                info(f"Lecture impossible : {escape(str(e))}", "red")
                subs = []
            shown = [d for d in subs if flt.lower() in d.lower()] if flt else subs
            self.clear()
            console.print(Rule(Text(" Ouvrir un dossier ", style="bold magenta"), style="magenta"))
            console.print(Text.assemble(("  ", ""), (cur, "bold"),
                                        (f"   ·  filtre « {flt} »" if flt else "", "yellow")))
            console.print()
            if shown:
                cell = max(len(d) for d in shown) + 6
                ncols = max(1, min(5, (console.width - 2) // min(cell, 40)))
                nrows = -(-len(shown) // ncols)
                grid = Table.grid(padding=(0, 2))
                for _ in range(ncols):
                    grid.add_column(no_wrap=True, overflow="ellipsis", max_width=40)
                for r in range(nrows):
                    row = []
                    for c in range(ncols):
                        k = c * nrows + r
                        if k < len(shown):
                            row.append(Text.assemble((f"{subs.index(shown[k]) + 1:>3} ", "cyan"), shown[k]))
                        else:
                            row.append(Text(""))
                    grid.add_row(*row)
                console.print(grid)
            else:
                console.print("  [dim](aucun sous-dossier)[/]" if not flt else "  [dim](rien ne correspond)[/]")
            console.print()
            console.print("  [dim]numéro = entrer · texte = filtrer · [bold]..[/] = dossier parent · "
                          "[bold]Entrée[/] = ouvrir ce dossier · [bold]q[/] = annuler[/]")
            r = ask("[bold magenta]›[/] ").strip()
            if r.lower() == "q":
                return
            if r == "":
                if flt:
                    flt = ""
                    continue
                if cur != self.folder:
                    self.open_folder(cur)
                return
            if r == "..":
                cur, flt = os.path.dirname(cur.rstrip(os.sep)) or os.sep, ""
                continue
            if r.isdigit() and 1 <= int(r) <= len(subs):
                nxt = os.path.join(cur, subs[int(r) - 1])
            elif len(shown) == 1 and flt == "" and r and r.lower() in shown[0].lower():
                nxt = os.path.join(cur, shown[0])
            else:
                match = [d for d in subs if r.lower() in d.lower()]
                if len(match) == 1:
                    nxt = os.path.join(cur, match[0])
                else:
                    flt = r
                    continue
            cur, flt = nxt, ""
            try:
                has_sub = any(e.is_dir() and not e.name.startswith(".") and e.name not in SYSTEM_DIR_NAMES
                              for e in os.scandir(cur))
            except OSError:
                has_sub = False
            if not has_sub:                  # un album : on l'ouvre directement
                self.open_folder(cur)
                return

    def change_folder(self) -> None:
        if readline is not None:
            readline.set_completer_delims("\t\n")
            readline.parse_and_bind("tab: complete")
            readline.set_completer(_path_completer)
        try:
            p = ask("  [bold]Dossier[/] [dim](Tab pour compléter)[/] [magenta]›[/] ",
                    prefill=self.folder + os.sep).strip()
        finally:
            if readline is not None:
                readline.set_completer(None)
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isdir(p):
            info(f"Dossier introuvable : {escape(p)}", "red")
            pause()
            return
        self.open_folder(p)

    def set_default_root(self) -> None:
        global MUSIC_ROOT
        if not confirm(f"Ouvrir désormais [bold]{escape(self.folder)}[/] au démarrage de musitag ?", True):
            return
        try:
            path = save_user_config("MUSIC_ROOT", self.folder)
        except OSError as e:
            info(f"Impossible d'enregistrer le réglage : {escape(str(e))}", "red")
            pause()
            return
        MUSIC_ROOT = self.folder
        self.notice = ""
        info(f"C'est noté : musitag s'ouvrira dans [bold]{escape(self.folder)}[/].\n"
             f"[dim]Réglage enregistré dans {escape(path)}[/]", "green")
        pause()

    def run(self) -> None:
        while True:
            self.screen()
            try:
                key = menu("Menu", [
                    ("1", "Afficher les métadonnées", "tableau de tous les fichiers"),
                    ("2", "Voir un fichier en détail", "tous les champs + infos techniques"),
                    ("3", "Modifier un fichier", "champ par champ, renommage, pochette"),
                    ("4", "Modifier tous les fichiers", "champs communs, paroles, outils en lot"),
                    ("5", "Découper un CUE", "image CD + .cue → pistes FLAC taguées"),
                    ("6", "Nettoyer le dossier", "supprimer cover.jpg, .cue, .m3u, scans… (tout sauf la musique)"),
                    None,
                    ("o", "Ouvrir un dossier", "naviguer : Musiques → Artiste → Album"),
                    ("d", "Taper un chemin", ""),
                    ("p", "Définir comme dossier musique", "musitag s'ouvrira ici au démarrage"),
                    ("r", "Mode récursif : " + ("oui → non" if self.recursive else "non → oui"), ""),
                    ("0", "Quitter", ""),
                ])
            except (KeyboardInterrupt, Cancel):
                key = "0"
            if key == "0":
                console.print("[magenta]♫[/] À plus !")
                return
            try:
                if key == "1":
                    self.view_all()
                elif key in ("2", "3"):
                    idx = pick_file(self)
                    if idx is not None:
                        file_screen(self, idx, edit=(key == "3"))
                elif key == "4":
                    self.batch()
                elif key == "5":
                    cue_flow(self)
                elif key == "6":
                    clean_flow(self)
                elif key == "o":
                    self.browse()
                elif key == "p":
                    self.set_default_root()
                elif key == "d":
                    self.change_folder()
                elif key == "r":
                    self.recursive = not self.recursive
                    self.rescan()
            except (KeyboardInterrupt, Cancel):
                console.print("\n[yellow]Annulé.[/]")
                time.sleep(0.6)


def _path_completer(text: str, state: int):
    matches = sorted(glob.glob(os.path.expanduser(text) + "*"))
    matches = [m + (os.sep if os.path.isdir(m) else "") for m in matches]
    return matches[state] if state < len(matches) else None


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="musitag",
        description="Éditeur interactif de métadonnées audio (FLAC, MP3, WAV, OGG, Opus, M4A, "
                    "WavPack, APE…) et découpage d'images CD avec .cue.",
        epilog=f"exemples :\n  musitag              {MUSIC_ROOT} (ou l'album où tu te trouves)\n"
               "  musitag .            le dossier courant, quel qu'il soit\n"
               "  musitag ~/Musique    un dossier précis\n"
               "  musitag -r Pixies    avec les sous-dossiers",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dossier", nargs="?", default=None,
                    help=f"dossier à ouvrir (défaut : {MUSIC_ROOT})")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("-r", "--recursif", action="store_true", help="inclure les sous-dossiers")
    g.add_argument("-n", "--non-recursif", action="store_true", help="ce dossier uniquement (défaut)")
    ap.add_argument("--version", action="version", version=f"musitag {VERSION}")
    args = ap.parse_args()

    notice = ""
    if args.dossier is not None:
        folder = os.path.abspath(os.path.expanduser(args.dossier))
        if not os.path.isdir(folder):
            console.print(f"[red]Dossier introuvable :[/] {escape(folder)}")
            sys.exit(1)
    elif os.path.isdir(MUSIC_ROOT):
        root, cwd = os.path.realpath(MUSIC_ROOT), os.path.realpath(os.getcwd())
        # lancé depuis un album de la bibliothèque : on ouvre cet album
        folder = os.getcwd() if cwd == root or cwd.startswith(root + os.sep) else MUSIC_ROOT
    else:
        folder = os.path.expanduser("~")
        notice = (f"Dossier musique non trouvé ({MUSIC_ROOT}) : ouverture de ton dossier personnel. "
                  "Va dans ta musique (o ou d), puis p pour la définir par défaut.")
    try:
        app = App(folder, args.recursif, notice)
        app.rescan()
        app.offer_sudo()
        app.run()
    except (KeyboardInterrupt, Cancel):
        console.print("\n[magenta]♫[/] À plus !")


if __name__ == "__main__":
    main()
