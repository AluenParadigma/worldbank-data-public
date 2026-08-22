import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

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

# En World Bank los servicios de consultoria se identifican
# estructuralmente con procurement_group = CS.
CONSULTING_GROUP = "CS"

# Para oportunidades de consultoria abiertas nos interesan
# principalmente los Request for Expression of Interest.
NOTICE_TYPE = "Request for Expression of Interest"

# La API devuelve actualmente ingles como "English".
# Para español se contemplan las dos variantes observables.
LANGUAGE_FILTERS = [
    "English",
    "Spanish; Castilian",
    "Spanish",
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
        "User-Agent": (
            "Paradigma-WorldBank-Monitor/1.0"
        ),
        "Accept": "application/json",
    }

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):
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
                    f"Reintento {attempt}/"
                    f"{MAX_RETRIES}"
                )

                last_error = RuntimeError(
                    f"HTTP {response.status_code}"
                )

                time.sleep(
                    RETRY_WAIT_SECONDS
                    * attempt
                )

                continue

            response.raise_for_status()

        except Exception as exc:
            last_error = exc

            print(
                f"Error HTTP: {exc}. "
                f"Reintento {attempt}/"
                f"{MAX_RETRIES}"
            )

            time.sleep(
                RETRY_WAIT_SECONDS
                * attempt
            )

    raise RuntimeError(
        "No fue posible consultar la API "
        "despues de "
        f"{MAX_RETRIES} intentos. "
        f"Ultimo error: {last_error}"
    )


# ============================================================
# ESTRUCTURA DE RESPUESTA
# ============================================================

def extract_records(payload):
    records = payload.get(
        "procnotices",
        []
    )

    if isinstance(records, list):
        return records

    # Protección para variantes antiguas
    if isinstance(records, dict):
        return [
            value
            for value in records.values()
            if isinstance(
                value,
                dict
            )
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
            f"Total invalido devuelto "
            f"por API: {value}"
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


def language_is_english_or_spanish(
    value
):
    value = normalize_language(value)

    return (
        value == "english"
        or value == "spanish"
        or value == "spanish; castilian"
        or "spanish" in value
    )


def is_clearly_inactive_status(
    record
):
    status = str(
        record.get(
            "notice_status",
            ""
        )
    ).strip().lower()

    inactive = {
        "cancelled",
        "canceled",
        "draft",
        "deleted",
        "withdrawn",
    }

    return status in inactive


# ============================================================
# DEADLINE
# ============================================================

def deadline_datetime(record):
    date_value = get_value(
        record,
        "submission_deadline_date",
        "deadline_date",
        "deadline",
    )

    dt = parse_date(
        date_value
    )

    if dt is None:
        return None

    # Si la API trae hora separada,
    # incorporarla.
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


def is_current_opportunity(
    record,
    extraction_dt,
):
    deadline = deadline_datetime(
        record
    )

    if deadline is None:
        return False

    if deadline < extraction_dt:
        return False

    if is_clearly_inactive_status(
        record
    ):
        return False

    return True


# ============================================================
# VALIDACION DE FILTROS API
# ============================================================

def validate_server_filter(
    record,
    requested_language,
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

    language = normalize_language(
        record.get(
            "notice_lang_name",
            ""
        )
    )

    if (
        procurement_group
        != CONSULTING_GROUP
    ):
        return False

    if notice_type != NOTICE_TYPE:
        return False

    requested = normalize_language(
        requested_language
    )

    if requested == "english":
        return language == "english"

    if "spanish" in requested:
        return "spanish" in language

    return False


# ============================================================
# DESCARGA COMPLETA DE UN SUBUNIVERSO
# ============================================================

def fetch_filtered_universe(
    language_filter
):
    print("")
    print(
        "------------------------------------------"
    )
    print(
        f"Idioma: {language_filter}"
    )
    print(
        "------------------------------------------"
    )

    first_params = {
        "format": "json",
        "rows": ROWS_PER_PAGE,
        "os": 0,
        "procurement_group": (
            CONSULTING_GROUP
        ),
        "notice_type_exact": (
            NOTICE_TYPE
        ),
        "notice_lang_exact": (
            language_filter
        ),
    }

    response = request_with_retry(
        first_params
    )

    payload = response.json()

    api_total = extract_total(
        payload
    )

    first_records = extract_records(
        payload
    )

    print(
        f"Total informado por API: "
        f"{api_total}"
    )

    # --------------------------------------------------------
    # CONTROL CRITICO:
    # verificar que los filtros realmente hayan sido aplicados
    # --------------------------------------------------------

    for record in first_records:
        if not validate_server_filter(
            record,
            language_filter,
        ):
            print("")
            print(
                "ERROR: La API no aplico "
                "correctamente los filtros."
            )
            print(
                "Registro conflictivo:"
            )
            print(
                json.dumps(
                    {
                        "id": record.get(
                            "id"
                        ),
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

    all_records = []
    all_records.extend(
        first_records
    )

    offset = len(
        first_records
    )

    page = 1

    while offset < api_total:
        params = {
            "format": "json",
            "rows": ROWS_PER_PAGE,
            "os": offset,
            "procurement_group": (
                CONSULTING_GROUP
            ),
            "notice_type_exact": (
                NOTICE_TYPE
            ),
            "notice_lang_exact": (
                language_filter
            ),
        }

        print(
            f"Pagina {page + 1} | "
            f"offset {offset} | "
            f"{len(all_records)}/"
            f"{api_total}"
        )

        response = request_with_retry(
            params
        )

        payload = response.json()

        current_total = extract_total(
            payload
        )

        # Si el total cambia durante la descarga,
        # preferimos abortar antes que declarar
        # falsamente cobertura 100%.
        if current_total != api_total:
            raise RuntimeError(
                "El total de la API cambio "
                "durante la extraccion: "
                f"{api_total} -> "
                f"{current_total}"
            )

        records = extract_records(
            payload
        )

        if not records:
            raise RuntimeError(
                "La API devolvio una pagina "
                "vacia antes de alcanzar "
                "el total esperado"
            )

        for record in records:
            if not validate_server_filter(
                record,
                language_filter,
            ):
                raise RuntimeError(
                    "La API devolvio un registro "
                    "fuera del filtro solicitado"
                )

        all_records.extend(
            records
        )

        offset += len(
            records
        )

        page += 1

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

    # --------------------------------------------------------
    # DEDUPLICAR DENTRO DEL SUBUNIVERSO
    # --------------------------------------------------------

    by_id = {}

    for record in all_records:
        notice_id = str(
            record.get("id", "")
        ).strip()

        if not notice_id:
            raise RuntimeError(
                "Registro sin id en API"
            )

        by_id[notice_id] = record

    unique_records = list(
        by_id.values()
    )

    if len(unique_records) != api_total:
        raise RuntimeError(
            "El total unico descargado "
            "no coincide con el total "
            "de la API: "
            f"{len(unique_records)} "
            f"!= {api_total}"
        )

    print(
        f"Cobertura {language_filter}: "
        f"{len(unique_records)}/"
        f"{api_total} - 100%"
    )

    return {
        "language": language_filter,
        "api_total": api_total,
        "records": unique_records,
        "pages": page,
    }


# ============================================================
# NORMALIZACION
# ============================================================

def normalize_record(
    record
):
    notice_id = get_value(
        record,
        "id",
    )

    source_url = (
        "https://projects.worldbank.org/"
        "en/projects-operations/"
        "procurement-detail/"
        f"{notice_id}"
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

    return {
        "notice_id": notice_id,

        "project_id": get_value(
            record,
            "project_id",
        ),

        "reference_no": get_value(
            record,
            "bid_reference_no",
        ),

        "title": get_value(
            record,
            "bid_description",
        ),

        "country": get_value(
            record,
            "project_ctry_name",
        ),

        "project_name": get_value(
            record,
            "project_name",
        ),

        "institution": get_value(
            record,
            "contact_organization",
        ),

        "publication_date": get_value(
            record,
            "noticedate",
        ),

        "deadline": deadline_iso,

        "deadline_date_raw": get_value(
            record,
            "submission_deadline_date",
        ),

        "deadline_time_raw": get_value(
            record,
            "submission_deadline_time",
        ),

        "language": get_value(
            record,
            "notice_lang_name",
        ),

        "notice_type": get_value(
            record,
            "notice_type",
        ),

        "notice_status": get_value(
            record,
            "notice_status",
        ),

        "procurement_group": get_value(
            record,
            "procurement_group",
        ),

        "procurement_method_code": (
            get_value(
                record,
                "procurement_method_code",
            )
        ),

        "procurement_method_name": (
            get_value(
                record,
                "procurement_method_name",
            )
        ),

        "notice_text": get_value(
            record,
            "notice_text",
        ),

        "contact_name": get_value(
            record,
            "contact_name",
        ),

        "contact_email": get_value(
            record,
            "contact_email",
        ),

        "source_url": source_url,
    }


# ============================================================
# HASH
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
        f"Notice type: {NOTICE_TYPE}"
    )

    # --------------------------------------------------------
    # DESCARGAR LOS TRES SUBUNIVERSOS
    # --------------------------------------------------------

    subsets = []

    for language_filter in (
        LANGUAGE_FILTERS
    ):
        result = (
            fetch_filtered_universe(
                language_filter
            )
        )

        subsets.append(
            result
        )

    # --------------------------------------------------------
    # CONSOLIDAR
    # --------------------------------------------------------

    all_consulting_by_id = {}

    language_controls = []

    total_api_sum = 0

    for subset in subsets:
        total_api_sum += (
            subset["api_total"]
        )

        language_controls.append(
            {
                "language_filter":
                    subset[
                        "language"
                    ],
                "api_total":
                    subset[
                        "api_total"
                    ],
                "records_downloaded":
                    len(
                        subset[
                            "records"
                        ]
                    ),
                "pages":
                    subset[
                        "pages"
                    ],
                "coverage_complete":
                    True,
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

            all_consulting_by_id[
                notice_id
            ] = record

    full_filtered_universe = list(
        all_consulting_by_id.values()
    )

    print("")
    print(
        "Universo historico filtrado "
        "unico:"
    )
    print(
        len(
            full_filtered_universe
        )
    )

    # --------------------------------------------------------
    # IDENTIFICAR OPORTUNIDADES ACTUALES
    # --------------------------------------------------------

    current_raw = []

    expired = 0
    no_deadline = 0
    inactive_status = 0

    for record in (
        full_filtered_universe
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

        if is_clearly_inactive_status(
            record
        ):
            inactive_status += 1
            continue

        current_raw.append(
            record
        )

    # --------------------------------------------------------
    # NORMALIZAR
    # --------------------------------------------------------

    current_records = [
        normalize_record(record)
        for record in current_raw
    ]

    current_records.sort(
        key=lambda x: (
            x.get(
                "deadline",
                ""
            ),
            x.get(
                "notice_id",
                ""
            ),
        )
    )

    # --------------------------------------------------------
    # VALIDACION SEMANTICA ESTRUCTURAL
    # --------------------------------------------------------

    for record in current_records:
        if (
            record[
                "procurement_group"
            ]
            != CONSULTING_GROUP
        ):
            raise RuntimeError(
                "Registro actual fuera "
                "del grupo CS"
            )

        if (
            record[
                "notice_type"
            ]
            != NOTICE_TYPE
        ):
            raise RuntimeError(
                "Registro actual fuera "
                "del tipo REOI"
            )

        if not (
            language_is_english_or_spanish(
                record["language"]
            )
        ):
            raise RuntimeError(
                "Registro actual fuera "
                "de EN/ES"
            )

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    output = {
        "source": (
            "World Bank "
            "Procurement Notices"
        ),

        "api": API_URL,

        "fechaExtraccion":
            extracted_at,

        "filters": {
            "procurement_group":
                CONSULTING_GROUP,
            "notice_type":
                NOTICE_TYPE,
            "languages": [
                "English",
                "Spanish",
            ],
            "current_definition":
                (
                    "submission_deadline "
                    ">= extraction time "
                    "and notice not "
                    "cancelled/draft/"
                    "withdrawn"
                ),
        },

        "languageControls":
            language_controls,

        "historicalFilteredUnique":
            len(
                full_filtered_universe
            ),

        "expiredRecords":
            expired,

        "recordsWithoutDeadline":
            no_deadline,

        "inactiveStatusRecords":
            inactive_status,

        "currentConsultingEnEs":
            len(
                current_records
            ),

        "coverageComplete": True,

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
    # VALIDAR JSON / CSV
    # --------------------------------------------------------

    json_count = len(
        current_records
    )

    with CSV_PATH.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        reader = csv.reader(f)

        csv_count = (
            sum(
                1
                for _ in reader
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
    # HASHES
    # --------------------------------------------------------

    sha_json = sha256_file(
        JSON_PATH
    )

    sha_csv = sha256_file(
        CSV_PATH
    )

    # --------------------------------------------------------
    # METADATA
    # --------------------------------------------------------

    metadata = {
        "source": (
            "World Bank "
            "Procurement Notices"
        ),

        "api": API_URL,

        "extracted_at":
            extracted_at,

        "procurement_group":
            CONSULTING_GROUP,

        "notice_type":
            NOTICE_TYPE,

        "languages": [
            "English",
            "Spanish",
        ],

        "language_controls":
            language_controls,

        "historical_filtered_unique":
            len(
                full_filtered_universe
            ),

        "expired_records":
            expired,

        "records_without_deadline":
            no_deadline,

        "inactive_status_records":
            inactive_status,

        "current_records":
            len(
                current_records
            ),

        "current_consulting_en_es":
            len(
                current_records
            ),

        "json_records":
            json_count,

        "csv_records":
            csv_count,

        "coverage_complete":
            True,

        "coverage_scope": (
            "100% of World Bank "
            "Request for Expression "
            "of Interest notices with "
            "procurement_group=CS in "
            "English or Spanish, "
            "then filtered to notices "
            "whose submission deadline "
            "has not expired"
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

    for control in (
        language_controls
    ):
        print(
            f"{control['language_filter']}: "
            f"{control['records_downloaded']}/"
            f"{control['api_total']} "
            "- 100%"
        )

    print(
        "Historical filtered unique: "
        f"{len(full_filtered_universe)}"
    )

    print(
        f"Expired:                   "
        f"{expired}"
    )

    print(
        f"Without deadline:           "
        f"{no_deadline}"
    )

    print(
        f"Inactive status:            "
        f"{inactive_status}"
    )

    print(
        f"CURRENT CONSULTING EN/ES:   "
        f"{len(current_records)}"
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
