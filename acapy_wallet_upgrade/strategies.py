from abc import ABC, abstractmethod
import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
from typing import Dict, Optional, Union

import base58
import cbor2
import msgpack
import nacl.pwhash

from acapy_wallet_upgrade.error import UpgradeError
from acapy_wallet_upgrade.pg_mwst_connection import PgMWSTConnection

from .db_connection import DbConnection, Wallet
from .pg_connection import PgConnection, PgWallet
from .sqlite_connection import SqliteConnection


# Constants
CHACHAPOLY_KEY_LEN = 32
CHACHAPOLY_NONCE_LEN = 12
CHACHAPOLY_TAG_LEN = 16
ENCRYPTED_KEY_LEN = CHACHAPOLY_NONCE_LEN + CHACHAPOLY_KEY_LEN + CHACHAPOLY_TAG_LEN


class Strategy(ABC):
    """Base class for upgrade strategies."""

    def __init__(self, conn: DbConnection):
        self.conn = conn

    def encrypt_merged(
        self, message: bytes, my_key: bytes, hmac_key: bytes = None
    ) -> bytes:
        if hmac_key:
            nonce = hmac.HMAC(hmac_key, message, digestmod=hashlib.sha256).digest()[
                :CHACHAPOLY_NONCE_LEN
            ]
        else:
            nonce = os.urandom(CHACHAPOLY_NONCE_LEN)

        ciphertext = nacl.bindings.crypto_aead_chacha20poly1305_ietf_encrypt(
            message, None, nonce, my_key
        )

        return nonce + ciphertext

    def encrypt_value(
        self, category: bytes, name: bytes, value: bytes, hmac_key: bytes
    ) -> bytes:
        hasher = hmac.HMAC(hmac_key, digestmod=hashlib.sha256)
        hasher.update(len(category).to_bytes(4, "big"))
        hasher.update(category)
        hasher.update(len(name).to_bytes(4, "big"))
        hasher.update(name)
        value_key = hasher.digest()
        return self.encrypt_merged(value, value_key)

    def decrypt_merged(self, enc_value: bytes, key: bytes, b64: bool = False) -> bytes:
        if b64:
            enc_value = base64.b64decode(enc_value)

        nonce, ciphertext = (
            enc_value[:CHACHAPOLY_NONCE_LEN],
            enc_value[CHACHAPOLY_NONCE_LEN:],
        )
        return nacl.bindings.crypto_aead_chacha20poly1305_ietf_decrypt(
            ciphertext, None, nonce, key
        )

    def decrypt_tags(
        self, tags: str, name_key: bytes, value_key: Optional[bytes] = None
    ):
        for tag in tags.split(","):
            tag_name, tag_value = map(bytes.fromhex, tag.split(":"))
            name = self.decrypt_merged(tag_name, name_key)
            value = self.decrypt_merged(tag_value, value_key) if value_key else tag[1]
            yield name, value

    def decrypt_item(self, row: tuple, keys: dict, b64: bool = False):
        row_id, row_type, row_name, row_value, row_key, tags_enc, tags_plain = row
        value_key = self.decrypt_merged(row_key, keys["value"])
        value = self.decrypt_merged(row_value, value_key) if row_value else None
        tags = [
            (0, k, v)
            for k, v in (
                (
                    self.decrypt_tags(tags_enc, keys["tag_name"], keys["tag_value"])
                    if tags_enc
                    else ()
                )
            )
        ]
        for k, v in (
            self.decrypt_tags(tags_plain, keys["tag_name"]) if tags_plain else ()
        ):
            tags.append((1, k, v))
        return {
            "id": row_id,
            "type": self.decrypt_merged(row_type, keys["type"], b64),
            "name": self.decrypt_merged(row_name, keys["name"], b64),
            "value": value,
            "tags": tags,
        }

    def update_item(self, item: dict, key: dict) -> dict:
        tags = []
        for plain, k, v in item["tags"]:
            if not plain:
                v = self.encrypt_merged(v, key["tvk"], key["thk"])
            k = self.encrypt_merged(k, key["tnk"], key["thk"])
            tags.append((plain, k, v))

        ret_val = {
            "id": item["id"],
            "category": self.encrypt_merged(item["type"], key["ick"], key["ihk"]),
            "name": self.encrypt_merged(item["name"], key["ink"], key["ihk"]),
            "value": self.encrypt_value(
                item["type"], item["name"], item["value"], key["ihk"]
            ),
            "tags": tags,
        }

        return ret_val

    async def update_items(
        self,
        wallet: Wallet,
        indy_key: dict,
        profile_key: dict,
    ):
        while True:
            rows = await wallet.fetch_pending_items(1)
            if not rows:
                break

            upd = []
            for row in rows:
                result = self.decrypt_item(
                    row, indy_key, b64=isinstance(wallet, PgWallet)
                )
                upd.append(self.update_item(result, profile_key))
            await wallet.update_items(upd)

    async def fetch_indy_key(self, wallet: Wallet, wallet_key: str) -> dict:
        metadata_json = await wallet.get_metadata()
        metadata = json.loads(metadata_json)
        keys_enc = bytes(metadata["keys"])
        salt = bytes(metadata["master_key_salt"])

        salt = salt[:16]
        master_key = nacl.pwhash.argon2i.kdf(
            CHACHAPOLY_KEY_LEN,
            wallet_key.encode("ascii"),
            salt,
            nacl.pwhash.argon2i.OPSLIMIT_MODERATE,
            nacl.pwhash.argon2i.MEMLIMIT_MODERATE,
        )

        keys_mpk = self.decrypt_merged(keys_enc, master_key)
        keys_lst = msgpack.unpackb(keys_mpk)
        keys = dict(
            zip(
                (
                    "type",
                    "name",
                    "value",
                    "item_hmac",
                    "tag_name",
                    "tag_value",
                    "tag_hmac",
                ),
                keys_lst,
            )
        )
        keys["master"] = master_key
        keys["salt"] = salt
        return keys

    async def convert_items_to_askar(
        self, uri: str, wallet_key: str, profile: str = None
    ):
        from aries_askar import Key, Store

        print("Opening wallet with Askar...")
        store = await Store.open(uri, pass_key=wallet_key, profile=profile)

        print("Updating keys...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                keys = await txn.fetch_all("Indy::Key", limit=50)
                if not keys:
                    break
                for row in keys:
                    await txn.remove("Indy::Key", row.name)
                    meta = await txn.fetch("Indy::KeyMetadata", row.name)
                    if meta:
                        await txn.remove("Indy::KeyMetadata", meta.name)
                        meta = json.loads(meta.value)["value"]
                    key_sk = base58.b58decode(json.loads(row.value)["signkey"])
                    key = Key.from_secret_bytes("ed25519", key_sk[:32])
                    await txn.insert_key(row.name, key, metadata=meta)
                    upd_count += 1
                await txn.commit()
        print(f" {upd_count} updated")

        print("Updating master secret(s)...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                ms = await txn.fetch_all("Indy::MasterSecret")
                if not ms:
                    break
                elif len(ms) > 1:
                    raise Exception("Encountered multiple master secrets")
                else:
                    row = ms[0]
                    await txn.remove("Indy::MasterSecret", row.name)
                    await txn.insert("master_secret", "default", value=row.value)
                    upd_count += 1
                await txn.commit()
        print(f" {upd_count} updated")

        print("Updating DIDs...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                dids = await txn.fetch_all("Indy::Did", limit=50)
                if not dids:
                    break
                for row in dids:
                    await txn.remove("Indy::Did", row.name)
                    info = json.loads(row.value)
                    meta = await txn.fetch("Indy::DidMetadata", row.name)
                    if meta:
                        await txn.remove("Indy::DidMetadata", meta.name)
                        meta = json.loads(meta.value)["value"]
                        with contextlib.suppress(json.JSONDecodeError):
                            meta = json.loads(meta)
                    await txn.insert(
                        "did",
                        row.name,
                        value_json={
                            "did": info["did"],
                            "verkey": info["verkey"],
                            "metadata": meta,
                        },
                        tags={"verkey": info["verkey"]},
                    )
                    upd_count += 1
                await txn.commit()
        print(f" {upd_count} updated")

        print("Updating stored schemas...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                schemas = await txn.fetch_all("Indy::Schema", limit=50)
                if not schemas:
                    break
                for row in schemas:
                    await txn.remove("Indy::Schema", row.name)
                    await txn.insert(
                        "schema",
                        row.name,
                        value=row.value,
                    )
                    upd_count += 1
                await txn.commit()
        print(f" {upd_count} updated")

        print("Updating stored credential definitions...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                cred_defs = await txn.fetch_all("Indy::CredentialDefinition", limit=50)
                if not cred_defs:
                    break
                for row in cred_defs:
                    await txn.remove("Indy::CredentialDefinition", row.name)
                    sid = await txn.fetch("Indy::SchemaId", row.name)
                    if not sid:
                        raise Exception(
                            f"Schema ID not found for credential definition: {row.name}"
                        )
                    sid = sid.value.decode("utf-8")
                    await txn.insert(
                        "credential_def",
                        row.name,
                        value=row.value,
                        tags={"schema_id": sid},
                    )

                    priv = await txn.fetch(
                        "Indy::CredentialDefinitionPrivateKey", row.name
                    )
                    if priv:
                        await txn.remove(
                            "Indy::CredentialDefinitionPrivateKey", priv.name
                        )
                        await txn.insert(
                            "credential_def_private",
                            priv.name,
                            value=priv.value,
                        )
                    proof = await txn.fetch(
                        "Indy::CredentialDefinitionCorrectnessProof", row.name
                    )
                    if proof:
                        await txn.remove(
                            "Indy::CredentialDefinitionCorrectnessProof", proof.name
                        )
                        value = json.loads(proof.value)["value"]
                        await txn.insert(
                            "credential_def_key_proof",
                            proof.name,
                            value_json=value,
                        )
                    upd_count += 1

                await txn.commit()
        print(f" {upd_count} updated")

        print("Updating stored revocation registry definitions...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                reg_defs = await txn.fetch_all(
                    "Indy::RevocationRegistryDefinition", limit=50
                )
                if not reg_defs:
                    break
                for row in reg_defs:
                    await txn.remove("Indy::RevocationRegistryDefinition", row.name)
                    await txn.insert("revocation_reg_def", row.name, value=row.value)
                    upd_count += 1
                await txn.commit()
        print(f" {upd_count} updated")

        print("Updating stored revocation registry keys...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                reg_defs = await txn.fetch_all(
                    "Indy::RevocationRegistryDefinitionPrivate", limit=50
                )
                if not reg_defs:
                    break
                for row in reg_defs:
                    await txn.remove(
                        "Indy::RevocationRegistryDefinitionPrivate", row.name
                    )
                    await txn.insert(
                        "revocation_reg_def_private", row.name, value=row.value
                    )
                    upd_count += 1
                await txn.commit()
        print(f" {upd_count} updated")

        print("Updating stored revocation registry states...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                reg_defs = await txn.fetch_all("Indy::RevocationRegistry", limit=50)
                if not reg_defs:
                    break
                for row in reg_defs:
                    await txn.remove("Indy::RevocationRegistry", row.name)
                    await txn.insert("revocation_reg", row.name, value=row.value)
                    upd_count += 1
                await txn.commit()
        print(f" {upd_count} updated")

        print("Updating stored revocation registry info...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                reg_defs = await txn.fetch_all("Indy::RevocationRegistryInfo", limit=50)
                if not reg_defs:
                    break
                for row in reg_defs:
                    await txn.remove("Indy::RevocationRegistryInfo", row.name)
                    await txn.insert("revocation_reg_info", row.name, value=row.value)
                    upd_count += 1
                await txn.commit()
        print(f" {upd_count} updated")

        print("Updating stored credentials...", end="")
        upd_count = 0
        while True:
            async with store.transaction() as txn:
                creds = await txn.fetch_all("Indy::Credential", limit=50)
                if not creds:
                    break
                for row in creds:
                    await txn.remove("Indy::Credential", row.name)
                    cred_data = row.value_json
                    tags = self._credential_tags(cred_data)
                    await txn.insert("credential", row.name, value=row.value, tags=tags)
                    upd_count += 1
                await txn.commit()
        print(f" {upd_count} updated")

        print("Closing wallet")
        await store.close()

    def _credential_tags(self, cred_data: dict) -> dict:
        schema_id = cred_data["schema_id"]
        schema_id_parts = re.match(r"^(\w+):2:([^:]+):([^:]+)$", schema_id)
        if not schema_id_parts:
            raise UpgradeError(f"Error parsing credential schema ID: {schema_id}")
        cred_def_id = cred_data["cred_def_id"]
        cdef_id_parts = re.match(r"^(\w+):3:CL:([^:]+):([^:]+)$", cred_def_id)
        if not cdef_id_parts:
            raise UpgradeError(f"Error parsing credential definition ID: {cred_def_id}")

        tags = {
            "schema_id": schema_id,
            "schema_issuer_did": schema_id_parts[1],
            "schema_name": schema_id_parts[2],
            "schema_version": schema_id_parts[3],
            "issuer_did": cdef_id_parts[1],
            "cred_def_id": cred_def_id,
            "rev_reg_id": cred_data.get("rev_reg_id") or "None",
        }
        for k, attr_value in cred_data["values"].items():
            attr_name = k.replace(" ", "")
            tags[f"attr::{attr_name}::value"] = attr_value["raw"]

        return tags

    async def create_config(self, name: str, indy_key: dict):
        pass_key = "kdf:argon2i:13:mod?salt=" + indy_key["salt"].hex()
        await self.conn.create_config(default_profile=name, key=pass_key)

    async def init_profile(self, wallet: Wallet, name: str, indy_key: dict) -> dict:
        profile_key = {
            "ver": "1",
            "ick": indy_key["type"],
            "ink": indy_key["name"],
            "ihk": indy_key["item_hmac"],
            "tnk": indy_key["tag_name"],
            "tvk": indy_key["tag_value"],
            "thk": indy_key["tag_hmac"],
        }

        enc_pk = self.encrypt_merged(cbor2.dumps(profile_key), indy_key["master"])
        await wallet.insert_profile(name, enc_pk)
        return profile_key

    @abstractmethod
    async def run(self):
        """Perform the upgrade."""


class DbpwStrategy(Strategy):
    """Database per wallet upgrade strategy."""

    def __init__(
        self,
        conn: Union[SqliteConnection, PgConnection],
        wallet_name: str,
        wallet_key: str,
    ):
        self.conn = conn
        self.wallet_name = wallet_name
        self.wallet_key = wallet_key

    async def run(self):
        """Perform the upgrade."""
        await self.conn.connect()
        wallet = self.conn.get_wallet()

        try:
            await self.conn.pre_upgrade()
            indy_key = await self.fetch_indy_key(wallet, self.wallet_key)
            await self.create_config(self.wallet_name, indy_key)
            profile_key = await self.init_profile(wallet, self.wallet_name, indy_key)
            await self.update_items(wallet, indy_key, profile_key)
            await self.conn.finish_upgrade()
        finally:
            await self.conn.close()

        await self.convert_items_to_askar(self.conn.uri, self.wallet_key)


class MwstAsProfilesStrategy(Strategy):
    """MultiWalletSingleTable as Askar Profiles upgrade strategy."""

    def __init__(
        self, conn: PgMWSTConnection, base_wallet_name: str, wallet_keys: Dict[str, str]
    ):
        self.conn = conn
        self.base_wallet_name = base_wallet_name
        self.base_wallet_key = wallet_keys[base_wallet_name]
        self.wallet_keys = wallet_keys

    async def init_profile(
        self, wallet: Wallet, name: str, base_indy_key: dict, indy_key: dict
    ) -> dict:
        profile_key = {
            "ver": "1",
            "ick": indy_key["type"],
            "ink": indy_key["name"],
            "ihk": indy_key["item_hmac"],
            "tnk": indy_key["tag_name"],
            "tvk": indy_key["tag_value"],
            "thk": indy_key["tag_hmac"],
        }

        enc_pk = self.encrypt_merged(cbor2.dumps(profile_key), base_indy_key["master"])
        await wallet.insert_profile(name, enc_pk)
        return profile_key

    async def run(self):
        """Perform the upgrade."""
        await self.conn.connect()

        try:
            await self.conn.pre_upgrade()
            base_wallet = self.conn.get_wallet(self.base_wallet_name)

            base_indy_key: dict = await self.fetch_indy_key(
                base_wallet, self.base_wallet_key
            )
            await self.create_config(self.base_wallet_name, base_indy_key)

            for wallet_name, wallet_key in self.wallet_keys.items():
                wallet = self.conn.get_wallet(wallet_name)
                indy_key = await self.fetch_indy_key(wallet, wallet_key)
                profile_key = await self.init_profile(
                    wallet, wallet_name, base_indy_key, indy_key
                )
                await self.update_items(wallet, indy_key, profile_key)

            await self.conn.finish_upgrade()
        finally:
            await self.conn.close()

        for wallet_name in self.wallet_keys.keys():
            await self.convert_items_to_askar(
                self.conn.uri, self.base_wallet_key, wallet_name
            )
