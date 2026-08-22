import json
import requests


API_URL = "https://search.worldbank.org/api/v2/procnotices"


TESTS = [
    {
        "name": "BASE",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
        },
    },

    {
        "name": "procurement_group",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "procurement_group": "CS",
        },
    },

    {
        "name": "procurement_group_exact",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "procurement_group_exact": "CS",
        },
    },

    {
        "name": "notice_type",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "notice_type": "Request for Expression of Interest",
        },
    },

    {
        "name": "notice_type_exact",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "notice_type_exact": "Request for Expression of Interest",
        },
    },

    {
        "name": "notice_lang_name",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "notice_lang_name": "English",
        },
    },

    {
        "name": "notice_lang_exact",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "notice_lang_exact": "English",
        },
    },

    {
        "name": "notice_lang_name_exact",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "notice_lang_name_exact": "English",
        },
    },

    {
        "name": "fct_procurement_group",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "fct": "procurement_group_exact:CS",
        },
    },

    {
        "name": "fct_notice_type",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "fct": (
                "notice_type_exact:"
                "Request for Expression of Interest"
            ),
        },
    },

    {
        "name": "fct_language",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "fct": "notice_lang_name_exact:English",
        },
    },

    {
        "name": "combined_direct",
        "params": {
            "format": "json",
            "rows": 5,
            "os": 0,
            "procurement_group_exact": "CS",
            "notice_type_exact": "Request for Expression of Interest",
            "notice_lang_name_exact": "English",
        },
    },
]


def extract_records(payload):
    value = payload.get("procnotices")

    if isinstance(value, list):
        return value

    if isinstance(value, dict):
        return [
            item
            for item in value.values()
            if isinstance(item, dict)
        ]

    for key in ["documents", "data", "results"]:
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            return [
                item
                for item in value.values()
                if isinstance(item, dict)
            ]

    return []


def summarize_record(record):
    keys = [
        "id",
        "procurement_group",
        "procurement_method_code",
        "procurement_method_name",
        "notice_type",
        "notice_lang_name",
        "noticedate",
        "submission_deadline_date",
        "submission_deadline_time",
        "project_id",
        "project_name",
        "project_ctry_name",
        "bid_reference_no",
        "bid_description",
    ]

    return {
        key: record.get(key)
        for key in keys
    }


def run_test(test):
    print("")
    print("=" * 90)
    print(f"TEST: {test['name']}")
    print("=" * 90)

    response = requests.get(
        API_URL,
        params=test["params"],
        timeout=60,
        headers={
            "User-Agent": "Paradigma-WorldBank-Probe/1.0",
            "Accept": "application/json",
        },
    )

    print(f"HTTP: {response.status_code}")
    print(f"URL: {response.url}")

    if response.status_code != 200:
        print(response.text[:1000])
        return

    payload = response.json()

    print(f"Payload keys: {list(payload.keys())}")
    print(f"Total: {payload.get('total')}")

    records = extract_records(payload)

    print(f"Records returned: {len(records)}")

    for index, record in enumerate(records[:5], start=1):
        print("")
        print(f"Record {index}")
        print(
            json.dumps(
                summarize_record(record),
                ensure_ascii=False,
                indent=2,
            )
        )


def main():
    print("WORLD BANK PROCUREMENT API PROBE")
    print(f"Endpoint: {API_URL}")

    for test in TESTS:
        try:
            run_test(test)

        except Exception as exc:
            print("")
            print(f"ERROR en test {test['name']}: {exc}")

    print("")
    print("=" * 90)
    print("PROBE FINISHED")
    print("=" * 90)


if __name__ == "__main__":
    main()
