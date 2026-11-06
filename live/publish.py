"""Stellt das Dashboard verschluesselt auf GitHub Pages (Online-Ansicht).

Wird nach jedem Abruf von flights.py aufgerufen; einzeln: python publish.py
Einstellungen in github.json (nie weitergeben):
  repo      BENUTZERNAME/flugradar
  token     Fine-grained Personal Access Token, nur fuer dieses Repository, "Contents: Read and write"
  password  Passwort fuer die Online-Seite (mind. 12 Zeichen)
  folder    zufaelliger Unterordner der Seite (wird beim ersten Mal selbst erzeugt)
  salt      Salz fuer die Schluesselableitung (wird beim ersten Mal selbst erzeugt)

Schutz: Die Preisdaten liegen nur verschluesselt im (oeffentlichen) Repository:
gzip -> AES-256-GCM, Schluessel per PBKDF2-SHA256 aus dem Passwort. Entschluesselt wird im Browser.
Jeder Upload ersetzt den Branch durch einen einzelnen neuen Commit (ohne Vorgaenger),
damit das Repository nicht mit jeder Datendatei waechst und keine alten Staende sichtbar bleiben.
"""
import base64
import gzip
import json
import os
import secrets
import string
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent
CONF = ROOT / "github.json"
ITERATIONS = 600_000
MIN_PASSWORD = 12

EMPTY_INDEX = ('<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">'
               '<title>404</title><p>404</p>\n')
ROBOTS = "User-agent: *\nDisallow: /\n"

UNLOCK = """<div id="lock" style="position:fixed;inset:0;z-index:50;background:var(--page);display:flex;align-items:center;justify-content:center;padding:16px">
  <form id="lockForm" class="card" style="width:100%;max-width:340px;margin:0">
    <h2 style="margin-bottom:12px">✈ Flugradar</h2>
    <input type="password" id="lockPw" autocomplete="current-password" required style="width:100%;font:inherit;padding:9px 10px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--ink)">
    <label class="toggle" style="margin:10px 0"><input type="checkbox" id="lockRem" checked> <span id="lockRemTxt"></span></label>
    <button class="btn-solid" id="lockBtn" type="submit" style="width:100%"></button>
    <div id="lockErr" class="sub" style="color:var(--bad);margin-top:8px;min-height:1.2em"></div>
  </form>
</div>
<script>
(async () => {
  const TX = {
    de: { pw: 'Passwort', rem: 'Auf diesem Gerät merken', open: 'Öffnen', wrong: 'Falsches Passwort.', load: 'Daten konnten nicht geladen werden.', old: 'Dieser Browser ist zu alt für die verschlüsselte Ansicht.' },
    en: { pw: 'Password', rem: 'Remember on this device', open: 'Open', wrong: 'Wrong password.', load: 'Could not load the data.', old: 'This browser is too old for the encrypted view.' },
    es: { pw: 'Contraseña', rem: 'Recordar en este dispositivo', open: 'Abrir', wrong: 'Contraseña incorrecta.', load: 'No se pudieron cargar los datos.', old: 'Este navegador es demasiado antiguo para la vista cifrada.' },
  };
  let lg = 'de'; try { lg = localStorage.getItem('fr-lang') || (navigator.language || 'de').slice(0, 2); } catch (e) {}
  const T = TX[lg] || TX.de;
  const $ = (id) => document.getElementById(id);
  $('lockPw').placeholder = T.pw; $('lockRemTxt').textContent = T.rem; $('lockBtn').textContent = T.open;
  if (!window.crypto || !crypto.subtle || !window.DecompressionStream) { $('lockErr').textContent = T.old; return; }
  let buf;
  try { buf = new Uint8Array(await (await fetch('data.bin?t=' + Date.now(), { cache: 'no-store' })).arrayBuffer()); }
  catch (e) { $('lockErr').textContent = T.load; return; }
  const SALT = buf.slice(0, 16), IV = buf.slice(16, 28), CT = buf.slice(28);
  const b64 = (u) => btoa(String.fromCharCode(...new Uint8Array(u)));
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  async function unlock(key) {
    const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: IV }, key, CT);
    const txt = await new Response(new Blob([plain]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
    window.FLIGHT_DATA = JSON.parse(txt);
    window.FLIGHT_KEY = key;  // fuer den verschluesselten Auftrag der Live-Pruefung
    $('lock').remove();
    const s = document.createElement('script'); s.textContent = $('app').textContent; document.body.appendChild(s);
  }
  try {  // gemerkter Schluessel (nicht das Passwort)
    const k = localStorage.getItem('fr-key');
    if (k) { await unlock(await crypto.subtle.importKey('raw', unb64(k), 'AES-GCM', true, ['encrypt', 'decrypt'])); return; }
  } catch (e) { try { localStorage.removeItem('fr-key'); } catch (e2) {} }
  $('lockForm').onsubmit = async (e) => {
    e.preventDefault();
    $('lockBtn').disabled = true; $('lockErr').textContent = '';
    try {
      const base = await crypto.subtle.importKey('raw', new TextEncoder().encode($('lockPw').value), 'PBKDF2', false, ['deriveKey']);
      const key = await crypto.subtle.deriveKey({ name: 'PBKDF2', salt: SALT, iterations: __ITER__, hash: 'SHA-256' }, base, { name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
      const remember = $('lockRem').checked;  // vor unlock() lesen - danach ist das Formular weg
      await crypto.subtle.decrypt({ name: 'AES-GCM', iv: IV }, key, CT);  // wirft bei falschem Passwort
      if (remember) { try { localStorage.setItem('fr-key', b64(await crypto.subtle.exportKey('raw', key))); } catch (e2) {} }
      await unlock(key);
      return;
    } catch (err) { if (!$('lock')) throw err; $('lockErr').textContent = T.wrong; $('lockBtn').disabled = false; $('lockPw').select(); }
  };
  $('lockPw').focus();
})();
</script>"""


def load_conf():
    try:
        return json.loads(CONF.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def page_url(cfg):
    owner, name = cfg["repo"].split("/", 1)
    base = f"https://{owner.lower()}.github.io/"
    return base + ("" if name.lower() == f"{owner.lower()}.github.io" else name + "/") + cfg["folder"] + "/"


def online_url():
    """Adresse der Online-Seite oder None, wenn noch nicht eingerichtet."""
    cfg = load_conf()
    return page_url(cfg) if cfg.get("repo", "").count("/") == 1 and cfg.get("folder") else None


def build_page():
    html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
    parts = [
        ("<head>", '<head>\n<meta name="robots" content="noindex,nofollow">\n<meta name="referrer" content="no-referrer">'),
        ('<script src="data.js"></script>\n<script>', UNLOCK.replace("__ITER__", str(ITERATIONS)) + '\n<script type="text/plain" id="app">'),
    ]
    for old, new in parts:
        if html.count(old) != 1:
            raise RuntimeError(f"dashboard.html hat sich geaendert, Stelle nicht gefunden: {old[:30]}")
        html = html.replace(old, new)
    return html.encode("utf-8")


def encrypt(password, salt, live=None):
    """Seitendaten verschluesseln; live = Zugang fuer den Live-Knopf (nur verschluesselt in der Seite)."""
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS).derive(password.encode("utf-8"))
    iv = os.urandom(12)
    data = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
    if live:
        data["liveApi"] = live
    data = gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
    return salt + iv + AESGCM(key).encrypt(iv, data, None)


def publish(log=print):
    """Laedt hoch und wartet, bis die neue Version online abrufbar ist. Rueckgabe: (ok, meldung)."""
    cfg = load_conf()
    repo, token, pw = cfg.get("repo", ""), cfg.get("token", ""), cfg.get("password", "")
    branch = cfg.get("branch") or "main"
    if not token or repo.count("/") != 1:
        msg = "Online-Ansicht noch nicht eingerichtet (github.json)"
        log(f"GitHub Pages: {msg}")
        return False, msg
    if len(pw) < MIN_PASSWORD:
        msg = f"Passwort in github.json fehlt oder ist kürzer als {MIN_PASSWORD} Zeichen"
        log(f"GitHub Pages: {msg}")
        return False, msg
    changed = False
    if not cfg.get("folder"):
        cfg["folder"] = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
        changed = True
    if not cfg.get("salt"):
        cfg["salt"] = secrets.token_hex(16)
        changed = True
    if changed:
        CONF.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    api = f"https://api.github.com/repos/{repo}"
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                      "X-GitHub-Api-Version": "2022-11-28"})

    def call(method, url, **kw):
        r = s.request(method, api + url, timeout=60, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url}: {r.status_code} {r.text[:200]}")
        return r.json()

    folder = cfg["folder"]
    live = {"repo": repo, "token": cfg["liveToken"]} if cfg.get("liveToken") else None
    enc = encrypt(pw, bytes.fromhex(cfg["salt"]), live)
    files = {".nojekyll": b"\n", "robots.txt": ROBOTS.encode(), "index.html": EMPTY_INDEX.encode(),
             f"{folder}/index.html": build_page(), f"{folder}/data.bin": enc}
    if live:  # Live-Pruefung bei GitHub Actions: Aufgabe + Skripte (ohne Geheimnisse)
        files[".github/workflows/live.yml"] = (ROOT / "live.yml").read_bytes()
        for name in ("flights.py", "publish.py", "live_action.py", "config.json"):
            files[f"live/{name}"] = (ROOT / name).read_bytes()
    def upload(files):
        tree = []
        for path, content in files.items():
            blob = call("POST", "/git/blobs", json={"content": base64.b64encode(content).decode(), "encoding": "base64"})
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        t = call("POST", "/git/trees", json={"tree": tree})
        c = call("POST", "/git/commits", json={"message": f"Update {datetime.now():%d.%m.%Y %H:%M}",
                                                "tree": t["sha"], "parents": []})
        call("PATCH", f"/git/refs/heads/{branch}", json={"sha": c["sha"], "force": True})

    try:
        try:
            upload(files)
        except RuntimeError as e:
            if not live or " 403 " not in str(e):
                raise
            # Upload-Schluessel ohne Recht "Workflows" -> Seite trotzdem hochladen, nur ohne Live-Pruefung bei GitHub
            log("GitHub Pages: Live-Knopf nicht hochgeladen - dem Upload-Schluessel fehlt das Recht 'Workflows: Read and write'")
            upload({k: v for k, v in files.items() if not k.startswith((".github/", "live/"))})
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if " 409 " in msg:
            msg += " (Repository ist leer - beim Anlegen 'Add a README file' ankreuzen)"
        log(f"GitHub Pages: Hochladen fehlgeschlagen - {msg}")
        return False, "Hochladen fehlgeschlagen - Details in update.log"

    # warten, bis GitHub Pages die neue Version ausliefert (Bau + Zwischenspeicher, meist 1-10 Min.)
    url = page_url(cfg)
    for _ in range(40):
        time.sleep(30)
        try:
            r = requests.get(url + "data.bin", params={"t": int(time.time())}, timeout=30,
                             headers={"Cache-Control": "no-cache"})
            if r.status_code == 200 and r.content == enc:
                log("GitHub Pages: neue Version online")
                return True, url
        except requests.RequestException:
            pass
    log("GitHub Pages: hochgeladen, aber nach 20 Min. noch nicht online sichtbar (Pages aktiviert?)")
    return False, "hochgeladen, aber noch nicht online sichtbar - ist GitHub Pages aktiviert?"


if __name__ == "__main__":
    ok, info = publish()
    print(info)
    sys.exit(0 if ok else 1)
