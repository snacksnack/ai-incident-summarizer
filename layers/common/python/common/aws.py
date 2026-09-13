"""DynamoDB tables and Secrets Manager values, built once per execution
environment and shared by every module in a function (RC1-431).

Before the two-function collapse each of the seven handlers carried its own
copy of this: a lazily built handle per table, a secrets client and a dict
cache keyed by ARN. Clients are created on first use rather than at import, so
a module can be loaded with no region and no credentials, which the eval
loader and the unit tests both rely on.
"""
import os

import boto3

_dynamodb = None
_secrets_client = None
_tables: dict[str, object] = {}
_secrets: dict[str, str] = {}


def table(env_var: str):
    """The DynamoDB table whose name the environment variable carries."""
    global _dynamodb
    name = os.environ[env_var]
    if name not in _tables:
        if _dynamodb is None:
            _dynamodb = boto3.resource("dynamodb")
        _tables[name] = _dynamodb.Table(name)
    return _tables[name]


def secret(arn: str) -> str:
    """The SecretString of a Secrets Manager secret, fetched once per environment."""
    global _secrets_client
    if arn not in _secrets:
        if _secrets_client is None:
            _secrets_client = boto3.client("secretsmanager")
        _secrets[arn] = _secrets_client.get_secret_value(SecretId=arn)["SecretString"]
    return _secrets[arn]


def reset() -> None:
    """Drop every cached client, table and secret. Tests only."""
    global _dynamodb, _secrets_client
    _dynamodb = None
    _secrets_client = None
    _tables.clear()
    _secrets.clear()
