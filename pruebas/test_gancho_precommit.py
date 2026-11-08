"""
El gancho de pre-commit inspecciona los bytes del índice de git, con rutas separadas por NUL.
The pre-commit hook inspects git index bytes, NUL-separated paths.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
GANCHO = RAIZ / ".githooks" / "pre-commit"
DICOM_FALSO = b"\x00" * 128 + b"DICM" + b"\x02\x00\x10\x00UI\x04\x001.2\x00" + b"\x00" * 64


def repo_temporal(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    return r


def correr_gancho(r: Path) -> int:
    return subprocess.run(["bash", str(GANCHO)], cwd=r, capture_output=True).returncode


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="requiere git y bash")
def test_rechaza_dicom_con_espacios_en_el_nombre(tmp_path):
    r = repo_temporal(tmp_path)
    (r / "Casos S").mkdir()
    (r / "Casos S" / "IM 136").write_bytes(DICOM_FALSO)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    assert correr_gancho(r) != 0


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="requiere git y bash")
def test_inspecciona_el_indice_y_no_la_copia_de_trabajo(tmp_path):
    r = repo_temporal(tmp_path)
    f = r / "IM0"
    f.write_bytes(DICOM_FALSO)
    subprocess.run(["git", "add", "IM0"], cwd=r, check=True)
    f.write_text("texto inofensivo")          # la copia de trabajo cambia, el índice sigue teniendo el DICOM
    assert correr_gancho(r) != 0


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="requiere git y bash")
def test_acepta_archivos_normales(tmp_path):
    r = repo_temporal(tmp_path)
    (r / "notas con espacio.md").write_text("# hola")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    assert correr_gancho(r) == 0
