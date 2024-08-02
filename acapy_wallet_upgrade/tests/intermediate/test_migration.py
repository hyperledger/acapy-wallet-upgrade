from pathlib import Path
import shutil
import time
import subprocess
from typing import Callable, Dict, Optional, cast

import docker
from docker.models.containers import Container
import pytest

from acapy_wallet_upgrade.__main__ import main
from acapy_wallet_upgrade.error import UpgradeError, MissingWalletError


async def migrate_pg_db(
    db_port: int,
    db_name: str,
    strategy: str,
    wallet_name: Optional[str] = None,
    wallet_key: Optional[str] = None,
    base_wallet_name: Optional[str] = None,
    base_wallet_key: Optional[str] = None,
    wallet_keys: Optional[Dict[str, str]] = None,
    allow_missing_wallet: Optional[bool] = False,
    delete_indy_wallets: Optional[bool] = False,
):
    """Run migration script on postgresql database."""
    db_host = "localhost"
    user_name = "postgres"
    db_user_password = "mysecretpassword"
    # postgres[ql]://[username[:password]@][host[:port],]/database[?parameter_list]
    # \_____________/\____________________/\____________/\_______/\_______________/
    #     |                   |                  |          |            |
    #     |- schema           |- userspec        |          |            |- parameter list
    #                                            |          |
    #                                            |          |- database name
    #                                            |
    #                                            |- hostspec
    await main(
        strategy,
        f"postgres://{user_name}:{db_user_password}@{db_host}:{db_port}/{db_name}",
        wallet_name,
        wallet_key,
        base_wallet_name,
        base_wallet_key,
        wallet_keys,
        allow_missing_wallet,
        delete_indy_wallets,
    )


@pytest.fixture
def sqlite_temp(tmp_path: Path):
    def _sqlite_temp(actor: str):
        input_dir = Path(__file__).parent / "input"
        for src in input_dir.glob(f"{actor}.db*"):
            dst = tmp_path / src.name
            shutil.copyfile(src, dst)
        return tmp_path / f"{actor}.db"

    yield _sqlite_temp


@pytest.fixture
def sqlite_alice(sqlite_temp):
    yield sqlite_temp("alice")


@pytest.fixture
def sqlite_bob(sqlite_temp):
    yield sqlite_temp("bob")


@pytest.mark.asyncio
async def test_migration_sqlite(sqlite_alice, sqlite_bob):
    """
    Run the migration script with SQLite db files.
    """
    # Alice
    await main(
        strategy="dbpw",
        uri=f"sqlite://{sqlite_alice}",
        wallet_name="alice",
        wallet_key="insecure",
    )

    # Bob
    await main(
        strategy="dbpw",
        uri=f"sqlite://{sqlite_bob}",
        wallet_name="bob",
        wallet_key="insecure",
    )


def test_migration_script(sqlite_alice):
    """
    Run the migration script with SQLite db files.
    """
    subprocess.run(
        [
            "askar-upgrade",
            "--strategy",
            "dbpw",
            "--uri",
            f"sqlite://{sqlite_alice}",
            "--wallet-name",
            "alice",
            "--wallet-key",
            "insecure",
        ],
        check=True,
    )


def poll_until_pg_is_ready(container: Container, attempts: int = 5):
    for _ in range(attempts):
        exit_code, _ = container.exec_run("pg_isready")
        if exit_code == 0:
            break
        else:
            time.sleep(1)


@pytest.fixture
def postgres_with_volume(tmp_path: Path, unused_tcp_port_factory: Callable[[], int]):
    client = docker.from_env()
    containers = []

    def _postgres_with_volume(volume_name: str):
        src = Path(__file__).parent / "input" / volume_name
        d = tmp_path / "sub"
        d.mkdir()
        dst = d / volume_name
        shutil.copytree(src, dst)

        port = unused_tcp_port_factory()
        container = client.containers.run(
            "postgres:11",
            volumes={dst: {"bind": "/var/lib/postgresql/data", "mode": "rw,z"}},
            ports={"5432/tcp": port},
            environment=["POSTGRES_PASSWORD=mysecretpassword"],
            auto_remove=True,
            detach=True,
        )
        containers.append(container)

        # Give the DB a moment to start
        poll_until_pg_is_ready(cast(Container, container))
        return port

    yield _postgres_with_volume

    for container in containers:
        container.stop()


@pytest.mark.asyncio
async def test_migration_dbpw(postgres_with_volume):
    """
    Run the migration script with the db in the docker container.
    """
    port = postgres_with_volume("dbpw")
    await migrate_pg_db(
        db_port=port,
        db_name="alice",
        strategy="dbpw",
        wallet_name="alice",
        wallet_key="alice_insecure0",
    )
    await migrate_pg_db(
        db_port=port,
        db_name="bob",
        strategy="dbpw",
        wallet_name="bob",
        wallet_key="bob_insecure0",
    )


@pytest.mark.asyncio
async def test_migration_mwst_as_profiles(postgres_with_volume):
    """
    Run the migration script with the db in the docker container.
    """
    port = postgres_with_volume("mt-mwst")
    await migrate_pg_db(
        db_port=port,
        db_name="wallets",
        strategy="mwst-as-profiles",
        base_wallet_name="agency",
        base_wallet_key="agency_insecure0",
    )


@pytest.mark.asyncio
async def test_migration_mwst_as_profiles_leftover_wallet_warning(postgres_with_volume):
    """
    Run the migration script with the db in the docker container.
    """
    port = postgres_with_volume("mt-mwst-leftover-wallet")
    await migrate_pg_db(
        db_port=port,
        db_name="wallets",
        strategy="mwst-as-profiles",
        base_wallet_name="agency",
        base_wallet_key="agency_insecure0",
        delete_indy_wallets=True,
    )


@pytest.mark.parametrize(
    "wallet_keys, allow_missing_wallet",
    [
        (
            {
                "alice": "alice_insecure1",
                "bob": "bob_insecure1",
            },
            False,
        ),
        (
            {
                "alice": "alice_insecure1",
            },
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_migration_mwst_as_separate_stores(
    postgres_with_volume, wallet_keys, allow_missing_wallet
):
    """
    Run the migration script with the db in the docker container.
    """
    port = postgres_with_volume("mwst")
    await migrate_pg_db(
        db_port=port,
        db_name="wallets",
        strategy="mwst-as-stores",
        wallet_keys=wallet_keys,
        allow_missing_wallet=allow_missing_wallet,
    )


@pytest.mark.asyncio
async def test_migration_mwst_as_separate_stores_delete_indy_wallets_not_deleted(
    postgres_with_volume,
):
    """
    Run the migration script with the db in the docker container.
    """
    port = postgres_with_volume("mwst")
    await migrate_pg_db(
        db_port=port,
        db_name="wallets",
        strategy="mwst-as-stores",
        wallet_keys={
            "alice": "alice_insecure1",
        },
        allow_missing_wallet=True,
        delete_indy_wallets=True,
    )
    # Wallets are not deleted in this scenario


@pytest.mark.asyncio
async def test_migration_mwst_as_separate_stores_delete_indy_wallets_deleted(
    postgres_with_volume,
):
    """
    Run the migration script with the db in the docker container.
    """
    port = postgres_with_volume("mwst")
    await migrate_pg_db(
        db_port=port,
        db_name="wallets",
        strategy="mwst-as-stores",
        wallet_keys={
            "alice": "alice_insecure1",
            "bob": "bob_insecure1",
        },
        allow_missing_wallet=False,
        delete_indy_wallets=True,
    )
    # Wallets are deleted in this scenario


@pytest.mark.parametrize(
    "wallet_keys, error",
    [
        (
            {
                "alice": "alice_insecure1",
            },
            MissingWalletError,
        ),
        (
            {
                "alice": "alice_insecure1",
                "bob": "bob_insecure1",
                "carol": "carol_insecure1",
            },
            UpgradeError,
        ),
    ],
)
@pytest.mark.asyncio
async def test_migration_mwst_as_stores_wallet_misalignment(
    postgres_with_volume, wallet_keys, error
):
    """
    Run the migration script with the db in the docker container.
    """
    port = postgres_with_volume("mwst")
    with pytest.raises(error):
        await migrate_pg_db(
            db_port=port,
            db_name="wallets",
            strategy="mwst-as-stores",
            base_wallet_name="agency",
            base_wallet_key="agency_insecure0",
            wallet_keys=wallet_keys,
        )
