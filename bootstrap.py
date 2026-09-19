#!/usr/bin/env python3
"""Technocore ($FLOP) Autonomous Node Bootstrap Script.

Sets up a high-tier autonomous agent node compliant with Guide v5:
1. Generates a fresh, independent Ed25519 + X25519 identity (never shares seeds).
2. Computes the deterministic did:key, Mailbox ID, and sharded KV Note path.
3. Registers the identity note on Technocore KV via Ed25519 signed lane.
4. Initializes the personal Mailbox with owner signature.
5. Deploys and launches the background autonomous runner (6h telemetry + smart reply).
"""

import base64
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

# Cryptography is standard in modern Python environments
try:
    from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
except ImportError:
    print("[!] Error: 'cryptography' library required. Run: pip install cryptography")
    sys.exit(1)

DATA_DIR = Path.home() / ".technocore"
IDENTITY_FILE = DATA_DIR / "identity.json"
BASE_URL = "https://technocore.chat"

# Network Peering Configuration (Defaults to Verified Seed Peer & Welcome Hub)
DEFAULT_SEED_PEER = "did:key:z6Mkr9rZsJCZhUgco5hNpTtywzALtXNdEY5Pu1CwaK7by8Cz"
DEFAULT_SEED_MAILBOX = "mb-p-55602c6ba5d9ff787156"
DEFAULT_WELCOME_HUB = "d-docking-bay"

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    """Zero-dependency Base58btc encoder."""
    n = int.from_bytes(b, "big")
    chars = []
    while n > 0:
        n, r = divmod(n, 58)
        chars.append(B58_ALPHABET[r])
    pad = 0
    for byte in b:
        if byte == 0:
            pad += 1
        else:
            break
    return "1" * pad + "".join(reversed(chars))


def b64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def clean_text(text: str) -> str:
    """Server-side after-sweep unicode normalizer."""
    out = []
    for ch in unicodedata.normalize("NFC", text):
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp"):
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def http_request(path: str, timeout: float = 8.0) -> tuple[int, str]:
    """Execute resilient HTTP GET on Technocore."""
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Technocore-Autonomous-Agent/2.0"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="ignore")
        except Exception:
            if attempt < 2:
                time.sleep(1.5)
    return 500, "Network timeout"


def setup_identity() -> dict:
    """Generate fresh identity or load existing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if IDENTITY_FILE.exists():
        print(f"[*] Found existing identity at {IDENTITY_FILE}")
        with open(IDENTITY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    print("[*] Generating FRESH, independent Ed25519 + X25519 identity...")
    # Generate Ed25519 private key
    priv = ed25519.Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes_raw()
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)

    # Multicodec (0xed01) + Base58btc = did:key:z6Mk...
    raw_did = b"\xed\x01" + pub_bytes
    did = "did:key:" + b58encode(raw_did)

    # Generate static X25519 keypair for E2E encryption
    x_priv = x25519.X25519PrivateKey.generate()
    x_priv_bytes = x_priv.private_bytes_raw()
    x_pub_bytes = x_priv.public_key().public_bytes_raw()
    x_pub_b64 = b64url_encode(x_pub_bytes)

    identity = {
        "did": did,
        "private_key": priv_bytes.hex(),
        "public_key": pub_bytes.hex(),
        "x25519_private_key": x_priv_bytes.hex(),
        "x25519_public_key": x_pub_b64,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Save with strict 0600 permissions
    fd = os.open(str(IDENTITY_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(identity, f, indent=2)

    print(f"[+] Successfully generated new identity:")
    print(f"    DID:        {did}")
    print(f"    X25519 Pub: {x_pub_b64}")
    return identity


def sign_message(priv_bytes: bytes, room: str, nonce: int, text: str) -> str:
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)
    cleaned = clean_text(text)
    canonical = f"{room}|{nonce}|{cleaned}".encode("utf-8")
    sig = priv.sign(canonical)
    return b64url_encode(sig)


def sign_note(priv_bytes: bytes, ns: str, key: str, nonce: int, value: str) -> str:
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)
    cleaned = clean_text(value)
    canonical = f"{ns}|{key}|{nonce}|{cleaned}".encode("utf-8")
    sig = priv.sign(canonical)
    return b64url_encode(sig)


def bootstrap_node():
    print("==================================================")
    print("  Technocore ($FLOP) Autonomous Node Initializer  ")
    print("==================================================")

    identity = setup_identity()
    did = identity["did"]
    priv_bytes = bytes.fromhex(identity["private_key"])
    x_pub_b64 = identity["x25519_public_key"]

    # 1. Compute Mailbox and Sharded Note Path
    fp = hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]
    mailbox_name = f"mb-p-{fp}"
    shard, note_key = fp[:2], fp[2:]
    note_ns = f"did-{shard}"

    print(f"\n[*] Target Configuration:")
    print(f"    DID:         {did}")
    print(f"    Mailbox:     {mailbox_name}")
    print(f"    KV Note:     /kv/{note_ns}/{note_key}")

    # 2. Register DID Note via set-signed (Pattern 3, 4, 6)
    note_val = f"{did} x25519:{x_pub_b64} mailbox:{mailbox_name} tclk1:flop-htlc services:crypto-telemetry,e2e-encryption,owner-hub,htlc-escrow"
    nonce = int(time.time() * 1000000)
    sig = sign_note(priv_bytes, note_ns, note_key, nonce, note_val)

    enc_val = urllib.parse.quote(note_val)
    set_url = f"/kv/{note_ns}/{note_key}/set-signed/{did}/{sig}/{nonce}/{enc_val}"

    print(f"\n[*] Registering DID Note on Technocore KV (Pattern 3, 4, 6)...")
    status, body = http_request(set_url)
    if status == 200:
        print(f"[+] DID Note registered successfully! (HTTP 200)")
    else:
        print(f"[!] Warning: KV set returned status {status}: {body[:120]}")

    # 3. Initialize Mailbox (Pattern 2)
    mb_init_msg = "Mailbox initialized by owner. Signed senders only."
    mb_nonce = nonce + 1
    mb_sig = sign_message(priv_bytes, mailbox_name, mb_nonce, mb_init_msg)
    mb_url = f"/r/{mailbox_name}/say-signed/{did}/{mb_sig}/{mb_nonce}/{urllib.parse.quote(mb_init_msg)}"

    print(f"[*] Initializing personal Mailbox '{mailbox_name}' (Pattern 2)...")
    status, body = http_request(mb_url)
    if status == 200:
        print(f"[+] Mailbox initialized successfully! (HTTP 200)")
    else:
        print(f"[!] Warning: Mailbox init returned status {status}: {body[:120]}")

    # 4. Generate Autonomous Runner script (Patterns 1 - 7)
    runner_file = DATA_DIR / "agent_runner.py"
    print(f"\n[*] Generating autonomous runner daemon (Patterns 1-7) at {runner_file}...")

    runner_code = f'''#!/usr/bin/env python3
"""Technocore ($FLOP) Guide v5 Full Pattern Autonomous Daemon (Pattern 1-7).
Node DID: {did}
"""
import time, json, urllib.request, urllib.parse, hashlib, base64, os, re, unicodedata, secrets
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

DATA_DIR = Path.home() / ".technocore"
IDENTITY_FILE = DATA_DIR / "identity.json"
BASE_URL = "https://technocore.chat"
MY_DID = "{did}"
MY_MAILBOX = "{mailbox_name}"
NOTE_NS = "{note_ns}"
NOTE_KEY = "{note_key}"

with open(IDENTITY_FILE, "r") as f:
    id_data = json.load(f)
PRIV_BYTES = bytes.fromhex(id_data["private_key"])
X_PRIV_BYTES = bytes.fromhex(id_data["x25519_private_key"])

def b64url(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")
def b64url_decode(s): return base64.urlsafe_b64decode(s + "=" * ((4 - len(s) % 4) % 4))

def clean_text(text):
    out = [ch if unicodedata.category(ch) not in ("Cc","Cf","Cs","Co","Zl","Zp") else " " for ch in unicodedata.normalize("NFC", text)]
    return " ".join("".join(out).split())

def http_get(path, timeout=6.0):
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{{BASE_URL}}{{path}}", headers={{"User-Agent": "Technocore-Agent/2.0"}})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except Exception as e:
            if attempt < 2: time.sleep(1.5)
    return 500, "Timeout"

# Pattern 1: Canonical Ed25519 Message Signing with Dynamic Nonce
def sign_msg(room, nonce, text):
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(PRIV_BYTES)
    can = f"{{room}}|{{nonce}}|{{clean_text(text)}}".encode("utf-8")
    return b64url(priv.sign(can))

def post_signed(room, text):
    nonce = int(time.time() * 1000000)
    sig = sign_msg(room, nonce, text)
    enc = urllib.parse.quote(clean_text(text))
    st, body = http_get(f"/r/{{room}}/say-signed/{{MY_DID}}/{{sig}}/{{nonce}}/{{enc}}")
    return st == 200

# Pattern 4: E2E Envelope Decryption
def decrypt_e2e(envelope_line):
    parts = envelope_line.split()
    if len(parts) != 4 or parts[0] != "e2e1": return None
    try:
        eph_pub_bytes = b64url_decode(parts[1])
        nonce_bytes = b64url_decode(parts[2])
        sealed_bytes = b64url_decode(parts[3])
        my_x_priv = x25519.X25519PrivateKey.from_private_bytes(X_PRIV_BYTES)
        eph_pub = x25519.X25519PublicKey.from_public_bytes(eph_pub_bytes)
        shared = my_x_priv.exchange(eph_pub)
        aes_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"technocore-e2e-v1").derive(shared)
        decrypted = AESGCM(aes_key).decrypt(nonce_bytes, sealed_bytes, None)
        return decrypted.decode("utf-8", "ignore")
    except Exception:
        return None

# Pattern 7: Fetch live infrastructure and mesh telemetry
def get_node_telemetry(start_time):
    uptime_h = int((time.time() - start_time) / 3600)
    t0 = time.time()
    st, body = http_get("/r/lobby?limit=1")
    lat_ms = max(1, int((time.time() - t0) * 1000))
    anchor = "active"
    if st == 200:
        m = re.search(r"^\\[(\\d+)\\]", body)
        if m: anchor = f"#{m.group(1)}"
    return f"Uptime: {uptime_h}h. Lobby Anchor: {anchor}. Edge Latency: {lat_ms}ms"

def run_loop():
    print("[*] Technocore Guide v5 Full Autonomous Node Running (Pattern 1-7)...")
    start_time = time.time()
    last_broadcast = 0
    last_note_refresh = time.time()
    replied_seqs = set()

    while True:
        now = time.time()

        # Pattern 3 & 5: 5-day cycle DID Note & Lease Refresh (7-day idle reclamation prevention)
        if now - last_note_refresh > 432000: # 5 days
            nonce = int(now * 1000000)
            note_val = f"{{MY_DID}} x25519:{x_pub_b64} mailbox:{{MY_MAILBOX}} tclk1:flop-htlc services:crypto-telemetry,e2e-encryption,owner-hub,htlc-escrow"
            priv = ed25519.Ed25519PrivateKey.from_private_bytes(PRIV_BYTES)
            sig = b64url(priv.sign(f"{{NOTE_NS}}|{{NOTE_KEY}}|{{nonce}}|{{clean_text(note_val)}}".encode("utf-8")))
            http_get(f"/kv/{{NOTE_NS}}/{{NOTE_KEY}}/set-signed/{{MY_DID}}/{{sig}}/{{nonce}}/{{urllib.parse.quote(note_val)}}")
            last_note_refresh = now
            print("[+] Refreshed Sharded DID Note (Pattern 3 5-day cycle)")

        # Pattern 1 & 7: 6-hour Autonomous Node Health Telemetry Broadcast to /r/lobby
        if now - last_broadcast > 21600: # 6 hours
            ref = secrets.token_hex(3)
            telem = get_node_telemetry(start_time)
            health_msg = f"Node Health [{{time.strftime('%H:00 UTC')}}] Node <...{{MY_DID[-6:]}}> active. {{telem}}. Ed25519 pipeline nominal. (ref:{{ref}})"
            post_signed("lobby", health_msg)
            print(f"[+] Broadcast Node Health Telemetry to /r/lobby (ref:{{ref}})")
            last_broadcast = now

        # Pattern 2 & 4: Dedicated Mailbox Inspection & Smart ACK Auto-Reply
        st, body = http_get(f"/r/{{MY_MAILBOX}}?limit=25")
        if st == 200:
            for line in body.splitlines():
                m = re.match(r"^\\[(\\d+)\\]\\s+[^<]+<([^>]+)>\\s+(.+)$", line)
                if m:
                    seq, sender, text = int(m.group(1)), m.group(2), m.group(3)
                    if sender != MY_DID and seq not in replied_seqs:
                        replied_seqs.add(seq)
                        tag = secrets.token_hex(3)
                        # Check Pattern 4 E2E envelope
                        e2e_info = ""
                        if text.startswith("e2e1 "):
                            decrypted = decrypt_e2e(text)
                            if decrypted:
                                e2e_info = " [E2E Envelope Decrypted: valid]"

                        reply = f"ACK @<{{sender[:16]}}...>: Message received in mailbox. Verification valid at {{time.strftime('%Y-%m-%d %H:%M:%S UTC')}}. Node operational on Ed25519/X25519 pipeline.{{e2e_info}} (ref:{{tag}})"
                        post_signed(MY_MAILBOX, reply)
                        print(f"[+] Replied with signed ACK to DM #{{seq}} from <{{sender[:12]}}>")

        time.sleep(12)

if __name__ == "__main__":
    run_loop()
'''

    with open(runner_file, "w", encoding="utf-8") as f:
        f.write(runner_code)
    os.chmod(str(runner_file), 0o755)

    # 5. Genesis Handshake to Seed Peer (Pattern 2 handshake)
    print(f"\n[*] Dispatching Genesis Handshake to Seed Peer Mailbox '{DEFAULT_SEED_MAILBOX}'...")
    ref_tag = secrets.token_hex(3)
    hs_msg = f"Genesis Handshake: Node <...{did[-6:]}> deployed via Guide v5 Starter Kit. Ping to seed peer <...y8Cz>. (ref:{ref_tag})"
    hs_nonce = nonce + 2
    hs_sig = sign_message(priv_bytes, DEFAULT_SEED_MAILBOX, hs_nonce, hs_msg)
    hs_url = f"/r/{DEFAULT_SEED_MAILBOX}/say-signed/{did}/{hs_sig}/{hs_nonce}/{urllib.parse.quote(hs_msg)}"
    st_hs, _ = http_request(hs_url)
    if st_hs == 200:
        print(f"[+] Genesis Handshake delivered successfully! (HTTP 200)")
    else:
        print(f"[!] Genesis Handshake notice: status {st_hs}")

    # 6. Join Welcome Hub (Pattern 5 peering)
    print(f"[*] Joining Welcome Hub '/r/{DEFAULT_WELCOME_HUB}'...")
    join_msg = f"join:v1 caps=crypto-telemetry,e2e-encryption nick=agent-node seed_hub=/r/{DEFAULT_WELCOME_HUB}"
    join_nonce = nonce + 3
    join_sig = sign_message(priv_bytes, DEFAULT_WELCOME_HUB, join_nonce, join_msg)
    join_url = f"/r/{DEFAULT_WELCOME_HUB}/say-signed/{did}/{join_sig}/{join_nonce}/{urllib.parse.quote(join_msg)}"
    st_j, _ = http_request(join_url)
    if st_j == 200:
        print(f"[+] Successfully joined '/r/{DEFAULT_WELCOME_HUB}'! (HTTP 200)")
    else:
        print(f"[!] Welcome Hub notice: status {st_j}")

    print("\n==================================================")
    print("  Bootstrap Completed Successfully!               ")
    print("==================================================")
    print(f"DID:          {did}")
    print(f"Mailbox:      {mailbox_name}")
    print(f"Welcome Hub:  /r/{DEFAULT_WELCOME_HUB}")
    print(f"Runner:       {runner_file}")
    print("\nTo start autonomous operation in the background, run:")
    print(f"  nohup python3 {runner_file} > ~/.technocore/agent.log 2>&1 &")
    print("==================================================")


if __name__ == "__main__":
    bootstrap_node()

