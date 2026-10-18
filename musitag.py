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

VERSION = "1.6.1"


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


LRCLIB_TIMEOUT = 15               # secondes par requête
BREAKER_FAILS = 4                 # échecs d'affilée (saturation, panne) avant de mettre LRCLIB en pause
BREAKER_COOLDOWN = 60.0           # durée de la pause ; doublée à chaque rechute
BREAKER_MAX = 600.0


class LrclibDeferred(LrclibError):
    """LRCLIB est en pause (il saturait) : la recherche est reportée sans être tentée."""


_BRK_LOCK = threading.Lock()
_brk = {"fails": 0, "until": 0.0, "cooldown": BREAKER_COOLDOWN, "why": ""}


def lrclib_breaker_reset() -> None:
    with _BRK_LOCK:
        _brk.update(fails=0, until=0.0, cooldown=BREAKER_COOLDOWN, why="")


def lrclib_paused() -> float:
    """Secondes avant la fin de la pause (0 si LRCLIB est interrogeable)."""
    with _BRK_LOCK:
        return max(0.0, _brk["until"] - time.monotonic())


def _breaker_check() -> None:
    with _BRK_LOCK:
        left, why = _brk["until"] - time.monotonic(), _brk["why"]
    if left > 0:
        raise LrclibDeferred(f"reportée : LRCLIB ne répond plus ({why}), pause de {left:.0f} s")


def _breaker_failure(why: str) -> None:
    """Un échec de plus. Au-delà de BREAKER_FAILS d'affilée : plus aucune requête pendant un moment
    (insister charge un serveur déjà saturé, et fait attendre pour rien)."""
    with _BRK_LOCK:
        _brk["fails"] += 1
        _brk["why"] = why
        if _brk["fails"] >= BREAKER_FAILS:
            _brk["until"] = time.monotonic() + _brk["cooldown"]
            _brk["cooldown"] = min(_brk["cooldown"] * 2, BREAKER_MAX)
            _brk["fails"] = BREAKER_FAILS - 1        # à la reprise, un seul échec suffit à re-déclencher


def _breaker_success() -> None:
    with _BRK_LOCK:
        _brk["fails"] = 0
        _brk["cooldown"] = BREAKER_COOLDOWN


def lrclib_request(path: str, params: Dict[str, str]):
    """GET sur l'API LRCLIB. Renvoie None si 404 (rien trouvé).
    Un seul réessai, court, sur les erreurs de saturation ou de réseau ; si LRCLIB enchaîne les
    échecs, il est mis en pause (LrclibDeferred) au lieu d'être relancé sans fin."""
    url = f"{LRCLIB_URL}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last = "erreur inconnue"
    for attempt in range(2):
        _breaker_check()
        _throttle()
        try:
            with url_open(req, timeout=LRCLIB_TIMEOUT) as r:
                data = json.loads(read_limited(r, MAX_JSON_BYTES).decode("utf-8"))
            _speed_up()
            _breaker_success()
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _speed_up()
                _breaker_success()
                return None
            last = _http_error_message(e.code)
            if e.code not in (429, 500, 502, 503, 504):
                raise LrclibError(last)                  # requête refusée : réessayer ne changera rien
            pause = 3.0
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra and ra.strip().isdigit():
                pause = float(min(max(int(ra), 1), 10))
            _breaker_failure(last)
            _slow_down(pause)                            # l'attente se fait dans _throttle()
        except ValueError:
            raise LrclibError("réponse illisible de LRCLIB")
        except OSError as e:                             # URLError, délai dépassé, connexion coupée…
            last = "LRCLIB injoignable (" + (str(getattr(e, "reason", "") or e) or type(e).__name__) + ")"
            _breaker_failure(last)
            if attempt == 0:
                time.sleep(2)
    raise LrclibError(last)


# ─── « introuvable » déjà vérifié : on ne repose pas la question pendant NOTFOUND_TTL_DAYS ───

NOTFOUND_TTL_DAYS = 30
NOTFOUND_FILE = os.path.join(_real_home(), ".cache", "musitag", "lrclib-introuvables.json")
_NF_LOCK = threading.Lock()
_nf: Optional[Dict[str, float]] = None
_nf_dirty = False


def _nf_key(title: str, artist: str, dur: float) -> str:
    return f"{_norm(artist)}|{_norm(title)}|{int(round(dur or 0))}"


def _nf_load() -> Dict[str, float]:
    global _nf
    if _nf is None:
        try:
            with open(NOTFOUND_FILE, encoding="utf-8") as fh:
                raw = json.load(fh)
            _nf = {str(k): float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            _nf = {}
    return _nf


def notfound_checked(key: str) -> Optional[float]:
    """Date (epoch) de la dernière vérification « introuvable », si elle est récente."""
    with _NF_LOCK:
        ts = _nf_load().get(key)
    if ts and time.time() - ts < NOTFOUND_TTL_DAYS * 86400:
        return ts
    return None


def notfound_remember(key: str) -> None:
    global _nf_dirty
    with _NF_LOCK:
        _nf_load()[key] = time.time()
        _nf_dirty = True


def notfound_save() -> None:
    global _nf_dirty
    with _NF_LOCK:
        if not _nf_dirty or _nf is None:
            return
        limit = time.time() - NOTFOUND_TTL_DAYS * 86400
        keep = dict(sorted(((k, v) for k, v in _nf.items() if v >= limit), key=lambda kv: -kv[1])[:50000])
        try:
            os.makedirs(os.path.dirname(NOTFOUND_FILE), exist_ok=True)
            tmp = NOTFOUND_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(keep, fh, ensure_ascii=False)
            os.replace(tmp, NOTFOUND_FILE)
            _nf_dirty = False
        except OSError:
            return
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:            # lancé via sudo : le cache reste à l'utilisateur
        for p in (os.path.dirname(NOTFOUND_FILE), NOTFOUND_FILE):
            try:
                os.chown(p, int(uid), int(gid))
            except OSError:
                pass


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


def _lookup_who(af, title: Optional[str], artist: Optional[str]):
    title = (af.first("title") if title is None else title).strip()
    artists = [artist] if artist else af.get("artist") + af.get("albumartist")
    artists = [a.strip() for a in dict.fromkeys(artists) if a and a.strip()]
    return title, artists


def lookup_exact(af, title: Optional[str] = None, artist: Optional[str] = None) -> LyricsResult:
    """1er temps : la correspondance exacte (titre + artiste + durée à ±2 s), une seule requête.
    Renvoie un résultat définitif, ou « pending » (match = version « texte seul » éventuelle)
    s'il faut chercher plus large."""
    manual = title is not None or artist is not None
    title, artists = _lookup_who(af, title, artist)
    dur = af.length()
    if not title or not artists:
        return LyricsResult(af, "skip", message="titre ou artiste manquant")
    if not manual:
        ts = notfound_checked(_nf_key(title, artists[0], dur))
        if ts:
            return LyricsResult(af, "notfound", message=time.strftime("introuvable, déjà vérifié le %d/%m",
                                                                     time.localtime(ts)))
    try:
        rec = None
        if dur:
            rec = lrclib_request("get", {"track_name": title, "artist_name": artists[0],
                                         "duration": str(round(dur))})
        if isinstance(rec, dict):
            m = _evaluate(rec, title, artists, dur, exact=True)
            if rec.get("instrumental"):
                return LyricsResult(af, "instrumental", m, [m])
            if m.kind == "synced":
                return LyricsResult(af, "exact", m, [m])
            if m.kind:
                return LyricsResult(af, "pending", m)        # texte seul : on cherche une version synchronisée
        return LyricsResult(af, "pending")
    except LrclibError as e:
        return LyricsResult(af, "error", message=str(e))
    except Exception as e:
        return LyricsResult(af, "error", message=str(e) or type(e).__name__)


def album_pool(artist: str, album: str) -> list:
    """Une recherche pour tout un album (jusqu'à 20 résultats), partagée entre ses morceaux ;
    sans l'artiste en secours (artiste crédité autrement : « SIDxRAM » pour « RAM »…)."""
    alb = base_title(album) or album
    for q in (f"{artist} {alb}", alb if len(alb) >= 4 else ""):
        if not q:
            continue
        try:
            res = lrclib_request("search", {"q": q}) or []
        except LrclibError:
            return []
        res = [r for r in res if isinstance(r, dict)] if isinstance(res, list) else []
        if res:
            return res
    return []


def lookup_wide(af, exact_m: Optional[LyricsMatch] = None, pool: Optional[list] = None,
                title: Optional[str] = None, artist: Optional[str] = None) -> LyricsResult:
    """2e temps : d'abord parmi les résultats de l'album (pool, sans requête), puis une recherche
    pour ce morceau. Sans album (pool None), une dernière sans l'artiste si LRCLIB n'a rien renvoyé :
    avec un album, la recherche de secours sans artiste a déjà été faite une fois pour tous."""
    manual = title is not None or artist is not None
    strict = not manual
    title, artists = _lookup_who(af, title, artist)
    dur = af.length()
    main, bt = artists[0], base_title(title)
    seen = {exact_m.rec.get("id")} if exact_m else set()
    found: List[LyricsMatch] = []
    instrumental = [None]

    def consider(records) -> None:
        for r in records if isinstance(records, list) else []:
            if not isinstance(r, dict) or r.get("id") in seen:
                continue
            seen.add(r.get("id"))
            m = _evaluate(r, title, artists, dur)
            if not _acceptable(m, strict):
                continue
            if r.get("instrumental") or not m.kind:
                if r.get("instrumental") and m.diff is not None and m.diff <= 3:
                    instrumental[0] = instrumental[0] or m
                continue
            found.append(m)

    def synced_twin() -> Optional[LyricsMatch]:
        """Le même morceau, même durée, en version synchronisée (le « get » n'avait que du texte)."""
        for m in sorted(found, key=lambda m: -m.score):
            if (m.kind == "synced" and m.diff is not None and m.diff <= 2
                    and m.title_sim >= 0.95 and m.artist_sim >= 0.8):
                return m
        return None

    try:
        if pool:
            consider(pool)
        if not found or (exact_m and not synced_twin()):
            res = lrclib_request("search", {"track_name": bt, "artist_name": main}) or []
            consider(res)
            if not res and not found and not exact_m and pool is None:
                consider(lrclib_request("search", {"track_name": bt}) or [])   # artiste crédité autrement
    except LrclibError as e:
        if exact_m:                     # on a déjà une version texte exacte : on s'en contente
            return LyricsResult(af, "exact", exact_m, [exact_m])
        return LyricsResult(af, "error", message=str(e))
    except Exception as e:
        return LyricsResult(af, "error", message=str(e) or type(e).__name__)
    found.sort(key=lambda m: -m.score)
    if exact_m:
        twin = synced_twin()
        if twin:
            twin.exact = True
            return LyricsResult(af, "exact", twin, [twin, exact_m] + [x for x in found if x is not twin][:6])
        return LyricsResult(af, "exact", exact_m, [exact_m] + found[:7])
    if found:
        return LyricsResult(af, "approx", found[0], found[:8])
    if instrumental[0]:
        return LyricsResult(af, "instrumental", instrumental[0], [instrumental[0]])
    if not manual:
        notfound_remember(_nf_key(title, main, dur))
    return LyricsResult(af, "notfound")


def lrclib_lookup(af, title: Optional[str] = None, artist: Optional[str] = None) -> LyricsResult:
    """Paroles d'un seul morceau : correspondance exacte, puis recherche élargie.
    Avec un titre/artiste tapé à la main, l'écart de durée n'élimine plus rien
    (il reste affiché pour que tu juges), et le cache des « introuvable » est ignoré."""
    r = lookup_exact(af, title, artist)
    if r.status == "pending":
        r = lookup_wide(af, r.match, None, title, artist)
    notfound_save()
    return r


def lyrics_search(files, pmap, on_result) -> None:
    """Paroles de plusieurs morceaux, en ménageant LRCLIB :
      1. la correspondance exacte de chaque morceau (1 requête chacun) ;
      2. pour ceux qui restent, UNE recherche par album, partagée entre ses morceaux ;
      3. en dernier recours, une recherche pour le morceau.
    pmap(fn, éléments, rappel) exécute fn en parallèle et appelle rappel(j, résultat) au fil de
    l'eau ; on_result(i, LyricsResult) reçoit chaque résultat définitif."""
    def as_result(i: int, r) -> LyricsResult:
        if isinstance(r, LyricsResult):
            return r
        return LyricsResult(files[i], "error", message=str(r) or type(r).__name__)

    pending: List[Tuple[int, LyricsResult]] = []
    try:
        def first(j: int, r) -> None:
            r = as_result(j, r)
            if r.status == "pending":
                pending.append((j, r))
            else:
                on_result(j, r)

        pmap(lookup_exact, list(files), first)
        if not pending:
            return
        groups: Dict[Tuple[str, str, str], List[int]] = {}
        for k, (i, _) in enumerate(pending):
            af = files[i]
            artist = af.first("albumartist") or af.first("artist")
            album = af.first("album")
            if artist and album:
                groups.setdefault((af.dir, artist, album), []).append(k)
        pools: Dict[int, list] = {}
        keys = list(groups)

        def got_pool(j: int, res) -> None:
            for k in groups[keys[j]]:
                pools[k] = res if isinstance(res, list) else []

        if keys:
            pmap(lambda key: album_pool(key[1], key[2]), keys, got_pool)
        jobs = [(files[i], r.match, pools.get(k)) for k, (i, r) in enumerate(pending)]

        def second(j: int, r) -> None:
            on_result(pending[j][0], as_result(pending[j][0], r))

        pmap(lambda job: lookup_wide(*job), jobs, second)
    finally:
        notfound_save()


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

    def pmap(fn, items, callback) -> None:
        with ThreadPoolExecutor(max_workers=LRCLIB_WORKERS) as ex:
            futs = {ex.submit(fn, it): j for j, it in enumerate(items)}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception as e:
                    r = e
                callback(futs[fut], r)

    with prog:
        task = prog.add_task("Recherche sur LRCLIB", total=len(files))

        def done(i: int, r: LyricsResult) -> None:
            results[i] = r
            prog.advance(task)

        lyrics_search(files, pmap, done)
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


def ops_table(ops: List[Op], limit: int = 60) -> Table:
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
    return t


def preview(ops: List[Op], limit: int = 60) -> None:
    console.print(ops_table(ops, limit))
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
    elif r.status in ("skip", "error") or (r.status == "notfound" and r.message):
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
        t.append(f"  ·  {c['error']} en erreur ou reportée(s)", style="bold red")
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
        k = menu(f"{len(err_idx)} recherche(s) en erreur ou reportée(s)", [
            ("1", "Relancer ces recherches", "LRCLIB saturait : musitag attend la fin de sa pause, puis réessaie"),
            ("2", "Continuer sans elles", "ces fichiers ne seront pas modifiés"),
            ("0", "Annuler", "rien n'est écrit"),
        ])
        if k == "0":
            return
        if k == "2":
            break
        try:
            with console.status("[magenta]Petite pause pour laisser souffler LRCLIB…[/]", spinner="dots") as st:
                time.sleep(3)
                while lrclib_paused() > 0:
                    st.update(f"[magenta]LRCLIB saturait : nouvel essai dans {lrclib_paused():.0f} s…[/] "
                              "[dim](Ctrl+C pour abandonner)[/]")
                    time.sleep(0.5)
        except KeyboardInterrupt:
            continue
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


def guess_disc(cs: CueSheet, cues: List[str]) -> None:
    """N° de disque deviné depuis le nom (« CD2 », « Disc 2 »…) et nombre de .cue du dossier."""
    if not cs.disc:
        m = re.search(r"(?:cd|disc|disk|disque)\s*[_.-]?\s*(\d{1,2})",
                      os.path.basename(cs.path) + " " + " ".join(t.file for t in cs.tracks), re.I)
        if m:
            cs.disc = str(int(m.group(1)))
    same_dir = [c for c in cues if os.path.dirname(c) == os.path.dirname(cs.path)]
    if cs.disc and not cs.totaldiscs and len(same_dir) > 1:
        cs.totaldiscs = str(len(same_dir))


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
    guess_disc(cs, cues)

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


def move_originals(cs: CueSheet, srcs: List[str], mode: str) -> Optional[str]:
    """Image CD d'origine + .cue : « ranger » (dans _source/, ignoré), « supprimer » ou « laisser ».
    Renvoie None si tout va bien, sinon l'erreur."""
    files = srcs + [cs.path]
    try:
        if mode == "ranger":
            dest = os.path.join(os.path.dirname(cs.path), "_source")
            os.makedirs(dest, exist_ok=True)
            open(os.path.join(dest, ".ignore"), "a").close()
            for f in files:
                shutil.move(f, os.path.join(dest, os.path.basename(f)))
        elif mode == "supprimer":
            for f in files:
                os.remove(f)
    except PermissionError:
        return "permission refusée — relance musitag avec sudo"
    except OSError as e:
        return str(e)
    return None


def handle_originals(cs: CueSheet, srcs: List[str]) -> None:
    key = menu("Que faire de l'image CD d'origine ?", [
        ("1", "La ranger dans « _source »", "sous-dossier ignoré par Jellyfin et musitag (.ignore)"),
        ("2", "La supprimer", "le .cue et " + ", ".join(os.path.basename(s) for s in srcs)),
        ("3", "La laisser en place", "Jellyfin verra l'album en double"),
    ])
    mode = {"1": "ranger", "2": "supprimer"}.get(key, "laisser")
    if mode == "supprimer" and not confirm("Supprimer définitivement ces fichiers ?", False):
        mode = "laisser"
    err = move_originals(cs, srcs, mode)
    if err:
        console.print(f"[red]Erreur : {escape(err)}[/]")
    elif mode == "ranger":
        console.print("[green]✓ Rangé dans « _source/ ».[/]")
    elif mode == "supprimer":
        console.print("[green]✓ Supprimé.[/]")
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


def delete_items(items: List[CleanItem], progress: bool = True):
    ok, errors = 0, []
    prog = make_progress() if progress and len(items) > 8 and console.is_terminal else _NullProgress()
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


# ═════════════════════════════════════ Scripts ════════════════════════════════════
# Un script (.mst) enchaîne des actions sur un album, sans intervention. Rien n'est écrit
# pendant qu'il tourne : chaque étape PRÉPARE ses changements sur un « brouillon » de chaque
# morceau (Draft) ; à la fin, un bilan et UNE confirmation, puis l'écriture.
# Pendant qu'il tourne : s = sauter l'étape en cours, q = arrêter (bilan de ce qui est prêt),
# v = verbeux oui / non.

SCRIPT_EXT = ".mst"
SCRIPTS_DIR = os.path.join(os.path.dirname(USER_CONFIG), "scripts")
BIG_RUN = 10                # à partir de ce nombre d'albums : avertissement avant de lancer
ONLINE_PICKS = 5            # sources « en ligne, 1er … 5e résultat »


class ScriptError(Exception):
    """Script introuvable ou illisible (message prêt à afficher)."""


class StepSkipped(Exception):
    """Touche s (ou q) pendant une étape."""


class StepFailed(Exception):
    """L'étape n'a rien pu faire (message prêt à afficher)."""


@dataclass
class OptSpec:
    key: str
    label: str
    choices: Optional[List[Tuple[str, str]]] = None     # (valeur, libellé) ; None = texte libre
    default: str = ""
    multi: bool = False                                 # liste ordonnée, séparée par des virgules


@dataclass
class ActionSpec:
    key: str
    label: str
    hint: str
    group: str
    opts: List[OptSpec] = dc_field(default_factory=list)
    album_only: bool = False      # n'a pas de sens sur un morceau seul (nettoyage, découpage…)
    can_fail: bool = False        # peut « échouer » : on règle alors quoi faire ensuite
    free_keys: bool = False       # [champs] : chaque clé est un nom de champ


FAIL_CHOICES = [("continuer", "passer à l'étape suivante"),
                ("arreter", "arrêter le script pour cet album")]
YES_NO = [("oui", "oui"), ("non", "non")]
EXISTING = [("remplacer", "les remplacer"), ("garder", "les garder")]
COVER_SOURCES = ([("garder", "la pochette déjà intégrée"), ("dossier", "l'image du dossier (cover.jpg…)")]
                 + [(f"en_ligne_{i}", f"en ligne, {i}{'er' if i == 1 else 'e'} résultat")
                    for i in range(1, ONLINE_PICKS + 1)])
CLEAN_TYPES = [("image", "Image"), ("cue", "CUE"), ("playlist", "Playlist"), ("lrc", "Paroles .lrc"),
               ("texte", "Texte / log"), ("controle", "Contrôle"), ("video", "Vidéo"),
               ("archive", "Archive"), ("temporaire", "Temporaire"), ("systeme", "Système"),
               ("dossier", "Dossier"), ("autre", "Autre")]
CLEAN_TYPE_KIND = dict(CLEAN_TYPES)
RENAME_PRESETS = [("{n} - {title}", "01 - Titre"), ("{n}. {title}", "01. Titre"),
                  ("{disc}-{n} - {title}", "1-01 - Titre (multi-disques)"),
                  ("{n} - {artist} - {title}", "01 - Artiste - Titre")]
# noms usuels → clés réelles selon le format (Vorbis, ID3, MP4, APE, WMA)
FIELD_ALIASES = {
    "encodeur": ("ENCODER", "ENCODEDBY", "ENCODED-BY", "ENCODED_BY", "ENCODERSETTINGS",
                 "ENCODER_SETTINGS", "TSSE", "TENC", "©too", "WM/EncodedBy", "WM/EncodingSettings"),
}

ACTIONS = [
    ActionSpec("paroles", "Chercher les paroles", "LRCLIB, synchronisées si possible", "Paroles", [
        OptSpec("accepter", "Accepter", [("exactes", "les correspondances exactes seulement"),
                                         ("premieres", "la première trouvée, même approchante")], "exactes"),
        OptSpec("existantes", "Paroles déjà présentes", EXISTING, "remplacer"),
        OptSpec("instrumental", "Morceaux instrumentaux",
                [("oui", f"écrire « {INSTRUMENTAL_TEXT} »"), ("non", "ne rien écrire")], "oui"),
    ], can_fail=True),
    ActionSpec("instrumental", "Marquer comme instrumental", f"« {INSTRUMENTAL_TEXT} »", "Paroles", [
        OptSpec("existantes", "Paroles déjà présentes", EXISTING, "remplacer"),
    ]),
    ActionSpec("sans_paroles", "Supprimer les paroles", "", "Paroles"),
    ActionSpec("pochette", "Intégrer une pochette", "sources par ordre de priorité", "Pochette", [
        OptSpec("sources", "Sources, par ordre de priorité", COVER_SOURCES, "dossier, en_ligne_1", multi=True),
    ], can_fail=True),
    ActionSpec("sans_pochette", "Supprimer les pochettes intégrées", "", "Pochette"),
    ActionSpec("champs", "Fixer des champs", "ex. genre = Rock", "Tags", free_keys=True),
    ActionSpec("supprimer_champs", "Supprimer des champs", "ex. commentaire, encodeur", "Tags", [
        OptSpec("champs", "Champs à supprimer", None, "comment, encodeur"),
    ]),
    ActionSpec("numeroter", "Numéroter les pistes", "d'après les noms ou dans l'ordre", "Tags", [
        OptSpec("mode", "Numéros", [("noms", "d'après le début du nom (« 01 - … »)"),
                                    ("ordre", "dans l'ordre des fichiers")], "noms"),
        OptSpec("total", "Total de pistes", [("oui", "l'écrire aussi"), ("non", "ne pas l'écrire")], "oui"),
    ], album_only=True, can_fail=True),
    ActionSpec("titres", "Titres d'après les noms de fichiers", "« 01 - Loser.flac » → Loser", "Tags"),
    ActionSpec("remplacer", "Rechercher / remplacer", "ex. retirer « (2016 Remaster) »", "Tags", [
        OptSpec("champ", "Dans le champ", [(f, LABEL[f]) for f in TEXT_FIELDS] + [("tous", "tous les champs texte")],
                "title"),
        OptSpec("chercher", "Rechercher", None, ""),
        OptSpec("par", "Remplacer par", None, ""),
        OptSpec("regex", "Expression régulière", [("non", "non, texte simple"), ("oui", "oui")], "non"),
    ]),
    ActionSpec("effacer_tags", "Effacer tous les tags", "tags et pochettes", "Tags"),
    ActionSpec("renommer", "Renommer les fichiers", "d'après les tags", "Fichiers", [
        OptSpec("modele", "Modèle", None, "{n} - {title}"),
    ], can_fail=True),
    ActionSpec("nettoyer", "Nettoyer le dossier", "supprimer tout sauf la musique", "Fichiers", [
        OptSpec("types", "Types à supprimer", [("tout", "tout")] + CLEAN_TYPES, "tout", multi=True),
    ], album_only=True),
    ActionSpec("cue", "Découper l'image CD (.cue)", "pistes FLAC au sample près", "Fichiers", [
        OptSpec("original", "Image d'origine", [("ranger", "la ranger dans « _source »"),
                                                ("supprimer", "la supprimer"),
                                                ("laisser", "la laisser en place")], "ranger"),
    ], album_only=True, can_fail=True),
]
ACTION = {a.key: a for a in ACTIONS}
SCRIPT_FIELDS = [f for f, _ in FIELDS]


def _choice_label(o: OptSpec, value: str) -> str:
    return dict(o.choices or []).get(value, value)


@dataclass
class Step:
    action: str
    opts: Dict[str, str] = dc_field(default_factory=dict)
    on_fail: str = "continuer"

    @property
    def spec(self) -> ActionSpec:
        return ACTION[self.action]

    def opt(self, key: str) -> str:
        if key in self.opts:
            return self.opts[key]
        return next((o.default for o in self.spec.opts if o.key == key), "")

    def describe(self) -> str:
        a = self.action
        if a == "champs":
            return " · ".join(f"{LABEL.get(f, f)} = {v or '(vide)'}" for f, v in self.opts.items())
        if a == "pochette":
            return " → ".join(_choice_label(self.spec.opts[0], s) for s in _split_list(self.opt("sources")))
        if a == "remplacer":
            where = _choice_label(self.spec.opts[0], self.opt("champ"))
            by = f"« {self.opt('par')} »" if self.opt("par") else "(rien : supprimé)"
            return (f"« {self.opt('chercher')} » → {by} dans {where.lower()}"
                    + (" (expression régulière)" if self.opt("regex") == "oui" else ""))
        if a == "nettoyer":
            types = _split_list(self.opt("types"))
            return "tout" if "tout" in types else ", ".join(CLEAN_TYPE_KIND.get(t, t) for t in types)
        if a in ("renommer", "supprimer_champs"):
            return self.opt(self.spec.opts[0].key)
        return " · ".join(f"{o.label.lower()} : {_choice_label(o, self.opt(o.key))}" for o in self.spec.opts)


@dataclass
class Script:
    name: str
    steps: List[Step] = dc_field(default_factory=list)
    verbose: bool = False
    path: str = ""

    def summary(self) -> str:
        return " → ".join(st.spec.label for st in self.steps) or "(aucune étape)"


def _split_list(v: str) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _unquote_value(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1]
    return v


def _quote_value(v: str) -> str:
    return f'"{v}"' if v != v.strip() or not v or (v[0] == v[-1] == '"') else v


def _check_option(o: OptSpec, v: str, where: str) -> str:
    if o.choices is None:
        return v
    allowed = [c for c, _ in o.choices]
    if not o.multi:
        v = v.lower()
        if v not in allowed:
            raise ScriptError(f"{where} : « {v} » n'est pas possible pour {o.key} (possibles : {', '.join(allowed)})")
        return v
    vals = []
    for x in _split_list(v.lower()):
        if x not in allowed:
            raise ScriptError(f"{where} : « {x} » n'est pas possible pour {o.key} (possibles : {', '.join(allowed)})")
        if x not in vals:
            vals.append(x)
    if not vals:
        raise ScriptError(f"{where} : {o.key} est vide")
    if "tout" in vals:
        vals = ["tout"]
    return ", ".join(vals)


def validate_step(st: Step, where: str = "") -> None:
    where = where or f"étape « {st.spec.label} »"
    if st.action == "champs" and not st.opts:
        raise ScriptError(f"{where} : aucun champ à fixer")
    if st.action == "supprimer_champs" and not _split_list(st.opt("champs")):
        raise ScriptError(f"{where} : aucun champ à supprimer")
    if st.action == "renommer" and not st.opt("modele").strip():
        raise ScriptError(f"{where} : modèle vide")
    if st.action == "remplacer":
        if not st.opt("chercher"):
            raise ScriptError(f"{where} : rien à rechercher")
        if st.opt("regex") == "oui":
            try:
                re.compile(st.opt("chercher"))
            except re.error as e:
                raise ScriptError(f"{where} : expression régulière invalide ({e})")


def parse_script(text: str, name: str = "", path: str = "") -> Script:
    sc = Script(name, path=path)
    cur: Optional[Step] = None
    start = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith(("#", ";")):
            continue
        where = f"ligne {n}"
        if line.startswith("[") and line.endswith("]"):
            if cur is not None:
                validate_step(cur, f"ligne {start}")
            key = line[1:-1].strip().lower()
            if key not in ACTION:
                raise ScriptError(f"{where} : action inconnue « {key} » (possibles : {', '.join(ACTION)})")
            cur, start = Step(key), n
            sc.steps.append(cur)
            continue
        if "=" not in line:
            raise ScriptError(f"{where} : « clé = valeur » attendu")
        k, v = line.split("=", 1)
        k, v = k.strip().lower(), _unquote_value(v)
        if cur is None:
            if k == "verbeux":
                sc.verbose = _check_option(OptSpec("verbeux", "", YES_NO), v, where) == "oui"
            elif k not in ("nom", "description"):
                raise ScriptError(f"{where} : réglage inconnu « {k} » (avant la 1re étape : verbeux)")
            continue
        if k == "si_echec":
            cur.on_fail = _check_option(OptSpec("si_echec", "", FAIL_CHOICES), v, where)
            continue
        if cur.spec.free_keys:
            if k not in SCRIPT_FIELDS:
                raise ScriptError(f"{where} : champ inconnu « {k} » (possibles : {', '.join(SCRIPT_FIELDS)})")
            cur.opts[k] = v
            continue
        o = next((o for o in cur.spec.opts if o.key == k), None)
        if o is None:
            known = ", ".join([o.key for o in cur.spec.opts] + ["si_echec"])
            raise ScriptError(f"{where} : option inconnue « {k} » pour [{cur.action}] (possibles : {known})")
        cur.opts[k] = _check_option(o, v, where)
    if cur is not None:
        validate_step(cur, f"ligne {start}")
    return sc


def dump_script(sc: Script) -> str:
    out = ["# Script musitag — se modifie dans musitag (touche s), ou ici à la main.",
           f"# Lancement, depuis le dossier d'un album : musitag -s {sc.name}",
           "", f"verbeux = {'oui' if sc.verbose else 'non'}"]
    for i, st in enumerate(sc.steps, 1):
        out += ["", f"# {i}. {st.spec.label}", f"[{st.action}]"]
        if st.spec.free_keys:
            out += [f"{k} = {_quote_value(v)}" for k, v in st.opts.items()]
        else:
            out += [f"{o.key} = {_quote_value(st.opt(o.key))}" for o in st.spec.opts]
        if st.spec.can_fail:
            out.append(f"si_echec = {st.on_fail}")
    return "\n".join(out) + "\n"


def clean_script_name(s: str) -> str:
    s = os.path.basename(s.strip())
    if s.lower().endswith(SCRIPT_EXT):
        s = s[:-len(SCRIPT_EXT)]
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\w.-]", "", s).strip(".-")
    return s[:60]


def list_scripts() -> List[str]:
    try:
        names = os.listdir(SCRIPTS_DIR)
    except OSError:
        return []
    return sorted((n[:-len(SCRIPT_EXT)] for n in names if n.endswith(SCRIPT_EXT) and not n.startswith(".")),
                  key=natural_key)


def find_script(arg: str) -> str:
    """Nom d'un script enregistré (avec ou sans .mst), ou chemin vers un fichier .mst."""
    p = os.path.expanduser(arg)
    if os.sep in arg or os.path.isfile(p):
        if os.path.isfile(p):
            return os.path.abspath(p)
        raise ScriptError(f"Fichier introuvable : {p}")
    name = clean_script_name(arg)
    path = os.path.join(SCRIPTS_DIR, name + SCRIPT_EXT)
    if os.path.isfile(path):
        return path
    known = list_scripts()
    raise ScriptError(f"Script « {arg} » introuvable dans {SCRIPTS_DIR}."
                      + (f"\nScripts disponibles : {', '.join(known)}" if known else
                         "\nAucun script pour l'instant : crée-en un dans musitag (touche s)."))


def load_script(path: str) -> Script:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        raise ScriptError(f"Lecture impossible de {path} : {e}")
    name = os.path.basename(path)
    name = name[:-len(SCRIPT_EXT)] if name.endswith(SCRIPT_EXT) else name
    try:
        return parse_script(text, name, path)
    except ScriptError as e:
        raise ScriptError(f"{os.path.basename(path)}, {e}")


def _give_back_to_user(paths: List[str]) -> None:
    """Lancé via sudo : les fichiers de réglages restent à l'utilisateur."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:
        for path in paths:
            try:
                os.chown(path, int(uid), int(gid))
            except OSError:
                pass


def save_script(sc: Script) -> str:
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    path = os.path.join(SCRIPTS_DIR, sc.name + SCRIPT_EXT)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(dump_script(sc))
    os.replace(tmp, path)
    sc.path = path
    _give_back_to_user([os.path.dirname(SCRIPTS_DIR), SCRIPTS_DIR, path])
    return path


# ─── brouillons : ce que deviendra chaque morceau, sans rien écrire ───

class Draft:
    """État prévu d'un morceau. Mêmes méthodes de lecture qu'AudioFile (get, first, length…),
    pour que les recherches existantes (LRCLIB, pochettes) fonctionnent dessus telles quelles."""

    def __init__(self, af: Optional[AudioFile] = None, path: str = "",
                 base: Optional[Dict[str, List[str]]] = None, length: float = 0.0, split=None):
        self.af = af
        self.path = af.path if af is not None else path
        self.orig_stem = os.path.splitext(os.path.basename(self.path))[0]
        self.base = base or {}               # tags d'une piste pas encore créée (découpage)
        self._length = length
        self.split = split                   # (SplitItem, CueSheet) si la piste naît d'un découpage
        self.fields: Dict[str, List[str]] = {}
        self.raw_del: List[str] = []
        self.wipe = False
        self.pic: Optional[Tuple[bytes, str, str]] = None      # (données, mime, description)
        self.pic_clear = False
        self.new_name: Optional[str] = None

    def get(self, f: str) -> List[str]:
        if f in self.fields:
            return list(self.fields[f])
        if self.wipe:
            return []
        if self.af is not None:
            return self.af.get(f)
        return list(self.base.get(f, []))

    def first(self, f: str) -> str:
        v = self.get(f)
        return v[0] if v else ""

    def length(self) -> float:
        return self.af.length() if self.af is not None else self._length

    @property
    def name(self) -> str:
        return self.new_name or os.path.basename(self.path)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1]

    @property
    def dir(self) -> str:
        return os.path.dirname(self.path)

    def has_picture(self) -> bool:
        if self.pic:
            return True
        if self.pic_clear or self.wipe:
            return False
        return bool(self.af is not None and self.af.backend.pictures())

    def picture_data(self) -> Optional[bytes]:
        if self.pic:
            return self.pic[0]
        if self.pic_clear or self.wipe or self.af is None:
            return None
        return first_picture(self.af)

    def raw_items(self) -> List[Tuple[str, str]]:
        if self.wipe or self.af is None:
            return []
        return [(k, v) for k, v in self.af.backend.raw_items() if k not in self.raw_del]

    def set(self, f: str, values: List[str]) -> None:
        self.fields[f] = normalize(f, values)

    def set_picture(self, data: bytes, mime: str, desc: str) -> None:
        self.pic, self.pic_clear = (data, mime, desc), False

    def clear_pictures(self) -> None:
        self.pic, self.pic_clear = None, True


def draft_ops(d: Draft, af: Optional[AudioFile] = None) -> List[Op]:
    """Les modifications qui font passer le fichier (réel, ou piste à créer) au brouillon."""
    real = af if af is not None else d.af
    target = real if real is not None else d

    def current(f: str) -> List[str]:
        return real.get(f) if real is not None else list(d.base.get(f, []))

    ops: List[Op] = []
    if d.wipe:
        n = len(real.backend.raw_items()) if real is not None else len(d.base)
        ops.append(Op(target, "wipe", before=f"{n} champ(s)", after="", label="Tous les tags"))
    for f, vals in d.fields.items():
        old = [] if d.wipe else current(f)
        if old == vals:
            continue
        if f == "lyrics":
            ops.append(Op(target, "set", f, vals, before=lyrics_summary("\n".join(old)),
                          after=lyrics_summary("\n".join(vals)), label="Paroles"))
        else:
            ops.append(Op(target, "set", f, vals, before=join_values(old), after=join_values(vals),
                          label=LABEL.get(f, f)))
    if not d.wipe and real is not None and d.raw_del:
        present = dict(real.backend.raw_items())
        for k in d.raw_del:
            if k in present:
                ops.append(Op(target, "raw_delete", k, before=oneline(present[k], 120), after="", label=k))
    npics = len(real.backend.pictures()) if real is not None else 0
    if d.pic:
        ops.append(Op(target, "pic_add", before=f"{npics} image(s)" if npics else "aucune",
                      after=d.pic[2], label="Pochette", data=d.pic[0], mime=d.pic[1]))
    elif d.pic_clear and npics and not d.wipe:
        ops.append(Op(target, "pics_clear", before=f"{npics} image(s)", after="", label="Pochette"))
    cur_name = os.path.basename(real.path if real is not None else d.path)
    if d.new_name and d.new_name != cur_name:
        ops.append(Op(target, "rename", before=cur_name, after=d.new_name, label="Nom"))
    return ops


@dataclass
class AlbumPlan:
    folder: str
    drafts: List[Draft]
    single: bool = False                                     # un seul morceau visé
    splits: list = dc_field(default_factory=list)           # [(CueSheet, [SplitItem], [sources], mode)]
    deletions: List[CleanItem] = dc_field(default_factory=list)
    excluded: set = dc_field(default_factory=set)           # image CD et .cue pris en charge
    report: List[Tuple[int, str, str, str]] = dc_field(default_factory=list)   # (n°, étape, statut, texte)
    unreadable: List[Tuple[str, str]] = dc_field(default_factory=list)
    cache: dict = dc_field(default_factory=dict)
    counts: Optional[Dict[str, int]] = None
    fixed_label: str = ""

    def release(self) -> None:
        """Album préparé : on garde le brouillon, mais on libère les tags et pochettes lus sur le
        disque (sur toute une bibliothèque, ça finirait par remplir la mémoire). Rechargés à l'écriture."""
        self.counts, self.fixed_label = album_counts(self), self.label
        self.cache.clear()
        for d in self.drafts:
            if d.split is None:
                d.af = None

    def reload(self) -> List[Tuple[str, str]]:
        errors = []
        for d in self.drafts:
            if d.af is None and d.split is None:
                try:
                    d.af = AudioFile(d.path)
                except Exception as e:
                    errors.append((os.path.basename(d.path), "illisible au moment d'écrire : " + friendly_error(e)))
        return errors

    @property
    def label(self) -> str:
        if self.fixed_label:
            return self.fixed_label
        artist = _most_common([d.first("albumartist") or d.first("artist") for d in self.drafts])
        album = _most_common([d.first("album") for d in self.drafts])
        if self.single and self.drafts:
            return self.drafts[0].name
        if artist and album:
            return f"{artist} – {album}"
        return os.path.basename(self.folder.rstrip(os.sep)) or self.folder


# ─── pendant l'exécution : touches s / q / v ───

class RunCtx:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.skip = threading.Event()
        self.stop = threading.Event()
        self.cache: dict = {}

    @property
    def stopping(self) -> bool:
        return self.stop.is_set()

    def check(self) -> None:
        """Point d'arrêt : lève StepSkipped si s ou q a été pressé."""
        if self.skip.is_set() or self.stop.is_set():
            raise StepSkipped()

    def log(self, msg: str) -> None:
        if self.verbose:
            console.print(Text("       · " + msg, style="dim"))


class KeyListener:
    """Lit les touches sans Entrée (mode cbreak) dans un fil à part, le temps d'un script.
    Le terminal est toujours remis dans son état d'origine à la sortie."""

    def __init__(self, ctx: RunCtx, writing: bool = False):
        self.ctx, self.writing = ctx, writing
        self.active = False
        self._end = threading.Event()
        self._thread = None
        self._saved = None
        self._fd = None
        self._old_sigint = None

    def __enter__(self):
        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            if not os.isatty(fd):
                return self
            if self.writing:
                import signal
                self._old_sigint = signal.signal(signal.SIGINT, lambda *a: self.ctx.stop.set())
            self._saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            self._fd, self.active = fd, True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        except Exception:
            self.active = False
        return self

    def _loop(self) -> None:
        import select
        while not self._end.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.1)
                if not ready:
                    continue
                ch = os.read(self._fd, 1).decode("latin-1").lower()
            except (OSError, ValueError):
                return
            if ch == "q":
                if not self.ctx.stop.is_set():
                    console.print(Text("  ■ arrêt demandé : " + ("fin du fichier en cours, puis arrêt"
                                                                  if self.writing else "on passe au bilan de ce qui est prêt"),
                                       style="bold yellow"))
                self.ctx.stop.set()
            elif ch == "s" and not self.writing:
                self.ctx.skip.set()
            elif ch == "v":
                self.ctx.verbose = not self.ctx.verbose
                console.print(Text(f"  (verbeux : {'oui' if self.ctx.verbose else 'non'})", style="dim"))

    def __exit__(self, *exc):
        self._end.set()
        if self._old_sigint is not None:
            import signal
            signal.signal(signal.SIGINT, self._old_sigint)
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._saved is not None:
            import termios
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
                termios.tcflush(self._fd, termios.TCIFLUSH)      # touches tapées en route : oubliées
            except Exception:
                pass
        return False


def _await(ctx: RunCtx, fn, *args):
    """Lance fn dans un fil à part et attend, en rendant la main dès que s ou q est pressé
    (le fil abandonné finit seul en arrière-plan ; son résultat est ignoré)."""
    box_: dict = {}
    done = threading.Event()

    def run():
        try:
            box_["res"] = fn(*args)
        except BaseException as e:      # noqa: B902 — renvoyé tel quel au fil principal
            box_["err"] = e
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    while not done.wait(0.1):
        ctx.check()
    if "err" in box_:
        raise box_["err"]
    return box_.get("res")


def _await_many(ctx: RunCtx, fn, items: list, workers: int, on_done) -> None:
    """fn(item) en parallèle ; on_done(i, résultat) dans le fil principal au fur et à mesure."""
    import queue
    todo: "queue.Queue[int]" = queue.Queue()
    for i in range(len(items)):
        todo.put(i)
    results: "queue.Queue[Tuple[int, object]]" = queue.Queue()
    abandon = threading.Event()

    def worker():
        while not abandon.is_set():
            try:
                i = todo.get_nowait()
            except queue.Empty:
                return
            try:
                r = fn(items[i])
            except Exception as e:
                r = e
            results.put((i, r))

    for _ in range(max(1, min(workers, len(items)))):
        threading.Thread(target=worker, daemon=True).start()
    got = 0
    try:
        while got < len(items):
            try:
                i, r = results.get(timeout=0.1)
            except queue.Empty:
                ctx.check()
                continue
            got += 1
            on_done(i, r)
            ctx.check()
    finally:
        abandon.set()


# ─── les étapes : chacune prépare, aucune n'écrit ───

def _set_album_picture(al: AlbumPlan, data: bytes, mime: str, desc: str) -> None:
    for d in al.drafts:
        d.set_picture(data, mime, desc)


def _download_cover(c: CoverCandidate) -> Optional[bytes]:
    for u in c.urls:
        try:
            data = http_get(u, CAA_THROTTLE, accept="image/*")
        except NetError:
            data = None
        if data and image_mime(data):
            return data
    return None


def plan_paroles(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    keep = st.opt("existantes") == "garder"
    approx_ok = st.opt("accepter") == "premieres"
    instr_ok = st.opt("instrumental") == "oui"
    todo = [d for d in al.drafts if not (keep and get_lyrics(d))]
    if not todo:
        return "rien à chercher : tous les morceaux ont déjà des paroles"
    if "lrclib_down" not in ctx.cache:
        ctx.cache["lrclib_down"] = _await(ctx, lrclib_ping)
    if ctx.cache["lrclib_down"]:
        raise StepFailed(f"LRCLIB injoignable ({ctx.cache['lrclib_down']})")
    c = dict(exact=0, approx=0, instrumental=0, ignored=0, notfound=0, checked=0, error=0)

    def done(i: int, r: LyricsResult) -> None:
        d = todo[i]
        text, why = None, ""
        if r.status == "exact":
            text, why = r.match.text, "exacte" + (" (synchronisée)" if r.match.kind == "synced" else " (texte)")
        elif r.status == "approx":
            if approx_ok:
                text, why = r.match.text, f"approchante, prise : {r.match.describe()}"
            else:
                c["ignored"] += 1
                why = f"approchante, ignorée : {r.match.describe()}"
        elif r.status == "instrumental":
            if instr_ok and (r.match is None or r.match.exact or approx_ok):
                text, why = INSTRUMENTAL_TEXT, "instrumental"
            else:
                c["ignored"] += 1
                why = "instrumental (rien écrit)"
        elif r.status == "error":
            c["error"] += 1
            why = r.message if r.message.startswith("reportée") else "reportée : " + r.message
        else:
            c["notfound"] += 1
            c["checked"] += r.message.startswith("introuvable, déjà")
            why = r.message or "introuvable"
        if text:
            c["instrumental" if text == INSTRUMENTAL_TEXT else r.status] += 1
            d.set("lyrics", [text.strip()])
        ctx.log(f"{d.name} : {why}")

    try:
        lyrics_search(todo, lambda fn, items, cb: _await_many(ctx, fn, items, LRCLIB_WORKERS, cb), done)
    finally:
        ctx.cache["deferred"] = ctx.cache.get("deferred", 0) + c["error"]
    found = c["exact"] + c["approx"] + c["instrumental"]
    nf = f"{c['notfound']} introuvable(s)" + (f" (dont {c['checked']} déjà vérifié(s))" if c["checked"] else "")
    parts = [f"{n} {lab}" if lab else nf
             for n, lab in ((c["exact"], "exacte(s)"), (c["approx"], "approchante(s)"),
                            (c["instrumental"], "instrumental(aux)"), (c["ignored"], "ignorée(s)"),
                            (c["notfound"], ""), (c["error"], "reportée(s) : LRCLIB ne répondait plus")) if n]
    summary = ", ".join(parts)
    if not found:
        raise StepFailed("aucune parole retenue" + (f" ({summary})" if summary else ""))
    return f"{found} morceau(x) : " + summary


def plan_instrumental(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    keep = st.opt("existantes") == "garder"
    n = 0
    for d in al.drafts:
        if keep and get_lyrics(d):
            continue
        if get_lyrics(d) != INSTRUMENTAL_TEXT:
            ctx.log(f"{d.name} : marqué instrumental")
            d.set("lyrics", [INSTRUMENTAL_TEXT])
            n += 1
    return f"{n} morceau(x) marqué(s)" if n else "rien à faire"


def plan_sans_paroles(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    n = 0
    for d in al.drafts:
        if get_lyrics(d):
            ctx.log(f"{d.name} : paroles retirées")
            d.set("lyrics", [])
            n += 1
    return f"paroles retirées de {n} morceau(x)" if n else "aucune parole à retirer"


def plan_pochette(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    tried: List[str] = []
    for src in _split_list(st.opt("sources")):
        ctx.check()
        if src == "garder":
            data = next((p for p in (d.picture_data() for d in al.drafts) if p), None)
            mime = image_mime(data) if data else None
            if data:
                missing = [d for d in al.drafts if not d.has_picture()]
                if missing and mime:
                    for d in missing:
                        d.set_picture(data, mime, f"pochette de l'album · {human_size(len(data))}")
                    return f"pochette déjà intégrée, gardée et copiée dans {len(missing)} morceau(x)"
                return "pochette déjà intégrée, gardée"
            tried.append("aucune pochette intégrée")
            ctx.log("pas de pochette intégrée")
        elif src == "dossier":
            cover = find_cover(al.folder)
            if not cover:
                tried.append("pas d'image dans le dossier")
                ctx.log("pas d'image dans le dossier")
                continue
            try:
                data, mime = read_image(cover)
            except (OSError, TagError) as e:
                tried.append(f"{os.path.basename(cover)} illisible ({e})")
                continue
            dims = image_dims(data)
            _set_album_picture(al, data, mime, " · ".join(x for x in (os.path.basename(cover), dims,
                                                                      human_size(len(data))) if x))
            return f"image du dossier : {os.path.basename(cover)}" + (f" ({dims})" if dims else "")
        else:
            n = int(src.rsplit("_", 1)[1])
            cands = al.cache.get("covers")
            if cands is None:
                artist = _most_common([d.first("albumartist") or d.first("artist") for d in al.drafts])
                album = _most_common([d.first("album") for d in al.drafts])
                cands = []
                if not artist or not album:
                    tried.append("artiste ou album absent des tags : pas de recherche en ligne")
                else:
                    ctx.log(f"recherche MusicBrainz / Cover Art Archive : {artist} – {album}")
                    try:
                        cands = _await(ctx, cover_candidates, artist, album) or []
                    except NetError as e:
                        tried.append(f"MusicBrainz injoignable ({e})")
                    ctx.log(f"{len(cands)} pochette(s) proposée(s)")
                al.cache["covers"] = cands
            if len(cands) < n:
                if cands or not tried:
                    tried.append(f"pas de {n}{'er' if n == 1 else 'e'} résultat en ligne")
                continue
            cand = cands[n - 1]
            label = " ".join(x for x in (cand.album, cand.date[:4], cand.country) if x)
            ctx.log(f"téléchargement : {label}")
            data = _await(ctx, _download_cover, cand)
            if not data:
                tried.append(f"téléchargement impossible ({label})")
                continue
            dims = image_dims(data)
            _set_album_picture(al, data, image_mime(data),
                               " · ".join(x for x in ("Cover Art Archive", label, dims) if x))
            return f"en ligne, {n}{'er' if n == 1 else 'e'} résultat : {label}" + (f" ({dims})" if dims else "")
    raise StepFailed("aucune pochette" + (" : " + " ; ".join(dict.fromkeys(tried)) if tried else ""))


def plan_sans_pochette(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    n = 0
    for d in al.drafts:
        if d.has_picture():
            ctx.log(f"{d.name} : pochette retirée")
            d.clear_pictures()
            n += 1
    return f"pochettes retirées de {n} morceau(x)" if n else "aucune pochette à retirer"


def plan_champs(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    changed = 0
    for d in al.drafts:
        touched = False
        for f, v in st.opts.items():
            vals = [] if not v else (split_values(v) if f not in NUM_FIELDS else [v])
            if d.get(f) != normalize(f, vals):
                ctx.log(f"{d.name} : {LABEL[f]} « {join_values(d.get(f))} » → « {join_values(normalize(f, vals))} »")
                d.set(f, vals)
                touched = True
        changed += touched
    return f"{changed} morceau(x) modifié(s)" if changed else "déjà comme demandé"


def plan_supprimer_champs(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    names = _split_list(st.opt("champs"))
    changed = 0
    for d in al.drafts:
        touched = False
        for name in names:
            low = name.lower()
            if low in LABEL and low != "lyrics":
                if d.get(low):
                    ctx.log(f"{d.name} : {LABEL[low]} supprimé")
                    d.set(low, [])
                    touched = True
                continue
            targets = {t.lower() for t in FIELD_ALIASES.get(low, (name,))}
            for key, _ in d.raw_items():
                if key.lower() in targets or key.split(":")[0].lower() in targets:
                    ctx.log(f"{d.name} : {key} supprimé")
                    d.raw_del.append(key)
                    touched = True
        changed += touched
    return f"{changed} morceau(x) concerné(s)" if changed else "aucun de ces champs n'est présent"


def plan_numeroter(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    drafts = sorted(al.drafts, key=lambda d: path_sort_key(os.path.join(d.dir, d.name)))
    if st.opt("mode") == "noms":
        parsed = [(d, parse_track_from_name(d.stem)) for d in drafts]
        ok = [(d, p) for d, p in parsed if p is not None]
        if not ok:
            raise StepFailed("aucun numéro trouvé au début des noms de fichiers")
        if any(p[1] for _, p in ok):                    # faces de vinyle A1, B2… → séquentiel
            ok.sort(key=lambda x: (x[1][1], x[1][2]))
            assigned = [(d, i, p[0]) for i, (d, p) in enumerate(ok, 1)]
        else:
            assigned = [(d, p[2], p[0]) for d, p in ok]
        missing = len(parsed) - len(ok)
    else:
        assigned = [(d, i, None) for i, d in enumerate(drafts, 1)]
        missing = 0
    total = max((n for _, n, _ in assigned), default=0)
    for d, n, disc in assigned:
        ctx.log(f"{d.name} : piste {n}" + (f", disque {disc}" if disc else ""))
        d.set("tracknumber", [str(n)])
        if st.opt("total") == "oui":
            d.set("tracktotal", [str(total)])
        if disc:
            d.set("discnumber", [str(disc)])
    return f"{len(assigned)} piste(s) numérotée(s)" + (f", {missing} sans numéro dans le nom" if missing else "")


def plan_titres(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    n = 0
    for d in al.drafts:
        new = title_from_filename(d.stem, d.get("artist") + d.get("albumartist"))
        if new and d.get("title") != [new]:
            ctx.log(f"{d.name} : titre « {d.first('title')} » → « {new} »")
            d.set("title", [new])
            n += 1
    return f"{n} titre(s)" if n else "titres déjà conformes aux noms"


def plan_remplacer(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    fields = TEXT_FIELDS if st.opt("champ") == "tous" else [st.opt("champ")]
    regex = st.opt("regex") == "oui"
    pat = re.compile(st.opt("chercher") if regex else re.escape(st.opt("chercher")))
    repl = st.opt("par")
    n = 0
    for d in al.drafts:
        touched = False
        for f in fields:
            old = d.get(f)
            if not old:
                continue
            new = [re.sub(r"\s{2,}", " ", pat.sub(repl if regex else (lambda m: repl), v)).strip() for v in old]
            new = [v for v in new if v]
            if new != old:
                ctx.log(f"{d.name} : {LABEL[f]} « {join_values(old)} » → « {join_values(new)} »")
                d.set(f, new)
                touched = True
        n += touched
    return f"{n} morceau(x) modifié(s)" if n else "texte introuvable, rien à remplacer"


def plan_effacer_tags(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    for d in al.drafts:
        d.wipe, d.fields, d.raw_del, d.pic, d.pic_clear = True, {}, [], None, True
    return f"tous les tags de {len(al.drafts)} morceau(x)"


def plan_renommer(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    tpl = st.opt("modele")
    taken = {os.path.join(d.dir, d.name) for d in al.drafts}
    planned: Dict[str, str] = {}
    problems: List[str] = []
    n = 0
    for d in al.drafts:
        tr, tot = d.first("tracknumber"), d.first("tracktotal")
        width = 3 if tot.isdigit() and int(tot) >= 100 else 2
        vals = {"n": tr.zfill(width) if tr.isdigit() else tr, "title": d.first("title"),
                "artist": d.first("artist"), "album": d.first("album"), "year": d.first("date")[:4],
                "disc": d.first("discnumber"), "albumartist": d.first("albumartist") or d.first("artist")}
        missing = [m for m in re.findall(r"\{(\w+)\}", tpl) if not vals.get(m)]
        if missing:
            problems.append(f"{d.name} (il manque : {', '.join(missing)})")
            continue
        name = tpl
        for key, v in vals.items():
            name = name.replace("{" + key + "}", v.replace("/", "-"))
        name = safe_name(name) + d.ext.lower()
        dest = os.path.join(d.dir, name)
        if name == d.name:
            continue
        if dest in planned or (os.path.exists(dest) and dest not in taken):
            problems.append(f"{d.name} (« {name} » existe déjà)")
            continue
        planned[dest] = d.name
        ctx.log(f"{d.name} → {name}")
        d.new_name = name
        n += 1
    for p in problems:
        ctx.log("pas renommé : " + p)
    if problems and not n:
        raise StepFailed(f"aucun fichier renommé : {problems[0]}" + (f" (+{len(problems) - 1})" if len(problems) > 1 else ""))
    return (f"{n} fichier(s) à renommer" if n else "noms déjà conformes") + \
        (f", {len(problems)} ignoré(s)" if problems else "")


def plan_nettoyer(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    types = _split_list(st.opt("types"))
    kinds = None if "tout" in types else {CLEAN_TYPE_KIND[t] for t in types}
    already = {it.path for it in al.deletions}
    chosen = [it for it in scan_clean(al.folder, False)
              if not it.protected and it.path not in al.excluded and it.path not in already
              and (kinds is None or it.kind in kinds)]
    pics = lyr = 0
    for it in chosen:               # sauvetage : pochette et .lrc qui n'existent que dans ces fichiers
        cover = it.path if (it.kind == "Image" and it.note == "pochette du dossier") else \
            (find_cover(it.path) if it.is_dir else None)
        if cover and any(not d.has_picture() for d in al.drafts):
            try:
                data, mime = read_image(cover)
            except (OSError, TagError):
                data = None
            if data:
                for d in al.drafts:
                    if not d.has_picture():
                        d.set_picture(data, mime, f"{os.path.relpath(cover, al.folder)} · {human_size(len(data))}")
                        pics += 1
        if it.kind == "Paroles .lrc":
            stem = os.path.splitext(os.path.basename(it.path))[0].lower()
            for d in al.drafts:
                if stem in (d.orig_stem.lower(), d.stem.lower()) and not get_lyrics(d):
                    try:
                        text = _read_text(it.path).strip()
                    except OSError:
                        text = ""
                    if text:
                        d.set("lyrics", [text])
                        lyr += 1
        ctx.log(f"à supprimer : {os.path.relpath(it.path, al.folder)}{os.sep if it.is_dir else ''}")
    al.deletions += chosen
    if not chosen:
        return "rien à supprimer"
    msg = f"{len(chosen)} élément(s) à supprimer ({human_size(sum(it.size for it in chosen))})"
    saved = ([f"pochette intégrée dans {pics} morceau(x)"] if pics else []) + \
        ([f"paroles .lrc intégrées dans {lyr} morceau(x)"] if lyr else [])
    return msg + (" ; avant : " + ", ".join(saved) if saved else "")


def plan_cue(ctx: RunCtx, al: AlbumPlan, st: Step) -> str:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise StepFailed("ffmpeg absent (" + install_cmd("ffmpeg-free", "ffmpeg") + ")")
    cues = [c for c in find_files(al.folder, False, {".cue"}) if c not in al.excluded]
    if not cues:
        raise StepFailed("aucun fichier .cue dans ce dossier")
    created = 0
    for path in cues:
        ctx.check()
        try:
            cs = parse_cue(path)
        except Exception as e:
            raise StepFailed(f"{os.path.basename(path)} illisible ({e})")
        if not cs.tracks:
            raise StepFailed(f"{os.path.basename(path)} ne contient aucune piste")
        guess_disc(cs, cues)
        sources: Dict[str, str] = {}
        for fname in dict.fromkeys(t.file for t in cs.tracks):
            src = resolve_source(cs, fname)
            if src is None:
                raise StepFailed(f"« {fname} », cité par {os.path.basename(path)}, est introuvable")
            sources[fname] = src
        try:
            probes = {f: probe(p) for f, p in sources.items()}
        except TagError as e:
            raise StepFailed(str(e))
        items = build_plan(cs, sources, probes)
        clash = [it.out_path for it in items if os.path.exists(it.out_path)]
        if clash:
            raise StepFailed(f"« {os.path.basename(clash[0])} » existe déjà : album déjà découpé ?")
        srcs = list(dict.fromkeys(sources.values()))
        al.excluded.update(srcs + [path])
        al.drafts = [d for d in al.drafts if d.path not in srcs]
        for it in items:
            length = it.expected / it.sr if it.expected else 0.0
            al.drafts.append(Draft(path=it.out_path, base=track_tags(cs, it.track), length=length, split=(it, cs)))
            ctx.log(f"piste à créer : {os.path.basename(it.out_path)}")
        al.splits.append((cs, items, srcs, st.opt("original")))
        created += len(items)
    al.drafts.sort(key=lambda d: path_sort_key(d.path))
    fate = {"ranger": "image d'origine rangée dans « _source »", "supprimer": "image d'origine supprimée",
            "laisser": "image d'origine laissée en place"}[st.opt("original")]
    return f"{created} piste(s) à créer, {fate}"


PLANNERS = {
    "paroles": plan_paroles, "instrumental": plan_instrumental, "sans_paroles": plan_sans_paroles,
    "pochette": plan_pochette, "sans_pochette": plan_sans_pochette, "champs": plan_champs,
    "supprimer_champs": plan_supprimer_champs, "numeroter": plan_numeroter, "titres": plan_titres,
    "remplacer": plan_remplacer, "effacer_tags": plan_effacer_tags, "renommer": plan_renommer,
    "nettoyer": plan_nettoyer, "cue": plan_cue,
}

STATUS_MARK = {"ok": ("✓", "green"), "skip": ("↷", "yellow"), "fail": ("✗", "red"),
               "na": ("·", "dim"), "stop": ("■", "yellow")}


def _report(al: AlbumPlan, n: int, label: str, status: str, text: str, show: bool = True) -> None:
    al.report.append((n, label, status, text))
    if show:
        mark, style = STATUS_MARK[status]
        console.print(Text.assemble(f"    {mark} ", (f"{n}. {label}", style if status != "ok" else "bold"),
                                    (" : " + text if text else "", style if status != "ok" else "")))


class _Quiet:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _spinner(msg: str):
    return console.status(msg, spinner="dots") if console.is_terminal else _Quiet()


def plan_album(ctx: RunCtx, sc: Script, al: AlbumPlan) -> None:
    keys = "  [dim](s : sauter · q : arrêter · v : verbeux)[/]" if sys.stdin.isatty() else ""
    for j, st in enumerate(sc.steps, 1):
        label = st.spec.label
        if ctx.stopping:
            _report(al, j, label, "stop", "pas faite (arrêt demandé)", show=False)
            continue
        if st.spec.album_only and al.single:
            _report(al, j, label, "na", "ne s'applique pas à un morceau seul")
            continue
        if not al.drafts and st.action not in ("nettoyer", "cue"):
            _report(al, j, label, "na", "aucun morceau")
            continue
        ctx.skip.clear()
        failed = None
        try:
            with _spinner(f"[magenta]{j}. {escape(label)}…[/]{keys}"):
                msg = PLANNERS[st.action](ctx, al, st)
            _report(al, j, label, "ok", msg)
        except StepSkipped:
            if ctx.stopping:
                _report(al, j, label, "stop", "interrompue (arrêt demandé) ; ce qui était prêt est gardé")
            else:
                _report(al, j, label, "skip", "sautée (touche s) ; ce qui était déjà prêt est gardé")
        except KeyboardInterrupt:
            ctx.stop.set()
            _report(al, j, label, "stop", "interrompue (Ctrl+C) ; ce qui était prêt est gardé")
        except StepFailed as e:
            failed = str(e)
        except Exception as e:                      # imprévu : traité comme un échec de l'étape
            failed = friendly_error(e)
        if failed is not None:
            _report(al, j, label, "fail", failed)
            if st.on_fail == "arreter":
                console.print(Text("      → arrêt du script pour cet album (étape réglée sur « arrêter »)",
                                   style="red"))
                for k, rest in enumerate(sc.steps[j:], j + 1):
                    _report(al, k, rest.spec.label, "na", "pas faite (arrêt après l'échec)", show=False)
                return


# ─── bilan, confirmation, écriture ───

def album_counts(al: AlbumPlan) -> Dict[str, int]:
    if al.counts is not None:
        return al.counts
    c = dict(tags=0, paroles=0, pochette=0, noms=0, crees=0, suppr=len(al.deletions))
    for d in al.drafts:
        ops = draft_ops(d)
        if any(o.kind in ("set", "raw_delete", "wipe") and o.key != "lyrics" for o in ops):
            c["tags"] += 1
        if any(o.kind == "set" and o.key == "lyrics" for o in ops):
            c["paroles"] += 1
        if any(o.kind in ("pic_add", "pics_clear") for o in ops):
            c["pochette"] += 1
        if any(o.kind == "rename" for o in ops):
            c["noms"] += 1
    c["crees"] = sum(len(items) for _, items, _, _ in al.splits)
    c["fichiers"] = sum(1 for d in al.drafts if draft_ops(d))
    return c


def _changes(c: Dict[str, int]) -> bool:
    return bool(c["fichiers"] or c["crees"] or c["suppr"])


def _steps_marks(al: AlbumPlan) -> Text:
    t = Text()
    for n, _, status, _ in al.report:
        mark, style = STATUS_MARK[status]
        t.append(f"{n}{mark} ", style=style)
    return t


def album_detail(al: AlbumPlan, limit: int = 60) -> list:
    out: list = []
    released = al.counts is not None
    for name, err in al.reload():
        out.append(Text(f"  ! {name} : {err}", style="yellow"))
    ops = [o for d in al.drafts for o in draft_ops(d)]
    if released:
        for d in al.drafts:
            if d.split is None:
                d.af = None
    if ops:
        out.append(ops_table(ops, limit))
        if len(ops) > limit:
            out.append(Text(f"… et {len(ops) - limit} autres modifications", style="dim"))
    for cs, items, srcs, mode in al.splits:
        names = ", ".join(os.path.basename(it.out_path) for it in items[:6]) + (" …" if len(items) > 6 else "")
        fate = {"ranger": "rangée dans « _source »", "supprimer": "supprimée", "laisser": "laissée en place"}[mode]
        out.append(Text.assemble(("  Découpage ", "bold"), f"{os.path.basename(cs.path)} → {len(items)} pistes : ",
                                 (names, "dim"), f"\n  Image d'origine {fate}."))
    if al.deletions:
        t = Text("  À supprimer : ", style="bold red")
        for it in al.deletions[:20]:
            t.append(f"\n    ✗ {os.path.relpath(it.path, al.folder)}{os.sep if it.is_dir else ''}", style="red")
        if len(al.deletions) > 20:
            t.append(f"\n    … et {len(al.deletions) - 20} autre(s)", style="dim")
        out.append(t)
    return out


def show_bilan(albums: List[AlbumPlan], not_started: int) -> Dict[str, int]:
    tot = dict(fichiers=0, crees=0, suppr=0, albums=0)
    counts = [album_counts(al) for al in albums]
    for c in counts:
        tot["fichiers"] += c["fichiers"]
        tot["crees"] += c["crees"]
        tot["suppr"] += c["suppr"]
        tot["albums"] += _changes(c)
    console.print()
    console.print(Rule(Text(" Bilan ", style="bold magenta"), style="magenta"))
    if len(albums) == 1:
        for r in album_detail(albums[0]):
            console.print(r)
    else:
        t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False)
        t.add_column("#", style="dim", justify="right", no_wrap=True)
        t.add_column("Album", ratio=3, overflow="ellipsis", no_wrap=True)
        t.add_column("Étapes", no_wrap=True)
        for col in ("Tags", "Paroles", "Pochette", "Noms", "Pistes", "Suppr."):
            t.add_column(col, justify="right", no_wrap=True)
        shown = 0
        for i, (al, c) in enumerate(zip(albums, counts), 1):
            if shown >= 40:
                break
            shown += 1
            cell = lambda v: Text(str(v), style="bold") if v else Text("·", style="dim")  # noqa: E731
            t.add_row(str(i), Text(al.label), _steps_marks(al), cell(c["tags"]), cell(c["paroles"]),
                      cell(c["pochette"]), cell(c["noms"]), cell(c["crees"]), cell(c["suppr"]))
        console.print(t)
        if len(albums) > shown:
            console.print(f"[dim]… et {len(albums) - shown} autres albums (le détail complet est dans « Voir le détail »)[/]")
        console.print("[dim]Étapes : ✓ faite · ↷ sautée · ✗ échec · · sans objet · ■ arrêtée[/]")
    if not_started:
        console.print(f"[yellow]{not_started} album(s) non préparé(s) (arrêt demandé) : ils ne seront pas touchés.[/]")
    parts = [f"{tot['fichiers']} fichier(s) modifié(s)"]
    if tot["crees"]:
        parts.append(f"{tot['crees']} piste(s) créée(s)")
    if tot["suppr"]:
        parts.append(f"{tot['suppr']} élément(s) supprimé(s)")
    console.print(Text.assemble(("  " + " · ".join(parts), "bold"),
                                (f"  dans {tot['albums']} album(s)" if len(albums) > 1 else "", "")))
    return tot


def show_detail(albums: List[AlbumPlan]) -> None:
    rends: list = []
    for i, al in enumerate(albums, 1):
        if not _changes(album_counts(al)):
            continue
        rends.append(Rule(Text(f" {i}. {al.label} ", style="bold"), style="magenta", align="left"))
        rends.append(Text(al.folder, style="dim"))
        for n, label, status, text in al.report:
            mark, style = STATUS_MARK[status]
            rends.append(Text(f"  {mark} {n}. {label} : {text}", style=style if status != "ok" else ""))
        rends += album_detail(al, limit=100000)
    print_long(rends)


def apply_album(ctx: RunCtx, al: AlbumPlan):
    """Écrit un album préparé. Renvoie (fichiers écrits, pistes créées, éléments supprimés, erreurs, notes)."""
    written = created = deleted = 0
    notes: List[str] = []
    errors: List[Tuple[str, str]] = al.reload()
    for cs, items, srcs, mode in al.splits:
        complete = True
        for it in items:
            if ctx.stopping:
                complete = False
                break
            d = next((d for d in al.drafts if d.split and d.split[0] is it), None)
            err = split_one(it, cs)
            if err:
                errors.append((os.path.basename(it.out_path), err))
                complete = False
                continue
            created += 1
            if d is None:
                continue
            try:
                d.af = AudioFile(it.out_path)
            except Exception as e:
                errors.append((os.path.basename(it.out_path), friendly_error(e)))
        if complete:
            err = move_originals(cs, srcs, mode)
            if err:
                errors.append((os.path.basename(cs.path), err))
        else:
            notes.append(f"image d'origine laissée en place ({os.path.basename(cs.path)})")
    for k, d in enumerate(al.drafts):
        if ctx.stopping:
            left = sum(1 for x in al.drafts[k:] if x.af is not None and draft_ops(x, x.af))
            if left:
                notes.append(f"arrêt demandé : {left} fichier(s) pas écrit(s)")
            break
        if d.af is None:
            continue
        ops = draft_ops(d, d.af)
        if not ops:
            continue
        try:
            _apply_file(d.af, ops)
            written += 1
        except Exception as e:
            errors.append((d.af.name, friendly_error(e)))
        finally:
            try:
                d.af.load()
            except Exception:
                pass
    if al.deletions:
        if ctx.stopping:
            notes.append("suppressions pas faites (arrêt demandé)")
        elif errors:
            notes.append("suppressions annulées : des écritures ont échoué dans cet album")
        else:
            ok, errs = delete_items(al.deletions, progress=False)
            deleted += ok
            errors += errs
    return written, created, deleted, errors, notes


def _covers_library(root: str) -> bool:
    try:
        r, lib = os.path.realpath(root), os.path.realpath(MUSIC_ROOT)
    except OSError:
        return False
    return r == lib or lib.startswith(r.rstrip(os.sep) + os.sep)


def _writable(targets) -> bool:
    if os.geteuid() == 0:
        return True
    for folder, paths, _ in targets:
        if not os.access(folder, os.W_OK) or not all(os.access(p, os.W_OK) for p in paths[:20]):
            return False
    return True


def collect_targets(path: str, recursive: bool) -> List[Tuple[str, List[str], bool]]:
    """(dossier, morceaux, morceau seul ?) pour chaque album visé."""
    if os.path.isfile(path):
        if os.path.splitext(path)[1].lower() not in AUDIO_EXTS:
            raise ScriptError(f"« {os.path.basename(path)} » n'est pas un fichier audio pris en charge.")
        return [(os.path.dirname(path), [path], True)]
    files = find_files(path, recursive, AUDIO_EXTS)
    groups: Dict[str, List[str]] = {}
    for p in files:
        groups.setdefault(os.path.dirname(p), []).append(p)
    return [(d, fl, False) for d, fl in groups.items()]


def run_script(sc: Script, targets, verbose: bool = False, simulation: bool = False, root: str = "") -> int:
    """Prépare toutes les étapes sur tous les albums, montre le bilan, demande UNE confirmation,
    puis écrit. Renvoie le code de sortie (0 = normal)."""
    if not sc.steps:
        info(f"Le script « {escape(sc.name)} » ne contient aucune étape.", "red")
        return 1
    root = root or (targets[0][0] if targets else os.getcwd())
    n_alb = len(targets)
    n_files = sum(len(p) for _, p, _ in targets)
    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim", justify="right", no_wrap=True)
    head.add_column(overflow="fold")
    head.add_row("Étapes", Text("  ".join(f"{i}. {st.spec.label}" for i, st in enumerate(sc.steps, 1))))
    if n_alb == 1 and targets[0][2]:
        head.add_row("Cible", Text(f"1 morceau : {targets[0][1][0]}"))
    else:
        head.add_row("Cible", Text(f"{n_alb} album(s) · {n_files} morceau(x) · {root}"))
    mode = ["simulation : rien ne sera écrit"] if simulation else \
        ["tout est préparé d'abord ; rien n'est écrit avant ta confirmation"]
    head.add_row("Mode", Text(" · ".join(mode + (["verbeux"] if verbose else [])), style="cyan"))
    if sys.stdin.isatty():
        head.add_row("Touches", Text("s = sauter l'étape en cours · q = arrêter · v = verbeux oui/non"))
    console.print(Panel(head, title=Text(f" ▶ Script « {sc.name} » ", style="bold"), title_align="left",
                        border_style="magenta", box=box.ROUNDED, padding=(0, 1)))
    if not targets:
        hint = ""
        if os.path.isdir(root) and find_files(root, True, AUDIO_EXTS):
            hint = ("\n[dim]Les sous-dossiers contiennent de la musique : ajoute -r pour traiter chaque album "
                    "(dans musitag : active le mode récursif avec r).[/]")
        info("Aucun fichier audio ici." + hint)
        return 0
    if n_alb >= BIG_RUN or (n_alb > 1 and _covers_library(root)):
        where = "toute ta bibliothèque" if _covers_library(root) else root
        info(f"[bold]Attention : ce script va être préparé sur {n_alb} albums ({n_files} morceaux), dans {escape(where)}.[/]\n\n"
             "• Tout est d'abord préparé (les recherches en ligne prennent du temps), puis "
             "[bold]une seule confirmation[/] porte sur l'ensemble.\n"
             "• Le bilan montrera un résumé par album ; le détail complet restera consultable.\n"
             "• Pendant la préparation, [bold]q[/] arrête et passe au bilan de ce qui est prêt.", "yellow")
        if not confirm("Lancer quand même ?", False):
            console.print("[yellow]Rien n'a été fait.[/]")
            return 0
    if not simulation and not _writable(targets):
        info("Ton utilisateur ne peut pas modifier ces fichiers.\n"
             f"Relance avec sudo : [bold]sudo musitag -s {escape(sc.name)} …[/]  "
             "(ou essaie d'abord avec [bold]--simulation[/]).", "red")
        return 1

    ctx = RunCtx(verbose)
    albums: List[AlbumPlan] = []
    with KeyListener(ctx):
        for pos, (folder, paths, single) in enumerate(targets, 1):
            if ctx.stopping:
                break
            files, bad = load_files(paths)
            al = AlbumPlan(folder, [Draft(af) for af in files], single=single, unreadable=bad)
            console.print()
            where = os.path.relpath(folder, root) if n_alb > 1 and os.path.isdir(root) else folder
            console.print(Text.assemble(("  ", ""), (f"Album {pos}/{n_alb} · " if n_alb > 1 else "", "dim"),
                                        (al.label, "bold"), (f"   {len(files)} morceau(x) · {where}", "dim")))
            for p, why in bad:
                console.print(Text(f"    ! illisible, ignoré : {os.path.basename(p)} ({why})", style="yellow"))
            try:
                plan_album(ctx, sc, al)
            except KeyboardInterrupt:
                ctx.stop.set()
            al.release()
            albums.append(al)
    not_started = n_alb - len(albums)

    tot = show_bilan(albums, not_started)
    deferred = ctx.cache.get("deferred", 0)
    if deferred:
        info(f"Paroles : [bold]{deferred}[/] recherche(s) reportée(s), LRCLIB ne répondait plus (serveur saturé).\n"
             "Relance le même script plus tard : avec « paroles déjà présentes : garder », seuls les "
             "morceaux encore sans paroles seront cherchés, et les introuvables déjà vérifiés ne le "
             "seront pas à nouveau.", "yellow")
    if not (tot["fichiers"] or tot["crees"] or tot["suppr"]):
        info("Rien à modifier : tout est déjà comme le script le demande.", "green")
        return 0
    if simulation:
        info("Simulation : rien n'a été écrit.", "cyan")
        return 0
    while True:
        k = menu("Confirmation", [
            ("1", "Tout appliquer", f"{tot['fichiers']} fichier(s)" + (f", {tot['crees']} piste(s) créée(s)" if tot["crees"] else "")
             + (f", {tot['suppr']} suppression(s)" if tot["suppr"] else "")),
            ("2", "Voir le détail", "toutes les modifications, album par album"),
            ("0", "Annuler", "rien n'est écrit"),
        ])
        if k == "2":
            show_detail(albums)
            continue
        if k == "0":
            console.print("[yellow]Rien n'a été modifié.[/]")
            return 0
        break

    console.print()
    ctx.stop.clear()
    total = dict(written=0, created=0, deleted=0)
    all_errors: List[Tuple[str, str]] = []
    todo = [al for al in albums if _changes(album_counts(al))]
    with KeyListener(ctx, writing=True):
        for pos, al in enumerate(todo, 1):
            if ctx.stopping:
                break
            try:
                with _spinner(f"[magenta]Écriture {pos}/{len(todo)} · {escape(al.label)}…[/]"
                              + ("  [dim](q : arrêter après le fichier en cours)[/]" if sys.stdin.isatty() else "")):
                    w, c, d, errs, notes = apply_album(ctx, al)
            except KeyboardInterrupt:
                ctx.stop.set()
                continue
            total["written"] += w
            total["created"] += c
            total["deleted"] += d
            all_errors += [(f"{al.label} · {n}", e) for n, e in errs]
            bits = [f"{w} fichier(s) écrit(s)"] + ([f"{c} piste(s) créée(s)"] if c else []) + \
                ([f"{d} supprimé(s)"] if d else [])
            stopped = any(n.startswith("arrêt demandé") for n in notes)
            mark, style = ("✗", "red") if errs else (("■", "yellow") if stopped else ("✓", "green"))
            console.print(Text.assemble(f"  {mark} ", (al.label, "bold"), " : " + " · ".join(bits), style=style))
            for n in notes:
                console.print(Text(f"    ! {n}", style="yellow"))
    if ctx.stopping:
        console.print("[yellow]Arrêté : les albums suivants n'ont pas été touchés.[/]")
    summary = f"✓ {total['written']} fichier(s) écrit(s)"
    if total["created"]:
        summary += f" · {total['created']} piste(s) créée(s)"
    if total["deleted"]:
        summary += f" · {total['deleted']} élément(s) supprimé(s)"
    console.print(Panel(Text(summary, style="bold green"), border_style="green", box=box.ROUNDED, expand=False))
    if all_errors:
        show_result(0, all_errors)
    return 1 if all_errors else 0


def script_cli(name: str, path: Optional[str], recursive: bool, verbose: bool, simulation: bool) -> int:
    try:
        sc = load_script(find_script(name))
        path = os.path.abspath(os.path.expanduser(path or os.getcwd()))
        if not os.path.exists(path):
            raise ScriptError(f"Introuvable : {path}")
        targets = collect_targets(path, recursive)
    except ScriptError as e:
        console.print(Text(str(e), style="red"))
        return 1
    try:
        return run_script(sc, targets, verbose or sc.verbose, simulation, root=path)
    except Cancel:
        console.print("[yellow]Annulé.[/]")
        return 1


# ─── éditeur sans code (touche s) ───

def steps_table(sc: Script) -> Table:
    t = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, pad_edge=False)
    t.add_column("#", style="dim", justify="right", no_wrap=True)
    t.add_column("Action", style="bold", no_wrap=True)
    t.add_column("Réglages", ratio=1, overflow="fold")
    t.add_column("Si échec", no_wrap=True, style="dim")
    for i, st in enumerate(sc.steps, 1):
        fail = ("arrêter" if st.on_fail == "arreter" else "étape suivante") if st.spec.can_fail else "—"
        t.add_row(str(i), Text(st.spec.label), Text(st.describe()), fail)
    if not sc.steps:
        t.add_row("", Text("aucune étape : « a » pour en ajouter", style="dim"), "", "")
    return t


def _ask_choice(title: str, choices: List[Tuple[str, str]], current: str) -> str:
    opts, default = [], None
    for i, (val, lab) in enumerate(choices, 1):
        opts.append((str(i), lab, "actuel" if val == current else ""))
        if val == current:
            default = str(i)
    k = menu(title, opts + [("0", "Annuler", "")], default=default)
    if k == "0":
        raise Cancel()
    return choices[int(k) - 1][0]


def _ask_ordered(title: str, choices: List[Tuple[str, str]], current: str, hint: str) -> str:
    console.print(Text(f"  {title}", style="bold"))
    for i, (val, lab) in enumerate(choices, 1):
        console.print(f"   [cyan]{i:>2}[/]  {escape(lab)}")
    cur_idx = [str(i) for i, (v, _) in enumerate(choices, 1) if v in _split_list(current)]
    cur_idx.sort(key=lambda s: _split_list(current).index(choices[int(s) - 1][0]))
    r = ask(f"  {hint} [magenta]›[/] ", prefill=", ".join(cur_idx)).strip()
    picked: List[str] = []
    for part in re.split(r"[,\s]+", r):
        if part.isdigit() and 1 <= int(part) <= len(choices):
            v = choices[int(part) - 1][0]
            if v not in picked:
                picked.append(v)
    if not picked:
        raise Cancel()
    return ", ".join(["tout"] if "tout" in picked else picked)


def _edit_fields(st: Step) -> bool:
    while True:
        console.print()
        opts = [(str(i), LABEL[f], st.opts.get(f, "") if f in st.opts else "")
                for i, f in enumerate(SCRIPT_FIELDS, 1)]
        k = menu("Fixer des champs : lequel ?", opts + [None, ("t", "Terminé", f"{len(st.opts)} champ(s)"),
                                                        ("0", "Annuler", "")], default="t")
        if k == "0":
            return False
        if k == "t":
            if not st.opts:
                info("Choisis au moins un champ.")
                continue
            return True
        f = SCRIPT_FIELDS[int(k) - 1]
        console.print("[dim]  Valeur écrite dans tous les morceaux. Plusieurs valeurs : sépare-les par ;  "
                      "· vide = vider ce champ · « x » = retirer de la liste[/]")
        v = ask(field_prompt(LABEL[f]), prefill=st.opts.get(f, "")).strip()
        if v.lower() == "x":
            st.opts.pop(f, None)
        else:
            st.opts[f] = v


def configure_step(spec: ActionSpec, cur: Optional[Step] = None) -> Optional[Step]:
    """Règle une étape avec de petits menus. None si annulé."""
    st = Step(spec.key, dict(cur.opts) if cur else {}, cur.on_fail if cur else "continuer")
    console.print()
    console.print(Rule(Text(f" {spec.label} ", style="bold magenta"), style="magenta", align="left"))
    try:
        if spec.free_keys and not _edit_fields(st):
            return None
        for o in spec.opts:
            value = st.opt(o.key)
            if spec.key == "pochette":
                value = _ask_ordered("Sources de la pochette (la 1re qui donne une image l'emporte) :",
                                     o.choices, value, "Numéros, dans l'ordre de priorité (ex : 2, 3, 4)")
            elif o.multi:
                value = _ask_ordered("Types de fichiers à supprimer :", o.choices, value,
                                     "Numéros (ex : 1 pour tout, ou 2, 5)")
            elif o.choices:
                value = _ask_choice(o.label, o.choices, value)
            elif spec.key == "renommer":
                choices = RENAME_PRESETS + [("perso", "modèle personnalisé")]
                cur_v = value if value in dict(RENAME_PRESETS) else "perso"
                pick = _ask_choice("Modèle de nom", [(v, f"{lab}   {v}" if v != "perso" else lab)
                                                     for v, lab in choices], cur_v)
                if pick == "perso":
                    console.print("[dim]  Champs possibles : {n} {title} {artist} {album} {year} {disc} {albumartist}[/]")
                    value = ask(field_prompt("Modèle"), prefill=value).strip()
                else:
                    value = pick
            else:
                if spec.key == "supprimer_champs":
                    console.print("[dim]  Séparés par des virgules : " + ", ".join(SCRIPT_FIELDS)
                                  + ", encodeur, ou un nom exact (ex : REPLAYGAIN_TRACK_GAIN)[/]")
                if spec.key == "remplacer" and o.key == "par":
                    console.print("[dim]  Vide = supprimer le texte trouvé[/]")
                value = ask(field_prompt(o.label), prefill=value)
                if spec.key != "remplacer":
                    value = value.strip()
            st.opts[o.key] = value
        validate_step(st)
        if spec.can_fail:
            st.on_fail = _ask_choice("Si cette étape échoue", FAIL_CHOICES, st.on_fail)
    except Cancel:
        return None
    except ScriptError as e:
        info(escape(str(e)), "red")
        pause()
        return None
    return st


def pick_action() -> Optional[ActionSpec]:
    opts, group = [], None
    for i, a in enumerate(ACTIONS, 1):
        if a.group != group and opts:
            opts.append(None)
        group = a.group
        opts.append((str(i), f"{a.group} · {a.label}", a.hint))
    k = menu("Ajouter une étape", opts + [None, ("0", "Retour", "")])
    return None if k == "0" else ACTIONS[int(k) - 1]


def _ask_number(msg: str, n: int) -> Optional[int]:
    r = ask(f"  {msg} [magenta]›[/] ").strip()
    if r.isdigit() and 1 <= int(r) <= n:
        return int(r) - 1
    if r:
        console.print("[red]Numéro hors liste.[/]")
        time.sleep(0.6)
    return None


def edit_script(app, sc: Script) -> bool:
    """Éditeur d'un script. Renvoie True s'il a été enregistré."""
    original = dump_script(sc)
    saved = False
    while True:
        app.clear()
        console.print(Panel(Group(Text.assemble(("Fichier : ", "dim"),
                                                (sc.path or os.path.join(SCRIPTS_DIR, sc.name + SCRIPT_EXT), "")),
                                  Text.assemble(("Lancement : ", "dim"), (f"musitag -s {sc.name}", "bold"),
                                                ("   (depuis le dossier d'un album ; -r pour tous les albums "
                                                 "d'un dossier ; ou le chemin d'un morceau)", "dim")),
                                  steps_table(sc)),
                            title=Text(f" Script « {sc.name} » ", style="bold"), title_align="left",
                            border_style="magenta", box=box.ROUNDED, padding=(0, 1)))
        dirty = dump_script(sc) != original
        opts = [("a", "Ajouter une étape", "")]
        if sc.steps:
            opts += [("m", "Modifier une étape", ""), ("d", "Déplacer une étape", ""), ("x", "Supprimer une étape", "")]
        opts += [("v", "Verbeux : " + ("oui → non" if sc.verbose else "non → oui"), "il raconte tout en temps réel"),
                 None, ("e", "Enregistrer", "modifications non enregistrées" if dirty else ""), ("0", "Retour", "")]
        try:
            k = menu("Modifier le script", opts)
        except Cancel:
            k = "0"
        try:
            if k == "a":
                spec = pick_action()
                if spec:
                    st = configure_step(spec)
                    if st:
                        sc.steps.append(st)
            elif k == "m":
                i = _ask_number("Numéro de l'étape à modifier", len(sc.steps))
                if i is not None:
                    st = configure_step(sc.steps[i].spec, sc.steps[i])
                    if st:
                        sc.steps[i] = st
            elif k == "d":
                i = _ask_number("Numéro de l'étape à déplacer", len(sc.steps))
                if i is not None:
                    j = _ask_number("Nouvelle position", len(sc.steps))
                    if j is not None:
                        sc.steps.insert(j, sc.steps.pop(i))
            elif k == "x":
                i = _ask_number("Numéro de l'étape à supprimer", len(sc.steps))
                if i is not None and confirm(f"Supprimer l'étape {i + 1} ({sc.steps[i].spec.label}) ?", True):
                    sc.steps.pop(i)
            elif k == "v":
                sc.verbose = not sc.verbose
            elif k == "e":
                path = save_script(sc)
                original, saved = dump_script(sc), True
                info(f"✓ Enregistré : [bold]{escape(path)}[/]", "green")
                time.sleep(0.8)
            elif k == "0":
                if dump_script(sc) != original and confirm("Enregistrer les modifications ?", True):
                    save_script(sc)
                    saved = True
                return saved
        except Cancel:
            continue
        except OSError as e:
            info(f"Enregistrement impossible : {escape(str(e))}", "red")
            pause()


def _ask_new_name(prefill: str = "") -> Optional[str]:
    console.print("[dim]  Lettres, chiffres, - et _ (les espaces deviennent des -). Ex : album-propre[/]")
    name = clean_script_name(ask("  [bold]Nom du script[/] [magenta]›[/] ", prefill=prefill))
    if not name:
        return None
    if os.path.exists(os.path.join(SCRIPTS_DIR, name + SCRIPT_EXT)):
        info(f"Un script « {escape(name)} » existe déjà.", "red")
        pause()
        return None
    return name


def run_from_menu(app, sc: Script, single: Optional[str] = None, simulation: bool = False) -> None:
    app.clear()
    try:
        targets = [(os.path.dirname(single), [single], True)] if single else \
            collect_targets(app.folder, app.recursive)
        run_script(sc, targets, sc.verbose, simulation, root=single or app.folder)
    except ScriptError as e:
        info(escape(str(e)), "red")
    except Cancel:
        console.print("[yellow]Annulé.[/]")
    pause()
    app.rescan()


def script_screen(app, name: str) -> None:
    while True:
        path = os.path.join(SCRIPTS_DIR, name + SCRIPT_EXT)
        app.screen()
        try:
            sc = load_script(path)
        except ScriptError as e:
            info(f"Ce script est illisible :\n{escape(str(e))}\n\n[dim]Fichier : {escape(path)}[/]", "red")
            k = menu(f"Script « {name} »", [("7", "Supprimer ce script", ""), ("0", "Retour", "")])
            if k == "7" and confirm(f"Supprimer « {escape(name)} » ?", False):
                os.remove(path)
            return
        console.print(steps_table(sc))
        where = app.folder + (" (et sous-dossiers : un album par dossier)" if app.recursive else "")
        k = menu(f"Script « {name} »", [
            ("1", "Lancer sur ce dossier", where),
            ("2", "Lancer sur un morceau", "choisir le fichier"),
            ("3", "Simulation sur ce dossier", "bilan sans rien écrire"),
            None,
            ("4", "Modifier", ""),
            ("5", "Dupliquer", ""),
            ("6", "Renommer", ""),
            ("7", "Supprimer", ""),
            ("0", "Retour", f"en ligne de commande : musitag -s {name}"),
        ])
        if k == "0":
            return
        if k == "1":
            run_from_menu(app, sc)
        elif k == "2":
            idx = pick_file(app)
            if idx is not None:
                run_from_menu(app, sc, single=app.files[idx].path)
        elif k == "3":
            run_from_menu(app, sc, simulation=True)
        elif k == "4":
            edit_script(app, sc)
        elif k == "5":
            new = _ask_new_name(name + "-copie")
            if new:
                sc.name, sc.path = new, ""
                save_script(sc)
                name = new
        elif k == "6":
            new = _ask_new_name(name)
            if new:
                old = path
                sc.name, sc.path = new, ""
                save_script(sc)
                os.remove(old)
                name = new
        elif k == "7":
            if confirm(f"Supprimer définitivement le script « {escape(name)} » ?", False):
                os.remove(path)
                return


def scripts_menu(app) -> None:
    while True:
        app.screen()
        names = list_scripts()
        opts = []
        for i, n in enumerate(names, 1):
            try:
                summary = load_script(os.path.join(SCRIPTS_DIR, n + SCRIPT_EXT)).summary()
            except ScriptError:
                summary = "⚠ illisible"
            opts.append((str(i), n, summary))
        if not names:
            console.print(Panel(Text.from_markup(
                "Un script enchaîne des actions sur un album : paroles, pochette, tags, renommage, "
                "nettoyage, découpage…\nTout est préparé, puis un bilan et [bold]une seule confirmation[/] "
                "avant d'écrire.\nEnsuite, depuis le dossier d'un album : [bold]musitag -s NOM[/]"),
                title=Text(" Scripts ", style="bold"), title_align="left", border_style="cyan", box=box.ROUNDED))
        k = menu("Scripts", opts + [None, ("n", "Nouveau script", ""), ("0", "Retour", "")])
        if k == "0":
            return
        if k == "n":
            name = _ask_new_name()
            if name:
                sc = Script(name)
                edit_script(app, sc)
            continue
        script_screen(app, names[int(k) - 1])


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
                    ("s", "Scripts", "enchaîner des actions automatiquement (musitag -s NOM)"),
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
                elif key == "s":
                    scripts_menu(self)
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
               "  musitag -r Pixies    avec les sous-dossiers\n\n"
               "scripts (créés dans musitag, touche s) :\n"
               "  musitag -s album-propre                 l'album du dossier courant\n"
               "  musitag -s album-propre CHEMIN          un autre album, ou un seul morceau\n"
               "  musitag -s album-propre -r CHEMIN       chaque album sous ce dossier\n"
               "  musitag -s album-propre --simulation    le bilan, sans rien écrire",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dossier", nargs="?", default=None, metavar="CHEMIN",
                    help=f"dossier à ouvrir (défaut : {MUSIC_ROOT}) ; avec -s : album, dossier ou morceau "
                         "(défaut : dossier courant)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("-r", "--recursif", action="store_true", help="inclure les sous-dossiers")
    g.add_argument("-n", "--non-recursif", action="store_true", help="ce dossier uniquement (défaut)")
    ap.add_argument("-s", "--script", metavar="NOM",
                    help="lance un script (.mst) : son nom, ou le chemin du fichier")
    ap.add_argument("-v", "--verbeux", action="store_true", help="avec -s : raconte tout en temps réel")
    ap.add_argument("--simulation", action="store_true", help="avec -s : le bilan, sans rien écrire")
    ap.add_argument("--version", action="version", version=f"musitag {VERSION}")
    args = ap.parse_args()
    if (args.verbeux or args.simulation) and not args.script:
        ap.error("-v et --simulation vont avec -s NOM")
    if args.script:
        try:
            code = script_cli(args.script, args.dossier, args.recursif, args.verbeux, args.simulation)
        except (KeyboardInterrupt, Cancel):
            console.print("\n[yellow]Interrompu.[/]")
            code = 1
        sys.exit(code)

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
