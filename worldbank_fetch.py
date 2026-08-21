```python
import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


API_URL = "https://search.worldbank.org/api/procnotices"

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

JSON_PATH = OUTPUT_DIR / "latest.json"
CSV_PATH = OUTPUT_DIR / "latest.csv"
METADATA_PATH = OUTPUT_DIR / "metadata.json"

ROWS_PER_PAGE = 100
MAX_RETRIES = 5
RETRY_WAIT_SECONDS = 5

# Protección para evitar loops infinitos
MAX_PAGES = 500


def now_utc():
    return datetime.now(timezone.utc)


def now_utc_iso():
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value):
    if not value:
        return None

    value = str(value).strip()

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%m/%d/%Y",
        "%d-%b-%Y",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(value[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return None


def request_with_retry(params):
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                API_URL,
                params=params,
                timeout=60,
            )

            if response.status_code == 200:
                return response

            if response.status_code in {500, 502, 503, 504}:
                print(
                    f"HTTP {response.status_code}. "
                    f"Retry {attempt}/{MAX_RETRIES}"
                )

                last_error = RuntimeError(
                    f"HTTP {response.status_code}"
                )

                time.sleep(RETRY_WAIT_SECONDS * attempt)
                continue

            response.raise_for_status()

        except Exception as exc:
            last_error = exc

            print(
                f"Request error: {exc}. "
                f"Retry {attempt}/{MAX_RETRIES}"
            )

            time.sleep(RETRY_WAIT_SECONDS * attempt)

    raise RuntimeError(
        f"No fue posible consultar la API despues de "
        f"{MAX_RETRIES} intentos: {last_error}"
    )


def extract_records(payload):
    if isinstance(payload, list):
        return payload

    candidates = [
        payload.get("documents"),
        payload.get("data"),
        payload.get("results"),
        payload.get("procnotices"),
    ]

    for value in candidates:
        if isinstance(value, list):
            return value

    return []


def get_value(record, *keys):
    for key in keys:
        value = record.get(key)

        if value not in (None, ""):
            return value

    return ""


def extract_deadline(record):
    return get_value(
        record,
        "deadline",
        "deadline_date",
        "submission_deadline",
        "closing_date",
    )


def extract_publication_date(record):
    return get_value(
        record,
        "publication_date",
        "published_date",
        "notice_date",
    )


def is_current(record, extraction_dt):
    deadline = extract_deadline(record)

    dt = parse_date(deadline)

    if not dt:
        return False

    return dt >= extraction_dt


def is_english_or_spanish(record):
    language = str(
        get_value(
            record,
            "language",
            "language_name",
            "lang_name",
        )
    ).strip().lower()

    return (
        language in {"en", "es", "english", "spanish"}
        or "english" in language
        or "spanish" in language
    )


def is_consulting(record):
    fields = [
        get_value(
            record,
            "procurement_method_name",
            "procurement_method",
        ),
        get_value(
            record,
            "procurement_method_code",
        ),
        get_value(
            record,
            "procurement_category",
            "procurement_type",
        ),
        get_value(
            record,
            "notice_type",
            "notice_type_name",
        ),
        get_value(
            record,
            "title",
            "notice_title",
        ),
    ]

    text = " ".join(
        str(value or "") for value in fields
    ).lower()

    consulting_markers = [
        "consulting",
        "consultancy",
        "consultant",
        "expression of interest",
        "request for expression of interest",
        "qcbs",
        "qbs",
        "cqs",
        "lcs",
        "fbs",
        "indv",
        "individual consultant",
    ]

    return any(
        marker in text
        for marker in consulting_markers
    )


def normalize_record(record):
    return {
        "notice_id": get_value(
            record,
            "id",
            "notice_id",
            "proc_notice_id",
        ),
        "project_id": get_value(
            record,
            "project_id",
            "projectid",
        ),
        "reference_no": get_value(
            record,
            "reference_no",
            "reference",
            "procurement_reference",
        ),
        "title": get_value(
            record,
            "title",
            "notice_title",
            "description",
        ),
        "country": get_value(
            record,
            "country",
            "country_name",
        ),
        "project_name": get_value(
            record,
            "project_name",
            "project",
        ),
        "institution": get_value(
            record,
            "borrower",
            "agency",
            "implementing_agency",
        ),
        "publication_date": extract_publication_date(
            record
        ),
        "deadline": extract_deadline(
            record
        ),
        "language": get_value(
            record,
            "language",
            "language_name",
            "lang_name",
        ),
        "notice_type": get_value(
            record,
            "notice_type",
            "notice_type_name",
        ),
        "procurement_method_code": get_value(
            record,
            "procurement_method_code",
        ),
        "procurement_method_name": get_value(
            record,
            "procurement_method_name",
            "procurement_method",
        ),
        "notice_text": get_value(
            record,
            "notice_text",
            "description",
        ),
        "source_url": get_value(
            record,
            "url",
            "notice_url",
        ),
    }


def sha256_file(path):
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(
            lambda: f.read(1024 * 1024),
            b"",
        ):
            h.update(chunk)

    return h.hexdigest()


def page_has_current_records(records, extraction_dt):
    for record in records:
        if is_current(record, extraction_dt):
            return True

    return False


def page_is_old_enough_to_stop(records, extraction_dt):
    """
    Devuelve True solo si todos los registros de la pagina
    tienen deadline conocida y vencida.
    """

    if not records:
        return True

    deadlines = []

    for record in records:
        dt = parse_date(extract_deadline(record))

        if dt is None:
            return False

        deadlines.append(dt)

    return all(
        deadline < extraction_dt
        for deadline in deadlines
    )


def main():
    extracted_at = now_utc_iso()
    extraction_dt = now_utc()

    all_scanned_records = []
    current_records = []

    page = 0
    coverage_complete = False
    stop_reason = ""

    print(
        "Consultando World Bank Procurement Notices..."
    )
    print(
        f"Extraction time: {extracted_at}"
    )

    while page < MAX_PAGES:
        offset = page * ROWS_PER_PAGE

        params = {
            "format": "json",
            "rows": ROWS_PER_PAGE,
            "os": offset,
        }

        print(
            f"Descargando pagina {page + 1} "
            f"- offset {offset}"
        )

        response = request_with_retry(params)
        payload = response.json()

        records = extract_records(payload)

        if not records:
            coverage_complete = True
            stop_reason = "API returned empty page"
            break

        all_scanned_records.extend(records)

        for record in records:
            if not is_current(
                record,
                extraction_dt,
            ):
                continue

            if not is_english_or_spanish(
                record
            ):
                continue

            if not is_consulting(
                record
            ):
                continue

            current_records.append(
                normalize_record(record)
            )

        if len(records) < ROWS_PER_PAGE:
            coverage_complete = True
            stop_reason = "Last partial page reached"
            break

        if page_is_old_enough_to_stop(
            records,
            extraction_dt,
        ):
            coverage_complete = True
            stop_reason = (
                "Reached page containing only "
                "expired notices"
            )
            break

        page += 1

    if not coverage_complete:
        raise RuntimeError(
            "No fue posible demostrar cobertura completa "
            f"antes de alcanzar MAX_PAGES={MAX_PAGES}"
        )

    # Deduplicar por notice_id + referencia
    unique = {}

    for record in current_records:
        key = (
            str(record.get("notice_id") or ""),
            str(record.get("reference_no") or ""),
        )

        unique[key] = record

    current_records = list(unique.values())

    current_records.sort(
        key=lambda x: (
            x.get("deadline") or "",
            x.get("notice_id") or "",
        )
    )

    output = {
        "source": "World Bank Procurement Notices",
        "fechaExtraccion": extracted_at,
        "pagesScanned": page + 1,
        "recordsScanned": len(
            all_scanned_records
        ),
        "currentConsultingEnEs": len(
            current_records
        ),
        "coverageComplete": coverage_complete,
        "stopReason": stop_reason,
        "data": current_records,
    }

    with JSON_PATH.open(
        "w",
        encoding="utf-8",
    ) as f:
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
        writer.writerows(
            current_records
        )

    sha_json = sha256_file(
        JSON_PATH
    )

    sha_csv = sha256_file(
        CSV_PATH
    )

    metadata = {
        "source": "World Bank Procurement Notices",
        "extracted_at": extracted_at,
        "pages_scanned": page + 1,
        "records_scanned": len(
            all_scanned_records
        ),
        "current_consulting_en_es": len(
            current_records
        ),
        "json_records": len(
            current_records
        ),
        "csv_records": len(
            current_records
        ),
        "coverage_complete": coverage_complete,
        "stop_reason": stop_reason,
        "validation_status": (
            "OK"
            if coverage_complete
            else "INCOMPLETE"
        ),
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
    print(
        "=========================================="
    )
    print(
        "WORLD BANK PROCUREMENT VALIDATION"
    )
    print(
        "=========================================="
    )
    print(
        f"Pages scanned:              {page + 1}"
    )
    print(
        f"Records scanned:            "
        f"{len(all_scanned_records)}"
    )
    print(
        f"Current consulting EN/ES:   "
        f"{len(current_records)}"
    )
    print(
        f"Coverage complete:          "
        f"{coverage_complete}"
    )
    print(
        f"Stop reason:                "
        f"{stop_reason}"
    )
    print(
        "Validation status:          OK"
    )
    print(
        "=========================================="
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"ERROR: {exc}"
        )
        sys.exit(1)
```
