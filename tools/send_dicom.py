#!/usr/bin/env python3
"""Generate and send synthetic screening mammography exams via DICOM C-STORE."""

import argparse
import random
import sys
from datetime import date, timedelta

import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid, ExplicitVRLittleEndian
from pynetdicom import AE, StoragePresentationContexts
import numpy as np

# SOP Class: Digital Mammography X-Ray Image Storage - For Processing
DIGITAL_MAMMO_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.1.2.1"

VIEWS = [
    {"desc": "Left CC",  "laterality": "L", "view": "CC",  "series_num": 1},
    {"desc": "Left MLO", "laterality": "L", "view": "MLO", "series_num": 2},
    {"desc": "Right CC",  "laterality": "R", "view": "CC",  "series_num": 3},
    {"desc": "Right MLO", "laterality": "R", "view": "MLO", "series_num": 4},
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Anderson", "Taylor", "Thomas", "Moore",
]
FIRST_NAMES = [
    "Maria", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica",
    "Sarah", "Karen", "Lisa", "Nancy", "Betty", "Dorothy", "Sandra",
]
REFERRING_PHYSICIANS = [
    "Lee^David", "Patel^Anita", "Chen^Wei", "Kim^Soo-Jin",
    "Garcia^Carlos", "Nguyen^Thi", "Williams^James", "Singh^Priya",
]


def generate_patient_info(index: int, rng: random.Random) -> dict:
    last = rng.choice(LAST_NAMES)
    first = rng.choice(FIRST_NAMES)
    birth_year = rng.randint(1950, 1990)
    birth_month = rng.randint(1, 12)
    birth_day = rng.randint(1, 28)
    return {
        "patient_id": f"SIM-{index:04d}",
        "patient_name": f"{last}^{first}",
        "patient_birth_date": f"{birth_year}{birth_month:02d}{birth_day:02d}",
        "patient_sex": "F",
    }


def generate_study_info(index: int, rng: random.Random) -> dict:
    study_date = date.today() - timedelta(days=rng.randint(0, 30))
    study_date_str = study_date.strftime("%Y%m%d")
    hour = rng.randint(7, 17)
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)
    return {
        "study_instance_uid": generate_uid(),
        "study_date": study_date_str,
        "study_time": f"{hour:02d}{minute:02d}{second:02d}",
        "accession_number": f"ACC-{study_date_str}-{index:03d}",
        "study_description": "Screening Mammography",
        "modality": "MG",
        "referring_physician": rng.choice(REFERRING_PHYSICIANS),
    }


def create_mammography_instance(
    patient: dict, study: dict, view: dict, instance_number: int, rng: random.Random
) -> Dataset:
    ds = Dataset()

    # --- File Meta ---
    file_meta = pydicom.Dataset()
    file_meta.MediaStorageSOPClassUID = DIGITAL_MAMMO_SOP_CLASS
    sop_instance_uid = generate_uid()
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = file_meta
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    # --- Patient ---
    ds.PatientID = patient["patient_id"]
    ds.PatientName = patient["patient_name"]
    ds.PatientBirthDate = patient["patient_birth_date"]
    ds.PatientSex = patient["patient_sex"]

    # --- Study ---
    ds.StudyInstanceUID = study["study_instance_uid"]
    ds.StudyDate = study["study_date"]
    ds.StudyTime = study["study_time"]
    ds.AccessionNumber = study["accession_number"]
    ds.StudyDescription = study["study_description"]
    ds.Modality = study["modality"]
    ds.ReferringPhysicianName = study["referring_physician"]

    # --- Series ---
    ds.SeriesInstanceUID = generate_uid()
    ds.SeriesNumber = view["series_num"]
    ds.SeriesDate = study["study_date"]
    ds.SeriesDescription = view["desc"]
    ds.BodyPartExamined = "BREAST"
    ds.Laterality = view["laterality"]
    ds.ViewPosition = view["view"]

    # --- Instance ---
    ds.SOPClassUID = DIGITAL_MAMMO_SOP_CLASS
    ds.SOPInstanceUID = sop_instance_uid
    ds.InstanceNumber = instance_number

    # --- Pixel Data (64x64 16-bit monochrome) ---
    ds.Rows = 64
    ds.Columns = 64
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    pixels = np.random.RandomState(rng.randint(0, 2**31)).randint(
        0, 4096, (64, 64), dtype=np.uint16
    )
    ds.PixelData = pixels.tobytes()

    return ds


def generate_exam(exam_index: int, rng: random.Random) -> tuple[dict, dict, list[Dataset]]:
    patient = generate_patient_info(exam_index, rng)
    study = generate_study_info(exam_index, rng)
    datasets = []
    for i, view in enumerate(VIEWS, start=1):
        ds = create_mammography_instance(patient, study, view, i, rng)
        datasets.append(ds)
    return patient, study, datasets


def send_exam(
    datasets: list[Dataset], host: str, port: int, ae_title: str, calling_ae: str
) -> tuple[int, int]:
    ae = AE(calling_ae)
    ae.add_requested_context(DIGITAL_MAMMO_SOP_CLASS, ExplicitVRLittleEndian)

    try:
        assoc = ae.associate(host, port, ae_title=ae_title)
    except Exception as exc:
        print(f"  Connection failed: {exc}", file=sys.stderr)
        return 0, len(datasets)

    if not assoc.is_established:
        print(f"  Association rejected by {ae_title}@{host}:{port}", file=sys.stderr)
        return 0, len(datasets)

    sent = 0
    failed = 0
    for i, ds in enumerate(datasets, start=1):
        view_label = f"{ds.Laterality}-{ds.ViewPosition}".ljust(5)
        print(f"  Sending {view_label} ({i}/{len(datasets)}) ... ", end="", flush=True)
        status = assoc.send_c_store(ds)
        if status and status.Status == 0x0000:
            print("OK")
            sent += 1
        else:
            code = f"0x{status.Status:04X}" if status else "no response"
            print(f"FAILED ({code})")
            failed += 1

    assoc.release()
    return sent, failed


def main():
    parser = argparse.ArgumentParser(
        description="Send synthetic screening mammography exams via DICOM C-STORE"
    )
    parser.add_argument("ae_title", help="Target AE title")
    parser.add_argument("--host", default="localhost", help="Target host (default: localhost)")
    parser.add_argument("--port", type=int, default=11112, help="Target port (default: 11112)")
    parser.add_argument("--count", type=int, default=1, help="Number of exams to send (default: 1)")
    parser.add_argument("--calling-ae", default="TEST_SCU", help="Calling AE title (default: TEST_SCU)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible data")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    total_sent = 0
    total_failed = 0

    for exam_idx in range(1, args.count + 1):
        patient, study, datasets = generate_exam(exam_idx, rng)
        print(
            f"\n[{exam_idx}/{args.count}] Patient: {patient['patient_id']} "
            f"({patient['patient_name']}) | {study['accession_number']}"
        )
        sent, failed = send_exam(datasets, args.host, args.port, args.ae_title, args.calling_ae)
        total_sent += sent
        total_failed += failed

    total_images = args.count * 4
    print(f"\nDone. Sent {args.count} exam(s) ({total_sent} images), {total_failed} failure(s).")
    sys.exit(1 if total_failed > 0 else 0)


if __name__ == "__main__":
    main()
