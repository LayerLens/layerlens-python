from __future__ import annotations

import asyncio
import threading
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Union, Mapping, Optional
from functools import cached_property
from typing_extensions import Self, override

import httpx

from . import _exceptions
from ._env import env_api_key, env_base_url
from ._ssrf import resolve_trusted_upload_hosts
from ._utils import is_mapping
from .models import Organization, OrganizationResponse, OrganizationsListResponse
from ._constants import NOT_GIVEN, DEFAULT_TIMEOUT, DEFAULT_BASE_URL, DIRTY_ROUTER_PREFIX, NotGiven, capped
from ._exceptions import APIStatusError, LayerLensError
from ._base_client import BaseClient, BaseAsyncClient, _request_for_error
from ._deprecation import module_getattr

if TYPE_CHECKING:
    from ._public_client import PublicClient, AsyncPublicClient
    from .resources.judges import Judges, AsyncJudges
    from .resources.models import Models, AsyncModels
    from .resources.traces import Traces, AsyncTraces
    from .resources.results import Results, AsyncResults
    from .resources.scorers import Scorers, AsyncScorers
    from .resources.benchmarks import Benchmarks, AsyncBenchmarks
    from .resources.evaluations import Evaluations, AsyncEvaluations
    from .resources.environments import Environments, AsyncEnvironments
    from .resources.integrations import Integrations, AsyncIntegrations
    from .resources.trace_evaluations import TraceEvaluations, AsyncTraceEvaluations
    from .resources.judge_optimizations import (
        JudgeOptimizations,
        AsyncJudgeOptimizations,
    )


__all__ = ["LayerLens", "AsyncLayerLens", "Client", "AsyncClient"]


class _SharedRead:
    """An organization read under way, and how it ended, for the callers that came while it ran.

    A read that raised an error hands that error to every caller that waited for it. One cut short
    by an interrupt or a cancel hands them nothing, and they read again.
    """

    def __init__(self, done: Union[threading.Event, asyncio.Event]) -> None:
        self.done = done
        self.error: Optional[Exception] = None


class _OrganizationScope:
    """The organization and project the credential belongs to, read from the API on first use.

    The constructor used to read them, so a program that builds its client at import, as the
    instrumentation docs show, did not start when LayerLens was unreachable
    (``APIConnectionError``) or its key was wrong or rotated (``AuthenticationError``) (BUG-03).
    Now the first call that needs them reads them and raises what the API said; the trace
    upload path counts that as a lost trace, so tracing never stops the program. Callers that
    come while a read is under way wait for it and share its outcome, its error included: with
    a read each, one after another, three callers on a stalled API failed at 30, 60 and 90 s. A
    failed read is not remembered: a call after it reads again. Ids set on the client are used as
    they are.
    """

    _organization_id: Optional[str]
    _project_id: Optional[str]
    # Never held across I/O: guards the ids as they are set and the reads under way, so a caller
    # never waits on a lock for another's HTTP call (nor a coroutine, on the event loop).
    _scope_state_lock: threading.Lock
    # The synchronous read under way, if any.
    _scope_read: Optional[_SharedRead]

    def _init_organization_scope(self) -> None:
        self._organization_id = None
        self._project_id = None
        self._scope_state_lock = threading.Lock()
        self._scope_read = None
        # The async client's read under way, and the event loop it runs on.
        self._scope_async_read: Optional[_SharedRead] = None
        self._scope_async_loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_organization(self) -> Optional[Organization]:
        raise NotImplementedError

    def _scope_known(self) -> Optional[Tuple[str, str]]:
        organization_id, project_id = self._organization_id, self._project_id
        if organization_id is not None and project_id is not None:
            return organization_id, project_id
        return None

    def _organization_scope(self) -> Tuple[str, str]:
        while True:
            known = self._scope_known()
            if known is not None:
                return known
            with self._scope_state_lock:
                known = self._scope_known()
                if known is not None:
                    return known
                read = self._scope_read
                reading = read is None
                if read is None:
                    read = self._scope_read = _SharedRead(threading.Event())
            if reading:
                return self._read_organization(read)
            read.done.wait()
            if read.error is not None:
                raise read.error
            # It succeeded, and the ids are known now, or it was cut short: look again.

    def _read_organization(self, read: _SharedRead) -> Tuple[str, str]:
        try:
            organization = self._get_organization()
            return self._adopt_read(organization)
        except Exception as err:
            read.error = err
            raise
        finally:
            with self._scope_state_lock:
                if self._scope_read is read:
                    self._scope_read = None
            read.done.set()

    def _adopt_read(self, organization: Optional[Organization]) -> Tuple[str, str]:
        with self._scope_state_lock:
            if self._organization_id is None or self._project_id is None:
                self._adopt_organization(organization)
            assert self._organization_id is not None and self._project_id is not None
            return self._organization_id, self._project_id

    def _adopt_organization(self, organization: Optional[Organization]) -> None:
        """Fill the ids not set on the client from ``organization``. Caller holds ``_scope_state_lock``."""
        if organization is None:
            raise LayerLensError("Organization could not be fetched. Please contact LayerLens support.")
        if not organization.projects:
            raise LayerLensError(
                f"Organization {organization.id} is missing project. Please contact LayerLens support."
            )
        if self._organization_id is not None and self._organization_id != organization.id:
            # Its first project belongs to another organization: the pair would not exist.
            raise LayerLensError(
                f"organization_id {self._organization_id} is not the organization this credential "
                f"reads ({organization.id}): set project_id as well, or leave both unset"
            )
        if self._organization_id is None:
            self._organization_id = organization.id
        if self._project_id is None:
            self._project_id = organization.projects[0].id

    @property
    def organization_id(self) -> str:
        """The credential's organization; read from the API the first time it is needed."""
        return self._organization_scope()[0]

    @organization_id.setter
    def organization_id(self, value: Optional[str]) -> None:
        self._organization_id = value

    @property
    def project_id(self) -> str:
        """The organization's first project; read from the API the first time it is needed."""
        return self._organization_scope()[1]

    @project_id.setter
    def project_id(self, value: Optional[str]) -> None:
        self._project_id = value


# Each httpx timeout phase of the organization read (connecting, each wait for data, writing,
# waiting for a pooled connection) waits at most this long, for each attempt, as it did when the
# constructor made the read. It is not a limit on the time the read takes.
_ORGANIZATION_READ_SECONDS = 30.0


def _organization_timeout(timeout: httpx.Timeout) -> httpx.Timeout:
    """The client's timeout for the organization read, with each httpx timeout phase capped at
    30 seconds: connecting, each wait for data, writing, waiting for a pooled connection.

    It bounds each phase of each attempt, not the time the read takes: a reply that keeps
    sending data is not cut off, and the client retries a read that fails on a 429, a 5xx or a
    lost connection. A shorter client timeout (``timeout=0.5``) is kept for every phase.
    """
    return capped(timeout, _ORGANIZATION_READ_SECONDS)


def _session_organization(organizations: List[Organization]) -> Optional[Organization]:
    """The organization a ``layerlens login`` session works in, among those its token reads.

    The logged-in user's own organization with projects first, then any with projects.
    """
    if not organizations:
        return None
    from .cli._auth import load_credentials

    creds = load_credentials()
    email = (creds or {}).get("user", {}).get("email") if creds else None
    if email:
        for org in organizations:
            if org.owner_id == email and org.projects:
                return org
    for org in organizations:
        if org.projects:
            return org
    return organizations[0]


class LayerLens(_OrganizationScope, BaseClient):
    api_key: str
    _use_bearer_auth: bool
    _given_base_url: Union[str, httpx.URL]

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | httpx.URL | None = None,
        timeout: Union[float, httpx.Timeout, None] = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        use_bearer_auth: bool = False,
        trusted_upload_hosts: list[str] | None = None,
    ) -> None:
        """Construct a new synchronous LayerLens client instance.

        This automatically infers the following arguments from their corresponding environment variables if they are not provided:
        - `api_key` from `LAYERLENS_API_KEY`
        - `base_url` from `LAYERLENS_BASE_URL`

        ``timeout`` (600 seconds, 5 to connect, by default; ``None`` for none) applies to every
        call, unless the call is given its own ``timeout=``. httpx applies a timeout to each
        phase of a request (connecting, each wait for data, writing), not to the whole of it.
        The organization read on first use caps each phase at 30 seconds, for each attempt, and
        is retried as any call is.

        ``trusted_upload_hosts`` (also read from ``LAYERLENS_TRUSTED_UPLOAD_HOSTS``,
        comma-separated) allowlists hosts the SDK may PUT presigned uploads to in
        addition to the default policy (public https only); list a self-hosted /
        MinIO endpoint here to permit http + private-IP uploads.
        """
        if api_key is None:
            api_key = env_api_key(stacklevel=2)
        if api_key is None or api_key == "":
            raise LayerLensError(
                "The api_key client option must be set either by passing api_key to the client or by setting the LAYERLENS_API_KEY environment variable"
            )
        self.api_key = api_key
        self._use_bearer_auth = use_bearer_auth
        self.trusted_upload_hosts = resolve_trusted_upload_hosts(trusted_upload_hosts)

        if base_url is None:
            base_url = env_base_url(stacklevel=2)
        if base_url is None:
            base_url = DEFAULT_BASE_URL
        # As given, before the session prefix below: what copy() builds its copy from.
        self._given_base_url = base_url

        # Bearer auth (OAuth tokens) routes through the dirty prefix
        if use_bearer_auth:
            base_url = str(base_url).rstrip("/") + DIRTY_ROUTER_PREFIX

        super().__init__(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

        # The organization and project are read on first use, not here (BUG-03).
        self._init_organization_scope()

    @cached_property
    def benchmarks(self) -> Benchmarks:
        from .resources.benchmarks import Benchmarks

        return Benchmarks(self)

    @cached_property
    def evaluations(self) -> Evaluations:
        from .resources.evaluations import Evaluations

        return Evaluations(self)

    @cached_property
    def environments(self) -> Environments:
        from .resources.environments import Environments

        return Environments(self)

    @cached_property
    def judges(self) -> Judges:
        from .resources.judges import Judges

        return Judges(self)

    @cached_property
    def judge_optimizations(self) -> JudgeOptimizations:
        from .resources.judge_optimizations import JudgeOptimizations

        return JudgeOptimizations(self)

    @cached_property
    def models(self) -> Models:
        from .resources.models import Models

        return Models(self)

    @cached_property
    def results(self) -> Results:
        from .resources.results import Results

        return Results(self)

    @cached_property
    def traces(self) -> Traces:
        from .resources.traces import Traces

        return Traces(self)

    @cached_property
    def trace_evaluations(self) -> TraceEvaluations:
        from .resources.trace_evaluations import TraceEvaluations

        return TraceEvaluations(self)

    @cached_property
    def integrations(self) -> Integrations:
        from .resources.integrations import Integrations

        return Integrations(self)

    @cached_property
    def scorers(self) -> Scorers:
        from .resources.scorers import Scorers

        return Scorers(self)

    @cached_property
    def public(self) -> PublicClient:
        from ._public_client import PublicClient

        # Built with this client's options, its retries too (L1R-037).
        return PublicClient(
            api_key=self.api_key, base_url=str(self.base_url), timeout=self.timeout, max_retries=self._max_retries
        )

    @property
    @override
    def auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        if self._use_bearer_auth:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {"x-api-key": self.api_key}

    def copy(
        self,
        *,
        api_key: str | None = None,
        base_url: str | httpx.URL | None = None,
        timeout: Union[float, httpx.Timeout, None, NotGiven] = NOT_GIVEN,
        max_retries: int | None = None,
        use_bearer_auth: bool | None = None,
        trusted_upload_hosts: list[str] | None = None,
        _extra_kwargs: Mapping[str, Any] = {},
    ) -> Self:
        """A new client with this client's options, and the ones given here instead (see :func:`_copy_options`)."""
        options = _copy_options(
            self,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            use_bearer_auth=use_bearer_auth,
            trusted_upload_hosts=trusted_upload_hosts,
        )
        copy = self.__class__(**options, **_extra_kwargs)
        _keep_scope_and_hosts(self, copy, options, hosts_given=trusted_upload_hosts is not None)
        return copy

    # Alias for `copy` for nicer inline usage, e.g.
    # client.with_options(timeout=10).foo.create(...)
    with_options = copy

    @override
    def _make_status_error(
        self,
        err_msg: str,
        *,
        body: object,
        response: httpx.Response,
    ) -> APIStatusError:
        data = body.get("error", body) if is_mapping(body) else body
        if response.status_code == HTTPStatus.BAD_REQUEST:
            return _exceptions.BadRequestError(err_msg, response=response, body=data)

        if response.status_code == HTTPStatus.UNAUTHORIZED:
            return _exceptions.AuthenticationError(err_msg, response=response, body=data)
        if response.status_code == HTTPStatus.PAYMENT_REQUIRED:
            return _exceptions.PaymentRequiredError(err_msg, response=response, body=data)

        if response.status_code == HTTPStatus.FORBIDDEN:
            return _exceptions.PermissionDeniedError(err_msg, response=response, body=data)

        if response.status_code == HTTPStatus.NOT_FOUND:
            return _exceptions.NotFoundError(err_msg, response=response, body=data)

        if response.status_code == HTTPStatus.CONFLICT:
            return _exceptions.ConflictError(err_msg, response=response, body=data)

        if response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
            return _exceptions.UnprocessableEntityError(err_msg, response=response, body=data)

        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            return _exceptions.RateLimitError(err_msg, response=response, body=data)

        if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            return _exceptions.InternalServerError(err_msg, response=response, body=data)

        return APIStatusError(err_msg, response=response, body=data)

    def _get_organization(self) -> Optional[Organization]:
        if self._use_bearer_auth:
            # JWT-authenticated route returns a list of organizations
            resp = super().get_cast(
                "/organizations",
                timeout=_organization_timeout(self.timeout),
                cast_to=OrganizationsListResponse,
            )
            return _session_organization(resp.data) if isinstance(resp, OrganizationsListResponse) else None

        organization = super().get_cast(
            "/organizations",
            timeout=_organization_timeout(self.timeout),
            cast_to=OrganizationResponse,
        )
        return organization.data if isinstance(organization, OrganizationResponse) else None


class AsyncLayerLens(_OrganizationScope, BaseAsyncClient):
    api_key: str
    _use_bearer_auth: bool
    _given_base_url: Union[str, httpx.URL]

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | httpx.URL | None = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        use_bearer_auth: bool = False,
        trusted_upload_hosts: list[str] | None = None,
    ) -> None:
        """Construct a new asynchronous LayerLens client instance.

        This automatically infers the following arguments from their corresponding environment variables if they are not provided:
        - `api_key` from `LAYERLENS_API_KEY`
        - `base_url` from `LAYERLENS_BASE_URL`

        ``timeout`` applies to every call unless the call is given its own, as on :class:`LayerLens`.

        ``trusted_upload_hosts`` (also read from ``LAYERLENS_TRUSTED_UPLOAD_HOSTS``,
        comma-separated) allowlists presigned-upload hosts beyond the default
        policy (public https only); list a self-hosted / MinIO endpoint to permit
        http + private-IP uploads.
        """
        if api_key is None:
            api_key = env_api_key(stacklevel=2)
        if api_key is None or api_key == "":
            raise LayerLensError(
                "The api_key client option must be set either by passing api_key to the client "
                "or by setting the LAYERLENS_API_KEY environment variable"
            )
        self.api_key = api_key
        self._use_bearer_auth = use_bearer_auth
        self.trusted_upload_hosts = resolve_trusted_upload_hosts(trusted_upload_hosts)

        if base_url is None:
            base_url = env_base_url(stacklevel=2)
        if base_url is None:
            base_url = DEFAULT_BASE_URL
        # As given, before the session prefix below: what copy() builds its copy from.
        self._given_base_url = base_url

        if use_bearer_auth:
            base_url = str(base_url).rstrip("/") + DIRTY_ROUTER_PREFIX

        super().__init__(base_url=base_url, timeout=timeout, max_retries=max_retries)

        # The organization and project are read on first use, not here (BUG-03).
        self._init_organization_scope()
        self._sync_twin_lock = threading.Lock()
        self._sync_twin_client: Optional[_SyncTwin] = None

    def _sync_twin(self) -> LayerLens:
        """A synchronous client with this client's key, URL, timeout, retries and upload hosts,
        working in this client's organization and project. Made once, then kept.

        For work that runs outside the caller's event loop, such as the trace upload worker,
        a thread of its own: this client's calls are coroutines, and its connections belong to
        the loop it is used on, which is busy then, or closed once ``asyncio.run`` has returned.
        """
        with self._sync_twin_lock:
            if self._sync_twin_client is None:
                self._sync_twin_client = _SyncTwin(self)
            return self._sync_twin_client

    @cached_property
    def benchmarks(self) -> AsyncBenchmarks:
        from .resources.benchmarks import AsyncBenchmarks

        return AsyncBenchmarks(self)

    @cached_property
    def evaluations(self) -> AsyncEvaluations:
        from .resources.evaluations import AsyncEvaluations

        return AsyncEvaluations(self)

    @cached_property
    def environments(self) -> AsyncEnvironments:
        from .resources.environments import AsyncEnvironments

        return AsyncEnvironments(self)

    @cached_property
    def judges(self) -> AsyncJudges:
        from .resources.judges import AsyncJudges

        return AsyncJudges(self)

    @cached_property
    def judge_optimizations(self) -> AsyncJudgeOptimizations:
        from .resources.judge_optimizations import AsyncJudgeOptimizations

        return AsyncJudgeOptimizations(self)

    @cached_property
    def models(self) -> AsyncModels:
        from .resources.models import AsyncModels

        return AsyncModels(self)

    @cached_property
    def results(self) -> AsyncResults:
        from .resources.results import AsyncResults

        return AsyncResults(self)

    @cached_property
    def traces(self) -> AsyncTraces:
        from .resources.traces import AsyncTraces

        return AsyncTraces(self)

    @cached_property
    def trace_evaluations(self) -> AsyncTraceEvaluations:
        from .resources.trace_evaluations import AsyncTraceEvaluations

        return AsyncTraceEvaluations(self)

    @cached_property
    def integrations(self) -> AsyncIntegrations:
        from .resources.integrations import AsyncIntegrations

        return AsyncIntegrations(self)

    @cached_property
    def scorers(self) -> AsyncScorers:
        from .resources.scorers import AsyncScorers

        return AsyncScorers(self)

    @cached_property
    def public(self) -> AsyncPublicClient:
        from ._public_client import AsyncPublicClient

        # Built with this client's options, its retries too (L1R-037).
        return AsyncPublicClient(
            api_key=self.api_key, base_url=str(self.base_url), timeout=self.timeout, max_retries=self._max_retries
        )

    @property
    @override
    def auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        if self._use_bearer_auth:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {"x-api-key": self.api_key}

    def copy(
        self,
        *,
        api_key: str | None = None,
        base_url: str | httpx.URL | None = None,
        timeout: Union[float, httpx.Timeout, None, NotGiven] = NOT_GIVEN,
        max_retries: int | None = None,
        use_bearer_auth: bool | None = None,
        trusted_upload_hosts: list[str] | None = None,
        _extra_kwargs: Mapping[str, Any] = {},
    ) -> Self:
        """A new client with this client's options, and the ones given here instead (see :func:`_copy_options`)."""
        options = _copy_options(
            self,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            use_bearer_auth=use_bearer_auth,
            trusted_upload_hosts=trusted_upload_hosts,
        )
        copy = self.__class__(**options, **_extra_kwargs)
        _keep_scope_and_hosts(self, copy, options, hosts_given=trusted_upload_hosts is not None)
        return copy

    # Alias for nicer inline usage
    with_options = copy

    def _make_status_error(
        self,
        err_msg: str,
        *,
        body: object,
        response: httpx.Response,
    ) -> APIStatusError:
        data = body.get("error", body) if is_mapping(body) else body

        if response.status_code == HTTPStatus.BAD_REQUEST:
            return _exceptions.BadRequestError(err_msg, response=response, body=data)
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            return _exceptions.AuthenticationError(err_msg, response=response, body=data)
        if response.status_code == HTTPStatus.PAYMENT_REQUIRED:
            return _exceptions.PaymentRequiredError(err_msg, response=response, body=data)
        if response.status_code == HTTPStatus.FORBIDDEN:
            return _exceptions.PermissionDeniedError(err_msg, response=response, body=data)
        if response.status_code == HTTPStatus.NOT_FOUND:
            return _exceptions.NotFoundError(err_msg, response=response, body=data)
        if response.status_code == HTTPStatus.CONFLICT:
            return _exceptions.ConflictError(err_msg, response=response, body=data)
        if response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
            return _exceptions.UnprocessableEntityError(err_msg, response=response, body=data)
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            return _exceptions.RateLimitError(err_msg, response=response, body=data)
        if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            return _exceptions.InternalServerError(err_msg, response=response, body=data)

        return APIStatusError(err_msg, response=response, body=data)

    def _get_organization(self) -> Optional[Organization]:
        url = f"{self.base_url}organizations"

        # Raised as the sync client raises them, so a call that needs the organization fails the
        # same way on either client: an SDK error, not httpx's.
        try:
            with httpx.Client(timeout=_organization_timeout(self.timeout)) as http:
                response = http.get(url, headers=self.default_headers)
        except httpx.TimeoutException as err:
            raise _exceptions.APITimeoutError(request=_request_for_error(err, "GET", url)) from err
        except httpx.RequestError as err:
            raise _exceptions.APIConnectionError(request=_request_for_error(err, "GET", url)) from err
        if response.status_code >= 400:
            raise self._make_status_error_from_response(response)

        data = response.json()

        if self._use_bearer_auth:
            return _session_organization(OrganizationsListResponse(**data).data)

        organization = OrganizationResponse(**data)
        return organization.data if isinstance(organization, OrganizationResponse) else None

    async def _organization_scope_async(self) -> Tuple[str, str]:
        """The ids, read without blocking the event loop the first time they are needed.

        Every coroutine of an async resource awaits this before it builds a URL (see
        :class:`~layerlens._resource.AsyncAPIResource`), so the synchronous properties then read
        ids already known. The read goes through this client, with its retries and errors. The
        tasks of an event loop that come while it is under way wait for it and share its outcome,
        its error included; a failed read is not remembered.
        """
        loop = asyncio.get_running_loop()
        while True:
            known = self._scope_known()
            if known is not None:
                return known
            with self._scope_state_lock:
                known = self._scope_known()
                if known is not None:
                    return known
                # An asyncio.Event serves only the loop it was made on: a read of another loop is
                # not waited for.
                read = self._scope_async_read if self._scope_async_loop is loop else None
                reading = read is None
                if read is None:
                    read = _SharedRead(asyncio.Event())
                    self._scope_async_loop, self._scope_async_read = loop, read
            if reading:
                return await self._read_organization_async(read)
            done = read.done
            assert isinstance(done, asyncio.Event)
            await done.wait()
            if read.error is not None:
                raise read.error
            # It succeeded, and the ids are known now, or it was cancelled: look again.

    async def _read_organization_async(self, read: _SharedRead) -> Tuple[str, str]:
        try:
            organization = await self._get_organization_async()
            return self._adopt_read(organization)
        except Exception as err:
            read.error = err
            raise
        finally:
            with self._scope_state_lock:
                if self._scope_async_read is read:
                    self._scope_async_read = None
            read.done.set()

    async def _get_organization_async(self) -> Optional[Organization]:
        if self._use_bearer_auth:
            resp = await self.get_cast(
                "/organizations", timeout=_organization_timeout(self.timeout), cast_to=OrganizationsListResponse
            )
            return _session_organization(resp.data) if isinstance(resp, OrganizationsListResponse) else None
        organization = await self.get_cast(
            "/organizations", timeout=_organization_timeout(self.timeout), cast_to=OrganizationResponse
        )
        return organization.data if isinstance(organization, OrganizationResponse) else None


class _SyncTwin(LayerLens):
    """The synchronous client of an :class:`AsyncLayerLens` (see ``AsyncLayerLens._sync_twin``).

    Its organization and project are the async client's, read through it, so the two never
    work in different projects, and ids set on the async client later are followed.
    """

    def __init__(self, owner: AsyncLayerLens) -> None:
        super().__init__(
            api_key=owner.api_key,
            base_url=owner.base_url,
            timeout=owner.timeout,
            max_retries=owner._max_retries,
            trusted_upload_hosts=list(owner.trusted_upload_hosts),
        )
        # Set after: the owner's base_url already carries the session prefix bearer auth adds.
        self._use_bearer_auth = owner._use_bearer_auth
        self.trusted_upload_hosts = list(owner.trusted_upload_hosts)
        self._owner = owner

    @override
    def _organization_scope(self) -> Tuple[str, str]:
        return self._owner._organization_scope()


def _copy_options(
    client: Union["LayerLens", "AsyncLayerLens"],
    *,
    api_key: Optional[str],
    base_url: Union[str, httpx.URL, None],
    timeout: Union[float, httpx.Timeout, None, NotGiven],
    max_retries: Optional[int],
    use_bearer_auth: Optional[bool],
    trusted_upload_hosts: Optional[List[str]],
) -> Dict[str, Any]:
    """The constructor options of a copy of ``client``: its own, and the ones given instead.

    Every option the client was built with is kept: its key, its URL as given (a bearer-auth
    client's gets the session prefix once), its timeout, retries and bearer auth, and (in
    :func:`_keep_scope_and_hosts`) its upload hosts as they were resolved then, since the
    environment is not read again, and the organization and project it works in, unless the key,
    the URL or the auth changes. Before, ``timeout`` never applied (``self.timeout or timeout``:
    an httpx ``Timeout`` is always true), the retries went back to two, the hosts were read again,
    a bearer-auth URL got the prefix twice, and the copy read its organization and project
    afresh, the first project instead of the client's (L1R-035b). ``timeout=None`` is no timeout.
    """
    return {
        "api_key": api_key or client.api_key,
        "base_url": client._given_base_url if base_url is None else base_url,
        "timeout": client.timeout if isinstance(timeout, NotGiven) else timeout,
        "max_retries": client._max_retries if max_retries is None else max_retries,
        "use_bearer_auth": client._use_bearer_auth if use_bearer_auth is None else use_bearer_auth,
        "trusted_upload_hosts": trusted_upload_hosts,
    }


def _keep_scope_and_hosts(
    client: Union["LayerLens", "AsyncLayerLens"],
    copy: Union["LayerLens", "AsyncLayerLens"],
    options: Dict[str, Any],
    *,
    hosts_given: bool,
) -> None:
    """Give ``copy`` the upload hosts ``client`` resolved, unless others were given, and the
    organization and project it works in when the copy has the same credential and URL."""
    if not hosts_given:
        copy.trusted_upload_hosts = list(client.trusted_upload_hosts)
    same_credential = (
        options["api_key"] == client.api_key
        and options["base_url"] == client._given_base_url
        and options["use_bearer_auth"] == client._use_bearer_auth
    )
    if same_credential:
        copy._organization_id, copy._project_id = client._organization_id, client._project_id


Client = LayerLens
AsyncClient = AsyncLayerLens

# Deprecated names (LAY-4220): the same classes as ``LayerLens`` / ``AsyncLayerLens``.
if TYPE_CHECKING:
    Stratix = LayerLens
    AsyncStratix = AsyncLayerLens
    Atlas = LayerLens
    AsyncAtlas = AsyncLayerLens
else:
    __getattr__ = module_getattr(
        __name__,
        {
            "Stratix": ("LayerLens", "layerlens.LayerLens"),
            "AsyncStratix": ("AsyncLayerLens", "layerlens.AsyncLayerLens"),
            "Atlas": ("LayerLens", "layerlens.LayerLens"),
            "AsyncAtlas": ("AsyncLayerLens", "layerlens.AsyncLayerLens"),
        },
        globals(),
    )
