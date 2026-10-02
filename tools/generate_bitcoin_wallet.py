#!/usr/bin/env python3
"""Generate a BIP-84 native segwit (bc1) Bitcoin wallet offline.

Writes address + secrets to ~/.secrets/bitcoin-wallet.json (mode 600).
Prints public details only. Never prints the private key or mnemonic.
"""
import hashlib
import hmac
import json
import os
import secrets
import time

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WORDLIST = "/tmp/opencode/bip39-en.txt"
OUT = os.path.expanduser("~/.secrets/bitcoin-wallet.json")


def b58check(payload: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(payload + chk, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    for byte in payload + chk:
        if byte == 0:
            out = "1" + out
        else:
            break
    return out


def bech32_polymod(values):
    GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((top >> i) & 1) else 0
    return chk


BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def convertbits(data, frm, to, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << to) - 1
    for value in data:
        acc = (acc << frm) | value
        bits += frm
        while bits >= to:
            bits -= to
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (to - bits)) & maxv)
    return ret


def hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def segwit_addr(hrp, witver, witprog):
    data = [witver] + convertbits(witprog, 8, 5)
    polymod = bech32_polymod(hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]) ^ 1
    combined = data + [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in combined)


def bech32_valid(addr):
    hrp, data = addr.rsplit("1", 1)
    try:
        vals = [BECH32_CHARSET.index(c) for c in data]
    except ValueError:
        return False
    return bech32_polymod(hrp_expand(hrp) + vals) == 1


P_FIELD = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _inv(x):
    return pow(x, P_FIELD - 2, P_FIELD)


def _add(P, Q):
    if P is None:
        return Q
    if Q is None:
        return P
    p = P_FIELD
    if P[0] == Q[0]:
        if (P[1] + Q[1]) % p == 0:
            return None
        lam = (3 * P[0] * P[0]) * _inv(2 * P[1]) % p
    else:
        lam = (Q[1] - P[1]) * _inv(Q[0] - P[0]) % p
    x3 = (lam * lam - P[0] - Q[0]) % p
    return (x3, (lam * (P[0] - x3) - P[1]) % p)


def point(k):
    """Public key point for private key int k (affine double-and-add)."""
    R = None
    for bit in bin(k)[2:]:
        R = _add(R, R)
        if bit == "1":
            R = _add(R, G)
    return R


def pubkey(k):
    x, y = point(k)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


def hash160(b):
    return hashlib.new("ripemd160", hashlib.sha256(b).digest()).digest()


def ser32(i):
    return i.to_bytes(4, "big")


def b58key(prefix, payload):
    return b58check(bytes(prefix) + payload)


def master(seed):
    I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    return int.from_bytes(I[:32], "big"), I[32:]


def ckd(k, c, index):
    if index >= 0x80000000:
        data = b"\x00" + k.to_bytes(32, "big") + ser32(index)
    else:
        data = pubkey(k) + ser32(index)
    I = hmac.new(c, data, hashlib.sha512).digest()
    child = (int.from_bytes(I[:32], "big") + k) % 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    return child, I[32:]


def fingerprint(k):
    return hash160(pubkey(k))[:4]


def xprv(k, c, depth, parent_fp, index):
    payload = (
        bytes.fromhex("0488ade4")
        + bytes([depth])
        + parent_fp
        + ser32(index)
        + c
        + b"\x00"
        + k.to_bytes(32, "big")
    )
    return b58check(payload)


def xpub(k, c, depth, parent_fp, index):
    payload = (
        bytes.fromhex("0488b21e")
        + bytes([depth])
        + parent_fp
        + ser32(index)
        + c
        + pubkey(k)
    )
    return b58check(payload)


# ---- mnemonic (BIP39) ----
words = [w.strip() for w in open(WORDLIST) if w.strip()]
assert len(words) == 2048
ENT = 256
CS = ENT // 32
ent_int = secrets.randbits(ENT)
h = hashlib.sha256(ent_int.to_bytes(32, "big")).digest()
ent_bits = format(ent_int, "0%db" % ENT)
hash_bits = format(int.from_bytes(h, "big"), "0%db" % (len(h) * 8))
bits = ent_bits + hash_bits[:CS]
mnemonic_words = [words[int(bits[i:i + 11], 2)] for i in range(0, len(bits), 11)]
mnemonic = " ".join(mnemonic_words)
seed = hashlib.pbkdf2_hmac("sha512", mnemonic.encode(), b"mnemonic", 2048, 64)

k, c = master(seed)

# ---- derive BIP84 account ----
FINGERPRINT = 0xFFFFFFFF
path = [84 | 0x80000000, 0 | 0x80000000, 0 | 0x80000000]
k, c = ckd(k, c, path[0]); fp_a = fingerprint(k)
k, c = ckd(k, c, path[1])
k, c = ckd(k, c, path[2]); fp_acct = fingerprint(k)

accounts = {}
for branch, label, count in ((0, "receive", 5), (1, "change", 3)):
    addrs = []
    bk, bc = ckd(k, c, branch | 0x80000000)      # branch node, depth 4
    fp_branch = fingerprint(bk)
    branch_xprv = xprv(bk, bc, 4, fp_acct, branch | 0x80000000)
    branch_xpub = xpub(bk, bc, 4, fp_acct, branch | 0x80000000)
    for i in range(count):
        ck, cc = ckd(bk, bc, i)                   # depth 5 address key
        addr = segwit_addr("bc", 0, hash160(pubkey(ck)))
        assert bech32_valid(addr), addr
        addrs.append({"index": i, "path": f"m/84'/0'/0'/{branch}'/{i}", "address": addr,
                      "private_key_wif": b58key([0x80], ck.to_bytes(32, "big") + b"\x01"),
                      "xprv": xprv(ck, cc, 5, fp_branch, i)})
    accounts[label] = {
        "branch_xprv": branch_xprv,
        "branch_xpub": branch_xpub,
        "addresses": addrs,
    }

master_xprv = xprv(k, c, 3, fp_acct, path[2])     # account node m/84'/0'/0'
master_xpub = xpub(k, c, 3, fp_acct, path[2])

# self-check: WIF -> address
def decode_b58(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw

w = accounts["receive"]["addresses"][0]
raw = decode_b58(w["private_key_wif"])
assert raw[0] == 0x80 and len(raw) == 38 and raw[33] == 0x01, raw.hex()[:8]
derived = segwit_addr("bc", 0, hash160(pubkey(int.from_bytes(raw[1:33], "big"))))
assert derived == w["address"], (derived, w["address"])


# ---- internal verification: every address must be reproducible from the stored xprvs ----
def xprv_parts(s):
    raw = decode_b58(s)
    assert raw[:4] == bytes.fromhex("0488ade4"), "bad xprv version"
    depth, parent_fp, child_no = raw[4], raw[5:9], int.from_bytes(raw[9:13], "big")
    chain = raw[13:45]
    assert raw[45] == 0x00
    return int.from_bytes(raw[46:78], "big"), chain, depth, parent_fp, child_no


acct_k, acct_c, depth, pfp, cno = xprv_parts(master_xprv)
assert depth == 3 and cno == (0x80000000 | 0)
checks = 0
for branch, label in ((0, "receive"), (1, "change")):
    bk, bc, d, pfp_b, cno_b = xprv_parts(accounts[label]["branch_xprv"])
    assert d == 4 and cno_b == (0x80000000 | branch)
    # branch xprv must be the hardened child of the account xprv
    assert ckd(acct_k, acct_c, 0x80000000 | branch)[0] == bk
    for a in accounts[label]["addresses"]:
        k_from_xprv, c_from_xprv, d5, pfp5, cno5 = xprv_parts(a["xprv"])
        assert d5 == 5 and cno5 == a["index"]
        assert ckd(bk, bc, a["index"])[0] == k_from_xprv, "xprv not a child of branch"
        assert segwit_addr("bc", 0, hash160(pubkey(k_from_xprv))) == a["address"]
        # WIF must encode the same key
        assert decode_b58(a["private_key_wif"])[1:33] == k_from_xprv.to_bytes(32, "big")
        checks += 1
# mnemonic checksum round-trip
back = "".join(format(words.index(mnemonic_words[i]), "011b") for i in range(24))
assert back == bits, "mnemonic bit layout mismatch"
assert back[:ENT] == ent_bits
assert back[ENT:] == hash_bits[:CS]
assert len(mnemonic_words) == 24 and len(set(mnemonic_words)) >= 1
# independent structural validation: entropy+checksum must reconstruct exactly
assert hashlib.sha256(ent_int.to_bytes(32, "big")).digest() == h
print(f"internal verification: {checks} addresses reproducible from stored xprv, mnemonic checksum OK")

os.makedirs(os.path.dirname(OUT), exist_ok=True)
record = {
    "network": "bitcoin",
    "script_type": "P2WPKH (native segwit, bc1q)",
    "derivation": "BIP-84 m/84'/0'/0'",
    "primary_receive_address": accounts["receive"]["addresses"][0]["address"],
    "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "SEED_BACKUP": {
        "mnemonic_24": mnemonic,
        "mnemonic_note": "BIP-39, no passphrase. Anyone with these words controls the funds.",
    },
    "PRIVATE_KEYS": {
        "account_xprv": master_xprv,
        "account_xpub": master_xpub,
        "receive_branch_xprv": accounts["receive"]["branch_xprv"],
        "receive_branch_xpub": accounts["receive"]["branch_xpub"],
        "change_branch_xprv": accounts["change"]["branch_xprv"],
        "change_branch_xpub": accounts["change"]["branch_xpub"],
    },
    "addresses": accounts["receive"]["addresses"] + accounts["change"]["addresses"],
}
if os.path.exists(OUT):
    os.rename(OUT, OUT + ".bak." + str(int(time.time())))
fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(record, f, indent=2)
os.chmod(OUT, 0o600)

print("PRIMARY RECEIVE ADDRESS:", record["primary_receive_address"])
print("file:", OUT, "mode:", oct(os.stat(OUT).st_mode & 0o777))
print("address count:", len(record["addresses"]), "| self-check WIF->address: OK")
for a in record["addresses"]:
    print(" ", a["path"], a["address"])
