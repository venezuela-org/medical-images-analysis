#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydicom>=3.0,<4", "numpy>=1.26"]
# ///
"""
demo.py 0.1.0
=============
Genera un caso PET/CT sintético para probar la aplicación sin ningún dato de pacientes:
un "paciente" inventado con identidad ficticia (nombre, ID, fecha de nacimiento, institución,
etiquetas privadas) para que la anonimización tenga algo que limpiar, un CT y un PET de ocho
cortes cada uno sobre la misma rejilla y el mismo marco de referencia, con un fantoma de
cilindros y una "lesión" caliente en el PET, y los atributos de dosis necesarios para el SUV.

Generates a synthetic PET/CT case with fictitious identity, so the whole workflow (clean,
compare, pair, SUV) can be exercised without patient information. Nothing here comes from a
real scan.

Uso / Usage
  uv run demo.py CARPETA_DESTINO [--cortes 8] [--semilla 1]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    pass

VERSION = "0.1.0"
CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
PET_IMAGE = "1.2.840.10008.5.1.4.1.1.128"


def base(sop: str, modalidad: str, estudio: str, serie: str, marco: str, numero: int, z: float) -> Dataset:
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sop
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID, ds.SOPInstanceUID = sop, ds.file_meta.MediaStorageSOPInstanceUID
    ds.Modality = modalidad
    ds.Manufacturer, ds.ManufacturerModelName = "DEMO SINTETICA", "Fantoma 1"
    # identidad ficticia, para que haya algo que anonimizar / fictitious identity to clean
    ds.PatientName = "DEMO^PACIENTE^SINTETICO"
    ds.PatientID = "DEMO-000123"
    ds.IssuerOfPatientID = "HOSPITAL DEMO"
    ds.PatientBirthDate = "19600615"
    ds.PatientSex, ds.PatientAge = "O", "064Y"
    ds.PatientWeight, ds.PatientSize = "72", "1.70"
    ds.InstitutionName, ds.OperatorsName, ds.ReferringPhysicianName = "Hospital Demo", "OPERADOR^DEMO", "MEDICO^DEMO"
    ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = ds.ContentDate = "20240305"
    ds.StudyTime = ds.SeriesTime = ds.AcquisitionTime = ds.ContentTime = "113000"
    ds.AccessionNumber, ds.StudyID = "ACC-DEMO-1", "DEMO1"
    ds.StudyDescription, ds.SeriesDescription = "PET CT DEMO SINTETICO", f"{modalidad} demo"
    ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.FrameOfReferenceUID = estudio, serie, marco
    ds.SeriesNumber = 1 if modalidad == "CT" else 2
    ds.InstanceNumber = numero
    ds.ImagePositionPatient = [-124.0, -124.0, z]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PatientPosition = "HFS"
    ds.add_new((0x0009, 0x0010), "LO", "DEMO PRIVADO")
    ds.add_new((0x0009, 0x1001), "LO", "valor privado de demostracion")
    ds.preamble = b"\x00" * 128
    return ds


def fantoma(n: int, numero: int, total: int, pet: bool, rng: np.random.Generator) -> np.ndarray:
    y, x = np.mgrid[0:n, 0:n]
    c = n / 2
    cuerpo = (x - c) ** 2 + (y - c) ** 2 < (0.42 * n) ** 2
    organo = (x - c + 0.15 * n) ** 2 + (y - c) ** 2 < (0.12 * n) ** 2
    lesion = ((x - c - 0.18 * n) ** 2 + (y - c - 0.1 * n) ** 2 < (0.05 * n) ** 2) & (total // 3 <= numero - 1 < 2 * total // 3)
    if pet:
        img = np.where(cuerpo, 1800.0, 0.0) + np.where(organo, 2500.0, 0.0) + np.where(lesion, 12000.0, 0.0)
        img += rng.normal(0, 120, img.shape) * cuerpo
        return np.clip(img, 0, None).astype(np.uint16)
    img = np.where(cuerpo, 40.0, -1000.0) + np.where(organo, 60.0, 0.0) + np.where(lesion, 30.0, 0.0)
    img += rng.normal(0, 15, img.shape) * cuerpo
    return (img + 1024).clip(0, 4095).astype(np.uint16)


def escribir_caso(destino: Path, cortes: int, semilla: int) -> tuple[int, int]:
    rng = np.random.default_rng(semilla)
    estudio, marco = generate_uid(), generate_uid()
    serie_ct, serie_pt = generate_uid(), generate_uid()
    paso = 3.27
    (destino / "SE0").mkdir(parents=True, exist_ok=True)
    (destino / "SE1").mkdir(parents=True, exist_ok=True)
    for i in range(cortes):
        z = -100.0 + i * paso
        ct = base(CT_IMAGE, "CT", estudio, serie_ct, marco, i + 1, z)
        ct.Rows = ct.Columns = 128
        ct.PixelSpacing, ct.SliceThickness = [1.953125, 1.953125], paso
        ct.KVP, ct.RescaleIntercept, ct.RescaleSlope, ct.RescaleType = 120, -1024, 1, "HU"
        ct.SamplesPerPixel, ct.PhotometricInterpretation = 1, "MONOCHROME2"
        ct.BitsAllocated = ct.BitsStored = 16
        ct.HighBit, ct.PixelRepresentation = 15, 0
        ct.PixelData = fantoma(128, i + 1, cortes, False, rng).tobytes()
        ct.save_as(destino / "SE0" / f"IM{i}", enforce_file_format=True)

        pt = base(PET_IMAGE, "PT", estudio, serie_pt, marco, i + 1, z)
        pt.Rows = pt.Columns = 64
        pt.PixelSpacing, pt.SliceThickness = [3.90625, 3.90625], paso
        pt.RescaleIntercept, pt.RescaleSlope = 0, 1.0
        pt.Units, pt.DecayCorrection, pt.CorrectedImage = "BQML", "START", ["ATTN", "DECY", "SCAT"]
        pt.SeriesType = ["WHOLE BODY", "IMAGE"]
        pt.FrameReferenceTime, pt.ActualFrameDuration = 0, 180000
        rf = Dataset()
        rf.RadionuclideTotalDose, rf.RadionuclideHalfLife = 370000000.0, 6586.2
        rf.RadiopharmaceuticalStartTime, rf.RadiopharmaceuticalStartDateTime = "103000", "20240305103000"
        rf.Radiopharmaceutical = "Fluorodeoxyglucose"
        pt.RadiopharmaceuticalInformationSequence = Sequence([rf])
        pt.SamplesPerPixel, pt.PhotometricInterpretation = 1, "MONOCHROME2"
        pt.BitsAllocated = pt.BitsStored = 16
        pt.HighBit, pt.PixelRepresentation = 15, 0
        pt.PixelData = fantoma(64, i + 1, cortes, True, rng).tobytes()
        pt.save_as(destino / "SE1" / f"IM{i}", enforce_file_format=True)
    return cortes, cortes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Caso PET/CT sintético de demostración (synthetic demo case)")
    p.add_argument("destino", help="carpeta donde crear el caso, por ejemplo ~/datos-pet/originales/DEMO")
    p.add_argument("--cortes", type=int, default=8)
    p.add_argument("--semilla", type=int, default=1)
    args = p.parse_args(argv)
    destino = Path(args.destino).expanduser()
    if destino.exists() and any(destino.iterdir()):
        print("La carpeta de destino ya tiene contenido; elige una vacía (destination not empty)")
        return 2
    ct, pt = escribir_caso(destino, args.cortes, args.semilla)
    print(f"demo {VERSION}: caso sintético escrito en {destino}: SE0 con {ct} cortes CT y SE1 con {pt} cortes PET, identidad ficticia, sin datos reales")
    return 0


if __name__ == "__main__":
    sys.exit(main())
