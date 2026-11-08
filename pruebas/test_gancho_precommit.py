"""
El gancho de pre-commit inspecciona los bytes del índice de git, con rutas separadas por NUL.
The pre-commit hook inspects git index bytes, NUL-separated paths.

En Windows el gancho se ejecuta con el propio git (`git hook run`), que usa el bash de Git for Windows; nunca con un
`bash.exe` cualquiera del PATH (el de WSL en System32 devuelve 1 sin ejecutar nada). Si git es anterior a 2.36, se
localiza el bash de Git a partir de `git --exec-path`. Los fallos muestran el comando, la ruta de bash, stdout y stderr.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
GANCHOS = RAIZ / ".githooks"
GANCHO = GANCHOS / "pre-commit"
DICOM_FALSO = b"\x00" * 128 + b"DICM" + b"\x02\x00\x10\x00UI\x04\x001.2\x00" + b"\x00" * 64
MENSAJE_DICOM = "DICOM preparado para commit"
MENSAJE_RECHAZO = "Commit rechazado"


def git_version() -> tuple[int, int]:
    salida = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    m = re.search(r"(\d+)\.(\d+)", salida)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def bash_de_git() -> str | None:
    """bash.exe de Git for Windows, derivado de `git --exec-path` (…/Git/mingw64/libexec/git-core)."""
    if platform.system() != "Windows":
        return shutil.which("bash")
    try:
        exec_path = Path(subprocess.run(["git", "--exec-path"], capture_output=True, text=True, check=True).stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        return None
    for raiz in [exec_path] + list(exec_path.parents):
        for cand in (raiz / "bin" / "bash.exe", raiz / "usr" / "bin" / "bash.exe"):
            if cand.is_file():
                return str(cand)
    return None


def repo_temporal(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    subprocess.run(["git", "config", "core.hooksPath", str(GANCHOS)], cwd=r, check=True)
    return r


def correr_gancho(r: Path) -> tuple[int, str]:
    """Ejecuta el gancho como lo haría git; devuelve (código, informe con comando, bash, stdout y stderr)."""
    if git_version() >= (2, 36):
        cmd = ["git", "hook", "run", "pre-commit"]
        bash = "(el shell de git)"
    else:
        bash = bash_de_git()
        if not bash:
            pytest.skip("sin bash de Git para ejecutar el gancho")
        cmd = [bash, GANCHO.as_posix() if platform.system() != "Windows" else str(GANCHO)]
    p = subprocess.run(cmd, cwd=r, capture_output=True, text=True, encoding="utf-8", errors="replace")
    retornos = GANCHO.read_bytes().count(b"\r")      # fuera del f-string: compatible con Python 3.10 y cuenta CR de verdad
    informe = (f"\ncomando: {cmd}\nbash: {bash}\ngancho: {GANCHO} (CR en el archivo: {retornos})\n"
               f"código: {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    return p.returncode, informe


def test_el_gancho_no_tiene_retornos_de_carro():
    assert GANCHO.read_bytes().count(b"\r") == 0, "el gancho tiene CRLF; .gitattributes debe fijar LF en .githooks/*"


def test_rechaza_dicom_con_espacios_en_el_nombre(tmp_path):
    r = repo_temporal(tmp_path)
    (r / "Casos S").mkdir()
    (r / "Casos S" / "IM 136").write_bytes(DICOM_FALSO)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    codigo, informe = correr_gancho(r)
    assert codigo != 0, informe
    assert MENSAJE_DICOM in informe and MENSAJE_RECHAZO in informe, informe


def test_inspecciona_el_indice_y_no_la_copia_de_trabajo(tmp_path):
    r = repo_temporal(tmp_path)
    f = r / "IM0"
    f.write_bytes(DICOM_FALSO)
    subprocess.run(["git", "add", "IM0"], cwd=r, check=True)
    f.write_text("texto inofensivo")          # la copia de trabajo cambia, el índice sigue teniendo el DICOM
    codigo, informe = correr_gancho(r)
    assert codigo != 0, informe
    assert MENSAJE_DICOM in informe and MENSAJE_RECHAZO in informe, informe


def test_rechaza_archivos_privados_por_nombre(tmp_path):
    r = repo_temporal(tmp_path)
    (r / "mapa_seudonimos.json").write_text("{}")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    codigo, informe = correr_gancho(r)
    assert codigo != 0, informe
    assert "archivo privado" in informe and MENSAJE_RECHAZO in informe, informe


def test_acepta_archivos_normales(tmp_path):
    r = repo_temporal(tmp_path)
    (r / "notas con espacio.md").write_text("# hola")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    codigo, informe = correr_gancho(r)
    assert codigo == 0, informe
    assert MENSAJE_RECHAZO not in informe, informe
