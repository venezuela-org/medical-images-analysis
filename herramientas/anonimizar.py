#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydicom>=3.0,<4"]
# ///
"""
anonimizar.py 0.4.6
===================
Anonimizador DICOM para el proyecto de segmentación PET/CT.
DICOM anonymizer for the PET/CT segmentation project.

Política / Policy  pet-ct-local/0.2  (versión escrita en cada archivo limpio y en el reporte)
  - DICOM PS3.15 Basic Application Confidentiality Profile
  - Retain Patient Characteristics Option (sexo, edad agrupada 90+, peso, talla)
  - Retain Longitudinal Temporal Information with Modified Dates Option
  - Solo se aceptan objetos CT, PET, MR y Segmentation (clásicos y Enhanced). Todo lo demás
    (SR, capturas secundarias, documentos, estados de presentación, RTSTRUCT, desconocidos)
    va a cuarentena y no se copia. / Only CT, PET, MR and Segmentation objects are accepted;
    everything else is quarantined and never copied.
  - Texto libre: solo se conserva una lista cerrada de campos técnicos; cualquier otro LO, LT,
    ST, UT, SH, UC o PN se elimina, también dentro de secuencias. / Free text: closed allowlist.
  - UID: solo se reescriben los identificadores de instancia (estudio, serie, SOP, marco de
    referencia y sus referencias) con la raíz del proyecto; clases SOP, sintaxis de
    transferencia y UID semánticos no se tocan. / Scoped UID remapping under a project root.
  - Preámbulo puesto a cero y metadatos de archivo reconstruidos. / Preamble zeroed, file meta rebuilt.
  - Etiquetas privadas eliminadas salvo una lista cerrada para cuantificación PET: los factores
    numéricos de Philips (7053,xx00 y 7053,xx09) y las fechas-hora de referencia de decaimiento
    de GE (0009,xx0D) y Siemens (0071,xx22), que se desplazan con las demás fechas del paciente.
    / Private tags removed except a closed allowlist for PET quantification: Philips numeric
    factors and the GE and Siemens decay-reference datetimes, date-shifted like every other date.
  - Píxeles sin cambios. Archivos con BurnedInAnnotation=YES van a cuarentena. / Pixels untouched;
    burned-in annotation → quarantine.
  - Para PET se recalcula el factor SUV (peso corporal) antes y después de limpiar y se exige que
    sea idéntico. / For PET the SUVbw factor is recomputed before and after cleaning and must match.

Uso / Usage
  uv run anonimizar.py limpiar   CARPETA_ORIGINAL CARPETA_LIMPIA [--mapa RUTA] [--prefijo ANON] [--conservar-descripciones]
  uv run anonimizar.py verificar CARPETA [--prefijo ANON] [--json RUTA]

Códigos de salida / exit codes: 0 todo limpio, 1 hubo cuarentena o problemas, 2 error de uso.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import sys

# Consola en UTF-8 también en Windows / UTF-8 console output on Windows too
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    pass
import tempfile
import warnings
from collections import Counter
from pathlib import Path

warnings.simplefilter("ignore")  # los avisos de pydicom imprimen valores; nunca en la bitácora
logging.getLogger("pydicom").setLevel(logging.CRITICAL)

import pydicom  # noqa: E402
from pydicom.dataset import Dataset, FileMetaDataset  # noqa: E402
from pydicom.sequence import Sequence  # noqa: E402
from pydicom.uid import PYDICOM_IMPLEMENTATION_UID, UID  # noqa: E402

pydicom.config.convert_wrong_length_to_UN = True  # elementos privados mal formados no detienen la corrida

VERSION = "0.4.6"
POLITICA = "pet-ct-local/0.4"
RAIZ_UID = "1.2.826.0.1.3680043.8.498."     # raíz pydicom, libre de uso; identifica los UID reescritos
DICOM_ROOT = "1.2.840.10008"
PREAMBULO_CERO = b"\x00" * 128
PSEUDO_RE = r"^[A-Z0-9]{2,12}-\d{4}$"

# Objetos aceptados / accepted SOP classes
SOP_PERMITIDAS = {
    "1.2.840.10008.5.1.4.1.1.2": "CT Image",
    "1.2.840.10008.5.1.4.1.1.2.1": "Enhanced CT Image",
    "1.2.840.10008.5.1.4.1.1.128": "PET Image",
    "1.2.840.10008.5.1.4.1.1.130": "Enhanced PET Image",
    "1.2.840.10008.5.1.4.1.1.4": "MR Image",
    "1.2.840.10008.5.1.4.1.1.4.1": "Enhanced MR Image",
    "1.2.840.10008.5.1.4.1.1.66.4": "Segmentation",
}
MODALIDADES_PERMITIDAS = {"CT", "PT", "MR", "SEG"}

# --- reglas por palabra clave / rules by keyword ------------------------------------------

REEMPLAZAR = {"PatientName", "PatientID"}                       # → seudónimo
VACIAR = {"AccessionNumber", "PatientBirthDate", "ReferringPhysicianName"}   # tipo 2 → vacío

# Palabras clave que se eliminan siempre, en cualquier nivel / always removed, any level
ELIMINAR = {
    "PatientBirthTime", "PatientAddress", "PatientTelephoneNumbers", "PatientTelecomInformation",
    "OtherPatientIDs", "OtherPatientIDsSequence", "OtherPatientNames", "PatientBirthName",
    "PatientMotherBirthName", "PatientInsurancePlanCodeSequence", "PatientPrimaryLanguageCodeSequence",
    "PatientReligiousPreference", "PatientComments", "AdditionalPatientHistory", "MedicalRecordLocator",
    "MedicalAlerts", "Allergies", "EthnicGroup", "Occupation", "MilitaryRank", "BranchOfService",
    "CountryOfResidence", "RegionOfResidence", "PatientInstitutionResidence", "ResponsiblePerson",
    "ResponsibleOrganization", "PatientState", "PregnancyStatus", "LastMenstrualDate", "SmokingStatus",
    "SpecialNeeds", "CurrentPatientLocation", "VisitStatusID", "VisitComments", "PatientTransportArrangements",
    "AdmittingDiagnosesDescription", "AdmittingDiagnosesCodeSequence", "AdmissionID", "IssuerOfAdmissionID",
    "IssuerOfPatientID", "IssuerOfPatientIDQualifiersSequence", "TypeOfPatientID", "ReferencedPatientSequence",
    "ReferencedPatientPhotoSequence", "PatientIdentityRemoved", "DeidentificationMethod",
    "DeidentificationMethodCodeSequence", "ClinicalTrialSubjectID", "ClinicalTrialSubjectReadingID",
    "ClinicalTrialSiteID", "ClinicalTrialSiteName", "ClinicalTrialSponsorName", "ClinicalTrialProtocolID",
    "ClinicalTrialProtocolName", "ClinicalTrialTimePointID", "ClinicalTrialTimePointDescription",
    "ClinicalTrialCoordinatingCenterName", "ClinicalTrialSeriesID", "ClinicalTrialSeriesDescription",
    "InstitutionName", "InstitutionAddress", "InstitutionCodeSequence", "InstitutionalDepartmentName",
    "InstitutionalDepartmentTypeCodeSequence", "StationName", "DeviceSerialNumber", "DeviceUID", "DetectorID",
    "GantryID", "PlateID", "CassetteID", "StorageMediaFileSetID", "StorageMediaFileSetUID",
    "ReferringPhysicianAddress", "ReferringPhysicianTelephoneNumbers", "ReferringPhysicianIdentificationSequence",
    "PerformingPhysicianName", "PerformingPhysicianIdentificationSequence", "NameOfPhysiciansReadingStudy",
    "PhysiciansReadingStudyIdentificationSequence", "PhysiciansOfRecord", "PhysiciansOfRecordIdentificationSequence",
    "OperatorsName", "OperatorIdentificationSequence", "RequestingPhysician",
    "RequestingPhysicianIdentificationSequence", "RequestingService", "RequestingServiceCodeSequence",
    "ScheduledPerformingPhysicianName", "ScheduledPerformingPhysicianIdentificationSequence",
    "ConsultingPhysicianName", "ConsultingPhysicianIdentificationSequence", "VerifyingObserverName",
    "VerifyingObserverSequence", "AuthorObserverSequence", "ParticipantSequence", "ReviewerName",
    "ContentCreatorName", "ContentCreatorIdentificationCodeSequence", "PersonName", "PersonIdentificationCodeSequence",
    "PersonAddress", "PersonTelephoneNumbers", "PersonTelecomInformation",
    "RequestAttributesSequence", "RequestedProcedureID", "RequestedProcedureDescription",
    "RequestedProcedureComments", "RequestedProcedureLocation", "RequestedProcedurePriority",
    "ScheduledProcedureStepID", "ScheduledProcedureStepDescription", "ScheduledProcedureStepLocation",
    "PerformedProcedureStepID", "PerformedProcedureStepDescription", "PerformedStationName",
    "PerformedLocation", "PerformedStationAETitle", "ScheduledStationName", "ScheduledStationAETitle",
    "ScheduledStudyLocation", "ScheduledStudyLocationAETitle", "ReferencedPerformedProcedureStepSequence",
    "PlacerOrderNumberImagingServiceRequest", "FillerOrderNumberImagingServiceRequest",
    "PlacerOrderNumberProcedure", "FillerOrderNumberProcedure", "OrderEnteredBy", "OrderEntererLocation",
    "OrderCallbackPhoneNumber", "OrderCallbackTelecomInformation", "ImagingServiceRequestComments",
    "ReasonForStudy", "ReasonForTheRequestedProcedure", "ReasonForTheImagingServiceRequest",
    "ReasonForRequestedProcedureCodeSequence", "StudyComments", "ImageComments", "AcquisitionComments",
    "FrameComments", "IdentifyingComments", "DerivationDescription", "DigitalSignaturesSequence", "MAC",
    "ProcedureCodeSequence", "PerformedProcedureCodeSequence", "ReferencedRequestSequence", "StudyPriorityID",
    "StudyStatusID", "ServiceEpisodeID", "ServiceEpisodeDescription", "IssuerOfServiceEpisodeID",
    "TextValue", "ContentSequence", "ModifiedAttributesSequence", "OriginalAttributesSequence",
    "ContributingEquipmentSequence", "ReferencedPatientAliasSequence", "PatientSupportTechnique", "Arbitrary",
    "TextComments", "ResultsComments", "InterpretationText", "ImpressionsText", "ScheduledStudyStartDate",
}

# Texto libre que se conserva (lista cerrada) / free text kept (closed allowlist)
TEXTO_PERMITIDO = {
    "Manufacturer", "ManufacturerModelName", "SoftwareVersions", "StudyDescription", "SeriesDescription",
    "ProtocolName", "ConvolutionKernel", "FilterType", "PositionReferenceIndicator",
    "ReconstructionMethod", "AttenuationCorrectionMethod", "ScatterCorrectionMethod",
    "RandomsCorrectionMethod", "DeadTimeCorrectionMethod", "DetectorNormalizationCorrection",
    "Radiopharmaceutical", "RadiopharmaceuticalRoute", "ContrastBolusAgent", "ContrastBolusRoute",
    "CodeMeaning", "CodeValue", "CodingSchemeDesignator", "CodingSchemeVersion", "LongCodeValue",
    "WindowCenterWidthExplanation", "SequenceName", "PulseSequenceName", "ReceiveCoilName", "TransmitCoilName",
    "MRAcquisitionType", "SegmentLabel", "SegmentAlgorithmName", "ContentLabel", "SeriesDescriptionCodeSequence",
    "FrameLabel", "FrameType", "StackID", "AnatomicRegionSequence", "ImageTypeValue3", "StudyID", "RescaleType",
    "ImagedNucleus",
    "MappingResourceName", "TriggerSourceOrType", "DecayCorrectionDateTime",
}
# Fechas semánticas (versiones de grupos de contexto), no fechas del paciente / semantic dates, never shifted
FECHAS_NO_DESPLAZAR = {"ContextGroupVersion", "ContextGroupLocalVersion"}
# Los que se conservan pero se revisan contra el nombre y el ID originales
TEXTO_REVISADO = {"StudyDescription", "SeriesDescription", "ProtocolName", "SegmentLabel", "ContentLabel"}
VR_TEXTO = {"LO", "LT", "ST", "UT", "SH", "UC"}
VR_BINARIO = {"OB", "OW", "OF", "OD", "OL", "OV", "UN"}
# Binarios que se conservan: píxeles y paletas; todo otro binario, URI o VR desconocido se elimina
BINARIO_PERMITIDO = {"PixelData", "FloatPixelData", "DoubleFloatPixelData", "LUTData", "ExtendedOffsetTable",
                     "ExtendedOffsetTableLengths", "RedPaletteColorLookupTableData", "GreenPaletteColorLookupTableData",
                     "BluePaletteColorLookupTableData", "SegmentedRedPaletteColorLookupTableData",
                     "SegmentedGreenPaletteColorLookupTableData", "SegmentedBluePaletteColorLookupTableData"}

# UID que se reescriben (identificadores de instancia y sus referencias) / UIDs that get remapped
UID_REESCRIBIR = {
    "SOPInstanceUID", "SeriesInstanceUID", "StudyInstanceUID", "FrameOfReferenceUID",
    "SynchronizationFrameOfReferenceUID", "InstanceCreatorUID", "ReferencedSOPInstanceUID",
    "ReferencedFrameOfReferenceUID", "RelatedFrameOfReferenceUID", "IrradiationEventUID",
    "DimensionOrganizationUID", "ConcatenationUID", "TrackingUID", "ReferencedSOPInstanceUIDInFile",
    "FailedSOPInstanceUIDList", "TargetUID", "SourceInstanceUID", "SeriesInstanceUIDInFile" ,
}

# Privadas permitidas: (patrón del creador, grupo, byte bajo, tipo) / allowed private elements
# tipo "numero": se conserva solo si es numérico; "fechahora": solo si es una fecha-hora DICOM, y se desplaza
PRIVADAS_PERMITIDAS = [
    (r"^Philips PET Private Group$", 0x7053, 0x00, "numero"),      # SUV Scale Factor
    (r"^Philips PET Private Group$", 0x7053, 0x09, "numero"),      # Activity Concentration Scale Factor
    (r"^GEMS_PETD_01$", 0x0009, 0x0D, "fechahora"),                 # GE scan datetime (referencia de decaimiento)
    (r"SIEMENS", 0x0071, 0x22, "fechahora"),                        # Siemens Decay Correction DateTime
]
RE_FECHAHORA = re.compile(r"^\d{8}(\d{2}(\d{2}(\d{2}(\.\d{1,6})?)?)?)?([+-]\d{4})?$")


def regla_privada(creador: str, grupo: int, byte_bajo: int):
    for patron, g, b, tipo in PRIVADAS_PERMITIDAS:
        if g == grupo and b == byte_bajo and re.search(patron, creador or ""):
            return tipo
    return None


def creador_permitido(nombre: str) -> bool:
    return any(re.search(patron, nombre or "") for patron, _, _, _ in PRIVADAS_PERMITIDAS)


def privada_valida(tipo: str, e) -> bool:
    if str(e.VR) == "SQ":
        return False
    if tipo == "numero":
        return es_numerico(e.value)
    return bool(RE_FECHAHORA.match(str(e.value).strip())) and not es_multi(e.value)

CODIGOS_DEID = [
    ("113100", "Basic Application Confidentiality Profile"),
    ("113108", "Retain Patient Characteristics Option"),
    ("113107", "Retain Longitudinal Temporal Information Modified Dates Option"),
]


# --------------------------------------------------------------------------------------
# utilidades / helpers
# --------------------------------------------------------------------------------------

def decir(es: str, en: str) -> None:
    print(f"{es} ({en})")


def nuevo_uid(secreto: str, uid_viejo: str) -> str:
    """Determinista bajo la raíz del proyecto; mismo secreto + mismo UID → mismo resultado."""
    digest = hashlib.sha256(f"{secreto}:{uid_viejo}".encode()).hexdigest()
    cuerpo = str(int(digest[:32], 16))
    return (RAIZ_UID + cuerpo)[:64]


def hash_corto(texto: str, n: int = 10) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()[:n]


def sha256_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def desplazar_da(valor: str, dias: int) -> str:
    valor = (valor or "").strip()
    if not valor:
        return ""
    partes = []
    for parte in valor.split("-"):
        try:
            fecha = dt.datetime.strptime(parte[:8], "%Y%m%d")
        except ValueError:
            return ""
        partes.append((fecha + dt.timedelta(days=dias)).strftime("%Y%m%d"))
    return "-".join(partes)


def desplazar_dt(valor: str, dias: int) -> str:
    valor = (valor or "").strip()
    if not valor:
        return ""
    m = re.match(r"^(\d{8})(.*)$", valor)
    if not m:
        return ""
    fecha = desplazar_da(m.group(1), dias)
    return fecha + m.group(2) if fecha else ""


def limitar_edad(valor: str) -> str:
    valor = (valor or "").strip()
    m = re.match(r"^(\d{1,3})([DWMY])$", valor)
    if m and m.group(2) == "Y" and int(m.group(1)) >= 90:
        return "090Y"
    return valor


def componentes_identidad(nombre: str, pid: str) -> list[str]:
    trozos = set()
    for fuente in (nombre or "", pid or ""):
        for t in re.split(r"[\^\s,;./_-]+", fuente):
            if len(t.strip()) >= 3:
                trozos.add(t.strip())
    return sorted(trozos, key=len, reverse=True)


def es_multi(valor) -> bool:
    return isinstance(valor, (list, tuple, pydicom.multival.MultiValue))


def valores(e) -> list:
    return list(e.value) if es_multi(e.value) else [e.value]


def asignar(e, nuevos: list) -> None:
    e.value = nuevos if len(nuevos) > 1 else (nuevos[0] if nuevos else "")


def significado_canonico(d: Dataset) -> str | None:
    """CodeMeaning se reemplaza por 'designador valor', que no puede llevar texto ajeno al código."""
    des, val = str(d.get("CodingSchemeDesignator", "")).strip(), str(d.get("CodeValue", "")).strip()
    if not des or not val:
        val = str(d.get("LongCodeValue", "")).strip() or str(d.get("URNCodeValue", "")).strip()
    if not des or not val:
        return None
    return f"{des} {val}"[:64]


def es_numerico(valor) -> bool:
    try:
        for v in (valor if es_multi(valor) else [valor]):
            float(str(v))
        return True
    except (TypeError, ValueError):
        return False


def iter_archivos(carpeta: Path):
    """Archivos regulares, sin seguir enlaces simbólicos / regular files, symlinks never followed."""
    for raiz, dirs, nombres in os.walk(carpeta, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(raiz, d)))
        for n in sorted(nombres):
            p = Path(raiz) / n
            if p.is_symlink():
                continue
            if p.is_file():
                yield p


def hora_a_segundos(valor: str) -> float | None:
    valor = (valor or "").strip()
    m = re.match(r"^(\d{2})(\d{2})(\d{2}(?:\.\d+)?)?$", valor.replace(":", ""))
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3) or 0)


def factor_suv(ds: Dataset) -> tuple[float | None, str]:
    """Factor SUVbw (g/Bq·mL) a partir de etiquetas públicas; None si falta algo.
    Se usa para comprobar que la limpieza no altera la cuantificación. Convención QIBA:
    DecayCorrection START → tiempo de decaimiento desde la inyección hasta la hora de la serie."""
    try:
        if str(ds.get("Modality", "")) != "PT":
            return None, "no es PET"
        unidades = str(ds.get("Units", ""))
        if unidades != "BQML":
            return None, f"unidades {unidades or 'ausentes'}, se requiere BQML"
        peso = float(ds.PatientWeight)
        rf = ds.RadiopharmaceuticalInformationSequence[0]
        dosis = float(rf.RadionuclideTotalDose)
        vida_media = float(rf.RadionuclideHalfLife)
        if not (1.0 <= peso <= 500.0) or dosis <= 0 or vida_media <= 0:
            return None, "peso, dosis o vida media fuera de rango / out of range"
        inicio = hora_a_segundos(str(rf.get("RadiopharmaceuticalStartTime", "")))
        if inicio is None and "RadiopharmaceuticalStartDateTime" in rf:
            inicio = hora_a_segundos(str(rf.RadiopharmaceuticalStartDateTime)[8:14])
        correccion = str(ds.get("DecayCorrection", ""))
        if correccion == "START":
            escaneo = hora_a_segundos(str(ds.get("SeriesTime", ""))) or hora_a_segundos(str(ds.get("AcquisitionTime", "")))
        elif correccion == "ADMIN":
            escaneo = inicio
        else:
            return None, f"DecayCorrection {correccion or 'ausente'} sin regla"
        if inicio is None or escaneo is None:
            return None, "falta hora de inyección o de serie"
        delta = escaneo - inicio
        if delta < 0:
            delta += 86400  # cruce de medianoche
        if not (0 <= delta <= 86400):
            return None, "intervalo inyección-serie fuera de rango / interval out of range"
        dosis_decaida = dosis * (2 ** (-delta / vida_media))
        if dosis_decaida <= 0:
            return None, "dosis decaída no válida"
        return (peso * 1000.0) / dosis_decaida, "ok"
    except Exception as e:  # falta alguna etiqueta
        return None, f"falta {type(e).__name__}"


# --------------------------------------------------------------------------------------
# mapa privado / private map (atómico, con permisos restringidos)
# --------------------------------------------------------------------------------------

class MapaPrivado:
    def __init__(self, ruta: Path, prefijo: str):
        self.ruta = ruta
        self.prefijo = prefijo
        if ruta.exists():
            self.datos = json.loads(ruta.read_text(encoding="utf-8"))
            if self.datos.get("prefijo", prefijo) != prefijo:
                raise SystemExit(f"el mapa existente usa el prefijo {self.datos.get('prefijo')}; usa el mismo")
        else:
            self.datos = {"version": VERSION, "politica": POLITICA, "prefijo": prefijo,
                          "secreto_uid": secrets.token_hex(32), "pacientes": {}}
            self.guardar()

    @property
    def secreto(self) -> str:
        return self.datos["secreto_uid"]

    @staticmethod
    def clave(ds: Dataset) -> tuple[str, str]:
        """Identidad local: emisor + ID; si no hay ID, nombre + fecha de nacimiento."""
        pid = str(ds.get("PatientID", "")).strip()
        emisor = str(ds.get("IssuerOfPatientID", "")).strip()
        if pid:
            return f"id:{emisor}|{pid}", "id"
        nombre = str(ds.get("PatientName", "")).strip()
        nac = str(ds.get("PatientBirthDate", "")).strip()
        if nombre:
            return f"nombre:{nombre}|{nac}", "nombre"
        return f"estudio:{ds.get('StudyInstanceUID', '')}", "estudio"

    def paciente(self, ds: Dataset) -> tuple[dict, str | None]:
        """Devuelve (registro, aviso). Guarda el mapa de inmediato cuando aparece un paciente nuevo."""
        clave, tipo = self.clave(ds)
        nombre = str(ds.get("PatientName", "")).strip()
        nac = str(ds.get("PatientBirthDate", "")).strip()
        aviso = None
        pac = self.datos["pacientes"].get(clave)
        if pac is None:
            n = len(self.datos["pacientes"]) + 1
            pac = {"seudonimo": f"{self.prefijo}-{n:04d}", "tipo_clave": tipo,
                   "nombre_original": nombre, "id_original": str(ds.get("PatientID", "")).strip(),
                   "emisor_original": str(ds.get("IssuerOfPatientID", "")).strip(), "nacimiento_original": nac,
                   "desplazamiento_dias": -secrets.choice(range(30, 1096))}
            self.datos["pacientes"][clave] = pac
            self.guardar()
        elif (nombre and pac["nombre_original"] and nombre != pac["nombre_original"]) or \
             (nac and pac["nacimiento_original"] and nac != pac["nacimiento_original"]):
            aviso = f"{pac['seudonimo']}: mismo ID pero nombre o fecha de nacimiento distintos; revisar a mano / same ID, different name or birth date; review"
        return pac, aviso

    def guardar(self) -> None:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.ruta.parent, 0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(dir=self.ruta.parent, prefix=".mapa_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.datos, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.ruta)
        aviso = self.ruta.parent / "LEEME_NO_COMPARTIR.txt"
        if not aviso.exists():
            aviso.write_text(
                "Esta carpeta contiene el mapa que relaciona seudónimos con pacientes reales y el secreto de los UID.\n"
                "Se queda en esta computadora. No la subas a Drive, GitHub, Mega ni correo.\n\n"
                "This folder holds the map linking pseudonyms to real patients and the UID secret.\n"
                "It stays on this computer. Never upload it to Drive, GitHub, Mega or email.\n", encoding="utf-8")


# --------------------------------------------------------------------------------------
# limpieza de un dataset / cleaning one dataset
# --------------------------------------------------------------------------------------

def creadores_privados(d: Dataset) -> dict[int, str]:
    """(grupo << 8 | bloque) → nombre del creador privado / private creator names per block."""
    out = {}
    for e in d:
        if e.tag.is_private and e.tag.element < 0x0100:
            try:
                out[(e.tag.group << 8) | e.tag.element] = str(e.value).strip()
            except Exception:
                pass
    return out


def limpiar_dataset(ds: Dataset, pac: dict, secreto: str, acciones: Counter, avisos: list[str],
                    conservar_descripciones: bool = False) -> None:
    seudonimo = pac["seudonimo"]
    dias = pac["desplazamiento_dias"]
    trozos = componentes_identidad(pac["nombre_original"], pac["id_original"])

    def regla(d: Dataset, e) -> None:
        tag = e.tag
        kw = e.keyword or f"({tag.group:04X},{tag.element:04X})"
        vr = str(e.VR)

        if tag.is_private:
            if tag.element < 0x0100:          # creador: se decide al final del dataset
                return
            creadores = creadores_privados(d)
            bloque = (tag.group << 8) | (tag.element >> 8)
            creador = creadores.get(bloque, "")
            tipo = regla_privada(creador, tag.group, tag.element & 0xFF)
            if tipo and privada_valida(tipo, e):
                if tipo == "fechahora":
                    e.value = desplazar_dt(str(e.value).strip(), dias)
                    acciones[f"(privada conservada) {creador} {tag.element & 0xFF:02X}: fecha-hora de referencia desplazada / reference datetime shifted"] += 1
                else:
                    acciones[f"(privada conservada) {creador} {tag.element & 0xFF:02X}: factor PET numérico / numeric PET factor kept"] += 1
                return
            del d[tag]
            acciones["(privadas) eliminadas / private removed"] += 1
            return
        if 0x6000 <= tag.group <= 0x60FF or 0x5000 <= tag.group <= 0x50FF:
            del d[tag]
            acciones["(overlay/curva) eliminado / removed"] += 1
            return
        if tag.group >= 0xFFFA or (tag.group == 0x7FE0 and kw not in BINARIO_PERMITIDO):
            del d[tag]
            acciones["(después de píxeles) eliminado / after-pixel element removed"] += 1
            return
        if not e.keyword:                      # etiqueta pública fuera del diccionario / unknown public tag
            del d[tag]
            acciones["(desconocida) eliminada / unknown public tag removed"] += 1
            return
        if kw in REEMPLAZAR:
            e.value = seudonimo
            acciones[f"{kw}: seudónimo / pseudonym"] += 1
            return
        if kw in ELIMINAR:
            del d[tag]
            acciones[f"{kw}: eliminado / removed"] += 1
            return
        if kw in VACIAR:
            e.value = ""
            acciones[f"{kw}: vaciado / emptied"] += 1
            return
        if vr == "PN":
            e.value = ""
            acciones[f"{kw}: nombre vaciado / name emptied"] += 1
            return
        if vr == "UR":
            del d[tag]
            acciones[f"{kw}: URI eliminada / URI removed"] += 1
            return
        if vr in VR_BINARIO and kw not in BINARIO_PERMITIDO:
            del d[tag]
            acciones[f"{kw}: binario eliminado / binary removed"] += 1
            return
        if kw == "CodeMeaning":
            canon = significado_canonico(d)
            if canon is None:
                del d[tag]
                acciones["CodeMeaning: sin código, eliminado / no code, removed"] += 1
            elif str(e.value) != canon:
                e.value = canon
                acciones["CodeMeaning: canonizado / canonicalized"] += 1
            return
        if vr == "UI":
            if kw in UID_REESCRIBIR:
                nuevos = [nuevo_uid(secreto, str(v)) if str(v) else v for v in valores(e)]
                asignar(e, nuevos)
                acciones[f"{kw}: UID reescrito / UID remapped"] += 1
            return
        if vr in ("DA", "DT") and kw in FECHAS_NO_DESPLAZAR:
            return
        if vr == "DA":
            nuevos = [desplazar_da(str(v), dias) for v in valores(e)]
            if any(str(v) and not n for v, n in zip(valores(e), nuevos)):
                avisos.append(f"{kw}: fecha ilegible, quedó vacía / unreadable date emptied")
            asignar(e, nuevos)
            acciones[f"{kw}: fecha desplazada / date shifted"] += 1
            return
        if vr == "DT":
            asignar(e, [desplazar_dt(str(v), dias) for v in valores(e)])
            acciones[f"{kw}: fecha-hora desplazada / datetime shifted"] += 1
            return
        if kw == "PatientAge":
            nuevo = limitar_edad(str(e.value))
            if nuevo != str(e.value):
                e.value = nuevo
                acciones["PatientAge: agrupada 90+ / grouped 90+"] += 1
            return
        if vr in VR_TEXTO:
            if kw not in TEXTO_PERMITIDO or (kw in TEXTO_REVISADO and not conservar_descripciones):
                del d[tag]
                acciones[f"{kw}: texto libre eliminado / free text removed"] += 1
                return
            if kw in TEXTO_REVISADO:
                texto = str(e.value)
                nuevo = re.sub(r"[<>\x00-\x1f]", " ", texto).strip()   # sin marcas HTML ni caracteres de control
                for t in trozos:
                    nuevo = re.sub(re.escape(t), "[REDACTADO]", nuevo, flags=re.IGNORECASE)
                if nuevo != texto:
                    e.value = nuevo
                    acciones[f"{kw}: identidad redactada / identity redacted"] += 1
                    avisos.append(f"{kw}: contenía nombre o ID, se redactó / contained name or ID, redacted")
            return

    ds.walk(regla)

    # creadores privados huérfanos / orphan private creators
    def podar_creadores(d: Dataset, e) -> None:
        if e.tag.is_private and e.tag.element < 0x0100:
            nombre = str(e.value).strip() if str(e.VR) != "SQ" else ""
            bloque_alto = e.tag.element << 8
            usados = any(x.tag.is_private and x.tag.group == e.tag.group and (x.tag.element & 0xFF00) == bloque_alto
                         for x in d)
            if not creador_permitido(nombre) or not usados:
                del d[e.tag]
                acciones["(creador privado) eliminado / private creator removed"] += 1
    ds.walk(podar_creadores)

    if "StudyInstanceUID" in ds:
        ds.StudyID = "E" + hash_corto(str(ds.StudyInstanceUID), 8)
        acciones["StudyID: derivado / derived"] += 1

    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = f"{POLITICA}; PS3.15 Basic; RetainPatChars; ModifiedDates"
    seq = Sequence()
    for valor, significado in CODIGOS_DEID:
        item = Dataset()
        item.CodeValue, item.CodingSchemeDesignator, item.CodeMeaning = valor, "DCM", f"DCM {valor}"
        seq.append(item)
    ds.DeidentificationMethodCodeSequence = seq

    # metadatos de archivo reconstruidos y preámbulo a cero / file meta rebuilt, preamble zeroed
    sintaxis = ds.file_meta.get("TransferSyntaxUID", None) if getattr(ds, "file_meta", None) else None
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = ds.SOPClassUID
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    meta.TransferSyntaxUID = sintaxis or pydicom.uid.ImplicitVRLittleEndian
    meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    meta.ImplementationVersionName = f"PETCTLOCAL_{POLITICA.split('/')[-1]}"   # estable entre versiones de la herramienta
    ds.file_meta = meta
    ds.preamble = PREAMBULO_CERO


# --------------------------------------------------------------------------------------
# verificación independiente / independent verification
# --------------------------------------------------------------------------------------

def leer_preambulo(ruta: Path) -> bytes:
    with open(ruta, "rb") as f:
        return f.read(128)


def verificar_dataset(ds: Dataset, prefijo: str, ruta: Path | None = None,
                      preambulo: bytes | None = None) -> tuple[list[str], list[str]]:
    """Comprueba la política sobre un archivo limpio. Devuelve (problemas, avisos), nunca valores."""
    problemas, avisos = [], []
    patron = re.compile(rf"^{re.escape(prefijo)}-\d{{4}}$")
    if preambulo is None and ruta is not None:
        preambulo = leer_preambulo(ruta)

    sop = str(ds.get("SOPClassUID", ""))
    if sop not in SOP_PERMITIDAS:
        problemas.append("clase SOP no permitida / SOP class not allowed")
    if str(ds.get("Modality", "")) not in MODALIDADES_PERMITIDAS:
        problemas.append("modalidad no permitida / modality not allowed")
    if preambulo is not None and preambulo != PREAMBULO_CERO:
        problemas.append("preámbulo con contenido / preamble not zeroed")
    meta = getattr(ds, "file_meta", None)
    if meta is not None:
        for kw in ("SourceApplicationEntityTitle", "SendingApplicationEntityTitle",
                   "ReceivingApplicationEntityTitle", "PrivateInformationCreatorUID", "PrivateInformation"):
            if kw in meta:
                problemas.append(f"metadato de archivo {kw} presente / file meta present")
        if "MediaStorageSOPInstanceUID" in meta and str(meta.MediaStorageSOPInstanceUID) != str(ds.get("SOPInstanceUID", "")):
            problemas.append("MediaStorageSOPInstanceUID distinto del SOPInstanceUID")
    if str(ds.get("PatientIdentityRemoved", "")).upper() != "YES":
        problemas.append("PatientIdentityRemoved distinto de YES / not YES")
    if not str(ds.get("DeidentificationMethod", "")).startswith(POLITICA):
        problemas.append("DeidentificationMethod sin la política esperada / unexpected policy")
    if not patron.match(str(ds.get("PatientID", ""))):
        problemas.append("PatientID con valor real / real value")
    if not patron.match(str(ds.get("PatientName", ""))):
        problemas.append("PatientName con valor real / real value")
    if str(ds.get("PatientBirthDate", "")).strip():
        problemas.append("PatientBirthDate con valor / populated")
    if str(ds.get("AccessionNumber", "")).strip():
        problemas.append("AccessionNumber con valor / populated")
    if str(ds.get("BurnedInAnnotation", "")).upper() == "YES":
        problemas.append("BurnedInAnnotation=YES: píxeles con texto / pixels carry text")

    def regla(d: Dataset, e) -> None:
        tag = e.tag
        kw = e.keyword or f"({tag.group:04X},{tag.element:04X})"
        vr = str(e.VR)
        if tag.is_private:
            if tag.element < 0x0100:
                nombre = str(e.value).strip() if vr != "SQ" else ""
                if not creador_permitido(nombre):
                    problemas.append("creador privado no permitido / private creator not allowed")
                return
            creadores = creadores_privados(d)
            creador = creadores.get((tag.group << 8) | (tag.element >> 8), "")
            tipo = regla_privada(creador, tag.group, tag.element & 0xFF)
            if not (tipo and privada_valida(tipo, e)):
                problemas.append("etiqueta privada no permitida o con valor no válido / private tag not allowed or invalid value")
            return
        if 0x6000 <= tag.group <= 0x60FF or 0x5000 <= tag.group <= 0x50FF:
            problemas.append("overlay o curva presente / overlay or curve present")
            return
        if not e.keyword:
            problemas.append("etiqueta pública desconocida presente / unknown public tag present")
            return
        if tag.group >= 0xFFFA or (tag.group == 0x7FE0 and kw not in BINARIO_PERMITIDO):
            problemas.append(f"{kw}: elemento después de los píxeles / element after pixel data")
            return
        if kw in ELIMINAR and kw not in ("PatientIdentityRemoved", "DeidentificationMethod", "DeidentificationMethodCodeSequence"):
            problemas.append(f"{kw}: debía eliminarse / should be removed")
        elif vr == "UR":
            problemas.append(f"{kw}: URI presente / URI present")
        elif vr in VR_BINARIO and kw not in BINARIO_PERMITIDO:
            problemas.append(f"{kw}: binario fuera de la lista permitida / binary outside allowlist")
        elif kw == "CodeMeaning":
            if str(e.value) != (significado_canonico(d) or ""):
                problemas.append("CodeMeaning no canónico / not canonical")
        elif vr == "PN" and str(e.value).strip() and kw != "PatientName":
            problemas.append(f"{kw}: nombre de persona con valor / person name populated")
        elif vr in VR_TEXTO and kw not in TEXTO_PERMITIDO and kw not in REEMPLAZAR and str(e.value).strip() and kw != "DeidentificationMethod":
            problemas.append(f"{kw}: texto libre fuera de la lista permitida / free text outside allowlist")
        elif vr == "UI" and kw in UID_REESCRIBIR:
            for v in valores(e):
                if str(v) and not str(v).startswith(RAIZ_UID):
                    problemas.append(f"{kw}: UID sin reescribir / UID not remapped")
                    break

    ds.walk(regla)
    return sorted(set(problemas)), sorted(set(avisos))


# --------------------------------------------------------------------------------------
# comandos / commands
# --------------------------------------------------------------------------------------

def canonico(ds: Dataset) -> bytes:
    """Serialización canónica del dataset tal como la escribe esta herramienta."""
    import io
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def ruta_destino(ds: Dataset, seudonimo: str, salida: Path) -> Path:
    """Ruta de un archivo limpio: derivada del seudónimo y de los UID ya reescritos."""
    modalidad = str(ds.get("Modality", "XX"))
    serie_num = ds.get("SeriesNumber", None)
    etiqueta_serie = f"{int(serie_num):03d}" if serie_num not in (None, "") else "000"
    return (salida / seudonimo / f"estudio_{hash_corto(str(ds.StudyInstanceUID))}"
            / f"serie_{hash_corto(str(ds.SeriesInstanceUID))}_{modalidad}_{etiqueta_serie}"
            / f"{hash_corto(str(ds.SOPInstanceUID), 16)}.dcm")


def verificar_bytes(datos: bytes, prefijo: str) -> tuple[list[str], list[str]]:
    """Verifica un archivo a partir de sus bytes exactos (los mismos que se empaquetan)."""
    import io
    try:
        ds = pydicom.dcmread(io.BytesIO(datos), force=True)   # lectura completa: también lo que sigue a PixelData
        if not ("SOPClassUID" in ds or "MediaStorageSOPClassUID" in ds.file_meta):
            raise ValueError("no es DICOM")
    except Exception:
        return ["archivo que no es DICOM limpio / not a clean DICOM file"], []
    problemas, avisos = verificar_dataset(ds, prefijo, preambulo=datos[:128])
    try:
        if canonico(ds) != datos:
            problemas.append("bytes no interpretados o codificación no canónica / unconsumed bytes or non-canonical encoding")
    except Exception:
        problemas.append("no se pudo reserializar / could not re-serialize")
    return sorted(set(problemas)), avisos


def politica_valida(ruta: Path) -> bool:
    try:
        p = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (set(p) == {"politica", "prefijo", "version"} and p["politica"] == POLITICA
            and re.fullmatch(r"[A-Z0-9]{2,12}", str(p["prefijo"])) is not None
            and re.fullmatch(r"\d+\.\d+\.\d+", str(p["version"])) is not None)


def nombre_sop(uid: str) -> str:
    try:
        return UID(uid).name
    except Exception:
        return uid or "desconocida"


def solo_cambia_version(existente: Path, nuevos_bytes: bytes) -> bool:
    """¿El archivo existente es la misma imagen limpia, escrita por otra versión de la herramienta?
    Tres condiciones, en este orden: la serialización canónica del existente reproduce sus bytes exactamente
    (ningún byte sin interpretar), su nombre de versión difiere del nuevo, y al igualar solo ese nombre la
    serialización coincide con los bytes nuevos. Cualquier otra diferencia es un conflicto."""
    try:
        import io
        actuales = existente.read_bytes()
        ds_e = pydicom.dcmread(io.BytesIO(actuales), force=True)
        if canonico(ds_e) != actuales:
            return False                                   # bytes sobrantes o codificación no canónica: nunca es solo la versión
        ds_n = pydicom.dcmread(io.BytesIO(nuevos_bytes), force=True)
        version_e = str(ds_e.file_meta.get("ImplementationVersionName", ""))
        version_n = str(ds_n.file_meta.get("ImplementationVersionName", ""))
        if version_e == version_n:
            return False                                   # misma versión y bytes distintos: diferencia real
        ds_e.file_meta.ImplementationVersionName = version_n
        return canonico(ds_e) == nuevos_bytes
    except Exception:
        return False


class IndiceOrigen:
    """Índice PRIVADO limpio → original (y la carpeta de entrada), una línea JSON por archivo, sin repetidos.
    Permite el panel antes/después de la app y la ejecución de cotejar desde ella."""

    def __init__(self, carpeta_privada: Path, salida: Path):
        self.ruta = carpeta_privada / "indice_origen.jsonl"
        self.salida = salida.resolve()
        self.vistos: set[tuple[str, str]] = set()
        try:
            for linea in self.ruta.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(linea)
                    self.vistos.add((d.get("limpio_rel") or d.get("limpio", ""), f"{d.get('raiz', '')}|{d['original_rel']}" if "original_rel" in d else d.get("original", "")))
                except (ValueError, KeyError):
                    continue
        except OSError:
            pass

    def anotar(self, limpio: Path, original: Path, raiz: Path) -> None:
        rel = str(limpio.resolve().relative_to(self.salida))   # relativo a la carpeta limpia: sobrevive a un cambio de sitio
        raiz_abs = raiz.resolve()
        original_rel = str(original.resolve().relative_to(raiz_abs))
        base = self.salida.parent                                # la carpeta de trabajo que contiene limpios/
        raiz_guardada = str(raiz_abs.relative_to(base)) if raiz_abs.is_relative_to(base) else str(raiz_abs)
        par = (rel, f"{raiz_guardada}|{original_rel}")
        if par in self.vistos:
            return
        try:
            with open(self.ruta, "a", encoding="utf-8") as f:
                f.write(json.dumps({"limpio_rel": rel, "original_rel": original_rel, "raiz": raiz_guardada}, ensure_ascii=False) + "\n")
            os.chmod(self.ruta, 0o600)
            self.vistos.add(par)
        except OSError:
            pass


def solapan(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def cmd_limpiar(args: argparse.Namespace) -> int:
    entrada, salida = Path(args.entrada).expanduser(), Path(args.salida).expanduser()
    if not entrada.is_dir():
        decir(f"No existe la carpeta de entrada", "input folder not found")
        return 2
    ruta_mapa_prev = Path(args.mapa).expanduser() if args.mapa else salida.parent / f"{salida.name}_PRIVADO" / "mapa_seudonimos.json"
    if solapan(entrada, salida) or solapan(entrada, ruta_mapa_prev.parent) or solapan(entrada, salida.parent / f"{salida.name}_REPORTES"):
        decir("La carpeta de entrada y la de salida (o la privada o la de reportes) se solapan; elige carpetas separadas",
              "input and output (or private/report) folders overlap; choose separate folders")
        return 2
    salida.mkdir(parents=True, exist_ok=True)
    cerrojo = salida / ".corriendo"
    if cerrojo.exists():
        decir("Hay otra limpieza en curso sobre esta carpeta (archivo .corriendo). Si no es así, bórralo",
              "another run is in progress; delete .corriendo if that is wrong")
        return 2
    cerrojo.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return _limpiar(args, entrada, salida)
    finally:
        cerrojo.unlink(missing_ok=True)


def _limpiar(args: argparse.Namespace, entrada: Path, salida: Path) -> int:
    ruta_mapa = Path(args.mapa).expanduser() if args.mapa else salida.parent / f"{salida.name}_PRIVADO" / "mapa_seudonimos.json"
    if ruta_mapa.resolve().is_relative_to(salida.resolve()):
        decir("El mapa privado no puede quedar dentro de la carpeta de salida", "private map cannot live inside the output")
        return 2
    mapa = MapaPrivado(ruta_mapa, args.prefijo)
    indice = IndiceOrigen(ruta_mapa.parent, salida)
    (salida / ".politica.json").write_text(json.dumps({"politica": POLITICA, "prefijo": args.prefijo,
                                                         "version": VERSION}, indent=2), encoding="utf-8")

    archivos = list(iter_archivos(entrada))
    decir(f"Leyendo {len(archivos)} archivos", f"reading {len(archivos)} files")

    acciones: Counter = Counter()
    avisos: list[str] = []
    cuarentena: list[dict] = []
    omitidos: Counter = Counter()
    escritos = duplicados = actualizados = ya_anonimizados = 0
    pacientes_vistos: set[str] = set()
    posverif: Counter = Counter()
    problemas_restantes: list[str] = []
    suv: Counter = Counter()

    for ruta in archivos:
        nombre_rel = str(ruta.relative_to(entrada))
        if ruta.name.upper() == "DICOMDIR":
            omitidos["DICOMDIR (lleva nombres, no se copia / carries names, not copied)"] += 1
            continue
        try:
            ds = pydicom.dcmread(ruta, force=True)
            meta_ok = getattr(ds, "file_meta", None) is not None and "MediaStorageSOPClassUID" in ds.file_meta
            if not (meta_ok or "SOPClassUID" in ds):
                omitidos["no es DICOM / not DICOM"] += 1
                continue
            sop = str(ds.get("SOPClassUID", "")) or str(ds.file_meta.get("MediaStorageSOPClassUID", ""))
            if sop not in SOP_PERMITIDAS:
                cuarentena.append({"archivo": hash_corto(nombre_rel, 8), "motivo": f"clase SOP no soportada: {nombre_sop(sop)}"})
                continue
            if str(ds.get("Modality", "")) not in MODALIDADES_PERMITIDAS:
                cuarentena.append({"archivo": hash_corto(nombre_rel, 8), "motivo": f"modalidad {ds.get('Modality', '')} no soportada"})
                continue
            if str(ds.get("BurnedInAnnotation", "")).upper() == "YES":
                cuarentena.append({"archivo": hash_corto(nombre_rel, 8), "motivo": "BurnedInAnnotation=YES: píxeles con texto, requiere revisión manual"})
                continue
            if str(ds.get("PatientIdentityRemoved", "")).upper() == "YES" and (
                    str(ds.get("DeidentificationMethod", "")).startswith("pet-ct-local") or re.match(PSEUDO_RE, str(ds.get("PatientID", "")))):
                ya_anonimizados += 1          # ya es una copia limpia: no se le asigna otro seudónimo ni se copia
                continue
            _ = ds.PixelData  # fuerza lectura completa; si falla, cuarentena

            pac, aviso = mapa.paciente(ds)
            if aviso:
                avisos.append(aviso)
            pacientes_vistos.add(pac["seudonimo"])
            factor_antes, _ = factor_suv(ds)

            avisos_arch: list[str] = []
            limpiar_dataset(ds, pac, mapa.secreto, acciones, avisos_arch, args.conservar_descripciones)
            avisos.extend(f"{pac['seudonimo']}: {a}" for a in avisos_arch)

            if str(ds.get("Modality", "")) == "PT":
                factor_despues, motivo = factor_suv(ds)
                if factor_antes is None:
                    suv[f"sin factor SUV: {motivo}"] += 1
                elif factor_despues == factor_antes:
                    suv["factor SUV idéntico antes y después / identical"] += 1
                else:
                    suv["FACTOR SUV CAMBIÓ, en cuarentena / changed, quarantined"] += 1
                    problemas_restantes.append(f"{pac['seudonimo']}: el factor SUV cambió al limpiar; archivo en cuarentena")
                    cuarentena.append({"archivo": hash_corto(nombre_rel, 8), "motivo": "el factor SUV cambió al limpiar / SUV factor changed"})
                    continue

            problemas, _ = verificar_dataset(ds, args.prefijo)
            if problemas:
                posverif["con problemas / with problems"] += 1
                problemas_restantes.extend(f"{pac['seudonimo']}: {p}" for p in problemas)
                cuarentena.append({"archivo": hash_corto(nombre_rel, 8), "motivo": "no pasó la verificación posterior"})
                continue

            ruta_salida = ruta_destino(ds, pac["seudonimo"], salida)
            destino = ruta_salida.parent
            destino.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=destino, prefix=".tmp_", suffix=".dcm")
            os.close(fd)
            ds.save_as(tmp, enforce_file_format=True)
            if ruta_salida.exists():
                nuevos_bytes = Path(tmp).read_bytes()
                if sha256_archivo(ruta_salida) == hashlib.sha256(nuevos_bytes).hexdigest():
                    duplicados += 1
                    os.unlink(tmp)
                    indice.anotar(ruta_salida, ruta, entrada)   # el duplicado verificado también queda indexado
                    continue
                if solo_cambia_version(ruta_salida, nuevos_bytes):
                    os.replace(tmp, ruta_salida)                 # misma imagen limpia escrita por una versión anterior: se actualiza en el sitio
                    os.chmod(ruta_salida, 0o600)
                    actualizados += 1
                    indice.anotar(ruta_salida, ruta, entrada)
                    continue
                os.unlink(tmp)
                cuarentena.append({"archivo": hash_corto(nombre_rel, 8), "motivo": "misma instancia SOP con contenido distinto / same SOP instance, different content"})
                continue
            os.replace(tmp, ruta_salida)
            os.chmod(ruta_salida, 0o600)
            indice.anotar(ruta_salida, ruta, entrada)
            posverif["limpios / clean"] += 1
            escritos += 1
            if escritos % 200 == 0:
                decir(f"  {escritos} archivos limpios", "files cleaned")
        except Exception as e:
            cuarentena.append({"archivo": hash_corto(nombre_rel, 8), "motivo": f"no se pudo procesar: {type(e).__name__}"})
            continue

    reporte = {
        "version": VERSION, "politica": POLITICA, "prefijo": args.prefijo,
        "conservar_descripciones": bool(args.conservar_descripciones),
        "fecha_ejecucion": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "archivos_leidos": len(archivos), "archivos_escritos": escritos, "duplicados_omitidos": duplicados,
        "actualizados_version": actualizados, "ya_anonimizados": ya_anonimizados,
        "archivos_omitidos": dict(omitidos), "cuarentena": cuarentena, "pacientes": len(pacientes_vistos),
        "acciones": dict(sorted(acciones.items())), "verificacion_posterior": dict(posverif),
        "cuantificacion_pet": dict(suv), "problemas_restantes": sorted(set(problemas_restantes)),
        "avisos": sorted(set(avisos)),
    }
    carpeta_reportes = salida.parent / f"{salida.name}_REPORTES"
    carpeta_reportes.mkdir(parents=True, exist_ok=True)
    marca = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    (carpeta_reportes / f"reporte_anonimizacion_{marca}.json").write_text(json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8")
    (carpeta_reportes / f"reporte_anonimizacion_{marca}.md").write_text(reporte_markdown(reporte), encoding="utf-8")
    (carpeta_reportes / "ultimo_reporte.json").write_text(json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8")
    (carpeta_reportes / "ultimo_reporte.md").write_text(reporte_markdown(reporte), encoding="utf-8")
    try:
        registro = {"fecha": reporte["fecha_ejecucion"], "version": VERSION, "politica": POLITICA, "prefijo": args.prefijo,
                    "conservar_descripciones": bool(args.conservar_descripciones), "entrada": str(entrada.resolve()),
                    "salida": str(salida.resolve()), "mapa": str(ruta_mapa.resolve()), "leidos": len(archivos), "escritos": escritos,
                    "duplicados": duplicados, "actualizados_version": actualizados, "ya_anonimizados": ya_anonimizados,
                    "cuarentena": len(cuarentena), "reporte": f"reporte_anonimizacion_{marca}.json"}
        with open(ruta_mapa.parent / "corridas.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
        os.chmod(ruta_mapa.parent / "corridas.jsonl", 0o600)
    except OSError:
        pass

    print()
    decir(f"Listo: {escritos} archivos limpios de {len(pacientes_vistos)} paciente(s); {duplicados} duplicados omitidos; "
          f"{actualizados} actualizados de una versión anterior; {len(cuarentena)} en cuarentena"
          + (f"; {ya_anonimizados} ya anonimizados en la entrada, no se tocaron" if ya_anonimizados else ""), "done")
    for k, v in suv.items():
        decir(f"PET: {k}: {v}", "PET quantification check")
    decir(f"Reporte compartible en {carpeta_reportes.name}/ (sin valores ni rutas)", "shareable report")
    decir("Mapa PRIVADO guardado; se queda en esta computadora", "PRIVATE map saved, stays on this computer")
    if problemas_restantes:
        decir("Atención: hubo problemas; revisa el reporte", "problems found, check the report")
    return 1 if (problemas_restantes or cuarentena) else 0


def cargar_prefijo(carpeta: Path, prefijo_cli: str | None) -> str:
    if prefijo_cli:
        return prefijo_cli
    for cand in (carpeta, carpeta.parent, carpeta.parent.parent):
        pol = cand / ".politica.json"
        if pol.exists():
            try:
                return json.loads(pol.read_text(encoding="utf-8")).get("prefijo", "ANON")
            except Exception:
                break
    return "ANON"




def verificar_carpeta(carpeta: Path, prefijo: str) -> dict:
    """Revisa TODOS los archivos de la carpeta: cada uno debe ser un DICOM limpio o un archivo propio."""
    total = limpios = 0
    fallos: dict[str, list[str]] = {}
    avisos_all: Counter = Counter()
    for raiz, dirs, nombres in os.walk(carpeta, followlinks=False):
        for d in list(dirs):
            if os.path.islink(os.path.join(raiz, d)):
                fallos[os.path.relpath(os.path.join(raiz, d), carpeta)] = ["enlace simbólico / symlink"]
                dirs.remove(d)
        for n in sorted(nombres):
            ruta = Path(raiz) / n
            rel = str(ruta.relative_to(carpeta))
            if ruta.is_symlink():
                fallos[rel] = ["enlace simbólico / symlink"]
                continue
            if Path(raiz) == carpeta and n == ".corriendo":
                continue
            if Path(raiz) == carpeta and n == ".politica.json":
                if not politica_valida(ruta):
                    fallos[rel] = [".politica.json no válido / invalid"]
                continue
            total += 1
            problemas, avisos = verificar_bytes(ruta.read_bytes(), prefijo)
            for a in avisos:
                avisos_all[a] += 1
            if problemas:
                fallos[rel] = problemas
            else:
                limpios += 1
    return {"carpeta": carpeta.name, "prefijo": prefijo, "archivos_revisados": total, "limpios": limpios,
            "con_problemas": len(fallos), "fallos": fallos, "avisos": dict(avisos_all)}


def cmd_verificar(args: argparse.Namespace) -> int:
    carpeta = Path(args.carpeta).expanduser()
    if not carpeta.is_dir():
        decir("No existe la carpeta", "folder not found")
        return 2
    prefijo = cargar_prefijo(carpeta, args.prefijo)
    r = verificar_carpeta(carpeta, prefijo)
    if args.json:
        Path(args.json).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    decir(f"Archivos revisados: {r['archivos_revisados']}, limpios: {r['limpios']}, con problemas: {r['con_problemas']} (prefijo {prefijo})",
          "files checked / clean / with problems")
    for ruta, problemas in list(r["fallos"].items())[:20]:
        print(f"  {ruta}")
        for p in problemas:
            print(f"    - {p}")
    if len(r["fallos"]) > 20:
        decir(f"  ... y {len(r['fallos']) - 20} más", "more")
    for a, n in r["avisos"].items():
        decir(f"Aviso ({n}): {a}", "warning")
    if r["archivos_revisados"] == 0 and not r["fallos"]:
        decir("No se encontraron archivos", "no files found")
        return 2
    return 1 if r["fallos"] else 0


def reporte_markdown(r: dict) -> str:
    L = ["# Reporte de anonimización / Anonymization report", "",
         f"Ejecutado el {r['fecha_ejecucion']} con anonimizar.py {r['version']}, política {r['politica']}. "
         "No contiene valores de etiquetas ni rutas, solo nombres y conteos, por eso se puede compartir. "
         "(No tag values or paths, only names and counts, so it is safe to share.)", "",
         "## Resumen / Summary", "",
         f"- Archivos leídos (files read): {r['archivos_leidos']}",
         f"- Archivos limpios escritos (clean files written): {r['archivos_escritos']}",
         f"- Duplicados omitidos (duplicates skipped): {r['duplicados_omitidos']}",
         f"- Actualizados desde una versión anterior de la herramienta (upgraded in place): {r.get('actualizados_version', 0)}",
         f"- Ya anonimizados en la entrada, no se tocaron (already anonymized inputs, left alone): {r.get('ya_anonimizados', 0)}",
         f"- Pacientes con seudónimo (patients pseudonymized): {r['pacientes']}",
         f"- En cuarentena, no copiados (quarantined, not copied): {len(r['cuarentena'])}"]
    for k, v in r["archivos_omitidos"].items():
        L.append(f"- Omitidos, {k}: {v}")
    L += ["", "## Verificación posterior / Post-check", ""]
    L += [f"- {k}: {v}" for k, v in r["verificacion_posterior"].items()] or ["- sin archivos / no files"]
    L += ["", "## Cuantificación PET / PET quantification", ""]
    L += [f"- {k}: {v}" for k, v in r["cuantificacion_pet"].items()] or ["- no había PET / no PET files"]
    if r["cuarentena"]:
        L += ["", "## Cuarentena / Quarantine", "", "Cada archivo se identifica por un hash de su ruta relativa, no por su nombre.", ""]
        motivos = Counter(c["motivo"] for c in r["cuarentena"])
        L += [f"- {m}: {n}" for m, n in motivos.items()]
    if r["problemas_restantes"]:
        L += ["", "## Problemas / Problems", ""] + [f"- {p}" for p in r["problemas_restantes"]]
    L += ["", "## Acciones por etiqueta / Actions by tag", "",
          "En la siguiente tabla se ve cuántas veces se aplicó cada acción (how many times each action was applied).", "",
          "| Etiqueta y acción / Tag and action | Veces / Count |", "|---|---:|"]
    L += [f"| {k} | {v} |" for k, v in r["acciones"].items()]
    L += ["", "## Avisos / Warnings", ""] + ([f"- {a}" for a in r["avisos"]] or ["- Ninguno / none"])
    L += ["", "## Política aplicada / Policy applied", "",
          f"- {r['politica']}: PS3.15 Basic Application Confidentiality Profile (113100), Retain Patient Characteristics (113108), "
          "Retain Longitudinal Temporal Information with Modified Dates (113107)",
          "- Solo objetos CT, PET, MR y Segmentation; el resto va a cuarentena",
          "- Texto libre: lista cerrada de campos técnicos; descripciones eliminadas salvo --conservar-descripciones; URI y binarios fuera de píxeles eliminados; CodeMeaning canónico",
          "- UID de instancia reescritos bajo la raíz del proyecto; clases SOP y UID semánticos intactos",
          "- Preámbulo a cero, metadatos de archivo reconstruidos, privadas eliminadas salvo factores PET de Philips y fechas-hora de referencia de GE y Siemens (desplazadas)",
          "- Píxeles sin cambios: texto grabado y rostros requieren un paso aparte", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="anonimizar.py",
                                description=f"Anonimizador DICOM, política {POLITICA} (DICOM anonymizer)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("limpiar", help="crear la copia limpia (create the clean copy)")
    a.add_argument("entrada")
    a.add_argument("salida")
    a.add_argument("--mapa", help="ruta del mapa privado (private map path)")
    a.add_argument("--prefijo", default="ANON", help="prefijo de seudónimo, letras y dígitos (pseudonym prefix)")
    a.add_argument("--conservar-descripciones", action="store_true",
                   help="conservar descripciones de estudio y serie y protocolo, revisadas contra nombre e ID (keep descriptions)")
    a.set_defaults(func=cmd_limpiar)
    v = sub.add_parser("verificar", help="revisar que una carpeta cumpla la política (check a folder)")
    v.add_argument("carpeta")
    v.add_argument("--prefijo", help="prefijo esperado; por defecto se lee de .politica.json")
    v.add_argument("--json")
    v.set_defaults(func=cmd_verificar)
    args = p.parse_args(argv)
    if getattr(args, "prefijo", None) and not re.fullmatch(r"[A-Z0-9]{2,12}", args.prefijo):
        decir("El prefijo debe tener de 2 a 12 letras mayúsculas o dígitos", "prefix: 2 to 12 uppercase letters or digits")
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
