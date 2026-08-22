import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


# ============================================================
# CONFIGURACION
# ============================================================

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

# Vamos a consultar las variantes posibles del español.
# Si alguna variante devuelve 0 registros, simplemente no aporta datos.
LANGUAGE_FILTERS = [
    "English",
    "Spanish",
    "Spanish; Castilian",
]


# ============================================================
# FECHAS
# ============================================================

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
            dt = datetime.strptime(
                value[:19],
                fmt
            )
            return dt.replace(
                tzinfo=timezone.utc
            )
        except Exception:
            pass

    return None


# ============================================================
# HTTP
# ============================================================

def request_with_retry(params):
    last_error = None

    headers = {
        "User-Agent": "Paradigma-WorldBank-Monitor/2.0",
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


# ============================================================
# RESPUESTA API
# ============================================================

def extract_records(payload):

    records = payload.get(
        "procnotices",
        []
    )

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

    try:
        return int(value)

    except Exception:
        raise RuntimeError(
            f"Total invalido: {value}"
        )


# ============================================================
# HELPERS
# ============================================================

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


def language_matches(
    actual,
    requested
):

    actual = normalize_language(
        actual
    )

    requested = normalize_language(
        requested
    )

    if requested == "english":
        return actual == "english"

    if "spanish" in requested:
        return "spanish" in actual

    return actual == requested


# ============================================================
# DEADLINE
# ============================================================

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


def is_current(record, extraction_dt):

    deadline = deadline_datetime(
        record
    )

    if deadline is None:
        return False

    if deadline < extraction_dt:
        return False

    if is_inactive(record):
        return False

    return True


# ============================================================
# VALIDACION DEL FILTRO SERVER-SIDE
# ============================================================

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


# ============================================================
# DESCARGAR SUBUNIVERSO COMPLETO
# ============================================================

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

    # --------------------------------------------------------
    # Primera pagina
    # --------------------------------------------------------

    params = dict(
        base_params
    )

    params["os"] = 0

    response = request_with_retry(
        params
    )

    payload = response.json()

    api_total = extract_total(
        payload
    )

    records = extract_records(
        payload
    )

    print(
        f"API total: {api_total}"
    )

    # Si no existe ninguna oportunidad en esa variante
    # de idioma, es un resultado valido.
    if api_total == 0:

        if records:
            raise RuntimeError(
                "API informa total=0 "
                "pero devolvio registros"
            )

        return {
            "language": language_filter,
            "api_total": 0,
            "records": [],
            "pages": 1,
            "coverage_complete": True,
        }

    if not records:
        raise RuntimeError(
            "API informa registros pero "
            "la primera pagina esta vacia"
        )

    # --------------------------------------------------------
    # Verificar que la API respete realmente los filtros
    # --------------------------------------------------------

    for record in records:

        if not validate_record_filter(
            record,
            language_filter
        ):

            print("")
            print(
                "REGISTRO FUERA DEL FILTRO:"
            )

            print(
                json.dumps(
                    {
                        "id":
                            record.get("id"),

                        "procurement_group":
                            record.get(
                                "procurement_group"
                            ),

                        "notice_type":
                            record.get(
                                "notice_type"
                            ),

                        "notice_lang_name":
                            record.get(
                                "notice_lang_name"
                            ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

            raise RuntimeError(
                "Los filtros server-side "
                "no fueron respetados"
            )

    all_records = list(
        records
    )

    page = 1

    # --------------------------------------------------------
    # Paginar hasta alcanzar exactamente api_total
    # --------------------------------------------------------

    while len(all_records) < api_total:

        offset = len(
            all_records
        )

        params = dict(
            base_params
        )

        params["os"] = offset

        print(
            f"Pagina {page + 1} | "
            f"offset={offset} | "
            f"{len(all_records)}/{api_total}"
        )

        response = request_with_retry(
            params
        )

        payload = response.json()

        new_total = extract_total(
            payload
        )

        # Si cambia el total durante la extracción,
        # abortamos antes de declarar falso 100%.
        if new_total != api_total:

            raise RuntimeError(
                "El total informado por la API "
                "cambio durante la extraccion: "
                f"{api_total} -> {new_total}"
            )

        records = extract_records(
            payload
        )

        if not records:

            raise RuntimeError(
                "La API devolvio una pagina vacia "
                "antes de alcanzar el total"
            )

        for record in records:

            if not validate_record_filter(
                record,
                language_filter
            ):

                raise RuntimeError(
                    "La API devolvio un registro "
                    "fuera del filtro solicitado"
                )

        all_records.extend(
            records
        )

        page += 1

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

    # --------------------------------------------------------
    # Validar IDs unicos
    # --------------------------------------------------------

    by_id = {}

    for record in all_records:

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

        by_id[
            notice_id
        ] = record

    unique_records = list(
        by_id.values()
    )

    if len(unique_records) != api_total:

        raise RuntimeError(
            "El numero de IDs unicos "
            "no coincide con total API: "
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
            page,

        "coverage_complete":
            True,
    }


# ============================================================
# NORMALIZACION
# ============================================================

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


# ============================================================
# SHA256
# ============================================================

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


# ============================================================
# MAIN
# ============================================================

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

    print(
        f"Extraction time: {extracted_at}"
    )

    print(
        f"API: {API_URL}"
    )

    print(
        f"Procurement group: "
        f"{CONSULTING_GROUP}"
    )

    print(
        f"Notice type: "
        f"{NOTICE_TYPE}"
    )

    # --------------------------------------------------------
    # Descargar EN + variantes ES
    # --------------------------------------------------------

    subsets = []

    for language_filter in LANGUAGE_FILTERS:

        result = fetch_language_universe(
            language_filter
        )

        subsets.append(
            result
        )

    # --------------------------------------------------------
    # Consolidar y deduplicar
    # --------------------------------------------------------

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

                "coverage_complete":
                    subset[
                        "coverage_complete"
                    ],
            }
        )

        for record in subset[
            "records"
        ]:

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

    # --------------------------------------------------------
    # Determinar vigentes
    # --------------------------------------------------------

    current_raw = []

    expired = 0
    no_deadline = 0
    inactive = 0

    for record in filtered_historical:

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

    # --------------------------------------------------------
    # Normalizar
    # --------------------------------------------------------

    current_records = [
        normalize_record(
            record
        )
        for record
        in current_raw
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

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Validar cantidad CSV
    # --------------------------------------------------------

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
            "JSON y CSV no coinciden: "
            f"{json_count} != "
            f"{csv_count}"
        )

    # --------------------------------------------------------
    # Hash
    # --------------------------------------------------------

    sha_json = sha256_file(
        JSON_PATH
    )

    sha_csv = sha256_file(
        CSV_PATH
    )

    # --------------------------------------------------------
    # Metadata
    # --------------------------------------------------------

    metadata = {

        "source":
            "World Bank Procurement Notices",

        "api":
            API_URL,

        "extracted_at":
            extracted_at,

        "procurement_group":
            CONSULTING_GROUP,

        "notice_type":
            NOTICE_TYPE,

        "languages":
            LANGUAGE_FILTERS,

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

        "coverage_scope":
            (
                "100% of World Bank "
                "Request for Expression "
                "of Interest notices with "
                "procurement_group=CS "
                "and English/Spanish "
                "language filters, "
                "subsequently restricted "
                "to notices with an "
                "unexpired submission "
                "deadline"
            ),

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

    # --------------------------------------------------------
    # LOG FINAL
    # --------------------------------------------------------

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

    for control in language_controls:

        print(
            f"{control['language_filter']}: "
            f"{control['records_downloaded']}/"
            f"{control['api_total']} "
            "- 100%"
        )

    print(
        f"Historical filtered unique: "
        f"{len(filtered_historical)}"
    )

    print(
        f"Expired records:            "
        f"{expired}"
    )

    print(
        f"Without deadline:           "
        f"{no_deadline}"
    )

    print(
        f"Inactive status:            "
        f"{inactive}"
    )

    print(
        f"CURRENT CONSULTING EN/ES:   "
        f"{json_count}"
    )

    print(
        f"JSON records:               "
        f"{json_count}"
    )

    print(
        f"CSV records:                "
        f"{csv_count}"
    )

    print(
        "Coverage complete:          True"
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
