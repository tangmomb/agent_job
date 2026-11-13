"""Lettre de motivation en PDF, pour les formulaires qui demandent un fichier (LinkedIn : « Cover letter »).

PDF texte minimal écrit à la main (aucune dépendance) : A4, Helvetica 11 pt, accents français via WinAnsiEncoding,
retour à la ligne d'après la largeur réelle des caractères, plusieurs pages si besoin.
"""
from __future__ import annotations

from pathlib import Path

PAGE_W, PAGE_H = 595, 842  # A4 en points
MARGIN = 72  # 2,5 cm
SIZE, LEADING = 11, 15.5
MAX_W = PAGE_W - 2 * MARGIN

# Largeurs Helvetica (millièmes de la taille du corps), codes WinAnsi 32 à 255
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556,
      556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667,
      556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556,
      556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722,
      500, 500, 500, 334, 260, 334, 584, 350, 556, 350, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350,
      611, 350, 350, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 350, 500, 667, 278, 333, 556, 556,
      556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 556, 537, 278, 333, 333,
      365, 556, 834, 834, 834, 611, 667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,
      722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556,
      889, 500, 556, 556, 556, 556, 278, 278, 278, 278, 556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556,
      556, 500, 556, 500]


def _encode(text: str) -> bytes:
    text = text.replace(" ", " ").replace(" ", " ").replace("‑", "-")
    return text.encode("cp1252", errors="replace")


def _width(data: bytes) -> float:
    return sum(_W[b - 32] if 32 <= b <= 255 else 556 for b in data) * SIZE / 1000


def _wrap(text: str) -> list[bytes]:
    lines: list[bytes] = []
    for para in text.replace("\r\n", "\n").split("\n"):
        line = b""
        for word in _encode(para).split(b" "):
            cand = line + b" " + word if line else word
            if line and _width(cand) > MAX_W:
                lines.append(line)
                line = word
            else:
                line = cand
        lines.append(line)
    return lines


def _escape(data: bytes) -> bytes:
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def write_letter_pdf(text: str, path: Path, title: str = "Lettre de motivation") -> Path:
    lines = _wrap(text.strip())
    per_page = int((PAGE_H - 2 * MARGIN) // LEADING)
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    objs: list[bytes] = []  # objets 1..n
    n_pages = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for page in pages:
        body = [f"BT /F1 {SIZE} Tf {LEADING} TL {MARGIN} {PAGE_H - MARGIN - SIZE} Td".encode()]
        body += [b"(" + _escape(line) + b") Tj T*" for line in page]
        body.append(b"ET")
        stream = b"\n".join(body)
        n = len(objs) + 1
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {n + 1} 0 R >>".encode())
        objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objs.append(b"<< /Title (" + _escape(_encode(title)) + b") >>")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {len(objs)} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path
