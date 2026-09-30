import json
import os

import requests

from config import IIFL_BASE_URL

SESSION_FILE = os.getenv("IIFL_SESSION_FILE", "/root/iifl/iifl_session.json")


class IIFLClientError(RuntimeError):
    pass


def load_user_session():
    if not os.path.exists(SESSION_FILE):
        raise IIFLClientError("IIFL session file not found. Login first.")

    try:
        with open(SESSION_FILE, "r") as f:
            data = json.load(f)
    except Exception as exc:
        raise IIFLClientError(f"Unable to read IIFL session: {exc}") from exc

    token = data.get("user_session")
    if not token:
        raise IIFLClientError("IIFL userSession missing. Login first.")

    return token


def auth_headers():
    return {
        "Authorization": f"Bearer {load_user_session()}",
        "Content-Type": "application/json",
    }


def request_json(method, path, *, params=None, payload=None, timeout=20):
    response = requests.request(
        method=method,
        url=f"{IIFL_BASE_URL}{path}",
        headers=auth_headers(),
        params=params,
        json=payload,
        timeout=timeout,
    )

    try:
        body = response.json()
    except Exception:
        body = {
            "status": "error",
            "message": "IIFL returned a non-JSON response",
            "http_status": response.status_code,
            "raw_response": response.text[:1000],
        }

    return response.status_code, body
