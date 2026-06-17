#!/usr/bin/env python3
"""
phraks_auto — Récupère les numéros de Phrack, les convertit en PDF optimisés
pour la reMarkable et les envoie via le cloud (rmapi).

Organisation sur la reMarkable :
  - CYBER/PHRAKS          : articles du DERNIER numéro paru
  - CYBER/PHRAKS/OLD/<N>  : articles des numéros précédents (un dossier par num.)

Quand un nouveau numéro paraît, les articles présents dans CYBER/PHRAKS sont
déplacés dans CYBER/PHRAKS/OLD/<ancien_numéro>, puis les nouveaux articles sont
déposés dans CYBER/PHRAKS.

Format : PDF généré sans dépendance (police Courier, standard PDF). Chaque page
est dimensionnée individuellement pour afficher son contenu à la plus grande
police possible sans coupure ni ré-enroulement — idéal pour le code / l'art
ASCII 80 colonnes de Phrack sur un petit écran.

Pas de traduction : texte d'origine (anglais).

Dépendances : Python >= 3.9 (stdlib) + rmapi (https://github.com/ddvk/rmapi).
"""

from __future__ import annotations

import argparse
import html
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

PHRACK_BASE = "https://phrack.org"
USER_AGENT = "phraks_auto/2.0 (+https://phrack.org)"

RM_DIR = os.environ.get("PHRAKS_RM_DIR", "CYBER/PHRAKS")
RM_OLD_DIR = f"{RM_DIR}/OLD"

STATE_FILE = Path(
    os.environ.get(
        "PHRAKS_STATE_FILE",
        Path(__file__).resolve().parent / "state" / "last_issue.txt",
    )
)

RMAPI = os.environ.get("PHRAKS_RMAPI", "rmapi")

log = logging.getLogger("phraks_auto")


# --------------------------------------------------------------------------- #
# Modèle
# --------------------------------------------------------------------------- #


@dataclass
class Article:
    issue: int
    phile: int
    title: str
    author: str
    text: str

    @property
    def display_title(self) -> str:
        return f"#{self.phile:02d} - {self.title}"


def issue_filename(issue: int) -> str:
    """Nom du document = nom affiché sur la reMarkable (tri correct via 0-pad)."""
    return f"Phrack {issue:02d}.pdf"


# --------------------------------------------------------------------------- #
# HTTP / Phrack
# --------------------------------------------------------------------------- #


def _get(url: str) -> bytes:
    log.debug("GET %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def latest_issue_number() -> int:
    page = _get(f"{PHRACK_BASE}/index_latest.html").decode("utf-8", "replace")
    m = re.search(r"issues/(\d+)/\d+\.html", page)
    if not m:
        raise RuntimeError("Impossible de détecter le dernier numéro de Phrack")
    return int(m.group(1))


def fetch_titles(issue: int) -> dict[int, tuple[str, str]]:
    """Titres + auteurs propres depuis la table d'index du numéro sur le web.
    Source fiable pour TOUS les numéros (les vieux .txt n'ont pas de bandeau
    machine-lisible). Retourne {phile: (titre, auteur)} ; {} si indisponible."""
    try:
        page = _get(f"{PHRACK_BASE}/issues/{issue}/1.html").decode(
            "utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        log.debug("Index web du numéro %d indisponible (%s)", issue, exc)
        return {}
    pat = re.compile(
        rf'issues/{issue}/(\d+)\.html[^"]*">([^<]+)</a>\s*</td>\s*'
        r'<td[^>]*>([^<]*)</td>'
    )
    out: dict[int, tuple[str, str]] = {}
    for m in pat.finditer(page):
        phile = int(m.group(1))
        title = html.unescape(m.group(2)).strip()
        author = html.unescape(m.group(3)).strip() or "Phrack"
        if title:
            out.setdefault(phile, (title, author))
    return out


def download_issue(issue: int) -> list[Article]:
    url = f"{PHRACK_BASE}/archives/tgz/phrack{issue}.tar.gz"
    log.info("Téléchargement du numéro %d : %s", issue, url)
    raw = _get(url)
    web_titles = fetch_titles(issue)

    articles: list[Article] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = os.path.basename(member.name)
            m = re.fullmatch(r"(\d+)\.txt", name)
            if not member.isfile() or not m:
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            text = fh.read().decode("utf-8", "replace")
            phile = int(m.group(1))
            if phile in web_titles:
                title, author = web_titles[phile]
            else:
                title, author = _parse_meta(text, issue, phile)
            articles.append(Article(issue, phile, title, author, text))

    articles.sort(key=lambda a: a.phile)
    log.info("%d articles extraits du numéro %d", len(articles), issue)
    return articles


def _parse_meta(text: str, issue: int, phile: int) -> tuple[str, str]:
    banners = re.findall(r"=\[\s*(.*?)\s*\]=", text[:2000])
    banners = [b.strip() for b in banners if b.strip()]
    title = banners[0] if banners else f"Phrack {issue} - {phile}"
    author = banners[1] if len(banners) > 1 else "Phrack"
    author = re.sub(r"\s*\(.*?\)", "", author).strip() or "Phrack"
    return title, author


# --------------------------------------------------------------------------- #
# Génération PDF (stdlib uniquement, police Courier standard)
# --------------------------------------------------------------------------- #

# Géométrie page : ratio reMarkable (portrait), en points PostScript (1/72").
PAGE_W = 447.36
PAGE_H = 596.46
MARGIN_X = 16.0
MARGIN_TOP = 18.0
MARGIN_BOT = 26.0          # place pour le numéro de page
FONT_CAP = 12.0            # taille max (pages étroites -> gros texte)
FONT_MIN = 4.5            # garde-fou
COURIER_ADVANCE = 0.60     # largeur d'un glyphe Courier en em
LINE_FACTOR = 1.22         # interligne
FOOTER_SIZE = 7.0

CONTENT_W = PAGE_W - 2 * MARGIN_X
CONTENT_H = PAGE_H - MARGIN_TOP - MARGIN_BOT


def _pdf_escape(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _font_for_width(width: int) -> float:
    """Plus grande police (<= cap) telle qu'une ligne de `width` car. tienne."""
    if width <= 0:
        return FONT_CAP
    fit = CONTENT_W / (width * COURIER_ADVANCE)
    return max(FONT_MIN, min(FONT_CAP, fit))


def _page_font(lines: list[str]) -> float:
    return _font_for_width(max((len(ln) for ln in lines), default=0))


def _paginate(lines: list[str]) -> list[list[str]]:
    """Remplit chaque page au maximum selon sa propre police : les pages
    étroites (gros texte) ont moins de lignes, les pages larges (petit texte)
    en ont davantage. Jamais de débordement vertical."""
    pages: list[list[str]] = []
    i = 0
    n = len(lines)
    while i < n:
        j = i
        maxw = 0
        while j < n:
            w = max(maxw, len(lines[j]))
            font = _font_for_width(w)
            capacity = max(1, int(CONTENT_H // (font * LINE_FACTOR)))
            if (j - i + 1) > capacity:
                break
            maxw = w
            j += 1
        if j == i:            # ligne unique trop grande : on la force seule
            j = i + 1
        pages.append(lines[i:j])
        i = j
    return pages or [[""]]


def _page_stream(page_lines: list[str], idx: int, total: int) -> bytes:
    size = _page_font(page_lines)
    lead = size * LINE_FACTOR
    y = PAGE_H - MARGIN_TOP - size
    parts = [f"BT\n/F1 {size:.2f} Tf\n{lead:.2f} TL\n{MARGIN_X:.2f} {y:.2f} Td\n"]
    for ln in page_lines:
        parts.append(f"({_pdf_escape(ln)}) Tj\nT*\n")
    parts.append("ET\n")
    footer = f"{idx + 1} / {total}"
    fx = (PAGE_W - len(footer) * FOOTER_SIZE * COURIER_ADVANCE) / 2
    parts.append(
        f"BT\n/F1 {FOOTER_SIZE:.2f} Tf\n{fx:.2f} {MARGIN_BOT - 12:.2f} Td\n"
        f"({_pdf_escape(footer)}) Tj\nET\n"
    )
    return "".join(parts).encode("latin-1", "replace")


def build_issue_pdf(issue: int, articles: list[Article], out_path: Path) -> None:
    """Un seul PDF pour tout le numéro. Chaque article démarre sur une nouvelle
    page et dispose d'un signet (table des matières PDF) pour la navigation."""
    all_pages: list[list[str]] = []
    bookmarks: list[tuple[str, int]] = []   # (titre, index de page de départ)
    for art in articles:
        lines = art.text.expandtabs(8).replace("\r", "").split("\n")
        bookmarks.append((art.display_title, len(all_pages)))
        all_pages.extend(_paginate(lines))
    if not all_pages:
        all_pages = [[""]]
    total = len(all_pages)
    n_art = len(bookmarks)

    # Plan des numéros d'objets (tous contigus 1..n_obj) :
    #   1 catalog, 2 pages, 3 font, 4 outlines,
    #   5 .. 4+n_art : items de signet,
    #   puis pour chaque page : (page, contenu).
    outline_ids = [5 + i for i in range(n_art)]
    first_page = 5 + n_art
    page_ids = [first_page + 2 * i for i in range(total)]
    content_ids = [first_page + 2 * i + 1 for i in range(total)]
    n_obj = 4 + n_art + 2 * total

    objs: dict[int, bytes] = {}
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[1] = (b"<< /Type /Catalog /Pages 2 0 R /Outlines 4 0 R "
               b"/PageMode /UseOutlines >>")
    objs[2] = f"<< /Type /Pages /Count {total} /Kids [{kids}] >>".encode()
    objs[3] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
               b"/Encoding /WinAnsiEncoding >>")
    if n_art:
        objs[4] = (f"<< /Type /Outlines /First {outline_ids[0]} 0 R "
                   f"/Last {outline_ids[-1]} 0 R /Count {n_art} >>").encode()
    else:
        objs[4] = b"<< /Type /Outlines /Count 0 >>"

    for k, (title, start) in enumerate(bookmarks):
        prev = f"/Prev {outline_ids[k - 1]} 0 R " if k > 0 else ""
        nxt = f"/Next {outline_ids[k + 1]} 0 R " if k < n_art - 1 else ""
        objs[outline_ids[k]] = (
            f"<< /Title ({_pdf_escape(title)}) /Parent 4 0 R "
            f"{prev}{nxt}/Dest [{page_ids[start]} 0 R /Fit] >>"
        ).encode("latin-1", "replace")

    for i, page_lines in enumerate(all_pages):
        stream = _page_stream(page_lines, i, total)
        objs[content_ids[i]] = (f"<< /Length {len(stream)} >>\nstream\n".encode()
                                + stream + b"\nendstream")
        objs[page_ids[i]] = (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_ids[i]} 0 R >>"
        ).encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (n_obj + 1)
    for i in range(1, n_obj + 1):
        offsets[i] = len(out)
        out += f"{i} 0 obj\n".encode() + objs[i] + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {n_obj + 1}\n".encode() + b"0000000000 65535 f \n"
    for i in range(1, n_obj + 1):
        out += f"{offsets[i]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {n_obj + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out)
    log.debug("Phrack %d -> %s : %d pages, %d articles",
              issue, out_path.name, total, n_art)


def build_issue(issue: int, articles: list[Article], outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / issue_filename(issue)
    build_issue_pdf(issue, articles, path)
    return path


# --------------------------------------------------------------------------- #
# reMarkable via rmapi
# --------------------------------------------------------------------------- #


def _rmapi(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([RMAPI, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"rmapi {' '.join(args)} a échoué ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def rmapi_available() -> bool:
    return shutil.which(RMAPI) is not None


def ensure_dir(path: str) -> None:
    """Crée un chemin reMarkable récursivement (mkdir niveau par niveau)."""
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        _rmapi("mkdir", "/".join(parts[:i]), check=False)


def rm_list_files(directory: str) -> list[str]:
    proc = _rmapi("ls", directory, check=False)
    return [line[3:].strip()
            for line in proc.stdout.splitlines()
            if line.strip().startswith("[f]")]


def upload(files: list[Path], dest: str) -> None:
    ensure_dir(dest)
    for f in files:
        log.info("Upload %s -> %s", f.name, dest)
        proc = _rmapi("put", str(f), dest, check=False)
        if proc.returncode != 0 and "already exists" in (proc.stderr + proc.stdout):
            # Le document existe déjà sur le cloud : on remplace son contenu.
            log.info("%s existe déjà -> remplacement du contenu", f.name)
            _rmapi("put", "--content-only", str(f), dest)
        elif proc.returncode != 0:
            raise RuntimeError(
                f"rmapi put {f} {dest} a échoué ({proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}")


def archive_current() -> None:
    """Déplace le(s) document(s) actuel(s) de PHRAKS vers OLD/."""
    current = rm_list_files(RM_DIR)
    if not current:
        log.info("Rien à archiver dans %s", RM_DIR)
        return
    ensure_dir(RM_OLD_DIR)
    log.info("Archivage de %d document(s) -> %s", len(current), RM_OLD_DIR)
    for name in current:
        _rmapi("mv", f"{RM_DIR}/{name}", RM_OLD_DIR, check=False)


# --------------------------------------------------------------------------- #
# État
# --------------------------------------------------------------------------- #


def read_state() -> int | None:
    if STATE_FILE.exists():
        try:
            return int(STATE_FILE.read_text().strip())
        except ValueError:
            return None
    return None


def write_state(issue: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(str(issue))


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #


def run_latest(force: bool, no_upload: bool, outdir: Path | None) -> int:
    """Mode normal (systemd) : publie le dernier numéro s'il est nouveau."""
    issue = latest_issue_number()
    last = read_state()
    log.info("Dernier numéro Phrack : %d | dernier traité : %s", issue, last)

    if not force and last is not None and issue <= last:
        log.info("Rien de nouveau (numéro %d déjà traité).", issue)
        return 0

    articles = download_issue(issue)
    if not articles:
        log.warning("Aucun article pour le numéro %d", issue)
        return 1

    workdir = outdir or Path(tempfile.mkdtemp(prefix="phraks_"))
    pdf = build_issue(issue, articles, workdir)
    log.info("PDF du numéro %d généré : %s", issue, pdf)

    if no_upload:
        log.info("--no-upload : PDF conservé dans %s", workdir)
        return 0
    if not rmapi_available():
        log.error("rmapi introuvable. Installe-le et fais le pairing.")
        return 2

    if last is not None and issue > last:
        archive_current()
    upload([pdf], RM_DIR)
    write_state(issue)

    if outdir is None:
        shutil.rmtree(workdir, ignore_errors=True)
    log.info("Terminé : numéro %d publié dans %s.", issue, RM_DIR)
    return 0


def run_backfill(start: int, end: int, no_upload: bool,
                 outdir: Path | None) -> int:
    """Publie une plage de numéros (1 PDF/numéro) dans OLD/ (sans toucher à
    PHRAKS)."""
    if not no_upload and not rmapi_available():
        log.error("rmapi introuvable. Installe-le et fais le pairing.")
        return 2

    failures = []
    for issue in range(start, end + 1):
        try:
            articles = download_issue(issue)
        except Exception as exc:  # noqa: BLE001
            log.warning("Numéro %d ignoré (%s)", issue, exc)
            failures.append(issue)
            continue
        if not articles:
            log.warning("Numéro %d : aucun article", issue)
            continue

        base = outdir or Path(tempfile.mkdtemp(prefix=f"phraks_{issue}_"))
        pdf = build_issue(issue, articles, base)
        log.info("Numéro %d : PDF généré (%s)", issue, pdf.name)

        if not no_upload:
            upload([pdf], RM_OLD_DIR)
            log.info("Numéro %d publié dans %s", issue, RM_OLD_DIR)
        if outdir is None:
            shutil.rmtree(base, ignore_errors=True)

    if failures:
        log.warning("Numéros en échec : %s", failures)
    log.info("Backfill terminé (%d -> %d).", start, end)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phrack -> reMarkable (PDF)")
    parser.add_argument("--force", action="store_true",
                        help="retraite le dernier numéro même s'il est connu")
    parser.add_argument("--no-upload", action="store_true",
                        help="génère les PDF sans les envoyer")
    parser.add_argument("--out", type=Path, default=None,
                        help="dossier de sortie des PDF (sinon temporaire)")
    parser.add_argument("--backfill", metavar="A:B", default=None,
                        help="publie les numéros A à B (1 PDF/numéro) dans OLD "
                             "(ex: 1:71). 'all' = 1 jusqu'au dernier-1.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        if args.backfill:
            if args.backfill == "all":
                start, end = 1, latest_issue_number() - 1
            else:
                a, b = args.backfill.split(":")
                start, end = int(a), int(b)
            return run_backfill(start, end, args.no_upload, args.out)
        return run_latest(args.force, args.no_upload, args.out)
    except Exception as exc:  # noqa: BLE001
        log.error("Échec : %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
