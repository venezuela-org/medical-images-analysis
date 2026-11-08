"""
Reglas de los adjuntos de una versión, con zips sintéticos. / Release-attachment rules, with synthetic zips.
"""
from __future__ import annotations

import importlib.util
import io
import zipfile
from pathlib import Path

import pydicom
import pytest

RAIZ = Path(__file__).resolve().parents[1]


def cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adj = cargar("comprobar_adjuntos", RAIZ / "pruebas" / "comprobar_adjuntos.py")
demo = cargar("demo", RAIZ / "herramientas" / "demo.py")
DICOM_FALSO = b"\x00" * 128 + b"DICM" + b"\x02\x00\x10\x00UI\x04\x001.2\x00" + b"\x00" * 64


@pytest.fixture
def demo_dir(tmp_path):
    d = tmp_path / "DEMO"
    demo.escribir_caso(d, 2, 1)
    return d


def zip_de(tmp_path, nombre, entradas):
    z = tmp_path / nombre
    with zipfile.ZipFile(z, "w") as f:
        for n, datos in entradas:
            f.writestr(n, datos)
    return z


def test_demo_limpia_se_acepta(tmp_path, demo_dir):
    z = zip_de(tmp_path, "datos-demo-sinteticos.zip", [(f"DEMO/{p.relative_to(demo_dir).as_posix()}", p.read_bytes()) for p in demo_dir.rglob("IM*")])
    assert adj.revisar_zip(z) == []


def test_dicom_en_paquete_de_app_se_rechaza(tmp_path):
    z = zip_de(tmp_path, "PET-CT-local-v0-linux.zip", [("PET-CT local/_internal/x/notas.txt", DICOM_FALSO)])
    assert any("contiene un DICOM" in p for p in adj.revisar_zip(z))


def test_carpeta_privada_en_la_raiz_se_rechaza(tmp_path):
    z = zip_de(tmp_path, "PET-CT-local-v0-linux.zip", [("PRIVADO/secreto.json", b"{}"), ("x\\limpios_PRIVADO\\mapa.json", b"{}")])
    problemas = adj.revisar_zip(z)
    assert sum("privado" in p for p in problemas) == 2


def test_entrada_repetida_se_rechaza(tmp_path):
    z = zip_de(tmp_path, "PET-CT-local-v0-linux.zip", [("a/notas.txt", DICOM_FALSO), ("a/notas.txt", b"texto")])
    problemas = adj.revisar_zip(z)
    assert any("repetida" in p for p in problemas) and any("contiene un DICOM" in p for p in problemas)


def test_demo_con_etiqueta_privada_ajena_se_rechaza(tmp_path, demo_dir):
    f = next(demo_dir.rglob("IM0"))
    ds = pydicom.dcmread(f)
    ds.add_new((0x0011, 0x1001), "LO", "dato ajeno sin creador")
    ds.save_as(f, enforce_file_format=True)
    z = zip_de(tmp_path, "datos-demo-sinteticos.zip", [(f"DEMO/{p.relative_to(demo_dir).as_posix()}", p.read_bytes()) for p in demo_dir.rglob("IM*")])
    assert any("etiquetas privadas ajenas" in p for p in adj.revisar_zip(z))


def test_demo_con_identidad_distinta_se_rechaza(tmp_path, demo_dir):
    f = next(demo_dir.rglob("IM0"))
    ds = pydicom.dcmread(f)
    ds.PatientName = "OTRO^NOMBRE"
    ds.StudyDescription = "DEMO^PACIENTE^SINTETICO DEMO SINTETICA"   # las cadenas en otro campo no deben bastar
    ds.save_as(f, enforce_file_format=True)
    z = zip_de(tmp_path, "datos-demo-sinteticos.zip", [(f"DEMO/{p.relative_to(demo_dir).as_posix()}", p.read_bytes()) for p in demo_dir.rglob("IM*")])
    assert any("identidad sintética exacta" in p for p in adj.revisar_zip(z))
