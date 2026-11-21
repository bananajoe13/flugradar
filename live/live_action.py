"""Live-Pruefung bei GitHub Actions (Knopf auf der Online-Seite).

Der Auftrag kommt verschluesselt (AES-GCM, Schluessel aus dem Seiten-Passwort wie bei data.bin),
das Ergebnis geht verschluesselt in den Branch "live" (Datei r/<id>.bin). Die Protokolle bei GitHub sind
oeffentlich, deshalb wird hier nichts von den Reisedaten ausgegeben.

Umgebung: FLUGRADAR_PASSWORD (Secret), REQ (verschluesselter Auftrag), GH_TOKEN, REPO.
"""
import base64
import glob
import json
import os
import sys
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import flights
import publish

HERE = Path(__file__).resolve().parent


def github(method, path, **kw):
    r = requests.request(method, f"https://api.github.com/repos/{os.environ['REPO']}{path}", timeout=60,
                         headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                                  "Accept": "application/vnd.github+json"}, **kw)
    return r


def save_result(name, content):
    """Datei in den Branch "live" schreiben (Branch wird beim ersten Mal angelegt)."""
    blob = github("POST", "/git/blobs", json={"content": base64.b64encode(content).decode(), "encoding": "base64"}).json()
    head = github("GET", "/git/ref/heads/live")
    parent = head.json()["object"]["sha"] if head.ok else None
    base = github("GET", f"/git/commits/{parent}").json()["tree"]["sha"] if parent else None
    tree = github("POST", "/git/trees", json={**({"base_tree": base} if base else {}),
                                               "tree": [{"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]}]}).json()
    commit = github("POST", "/git/commits", json={"message": "Live-Pruefung", "tree": tree["sha"],
                                                  "parents": [parent] if parent else []}).json()
    if parent:
        r = github("PATCH", "/git/refs/heads/live", json={"sha": commit["sha"]})
    else:
        r = github("POST", "/git/refs", json={"ref": "refs/heads/live", "sha": commit["sha"]})
    r.raise_for_status()


def main():
    data_bin = glob.glob(str(HERE.parent / "*" / "data.bin"))
    salt = Path(data_bin[0]).read_bytes()[:16]  # gleiches Salz wie die Seite -> gleicher Schluessel
    key = publish.PBKDF2HMAC(algorithm=publish.hashes.SHA256(), length=32, salt=salt,
                             iterations=publish.ITERATIONS).derive(os.environ["FLUGRADAR_PASSWORD"].encode("utf-8"))
    raw = base64.b64decode(os.environ["REQ"])
    req = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))
    rid = "".join(c for c in str(req["id"]) if c.isalnum())[:32]
    try:
        flights.rt_check(req["trip"], req["a"], req["b"], req["f"])
        result = json.loads((HERE / "rtcheck.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - Fehler ohne Details zurueckmelden (Protokoll ist oeffentlich)
        result = {**req, "status": "error", "rt": {}}
    iv = os.urandom(12)
    save_result(f"r/{rid}.bin", iv + AESGCM(key).encrypt(iv, json.dumps(result).encode("utf-8"), None))
    print("Live-Pruefung ausgefuehrt:", result.get("status"))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
