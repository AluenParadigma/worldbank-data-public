import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


API_URL = "https://search.worldbank.org/api/v2/procnotices"

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

JSON_PATH = OUTPUT_DIR / "latest.json"
CSV_PATH = OUTPUT_DIR / "latest.csv"
METADATA_PATH = OUTPUT_DIR / "metadata.json"

ROWS_PER_PAGE = 100

MAX_RETRIES = 5
RETRY_WAIT_SECONDS = 3
REQUEST_DELAY_SECONDS = 0.10

CONSULTING_GROUP = "CS"
NOTICE_TYPE = "Request for Expression of Interest"

LANGUAGE_FILTERS = [
    "English",
    "Spanish",
    "Spanish; Castilian",
]


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value):
    if not value:
        return None

    value = str(value).strip()

    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%b-%Y %H:%M:%S",
        "%m/%d/%Y",
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

    headers = {
        "User-Agent": "Paradigma-WorldBank-Monitor/3.0",
        "Accept": "application/json",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                API_URL,
                params=params,
                headers=headers,
                timeout=90,
            )

            if response.status_code == 200:
                return response

            if response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }:
                print(
                    f"HTTP {response.status_code}. "
                    f"Reintento {attempt}/{MAX_RETRIES}"
                )

                last_error = RuntimeError(
                    f"HTTP {response.status_code}"
                )

                time.sleep(
                    RETRY_WAIT_SECONDS * attempt
                )

                continue

            response.raise_for_status()

        except Exception as exc:
            last_error = exc

            print(
                f"Error HTTP: {exc}. "
                f"Reintento {attempt}/{MAX_RETRIES}"
            )

            time.sleep(
                RETRY_WAIT_SECONDS * attempt
            )

    raise RuntimeError(
        "No fue posible consultar la API despues de "
        f"{MAX_RETRIES} intentos. "
        f"Ultimo error: {last_error}"
    )


def extract_records(payload):
    records = payload.get("procnotices", [])

    if isinstance(records, list):
        return records

    if isinstance(records, dict):
        return [
            item
            for item in records.values()
            if isinstance(item, dict)
        ]

    return []


def extract_total(payload):
    value = payload.get("total")

    if value is None:
        raise RuntimeError(
            "La API no devolvio el campo total"
        )

    return int(value)


def get_value(record, *keys):
    for key in keys:
        value = record.get(key)

        if value not in (
            None,
            "",
            [],
            {},
        ):
            return value

    return ""


def normalize_language(value):
    return str(
        value or ""
    ).strip().lower()


def language_matches(actual, requested):
    actual = normalize_language(actual)
    requested = normalize_language(requested)

    if requested == "english":
        return actual == "english"

    if "spanish" in requested:
        return "spanish" in actual

    return actual == requested


def deadline_datetime(record):
    value = get_value(
        record,
        "submission_deadline_date",
        "deadline_date",
        "deadline",
    )

    dt = parse_date(value)

    if dt is None:
        return None

    time_value = str(
        get_value(
            record,
            "submission_deadline_time",
        )
    ).strip()

    if time_value:
        try:
            parts = (
                time_value
                .replace(".", ":")
                .split(":")
            )

            hour = int(parts[0])

            minute = (
                int(parts[1])
                if len(parts) > 1
                else 0
            )

            dt = dt.replace(
                hour=hour,
                minute=minute,
                second=0,
            )

        except Exception:
            pass

    return dt


def is_inactive(record):
    status = str(
        record.get(
            "notice_status",
            ""
        )
    ).strip().lower()

    return status in {
        "cancelled",
        "canceled",
        "withdrawn",
        "deleted",
        "draft",
    }


def validate_record_filter(
    record,
    requested_language
):
    procurement_group = str(
        record.get(
            "procurement_group",
            ""
        )
    ).strip()

    notice_type = str(
        record.get(
            "notice_type",
            ""
        )
    ).strip()

    actual_language = record.get(
        "notice_lang_name",
        ""
    )

    if procurement_group != CONSULTING_GROUP:
        return False

    if notice_type != NOTICE_TYPE:
        return False

    if not language_matches(
        actual_language,
        requested_language
    ):
        return False

    return True


def fetch_page(
    base_params,
    offset
):
    params = dict(base_params)
    params["os"] = offset

    response = request_with_retry(params)
    payload = response.json()

    return (
        extract_total(payload),
        extract_records(payload),
    )


def validate_page_records(
    records,
    language_filter
):
    for record in records:
        if not validate_record_filter(
            record,
            language_filter
        ):
            raise RuntimeError(
                "La API devolvio un registro "
                "fuera del filtro solicitado"
            )


def fetch_language_universe(
    language_filter
):
    print("")
    print(
        "=========================================="
    )
    print(
        f"LANGUAGE FILTER: {language_filter}"
    )
    print(
        "=========================================="
    )

    base_params = {
        "format": "json",
        "rows": ROWS_PER_PAGE,
        "procurement_group_exact":
            CONSULTING_GROUP,
        "notice_type_exact":
            NOTICE_TYPE,
        "notice_lang_name_exact":
            language_filter,
    }

    # Primera consulta
    api_total, records = fetch_page(
        base_params,
        0
    )

    print(
        f"API total: {api_total}"
    )

    if api_total == 0:
        return {
            "language": language_filter,
            "api_total": 0,
            "records": [],
            "pages": 1,
            "duplicate_records": 0,
            "recovery_passes": 0,
            "coverage_complete": True,
        }

    if not records:
        raise RuntimeError(
            "Primera pagina vacia "
            "con total > 0"
        )

    validate_page_records(
        records,
        language_filter
    )

    all_by_id = {}

    raw_downloaded = 0
    pages = 0

    # --------------------------------------------------------
    # PASADA PRINCIPAL
    # --------------------------------------------------------

    offset = 0

    while offset < api_total:
        current_total, records = fetch_page(
            base_params,
            offset
        )

        if current_total != api_total:
            raise RuntimeError(
                "El total de la API cambio "
                "durante la extraccion: "
                f"{api_total} -> "
                f"{current_total}"
            )

        if not records:
            raise RuntimeError(
                "Pagina vacia antes "
                "de alcanzar el total"
            )

        validate_page_records(
            records,
            language_filter
        )

        for record in records:
            notice_id = str(
                record.get(
                    "id",
                    ""
                )
            ).strip()

            if not notice_id:
                raise RuntimeError(
                    "Registro sin ID"
                )

            all_by_id[notice_id] = record

        raw_downloaded += len(records)
        pages += 1

        print(
            f"Pagina {pages} | "
            f"offset={offset} | "
            f"raw={raw_downloaded}/"
            f"{api_total} | "
            f"unique={len(all_by_id)}"
        )

        offset += len(records)

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

    duplicates = (
        raw_downloaded
        - len(all_by_id)
    )

    print("")
    print(
        f"Primera pasada:"
    )
    print(
        f"Raw downloaded: {raw_downloaded}"
    )
    print(
        f"Unique IDs: {len(all_by_id)}"
    )
    print(
        f"Duplicates detected: {duplicates}"
    )

    # --------------------------------------------------------
    # RECUPERACION CON PAGINACION SOLAPADA
    #
    # Si el dataset se movio durante la primera pasada,
    # repetimos usando pasos de 50 registros.
    # Esto genera overlap y permite capturar IDs omitidos
    # por cambios de orden entre llamadas.
    # --------------------------------------------------------

    recovery_passes = 0

    overlap_step = 50

    while (
        len(all_by_id) < api_total
        and recovery_passes < 3
    ):
        recovery_passes += 1

        print("")
        print(
            "=========================================="
        )
        print(
            f"RECOVERY PASS {recovery_passes}"
        )
        print(
            "=========================================="
        )

        offset = 0

        before = len(all_by_id)

        while offset < api_total:
            current_total, records = fetch_page(
                base_params,
                offset
            )

            if current_total != api_total:
                raise RuntimeError(
                    "El total de la API cambio "
                    "durante recovery: "
                    f"{api_total} -> "
                    f"{current_total}"
                )

            if not records:
                break

            validate_page_records(
                records,
                language_filter
            )

            for record in records:
                notice_id = str(
                    record.get(
                        "id",
                        ""
                    )
                ).strip()

                if notice_id:
                    all_by_id[
                        notice_id
                    ] = record

            print(
                f"Recovery offset={offset} | "
                f"unique="
                f"{len(all_by_id)}/"
                f"{api_total}"
            )

            offset += overlap_step

            time.sleep(
                REQUEST_DELAY_SECONDS
            )

        added = (
            len(all_by_id)
            - before
        )

        print(
            f"Nuevos IDs recuperados: "
            f"{added}"
        )

        if added == 0:
            break

    # --------------------------------------------------------
    # VALIDACION FINAL
    # --------------------------------------------------------

    unique_records = list(
        all_by_id.values()
    )

    if len(unique_records) != api_total:
        raise RuntimeError(
            "No fue posible alcanzar cobertura "
            "100% aun despues de recovery: "
            f"{len(unique_records)} "
            f"!= {api_total}"
        )

    print("")
    print(
        f"COVERAGE {language_filter}: "
        f"{len(unique_records)}/"
        f"{api_total} - 100%"
    )

    return {
        "language":
            language_filter,

        "api_total":
            api_total,

        "records":
            unique_records,

        "pages":
            pages,

        "duplicate_records":
            duplicates,

        "recovery_passes":
            recovery_passes,

        "coverage_complete":
            True,
    }


def normalize_record(record):
    notice_id = get_value(
        record,
        "id",
    )

    deadline = deadline_datetime(
        record
    )

    deadline_iso = (
        deadline.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        if deadline
        else ""
    )

    source_url = (
        "https://projects.worldbank.org/"
        "en/projects-operations/"
        "procurement-detail/"
        f"{notice_id}"
    )

    return {
        "notice_id":
            notice_id,

        "project_id":
            get_value(
                record,
                "project_id",
            ),

        "reference_no":
            get_value(
                record,
                "bid_reference_no",
            ),

        "title":
            get_value(
                record,
                "bid_description",
            ),

        "country":
            get_value(
                record,
                "project_ctry_name",
            ),

        "project_name":
            get_value(
                record,
                "project_name",
            ),

        "institution":
            get_value(
                record,
                "contact_organization",
                "agency",
                "implementing_agency",
            ),

        "publication_date":
            get_value(
                record,
                "noticedate",
            ),

        "deadline":
            deadline_iso,

        "deadline_date_raw":
            get_value(
                record,
                "submission_deadline_date",
            ),

        "deadline_time_raw":
            get_value(
                record,
                "submission_deadline_time",
            ),

        "language":
            get_value(
                record,
                "notice_lang_name",
            ),

        "notice_type":
            get_value(
                record,
                "notice_type",
            ),

        "notice_status":
            get_value(
                record,
                "notice_status",
            ),

        "procurement_group":
            get_value(
                record,
                "procurement_group",
            ),

        "procurement_method_code":
            get_value(
                record,
                "procurement_method_code",
            ),

        "procurement_method_name":
            get_value(
                record,
                "procurement_method_name",
            ),

        "notice_text":
            get_value(
                record,
                "notice_text",
            ),

        "contact_name":
            get_value(
                record,
                "contact_name",
            ),

        "contact_email":
            get_value(
                record,
                "contact_email",
            ),

        "source_url":
            source_url,
    }


def sha256_file(path):
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(
            lambda: f.read(
                1024 * 1024
            ),
            b"",
        ):
            h.update(chunk)

    return h.hexdigest()


def main():
    extraction_dt = utc_now()
    extracted_at = utc_now_iso()

    print(
        "=========================================="
    )
    print(
        "WORLD BANK PROCUREMENT MONITOR"
    )
    print(
        "=========================================="
    )

    subsets = []

    for language_filter in (
        LANGUAGE_FILTERS
    ):
        result = (
            fetch_language_universe(
                language_filter
            )
        )

        subsets.append(
            result
        )

    all_by_id = {}

    language_controls = []

    for subset in subsets:
        language_controls.append(
            {
                "language_filter":
                    subset["language"],

                "api_total":
                    subset["api_total"],

                "records_downloaded":
                    len(
                        subset["records"]
                    ),

                "pages":
                    subset["pages"],

                "duplicate_records":
                    subset[
                        "duplicate_records"
                    ],

                "recovery_passes":
                    subset[
                        "recovery_passes"
                    ],

                "coverage_complete":
                    True,
            }
        )

        for record in (
            subset["records"]
        ):
            notice_id = str(
                record.get(
                    "id",
                    ""
                )
            )

            all_by_id[
                notice_id
            ] = record

    filtered_historical = list(
        all_by_id.values()
    )

    current_raw = []

    expired = 0
    no_deadline = 0
    inactive = 0

    for record in (
        filtered_historical
    ):
        deadline = deadline_datetime(
            record
        )

        if deadline is None:
            no_deadline += 1
            continue

        if deadline < extraction_dt:
            expired += 1
            continue

        if is_inactive(record):
            inactive += 1
            continue

        current_raw.append(
            record
        )

    current_records = [
        normalize_record(record)
        for record in current_raw
    ]

    current_records.sort(
        key=lambda item: (
            item.get(
                "deadline",
                ""
            ),
            item.get(
                "notice_id",
                ""
            ),
        )
    )

    output = {
        "source":
            "World Bank Procurement Notices",

        "api":
            API_URL,

        "fechaExtraccion":
            extracted_at,

        "filters": {
            "procurement_group_exact":
                CONSULTING_GROUP,

            "notice_type_exact":
                NOTICE_TYPE,

            "languages":
                LANGUAGE_FILTERS,
        },

        "languageControls":
            language_controls,

        "historicalFilteredUnique":
            len(
                filtered_historical
            ),

        "expiredRecords":
            expired,

        "recordsWithoutDeadline":
            no_deadline,

        "inactiveStatusRecords":
            inactive,

        "currentConsultingEnEs":
            len(
                current_records
            ),

        "coverageComplete":
            True,

        "validationStatus":
            "OK",

        "data":
            current_records,
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
        "deadline_date_raw",
        "deadline_time_raw",
        "language",
        "notice_type",
        "notice_status",
        "procurement_group",
        "procurement_method_code",
        "procurement_method_name",
        "notice_text",
        "contact_name",
        "contact_email",
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

    json_count = len(
        current_records
    )

    with CSV_PATH.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        csv_count = (
            sum(
                1
                for _
                in csv.reader(f)
            )
            - 1
        )

    if json_count != csv_count:
        raise RuntimeError(
            "JSON y CSV no coinciden"
        )

    sha_json = sha256_file(
        JSON_PATH
    )

    sha_csv = sha256_file(
        CSV_PATH
    )

    metadata = {
        "source":
            "World Bank Procurement Notices",

        "api":
            API_URL,

        "extracted_at":
            extracted_at,

        "language_controls":
            language_controls,

        "historical_filtered_unique":
            len(
                filtered_historical
            ),

        "expired_records":
            expired,

        "records_without_deadline":
            no_deadline,

        "inactive_status_records":
            inactive,

        "current_records":
            json_count,

        "current_consulting_en_es":
            json_count,

        "json_records":
            json_count,

        "csv_records":
            csv_count,

        "coverage_complete":
            True,

        "validation_status":
            "OK",

        "sha256_json":
            sha_json,

        "sha256_csv":
            sha_csv,
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
        "WORLD BANK FINAL VALIDATION"
    )
    print(
        "=========================================="
    )

    for control in (
        language_controls
    ):
        print(
            f"{control['language_filter']}: "
            f"{control['records_downloaded']}/"
            f"{control['api_total']} "
            f"- 100% | "
            f"duplicates="
            f"{control['duplicate_records']} | "
            f"recovery="
            f"{control['recovery_passes']}"
        )

    print(
        f"CURRENT CONSULTING EN/ES: "
        f"{json_count}"
    )

    print(
        f"JSON records: "
        f"{json_count}"
    )

    print(
        f"CSV records: "
        f"{csv_count}"
    )

    print(
        "Coverage complete: True"
    )

    print(
        "Validation status: OK"
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print("")
        print(
            "=========================================="
        )
        print(
            f"ERROR: {exc}"
        )
        print(
            "Coverage complete: False"
        )
        print(
            "=========================================="
        )

        sys.exit(1)
