import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
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

# Ventana conservadora de publicación.
# 180 días reduce mucho el universo pero sigue siendo suficientemente
# amplia para capturar licitaciones con plazos largos.
LOOKBACK_DAYS = 180

# Protección contra loops anómalos
MAX_PAGES = 1000


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value):
    if not value:
        return None

    value = str(value).strip()

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
        "%d-%b-%Y",
        "%d-%b-%Y %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(
                value[:19],
                fmt
            ).replace(
                tzinfo=timezone.utc
            )
        except Exception:
            pass

    return None


def request_with_retry(params):
    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):
        try:
            response = requests.get(
                API_URL,
                params=params,
                timeout=60,
            )

            if response.status_code == 200:
                return response

            if response.status_code in {
                500,
                502,
                503,
                504,
            }:
                print(
                    f"HTTP {response.status_code}. "
                    f"Retry {attempt}/{MAX_RETRIES}"
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
                f"Request error: {exc}. "
                f"Retry {attempt}/{MAX_RETRIES}"
            )

            time.sleep(
                RETRY_WAIT_SECONDS * attempt
            )

    raise RuntimeError(
        "No fue posible consultar la API "
        f"despues de {MAX_RETRIES} intentos: "
        f"{last_error}"
    )


def extract_records(payload):
    if isinstance(payload, list):
        return payload

    possible_keys = [
        "documents",
        "data",
        "results",
        "procnotices",
    ]

    for key in possible_keys:
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            # Algunas APIs del Banco Mundial
            # devuelven documents como diccionario
            nested = list(value.values())

            if nested and all(
                isinstance(x, dict)
                for x in nested
            ):
                return nested

    return []


def extract_total(payload):
    candidates = [
        payload.get("total"),
        payload.get("total_records"),
        payload.get("count"),
        payload.get("numFound"),
    ]

    for value in candidates:
        try:
            if value is not None:
                return int(value)
        except Exception:
            pass

    return None


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


def publication_date(record):
    return get_value(
        record,
        "noticedate",
        "publication_date",
        "published_date",
        "notice_date",
    )


def deadline_date(record):
    return get_value(
        record,
        "submission_date",
        "deadline",
        "deadline_date",
        "submission_deadline",
        "closing_date",
    )


def language(record):
    return get_value(
        record,
        "notice_lang_name",
        "language",
        "language_name",
        "lang_name",
    )


def is_current(record, extraction_dt):
    deadline = parse_date(
        deadline_date(record)
    )

    if deadline is None:
        return False

    return deadline >= extraction_dt


def is_recent(record, cutoff_dt):
    published = parse_date(
        publication_date(record)
    )

    if published is None:
        return False

    return published >= cutoff_dt


def is_english_or_spanish(record):
    value = str(
        language(record)
    ).strip().lower()

    return (
        value in {
            "english",
            "spanish",
            "en",
            "es",
        }
        or "english" in value
        or "spanish" in value
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
            "bid_description",
            "description",
        ),
        get_value(
            record,
            "title",
            "notice_title",
        ),
    ]

    text = " ".join(
        str(x or "")
        for x in fields
    ).lower()

    consulting_markers = [
        "consulting services",
        "consultancy",
        "consultant",
        "expression of interest",
        "request for expression of interest",
        "qcbs",
        "qbs",
        "cqs",
        "lcs",
        "fbs",
        "individual consultant",
        "indv",
    ]

    return any(
        marker in text
        for marker in consulting_markers
    )


def normalize(record):
    notice_id = get_value(
        record,
        "id",
        "notice_id",
        "proc_notice_id",
    )

    return {
        "notice_id": notice_id,
        "project_id": get_value(
            record,
            "project_id",
            "projectid",
        ),
        "reference_no": get_value(
            record,
            "bid_reference_no",
            "reference_no",
            "reference",
            "procurement_reference",
        ),
        "title": get_value(
            record,
            "title",
            "notice_title",
            "bid_description",
            "description",
        ),
        "country": get_value(
            record,
            "country_name",
            "country",
        ),
        "project_name": get_value(
            record,
            "project_name",
            "project",
        ),
        "institution": get_value(
            record,
            "agency",
            "implementing_agency",
            "borrower",
        ),
        "publication_date": publication_date(
            record
        ),
        "deadline": deadline_date(
            record
        ),
        "language": language(
            record
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
            "bid_description",
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
            lambda: f.read(
                1024 * 1024
            ),
            b"",
        ):
            h.update(chunk)

    return h.hexdigest()


def main():
    extraction_dt = utc_now()
    extracted_at = iso(
        extraction_dt
    )

    cutoff_dt = (
        extraction_dt
        - timedelta(
            days=LOOKBACK_DAYS
        )
    )

    print(
        "World Bank Procurement Notices"
    )
    print(
        f"Extraction time: {extracted_at}"
    )
    print(
        f"Publication cutoff: {iso(cutoff_dt)}"
    )

    # ---------------------------------------------------
    # PRIMERA LLAMADA
    # ---------------------------------------------------

    params = {
        "format": "json",
        "rows": ROWS_PER_PAGE,
        "os": 0,
    }

    first_response = request_with_retry(
        params
    )

    first_payload = (
        first_response.json()
    )

    first_records = extract_records(
        first_payload
    )

    api_total = extract_total(
        first_payload
    )

    print(
        f"API total informado: {api_total}"
    )

    if not first_records:
        raise RuntimeError(
            "La API no devolvio registros"
        )

    # ---------------------------------------------------
    # PAGINACION RECIENTE
    # ---------------------------------------------------

    scanned = []
    page = 0
    found_old_boundary = False
    stop_reason = ""

    while page < MAX_PAGES:
        offset = (
            page * ROWS_PER_PAGE
        )

        if page == 0:
            records = first_records
        else:
            params = {
                "format": "json",
                "rows": ROWS_PER_PAGE,
                "os": offset,
            }

            print(
                f"Descargando pagina {page + 1} "
                f"- offset {offset}"
            )

            response = request_with_retry(
                params
            )

            payload = response.json()

            records = extract_records(
                payload
            )

        if not records:
            found_old_boundary = True
            stop_reason = (
                "API returned empty page"
            )
            break

        scanned.extend(
            records
        )

        parsed_publication_dates = []

        for record in records:
            dt = parse_date(
                publication_date(
                    record
                )
            )

            if dt is not None:
                parsed_publication_dates.append(
                    dt
                )

        # Sólo cortamos si TODA la página tiene
        # fechas conocidas y TODAS son anteriores
        # a la ventana configurada.
        if (
            len(
                parsed_publication_dates
            )
            == len(records)
            and all(
                dt < cutoff_dt
                for dt
                in parsed_publication_dates
            )
        ):
            found_old_boundary = True
            stop_reason = (
                "Reached page entirely older "
                f"than {LOOKBACK_DAYS} days"
            )
            break

        if (
            len(records)
            < ROWS_PER_PAGE
        ):
            found_old_boundary = True
            stop_reason = (
                "Reached final partial page"
            )
            break

        page += 1

    if not found_old_boundary:
        raise RuntimeError(
            "No fue posible demostrar el final "
            "del universo reciente antes de "
            f"MAX_PAGES={MAX_PAGES}"
        )

    # ---------------------------------------------------
    # UNIVERSO RECIENTE
    # ---------------------------------------------------

    recent_records = [
        r
        for r in scanned
        if is_recent(
            r,
            cutoff_dt
        )
    ]

    # ---------------------------------------------------
    # OPORTUNIDADES ACTUALES
    # ---------------------------------------------------

    current_records = [
        r
        for r in recent_records
        if is_current(
            r,
            extraction_dt
        )
    ]

    # ---------------------------------------------------
    # CONSULTORIA + EN / ES
    # ---------------------------------------------------

    consulting_records = []

    for record in current_records:
        if not is_english_or_spanish(
            record
        ):
            continue

        if not is_consulting(
            record
        ):
            continue

        consulting_records.append(
            normalize(record)
        )

    # ---------------------------------------------------
    # DEDUPLICACION
    # ---------------------------------------------------

    unique = {}

    for record in consulting_records:
        key = (
            str(
                record.get(
                    "notice_id"
                )
                or ""
            ),
            str(
                record.get(
                    "reference_no"
                )
                or ""
            ),
        )

        unique[key] = record

    consulting_records = list(
        unique.values()
    )

    consulting_records.sort(
        key=lambda x: (
            x.get(
                "deadline"
            )
            or "",
            x.get(
                "notice_id"
            )
            or "",
        )
    )

    # ---------------------------------------------------
    # GUARDAR JSON
    # ---------------------------------------------------

    output = {
        "source": (
            "World Bank "
            "Procurement Notices"
        ),
        "fechaExtraccion": extracted_at,
        "lookbackDays": LOOKBACK_DAYS,
        "publicationCutoff": iso(
            cutoff_dt
        ),
        "apiHistoricalTotal": api_total,
        "pagesScanned": page + 1,
        "recordsScanned": len(
            scanned
        ),
        "recentRecords": len(
            recent_records
        ),
        "currentRecords": len(
            current_records
        ),
        "currentConsultingEnEs": len(
            consulting_records
        ),
        "coverageComplete": True,
        "stopReason": stop_reason,
        "data": consulting_records,
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

    # ---------------------------------------------------
    # GUARDAR CSV
    # ---------------------------------------------------

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
            consulting_records
        )

    # ---------------------------------------------------
    # VALIDACION
    # ---------------------------------------------------

    json_count = len(
        consulting_records
    )

    with CSV_PATH.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        csv_count = (
            sum(
                1
                for _ in csv.reader(f)
            )
            - 1
        )

    if json_count != csv_count:
        raise RuntimeError(
            "JSON y CSV no coinciden: "
            f"{json_count} != {csv_count}"
        )

    # ---------------------------------------------------
    # HASH
    # ---------------------------------------------------

    sha_json = sha256_file(
        JSON_PATH
    )

    sha_csv = sha256_file(
        CSV_PATH
    )

    # ---------------------------------------------------
    # METADATA
    # ---------------------------------------------------

    metadata = {
        "source": (
            "World Bank "
            "Procurement Notices"
        ),
        "extracted_at": extracted_at,
        "lookback_days": LOOKBACK_DAYS,
        "publication_cutoff": iso(
            cutoff_dt
        ),
        "api_historical_total": api_total,
        "pages_scanned": page + 1,
        "records_scanned": len(
            scanned
        ),
        "recent_records": len(
            recent_records
        ),
        "current_records": len(
            current_records
        ),
        "current_consulting_en_es": (
            json_count
        ),
        "json_records": json_count,
        "csv_records": csv_count,
        "coverage_complete": True,
        "coverage_scope": (
            "All notices published within "
            f"the last {LOOKBACK_DAYS} days, "
            "then filtered to current "
            "consulting opportunities in "
            "English or Spanish"
        ),
        "stop_reason": stop_reason,
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

    # ---------------------------------------------------
    # LOG FINAL
    # ---------------------------------------------------

    print("")
    print(
        "========================================="
    )
    print(
        "WORLD BANK PROCUREMENT VALIDATION"
    )
    print(
        "========================================="
    )
    print(
        f"API historical total:        {api_total}"
    )
    print(
        f"Pages scanned:               {page + 1}"
    )
    print(
        f"Records scanned:             {len(scanned)}"
    )
    print(
        f"Recent records:              {len(recent_records)}"
    )
    print(
        f"Current records:             {len(current_records)}"
    )
    print(
        "Current consulting EN/ES:    "
        f"{json_count}"
    )
    print(
        f"JSON records:                {json_count}"
    )
    print(
        f"CSV records:                 {csv_count}"
    )
    print(
        "Coverage complete:           True"
    )
    print(
        f"Coverage scope:              last {LOOKBACK_DAYS} days"
    )
    print(
        f"Stop reason:                 {stop_reason}"
    )
    print(
        "Validation status:           OK"
    )
    print(
        "========================================="
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"ERROR: {exc}"
        )
        sys.exit(1)
