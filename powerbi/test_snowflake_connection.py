"""Snowflake connection test using private-key authentication.

Usage:
  python test_snowflake_connection.py

This script defaults to:
- user: LUDWIG_POWER_BI
- account: zna84829
- host: zna84829.snowflakecomputing.com
- private key file: snowflake_key.p8

You can override values via environment variables:
- SNOWFLAKE_USER
- SNOWFLAKE_ACCOUNT
- SNOWFLAKE_HOST
- SNOWFLAKE_PRIVATE_KEY_PATH
"""

import os
import pathlib

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from dbt_client import load_dotenv


def load_private_key(path: pathlib.Path) -> bytes:
    if not path.exists():
        raise FileNotFoundError(f"Private key file not found: {path}")

    private_key = serialization.load_pem_private_key(
        path.read_bytes(),
        password=None,
        backend=default_backend(),
    )
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def main() -> None:
    env = load_dotenv()

    def cfg(key: str, default: str) -> str:
        # real process env wins, then .env, then the built-in default
        return os.environ.get(key) or env.get(key) or default

    user = cfg("SNOWFLAKE_USER", "LUDWIG_POWER_BI")
    account = cfg("SNOWFLAKE_ACCOUNT", "zna84829")
    host = cfg("SNOWFLAKE_HOST", "zna84829.snowflakecomputing.com")
    key_path = pathlib.Path(cfg("SNOWFLAKE_PRIVATE_KEY_PATH", "snowflake_key.p8"))

    print(f"Using user={user} account={account} host={host} key={key_path}")
    private_key_bytes = load_private_key(key_path)

    conn = snowflake.connector.connect(
        user=user,
        account=account,
        private_key=private_key_bytes,
        host=host,
        protocol="https",
    )

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_version()")
            row = cur.fetchone()
            print("Snowflake current version:", row)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
