"""
Pruebas de la política de anonimización con DICOM sintéticos (sin datos reales).
Synthetic-DICOM tests of the anonymization policy; no real data involved.

Ejecutar / run:  uv run --with pytest --with pydicom --with numpy pytest -q pruebas
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

RAIZ = Path(__file__).resolve().parents[1] / "herramientas"


def cargar(nombre):
    spec = importlib.util.spec_from_file_location(nombre, RAIZ / f"{nombre}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


anon = cargar("anonimizar")
cotejar = cargar("cotejar")


def ct_sintetico(ruta: Path, **extra) -> Path:
    """Un CT 32x32 con identidad sintética y etiquetas privadas; nada real."""
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = extra.pop("SOPClassUID", "1.2.840.10008.5.1.4.1.1.2")
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.SourceApplicationEntityTitle = "ZZ_AE"
    ds.SOPClassUID = ds.file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.Modality = extra.pop("Modality", "CT")
    ds.PatientName = "ZZ^PERSONA"
    ds.PatientID = "ZZ-0001"
    ds.IssuerOfPatientID = "ZZ-HOSP"
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "O"
    ds.PatientAge = "095Y"
    ds.PatientWeight = "70"
    ds.InstitutionName = "ZZ Hospital"
    ds.OperatorsName = "ZZ^OPERADOR"
    ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = "20200102"
    ds.StudyTime = ds.SeriesTime = ds.AcquisitionTime = "101010"
    ds.StudyInstanceUID = extra.pop("StudyInstanceUID", generate_uid())
    ds.SeriesInstanceUID = extra.pop("SeriesInstanceUID", generate_uid())
    ds.FrameOfReferenceUID = generate_uid()
    ds.SeriesNumber = 2
    ds.InstanceNumber = extra.pop("InstanceNumber", 1)
    ds.ImagePositionPatient = [0.0, 0.0, float(extra.pop("z", 0.0))]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.SliceThickness = 1.0
    ds.StudyDescription = "ZZ PERSONA estudio"
    ds.ImageComments = "ZZ comentario"
    ds.Rows = ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.RescaleIntercept = -1024
    ds.RescaleSlope = 1
    ds.PixelData = np.random.default_rng(int(ds.InstanceNumber)).integers(-1000, 1000, (32, 32), dtype=np.int16).tobytes()
    ds.add_new((0x0009, 0x0010), "LO", "ZZ PRIVADO")
    ds.add_new((0x0009, 0x1001), "LO", "ZZ valor privado")
    for k, v in extra.items():
        setattr(ds, k, v)
    ds.preamble = b"ZZ PREAMBULO".ljust(128, b"\x00")
    ds.save_as(ruta, enforce_file_format=True)
    return ruta


@pytest.fixture
def caso(tmp_path):
    entrada = tmp_path / "originales" / "caso"
    entrada.mkdir(parents=True)
    suid, stuid = generate_uid(), generate_uid()
    for i in range(3):
        ct_sintetico(entrada / f"IM{i}", InstanceNumber=i + 1, z=float(i), SeriesInstanceUID=suid, StudyInstanceUID=stuid)
    return tmp_path, entrada


def limpiar(tmp_path, entrada, salida="limpios"):
    return anon.main(["limpiar", str(entrada), str(tmp_path / salida)]), tmp_path / salida


def test_limpieza_elimina_identidad_y_pasa_la_compuerta(caso):
    tmp, entrada = caso
    codigo, salida = limpiar(tmp, entrada)
    assert codigo == 0
    limpios = list(salida.rglob("*.dcm"))
    assert len(limpios) == 3
    for f in limpios:
        raw = f.read_bytes()
        assert b"ZZ" not in raw                      # ninguna cadena sintética sobrevive
        assert raw[:128] == b"\x00" * 128
        ds = pydicom.dcmread(f)
        assert str(ds.PatientName) == str(ds.PatientID) == "ANON-0001"
        assert str(ds.PatientBirthDate) == ""
        assert str(ds.PatientAge) == "090Y"
        assert not any(e.tag.is_private for e in ds.iterall())
        assert "SourceApplicationEntityTitle" not in ds.file_meta
    assert anon.main(["verificar", str(salida)]) == 0


def test_cotejo_exacto_y_reimportacion_idempotente(caso):
    tmp, entrada = caso
    _, salida = limpiar(tmp, entrada)
    assert cotejar.main([str(entrada), str(salida)]) == 0
    assert anon.main(["limpiar", str(entrada), str(salida)]) == 0   # segunda corrida: duplicados, nada nuevo
    assert len(list(salida.rglob("*.dcm"))) == 3


def test_bytes_sobrantes_son_rechazados(caso):
    tmp, entrada = caso
    _, salida = limpiar(tmp, entrada)
    f = next(salida.rglob("*.dcm"))
    with open(f, "ab") as h:
        h.write(b"ZZPERSN")
    assert anon.main(["verificar", str(salida)]) == 1
    assert cotejar.main([str(entrada), str(salida)]) == 1
    assert anon.main(["limpiar", str(entrada), str(salida)]) == 1   # cuarentena, nunca "actualización"


def test_actualizacion_de_version_en_el_sitio(caso):
    tmp, entrada = caso
    _, salida = limpiar(tmp, entrada)
    for f in salida.rglob("*.dcm"):
        ds = pydicom.dcmread(f)
        ds.file_meta.ImplementationVersionName = "ANONIMIZAR_0.4.2"
        ds.save_as(f, enforce_file_format=True)
    assert cotejar.main([str(entrada), str(salida)]) == 0            # tolera la versión antigua
    assert anon.main(["limpiar", str(entrada), str(salida)]) == 0    # y la actualiza en el sitio
    assert {str(pydicom.dcmread(f).file_meta.ImplementationVersionName) for f in salida.rglob("*.dcm")} == {"PETCTLOCAL_0.4"}


def test_gemelos_con_pixeles_iguales_son_instancias_distintas(tmp_path):
    entrada = tmp_path / "originales"
    entrada.mkdir()
    suid, stuid = generate_uid(), generate_uid()
    for i in range(2):
        ct_sintetico(entrada / f"IM{i}", InstanceNumber=1, z=0.0, SeriesInstanceUID=suid, StudyInstanceUID=stuid)
    codigo, salida = limpiar(tmp_path, entrada)
    assert codigo == 0
    limpios = list(salida.rglob("*.dcm"))
    assert len(limpios) == 2
    limpios[0].unlink()
    assert cotejar.main([str(entrada), str(salida)]) == 1


def test_entrada_y_salida_solapadas_se_rechazan(caso):
    tmp, entrada = caso
    assert anon.main(["limpiar", str(entrada.parent), str(entrada.parent / "limpios")]) == 2


def test_entrada_ya_anonimizada_no_recibe_otro_seudonimo(caso):
    tmp, entrada = caso
    _, salida = limpiar(tmp, entrada)
    assert anon.main(["limpiar", str(salida), str(tmp / "limpios2")]) == 0
    assert not list((tmp / "limpios2").rglob("*.dcm"))


def test_objetos_no_soportados_van_a_cuarentena(tmp_path):
    entrada = tmp_path / "originales"
    entrada.mkdir()
    ct_sintetico(entrada / "IM0", InstanceNumber=1)
    ct_sintetico(entrada / "SR0", InstanceNumber=2, SOPClassUID="1.2.840.10008.5.1.4.1.1.88.11", Modality="SR")
    codigo, salida = limpiar(tmp_path, entrada)
    assert codigo == 1
    assert len(list(salida.rglob("*.dcm"))) == 1
