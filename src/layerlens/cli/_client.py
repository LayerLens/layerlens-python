from __future__ import annotations

import re
import sys
import functools
import traceback as tb
from typing import Any, Callable

import click

from .._env import API_KEY_ENV, BASE_URL_ENV, EnvValue, find_api_key, find_base_url
from .._client import LayerLens
from .._exceptions import NotFoundError, APIStatusError, LayerLensError, AuthenticationError


def _env_notice(found: EnvValue, canonical: str) -> None:
    """Tell the user to rename a legacy variable (LAY-4220). stderr only."""
    if found.source != canonical:
        click.echo(
            f"warning: {found.source} is deprecated and will be removed in a future major release; "
            f"rename it to {canonical}",
            err=True,
        )


def resolve_api_key(ctx: click.Context) -> EnvValue | None:
    """The API key to use and where it came from, or ``None``.

    ``--api-key-file`` comes first, then ``--api-key`` and ``LAYERLENS_API_KEY``
    (through click); the legacy ``LAYERLENS_STRATIX_API_KEY`` /
    ``LAYERLENS_ATLAS_API_KEY`` still work, with a notice. An API key is always
    sent as ``x-api-key``, never as a Bearer token: the login (``/dgklmnr``)
    router only accepts Cognito tokens.
    """
    api_key = ctx.obj.get("api_key")
    key_file = ctx.obj.get("api_key_file")
    if key_file is not None:
        if api_key and ctx.obj.get("api_key_source") == "--api-key":
            raise click.UsageError("pass --api-key or --api-key-file, not both")
        return EnvValue(_read_key_file(key_file), "--api-key-file")
    if api_key:
        return EnvValue(api_key, ctx.obj.get("api_key_source", "--api-key"))
    found = find_api_key()
    if found is not None:
        _env_notice(found, API_KEY_ENV)
    return found


def _read_key_file(path: str) -> str:
    if not path:
        raise click.UsageError("--api-key-file needs a path, or - to read the key from standard input")
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8") as f:
                text = f.read()
    except (OSError, UnicodeDecodeError) as err:
        raise click.UsageError(f"--api-key-file {path}: the key could not be read: {err}") from None
    key = text.strip()
    if not key:
        raise click.UsageError(f"--api-key-file {path} holds no key")
    return key


def resolve_base_url(ctx: click.Context) -> str | None:
    """``--host``/``--port`` first, then ``LAYERLENS_BASE_URL`` (or a legacy name)."""
    base_url: str | None = ctx.obj.get("base_url")
    if base_url:
        return base_url
    found = find_base_url()
    if found is None:
        return None
    _env_notice(found, BASE_URL_ENV)
    return found.value


def api_error_text(err: APIStatusError) -> str:
    """The API's own sentence: the envelope's ``error``, or API Gateway's ``message``."""
    body = err.body
    if isinstance(body, str) and body:
        text = body
    elif isinstance(body, dict) and isinstance(body.get("message"), str):
        text = body["message"]
    else:
        text = err.message
    return f"{text} (HTTP {err.status_code})"


def build_client(ctx: click.Context, api_key: str, *, use_bearer_auth: bool) -> LayerLens:
    """Construct the client and verify the credential, or exit with the error.

    The library reads the organization on first use, so an instrumented program starts while
    LayerLens is down (BUG-03); a CLI command reads it here, so a bad key or an unreachable
    API fails at once in the API's own words.
    """
    try:
        client = LayerLens(api_key=api_key, base_url=resolve_base_url(ctx), use_bearer_auth=use_bearer_auth)
        client.organization_id  # noqa: B018 - reads the organization, verifying the credential
        return client
    except APIStatusError as e:
        click.echo(f"Error: {api_error_text(e)}", err=True)
        sys.exit(1)
    except LayerLensError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def get_client(ctx: click.Context) -> LayerLens:
    """Create a LayerLens client from CLI context options.

    Credential order:
    1. ``--api-key`` / ``LAYERLENS_API_KEY``, sent as ``x-api-key``
    2. ``LAYERLENS_STRATIX_API_KEY`` / ``LAYERLENS_ATLAS_API_KEY`` (deprecated), sent as ``x-api-key``
    3. The stored token from ``layerlens login``, sent as a Bearer token
    """
    resolved = resolve_api_key(ctx)
    if resolved is not None:
        return build_client(ctx, resolved.value, use_bearer_auth=False)

    from ._auth import SessionError, get_valid_token

    try:
        token = get_valid_token()
    except SessionError as err:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)
    if token:
        return build_client(ctx, token, use_bearer_auth=True)

    click.echo(
        "Error: no credentials found. Pass --api-key, set LAYERLENS_API_KEY, or run `layerlens login`.",
        err=True,
    )
    sys.exit(1)


def handle_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that catches SDK errors and prints user-friendly messages."""

    @functools.wraps(fn)
    @click.pass_context
    def wrapper(ctx: click.Context, *args: Any, **kwargs: Any) -> Any:
        try:
            return ctx.invoke(fn, *args, **kwargs)
        except AuthenticationError:
            click.echo("Error: Invalid or missing API key.", err=True)
            sys.exit(1)
        except NotFoundError as e:
            click.echo(f"Error: Resource not found. {e}", err=True)
            sys.exit(1)
        except APIStatusError as e:
            # The API's own sentence, as the env commands print it, not the exception's repr.
            click.echo(f"Error: {api_error_text(e)}", err=True)
            sys.exit(1)
        except LayerLensError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except (click.exceptions.Exit, click.ClickException, click.Abort):
            # A usage error raised by the command is click's to report, with its exit code.
            raise
        except Exception as e:
            if ctx.obj.get("verbose"):
                tb.print_exc()
            click.echo(f"Unexpected error: {e}", err=True)
            sys.exit(1)

    return wrapper


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


def resolve_model(client: LayerLens, identifier: str) -> Any:
    """Resolve a model by ID, key, or name."""
    # Try by ID first if it looks like a UUID
    if _is_uuid(identifier):
        model = client.models.get_by_id(identifier)
        if model:
            return model

    # Try by key
    model = client.models.get_by_key(identifier)
    if model:
        return model

    # Try by name
    models = client.models.get(name=identifier)
    if models:
        return models[0]

    return None


def resolve_benchmark(client: LayerLens, identifier: str) -> Any:
    """Resolve a benchmark by ID, key, or name."""
    # Try by ID first if it looks like a UUID
    if _is_uuid(identifier):
        benchmark = client.benchmarks.get_by_id(identifier)
        if benchmark:
            return benchmark

    # Try by key
    benchmark = client.benchmarks.get_by_key(identifier)
    if benchmark:
        return benchmark

    # Try by name
    benchmarks = client.benchmarks.get(name=identifier)
    if benchmarks:
        return benchmarks[0]

    return None
