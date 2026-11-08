"""
Ningún archivo del repositorio puede ser un DICOM, tenga la extensión que tenga. / No DICOM file may live in the repository.
"""
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
EXCLUIR = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}


def es_dicom(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            f.seek(128)
            return f.read(4) == b"DICM"
    except OSError:
        return False


def test_no_hay_dicom_en_el_repositorio():
    sospechosos = [str(p.relative_to(RAIZ)) for p in RAIZ.rglob("*")
                   if p.is_file() and not (set(p.parts) & EXCLUIR) and p.stat().st_size > 132 and es_dicom(p)]
    assert sospechosos == [], f"DICOM en el repositorio: {sospechosos}"
