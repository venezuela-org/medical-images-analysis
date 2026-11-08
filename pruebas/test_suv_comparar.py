"""
Completitud de la comparación SUV original / limpio, con PET sintéticos. / SUV comparison completeness tests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

RAIZ = Path(__file__).resolve().parents[1] / "herramientas"


def cargar(nombre):
    spec = importlib.util.spec_from_file_location(nombre, RAIZ / f"{nombre}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


anon = cargar("anonimizar")
suv = cargar("suv")


def pet_sintetico(ruta: Path, instancia: int, z: float, suid: str, stuid: str) -> Path:
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.128"
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID, ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPClassUID, ds.file_meta.MediaStorageSOPInstanceUID
    ds.Modality, ds.Manufacturer = "PT", "SYNTHETIC"
    ds.PatientName, ds.PatientID, ds.PatientBirthDate = "ZZ^PET", "ZZ-PET-1", "19700101"
    ds.PatientSex, ds.PatientWeight = "O", "70"
    ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = "20200102"
    ds.StudyTime = ds.SeriesTime = ds.AcquisitionTime = "110000"
    ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.FrameOfReferenceUID = stuid, suid, "1.2.3.4.5.6"
    ds.SeriesNumber, ds.InstanceNumber = 3, instancia
    ds.ImagePositionPatient, ds.ImageOrientationPatient = [0.0, 0.0, z], [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing, ds.SliceThickness = [4.0, 4.0], 4.0
    ds.Rows = ds.Columns = 16
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.RescaleSlope, ds.RescaleIntercept = 1.5, 0
    ds.Units, ds.DecayCorrection = "BQML", "ADMIN"
    rf = Dataset()
    rf.RadionuclideTotalDose, rf.RadionuclideHalfLife, rf.RadiopharmaceuticalStartTime = 370000000.0, 6586.2, "100000"
    ds.RadiopharmaceuticalInformationSequence = Sequence([rf])
    ds.PixelData = np.random.default_rng(instancia).integers(0, 5000, (16, 16), dtype=np.uint16).tobytes()
    ds.preamble = b"\x00" * 128
    ds.save_as(ruta, enforce_file_format=True)
    return ruta


@pytest.fixture
def caso_pet(tmp_path):
    entrada = tmp_path / "originales" / "caso"
    entrada.mkdir(parents=True)
    suid, stuid = generate_uid(), generate_uid()
    for i in range(4):
        pet_sintetico(entrada / f"IM{i}", i + 1, 4.0 * i, suid, stuid)
    assert anon.main(["limpiar", str(entrada), str(tmp_path / "limpios")]) == 0
    serie = next(p for p in (tmp_path / "limpios").rglob("serie_*") if p.is_dir())
    return tmp_path, entrada, serie


def test_comparacion_completa_es_identica(caso_pet, capsys):
    tmp, entrada, serie = caso_pet
    assert suv.main(["comparar", str(serie), "--json", str(tmp / "c.json")]) == 0
    assert "correspondencia completa: sí" in capsys.readouterr().out


def test_un_corte_limpio_borrado_rompe_la_correspondencia(caso_pet, capsys):
    tmp, entrada, serie = caso_pet
    next(serie.glob("*.dcm")).unlink()
    assert suv.main(["comparar", str(serie), "--json", str(tmp / "c.json")]) == 1
    out = capsys.readouterr().out
    assert "faltantes" in out and "correspondencia completa: NO" in out


def test_instance_number_cambiado_se_reporta(caso_pet, capsys):
    tmp, entrada, serie = caso_pet
    f = next(serie.glob("*.dcm"))
    ds = pydicom.dcmread(f)
    ds.InstanceNumber = 99
    ds.save_as(f, enforce_file_format=True)
    assert suv.main(["comparar", str(serie), "--json", str(tmp / "c.json")]) == 1
    assert "número(s) de instancia distinto(s)" in capsys.readouterr().out


def test_original_no_importado_cuenta_como_faltante(caso_pet, capsys):
    tmp, entrada, serie = caso_pet
    primero = pydicom.dcmread(next(entrada.glob("IM*")))
    pet_sintetico(entrada / "IM9", 5, 16.0, str(primero.SeriesInstanceUID), str(primero.StudyInstanceUID))
    assert suv.main(["comparar", str(serie), "--json", str(tmp / "c.json")]) == 1
    assert "1 corte(s) de la serie original sin archivo limpio" in capsys.readouterr().out


def test_un_voxel_alterado_se_detecta(caso_pet, capsys):
    tmp, entrada, serie = caso_pet
    f = next(serie.glob("*.dcm"))
    ds = pydicom.dcmread(f)
    a = ds.pixel_array.copy()
    a[3, 3] += 1
    ds.PixelData = a.tobytes()
    ds.save_as(f, enforce_file_format=True)
    assert suv.main(["comparar", str(serie), "--json", str(tmp / "c.json")]) == 1
    assert "1 vóxeles distintos" in capsys.readouterr().out


def corte_interior_por_posicion(serie: Path):
    """El corte del medio por posición física (ningún extremo), para que romper su número rompa el orden de verdad."""
    cortes = sorted(serie.glob("*.dcm"), key=lambda f: float(pydicom.dcmread(f, stop_before_pixels=True).ImagePositionPatient[2]))
    return cortes[len(cortes) // 2]


def test_orden_incoherente_en_ambos_lados_cambia_el_veredicto(caso_pet):
    """El mismo InstanceNumber alterado en un corte interior del original y del limpio: los píxeles siguen iguales y la
    correspondencia sigue completa, pero el orden no es coherente, así que el veredicto no puede ser IDENTICO."""
    import json
    tmp, entrada, serie = caso_pet
    indice = suv.cargar_indice(tmp / "limpios_PRIVADO", tmp / "limpios")
    limpio = corte_interior_por_posicion(serie)
    original = Path(indice[str(limpio.resolve())])
    for f in (limpio, original):
        ds = pydicom.dcmread(f)
        ds.InstanceNumber = 99
        ds.save_as(f, enforce_file_format=True)
    assert suv.main(["comparar", str(serie), "--json", str(tmp / "c.json")]) == 1
    r = json.loads((tmp / "c.json").read_text(encoding="utf-8"))
    assert r["correspondencia_completa"] is True
    assert r["pixeles_y_suv_identicos"] is True
    assert r["orden_y_geometria_ok"] is False and r["veredicto"] == "DIFERENCIAS"


def test_geometria_ausente_en_todos_los_cortes_cambia_el_veredicto(caso_pet):
    import json
    tmp, entrada, serie = caso_pet
    indice = suv.cargar_indice(tmp / "limpios_PRIVADO", tmp / "limpios")
    for limpio in serie.glob("*.dcm"):
        for f in (limpio, Path(indice[str(limpio.resolve())])):
            ds = pydicom.dcmread(f)
            del ds.ImagePositionPatient
            ds.save_as(f, enforce_file_format=True)
    assert suv.main(["comparar", str(serie), "--json", str(tmp / "c.json")]) == 1
    r = json.loads((tmp / "c.json").read_text(encoding="utf-8"))
    assert r["geometria_incompleta"] == 4 and r["orden_y_geometria_ok"] is False


def test_geometria_ausente_en_un_solo_corte_cambia_el_veredicto(caso_pet):
    """Un solo par sin posición basta: la geometría debe estar completa en cada corte, no solo en alguno."""
    import json
    tmp, entrada, serie = caso_pet
    indice = suv.cargar_indice(tmp / "limpios_PRIVADO", tmp / "limpios")
    limpio = corte_interior_por_posicion(serie)
    for f in (limpio, Path(indice[str(limpio.resolve())])):
        ds = pydicom.dcmread(f)
        del ds.ImagePositionPatient
        ds.save_as(f, enforce_file_format=True)
    assert suv.main(["comparar", str(serie), "--json", str(tmp / "c.json")]) == 1
    r = json.loads((tmp / "c.json").read_text(encoding="utf-8"))
    assert r["geometria_incompleta"] == 1
    assert r["correspondencia_completa"] is True and r["orden_y_geometria_ok"] is False and r["veredicto"] == "DIFERENCIAS"
