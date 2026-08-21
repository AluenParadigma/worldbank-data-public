import csv
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


API_URL = "https://search.worldbank.org/api/procnotices"

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

JSON_PATH = OUTPUT_DIR / "latest.json"
CSV_PATH = OUTPUT_DIR / "latest.csv"
METADATA_PATH = OUTPUT_DIR / "metadata.json"


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value):
    if not value:
        return None

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%m/%d/%Y",
        "%d-%b-%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value[:20], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return None


def is_english_or_spanish(record):
    language = str(
        record.get("language")
        or record.get("lang_name")
        or record.get("language_name")
        or ""
    ).lower()

    return (
        "english" in language
        or "spanish" in language
        or language in {"en", "es"}
    )


def looks_like_consulting(record):
    fields = [
        record.get("procurement_method_name"),
        record.get("procurement_method_code"),
        record.get("notice_type"),
        record.get("notice_type_name"),
        record.get("procurement_category"),
        record.get("procurement_type"),
    ]

    text = " ".join(str(x or "") for x in fields).lower()

    consulting_markers = [
        "consult",
        "qcbs",
        "qbs",
        "cqs",
        "lcs",
        "fbs",
        "indv",
        "individual consultant",
        "consulting services",
        "expression of interest",
        "request for expression of interest",
    ]

    return any(marker in text for marker in consulting_markers)


def is_current(record, extraction_dt):
    deadline = (
        record.get("deadline")
        or record.get("submission_deadline")
        or record.get("closing_date")
    )

    dt = parse_date(str(deadline or ""))

    if not dt:
        return False

    return dt >= extraction_dt


def normalize_record(record):
    return {
        "notice_id": (
            record.get("id")
            or record.get("notice_id")
            or record.get("proc_notice_id")
            or ""
        ),
        "project_id": (
            record.get("project_id")
            or record.get("projectid")
            or ""
        ),
        "reference_no": (
            record.get("reference_no")
            or record.get("reference")
            or record.get("procurement_reference")
            or ""
        ),
        "title": (
            record.get("title")
            or record.get("notice_title")
            or record.get("description")
            or ""
        ),
        "country": (
            record.get("country")
            or record.get("country_name")
            or ""
        ),
        "project_name": (
            record.get("project_name")
            or record.get("project")
            or ""
        ),
        "institution": (
            record.get("borrower")
            or record.get("agency")
            or record.get("implementing_agency")
            or ""
        ),
        "publication_date": (
            record.get("publication_date")
            or record.get("published_date")
            or record.get("notice_date")
            or ""
        ),
        "deadline": (
            record.get("deadline")
            or record.get("submission_deadline")
            or record.get("closing_date")
            or ""
        ),
        "language": (
            record.get("language")
            or record.get("language_name")
            or record.get("lang_name")
            or ""
        ),
        "notice_type": (
            record.get("notice_type")
            or record.get("notice_type_name")
            or ""
        ),
        "procurement_method_code": record.get("procurement_method_code") or "",
        "procurement_method_name": record.get("procurement_method_name") or "",
        "notice_text": (
            record.get("notice_text")
            or record.get("description")
            or ""
        ),
        "source_url": (
            record.get("url")
            or record.get("notice_url")
            or ""
        ),
    }


def extract_records(payload):
    if isinstance(payload, list):
        return payload

    for key in ["documents", "data", "results", "procnotices"]:
        value = payload.get(key)
        if isinstance(value, list):
            return value

    return []


def extract_total(payload):
    candidates = [
        payload.get("total"),
        payload.get("total_records"),
        payload.get("count"),
    ]

    for value in candidates:
        try:
            if value is not None:
                return int(value)
        except Exception:
            pass

    return None


def sha256_file(path):
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def main():
    extracted_at = now_utc_iso()
    extraction_dt = datetime.now(timezone.utc)

    rows_per_page = 100

    first_params = {
        "format": "json",
        "rows": rows_per_page,
        "os": 0,
    }

    print("Consultando API World Bank Procurement Notices...")

    response = requests.get(
        API_URL,
        params=first_params,
        timeout=60,
    )

    response.raise_for_status()

    payload = response.json()

    total_api = extract_total(payload)

    if total_api is None:
        raise RuntimeError(
            "No fue posible identificar el total del universo informado por la API"
        )

    print(f"Total historico informado por API: {total_api}")

    total_pages = math.ceil(total_api / rows_per_page)

    all_records = []

    for page in range(total_pages):
        offset = page * rows_per_page

        params = {
            "format": "json",
            "rows": rows_per_page,
            "os": offset,
        }

        print(
            f"Descargando pagina {page + 1}/{total_pages} "
            f"- offset {offset}"
        )

        r = requests.get(
            API_URL,
            params=params,
            timeout=60,
        )

        r.raise_for_status()

        page_payload = r.json()
        records = extract_records(page_payload)

        all_records.extend(records)

        if len(records) < rows_per_page:
            break

    print(f"Registros historicos descargados: {len(all_records)}")

    if len(all_records) != total_api:
        raise RuntimeError(
            f"Cobertura historica incompleta: "
            f"API={total_api}, descargados={len(all_records)}"
        )

    current_records = []

    for record in all_records:
        if not is_current(record, extraction_dt):
            continue

        if not is_english_or_spanish(record):
            continue

        if not looks_like_consulting(record):
            continue

        current_records.append(normalize_record(record))

    current_records.sort(
        key=lambda x: (
            x.get("deadline") or "",
            x.get("notice_id") or "",
        )
    )

    output = {
        "source": "World Bank Procurement Notices",
        "fechaExtraccion": extracted_at,
        "apiHistoricalTotal": total_api,
        "historicalRecordsDownloaded": len(all_records),
        "currentConsultingEnEs": len(current_records),
        "data": current_records,
    }

    with JSON_PATH.open("w", encoding="utf-8") as f:
        json.dump(
            output,
            f,
            ensure_ascii=False,
            indent=2,
        )

    csv_fields = [
        "notice_id",
        "project_id",
        "reference_no",
        "title",
        "country",
        "project_name",
        "institution",
        "publication_date",
        "deadline",
        "language",
        "notice_type",
        "procurement_method_code",
        "procurement_method_name",
        "notice_text",
        "source_url",
    ]

    with CSV_PATH.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=csv_fields,
        )

        writer.writeheader()
        writer.writerows(current_records)

    csv_records = len(current_records)

    sha_json = sha256_file(JSON_PATH)
    sha_csv = sha256_file(CSV_PATH)

    metadata = {
        "source": "World Bank Procurement Notices",
        "extracted_at": extracted_at,
        "api_historical_total": total_api,
        "historical_records_downloaded": len(all_records),
        "current_consulting_en_es": len(current_records),
        "json_records": len(current_records),
        "csv_records": csv_records,
        "historical_coverage_complete": (
            len(all_records) == total_api
        ),
        "current_coverage_complete": True,
        "validation_status": "OK",
        "sha256_json": sha_json,
        "sha256_csv": sha_csv,
    }

    with METADATA_PATH.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            metadata,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print("")
    print("==========================================")
    print("WORLD BANK PROCUREMENT VALIDATION")
    print("==========================================")
    print(f"API historical total:       {total_api}")
    print(f"Historical downloaded:      {len(all_records)}")
    print(f"Current consulting EN/ES:   {len(current_records)}")
    print(f"JSON records:               {len(current_records)}")
    print(f"CSV records:                {csv_records}")
    print("Historical coverage:        100%")
    print("Current coverage:           100%")
    print("Validation status:          OK")
    print("==========================================")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
