from __future__ import annotations

import os
import math
import mimetypes
from typing import Any, Dict, List, Literal, Optional

import httpx

from ..._ssrf import ensure_safe_upload_url
from ..._pages import every_page, page_params, every_page_async
from ...models import (
    Benchmark,
    BenchmarkPrompt,
    CustomBenchmark,
    PublicBenchmark,
    BenchmarksResponse,
    BenchmarkPromptsData,
    CreateBenchmarkResponse,
)
from ..._resource import SyncAPIResource, AsyncAPIResource
from ..._constants import NOT_GIVEN, NotGiven, timeout_or
from ..._presigned import put_presigned, put_presigned_async

DEFAULT_PROMPTS_PAGE_SIZE = 100

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB


def _get_content_type(filename: str) -> str:
    ct, _ = mimetypes.guess_type(filename)
    if ct:
        return ct
    ext = os.path.splitext(filename)[1].lower()
    return {
        ".jsonl": "application/jsonl",
        ".json": "application/json",
        ".csv": "text/csv",
        ".parquet": "application/x-parquet",
    }.get(ext, "application/octet-stream")


def _patched(resp: Any) -> bool:
    """Whether the project PATCH succeeded: the API answers ``{"status": "success", "data": {"id": ...}}``.

    Read as the models' PATCH reads it. Looking for ``id`` at the top level made a PATCH that
    succeeded read as a failure (BUG-11).
    """
    data = resp.get("data", resp) if isinstance(resp, dict) else None
    return isinstance(data, dict) and "id" in data


class Benchmarks(SyncAPIResource):
    def get(
        self,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
        type: Literal["custom", "public"] | None = None,
        name: Optional[str] = None,
        key: Optional[str] = None,
        categories: Optional[List[str]] = None,
        languages: Optional[List[str]] = None,
    ) -> Optional[List[Benchmark]]:
        """Every benchmark of the project that matches the filters, custom and public unless ``type`` says one.

        Returns every matching row: the SDK reads the API's pages for you.
        """

        def fetch(bench_type: str) -> List[Benchmark]:
            params = {"type": bench_type}
            if name:
                params["name"] = name
            if key:
                params["key"] = key
            if categories:
                params["categories"] = ",".join(categories)
            if languages:
                params["languages"] = ",".join(languages)
            return self._every_row(params, timeout)

        benchmarks: List[Benchmark] = []

        def cast_benchmark(b: Benchmark, bench_type: str) -> Benchmark:
            if bench_type == "custom":
                return CustomBenchmark(**b.model_dump())
            elif bench_type == "public":
                return PublicBenchmark(**b.model_dump())
            return b  # fallback, just base class

        if type is None:
            for t in ["custom", "public"]:
                benchmarks.extend([cast_benchmark(b, t) for b in fetch(t)])
        else:  # fetch only one type
            benchmarks.extend([cast_benchmark(b, type) for b in fetch(type)])

        # Exclude custom benchmarks when filtering by fields they don't have
        if categories:
            cat_set = {c.lower() for c in categories}
            benchmarks = [
                b
                for b in benchmarks
                if isinstance(b, PublicBenchmark) and b.categories and any(c.lower() in cat_set for c in b.categories)
            ]

        if languages:
            lang_set = {l.lower() for l in languages}
            benchmarks = [
                b
                for b in benchmarks
                if isinstance(b, PublicBenchmark) and b.language and b.language.lower() in lang_set
            ]

        return benchmarks

    def get_by_id(
        self, id: str, *, timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN
    ) -> Optional[Benchmark]:
        """A benchmark, by id: one of the catalog or a custom one of the project.

        Raises :class:`NotFoundError` when no benchmark has this id. Returns None only for a reply that
        holds no benchmark.
        """
        base_url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/benchmarks/{id}"

        resp = self._get(
            base_url,
            timeout=timeout,
            cast_to=dict,
        )

        if not isinstance(resp, dict):
            return None

        benchmark = resp.get("data")
        if not isinstance(benchmark, dict):
            return None

        # Detect type dynamically: presence of "organization_id" means custom
        if "organization_id" in benchmark:
            return CustomBenchmark(**benchmark)
        else:
            return PublicBenchmark(**benchmark)

    def get_by_key(
        self,
        key: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Benchmark]:
        """Fetch a single benchmark of the project by its unique key; None when none has this key."""
        benchmarks = self.get(timeout=timeout, key=key)

        if not benchmarks:
            return None

        for benchmark in benchmarks:
            if benchmark.key == key:
                return benchmark
        return None

    def add(
        self,
        *benchmark_ids: str,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Add public benchmarks to the project by their IDs.

        The project's ``datasets`` lists its public benchmarks only: a custom benchmark belongs
        to the project already, and its id there made the API answer 500 (BUG-11). The PATCH replaces
        that whole list, so it is read whole first, every page and the deprecated benchmarks too: no
        benchmark the project holds is dropped.
        """
        current_ids = self._public_ids(timeout)
        new_ids = list(dict.fromkeys(current_ids + list(benchmark_ids)))
        return self._patch_project_benchmarks(new_ids, timeout)

    def remove(
        self,
        *benchmark_ids: str,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Remove public benchmarks from the project by their IDs (see :meth:`add`)."""
        remove_set = set(benchmark_ids)
        new_ids = [benchmark_id for benchmark_id in self._public_ids(timeout) if benchmark_id not in remove_set]
        return self._patch_project_benchmarks(new_ids, timeout)

    def get_prompts(
        self,
        benchmark_id: str,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        search_field: Optional[Literal["id", "input", "truth"]] = None,
        search_value: Optional[str] = None,
        sort_by: Optional[Literal["id", "input", "truth"]] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[BenchmarkPromptsData]:
        """Fetch a page of prompts for a benchmark.

        Uses the org-scoped endpoint:
        GET /organizations/{org}/projects/{proj}/benchmarks/{id}/prompts

        Args:
            benchmark_id: The benchmark / dataset ID.
            page: Page number (1-based).
            page_size: Number of prompts per page.
            search_field: Field to search in.
            search_value: Value to search for.
            sort_by: Field to sort by.
            sort_order: Sort direction.
            timeout: Request timeout override.

        Returns:
            BenchmarkPromptsData with prompts list and count, or None on failure.
        """
        params: Dict[str, str] = {}
        if page is not None:
            params["page"] = str(page)
        if page_size is not None:
            params["page_size"] = str(page_size)
        if search_field:
            params["search"] = search_field
        if search_value:
            params["search_value"] = search_value
        if sort_by:
            params["sort_by"] = sort_by
        if sort_order:
            params["sort_order"] = sort_order

        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/benchmarks/{benchmark_id}/prompts"
        resp = self._get(
            url,
            params=params,
            timeout=timeout,
            cast_to=dict,
        )

        if not isinstance(resp, dict):
            return None

        # Unwrap {"status": ..., "data": {...}} envelope if present
        if "data" in resp and "status" in resp:
            resp = resp["data"]

        return BenchmarkPromptsData.model_validate(resp)

    def get_all_prompts(
        self,
        benchmark_id: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> List[BenchmarkPrompt]:
        """Fetch all prompts for a benchmark, automatically paginating."""
        all_prompts: List[BenchmarkPrompt] = []
        page = 1
        page_size = DEFAULT_PROMPTS_PAGE_SIZE

        while True:
            resp = self.get_prompts(
                benchmark_id,
                page=page,
                page_size=page_size,
                timeout=timeout,
            )
            if resp is None or not resp.prompts:
                break

            all_prompts.extend(resp.prompts)

            total_count = resp.count
            total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0
            if page >= total_pages:
                break

            page += 1

        return all_prompts

    def _every_row(
        self,
        params: Dict[str, str],
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> List[Benchmark]:
        """Every row the project's benchmarks list answers for ``params``, read a page at a time."""
        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/benchmarks"

        def page_of(page: int) -> Optional[List[Benchmark]]:
            resp = self._get(
                url,
                params={**params, **page_params(page)},
                timeout=timeout,
                cast_to=BenchmarksResponse,
            )
            return resp.data.benchmarks if isinstance(resp, BenchmarksResponse) else None

        return every_page(page_of)

    def _public_ids(self, timeout: float | httpx.Timeout | None | NotGiven) -> List[str]:
        """The ids of every public benchmark the project holds, deprecated ones too.

        The PATCH replaces the project's whole list, so ``add()`` and ``remove()`` send back every row
        it holds: the list hides a deprecated benchmark unless asked for it, and it answers 20 rows a
        page unless asked for more.
        """
        rows = self._every_row({"type": "public", "include_deprecated": "true"}, timeout)
        return [str(b.id) for b in rows]

    def _patch_project_benchmarks(
        self,
        dataset_ids: List[str],
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> bool:
        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        resp = self._patch(
            url,
            body={"datasets": dataset_ids},
            timeout=timeout,
            cast_to=dict,
        )
        return _patched(resp)

    def _upload_file(
        self,
        file_path: str,
        benchmark_name: str,
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> str:
        """Upload a file and return the filename for use in benchmark creation."""
        file_path = os.path.abspath(file_path)
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        if file_size > MAX_UPLOAD_SIZE:
            raise ValueError(f"File size {file_size} exceeds maximum of {MAX_UPLOAD_SIZE} bytes (50 MB)")

        content_type = _get_content_type(filename)
        base = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"

        raw_resp = self._post(
            f"{base}/upload",
            body={
                "key": benchmark_name,
                "filename": filename,
                "type": content_type,
                "size": file_size,
            },
            timeout=timeout,
            cast_to=dict,
        )
        # Unwrap {"status": ..., "data": {...}} envelope if present
        resp = raw_resp
        if isinstance(resp, dict) and "data" in resp and "status" in resp:
            resp = resp["data"]
        if not isinstance(resp, dict) or "url" not in resp:
            raise ValueError("Failed to get upload URL")

        # SSRF guard: never PUT benchmark bytes to an untrusted/internal host.
        ensure_safe_upload_url(resp["url"], getattr(self._client, "trusted_upload_hosts", None))

        with open(file_path, "rb") as f:
            content = f.read()
        put_presigned(
            resp["url"],
            content,
            content_type=content_type,
            timeout=timeout_or(timeout, self._client.timeout),
            client=self._client,
            what="benchmark file",
        )

        return filename

    def create_custom(
        self,
        *,
        name: str,
        description: str,
        file_path: str,
        additional_metrics: Optional[List[str]] = None,
        custom_scorer_ids: Optional[List[str]] = None,
        input_type: Optional[str] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[CreateBenchmarkResponse]:
        """Create a custom benchmark by uploading a JSONL file.

        Args:
            name: Benchmark name (max 64 characters).
            description: Benchmark description (max 280 characters).
            file_path: Path to a JSONL file with benchmark prompts.
            additional_metrics: Optional metrics: "readability", "toxicity", "hallucination".
            custom_scorer_ids: Optional list of custom scorer IDs.
            input_type: Optional input type: "messages" or "json_payload".
            timeout: Request timeout override.

        Returns:
            CreateBenchmarkResponse with benchmark_id, or None on failure.
        """
        filename = self._upload_file(file_path, name, timeout)

        base = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "file": filename,
        }
        if additional_metrics:
            body["additional_metrics"] = additional_metrics
        if custom_scorer_ids:
            body["custom_scorers"] = custom_scorer_ids
        if input_type:
            body["input_type"] = input_type

        resp = self._post(
            f"{base}/custom-benchmarks",
            body=body,
            timeout=timeout,
            cast_to=dict,
        )
        if isinstance(resp, dict) and "data" in resp and "status" in resp:
            resp = resp["data"]
        if isinstance(resp, dict) and "benchmark_id" in resp:
            return CreateBenchmarkResponse(**resp)
        return None

    def create_smart(
        self,
        *,
        name: str,
        description: str,
        system_prompt: str,
        file_paths: List[str],
        metrics: Optional[List[str]] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[CreateBenchmarkResponse]:
        """Create a smart benchmark from uploaded files.

        The platform will use AI to generate benchmark prompts from the provided files.

        Args:
            name: Benchmark name (max 256 characters).
            description: Benchmark description (max 500 characters).
            system_prompt: System prompt for benchmark generation (max 4000 characters).
            file_paths: List of file paths to upload (1-20 files).
            metrics: Optional metrics: "readability", "toxicity", "hallucination".
            timeout: Request timeout override.

        Returns:
            CreateBenchmarkResponse with benchmark_id, or None on failure.
        """
        filenames = []
        for fp in file_paths:
            filename = self._upload_file(fp, name, timeout)
            filenames.append(filename)

        base = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "files": filenames,
        }
        if metrics:
            body["metrics"] = metrics

        resp = self._post(
            f"{base}/smart-benchmarks",
            body=body,
            timeout=timeout,
            cast_to=dict,
        )
        if isinstance(resp, dict) and "data" in resp and "status" in resp:
            resp = resp["data"]
        if isinstance(resp, dict) and "benchmark_id" in resp:
            return CreateBenchmarkResponse(**resp)
        return None


class AsyncBenchmarks(AsyncAPIResource):
    async def get(
        self,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
        type: Literal["custom", "public"] | None = None,
        name: Optional[str] = None,
        key: Optional[str] = None,
        categories: Optional[List[str]] = None,
        languages: Optional[List[str]] = None,
    ) -> Optional[List[Benchmark]]:
        """Every benchmark of the project that matches the filters, custom and public unless ``type`` says one.

        Returns every matching row: the SDK reads the API's pages for you.
        """

        async def fetch(bench_type: str) -> List[Benchmark]:
            params = {"type": bench_type}
            if name:
                params["name"] = name
            if key:
                params["key"] = key
            if categories:
                params["categories"] = ",".join(categories)
            if languages:
                params["languages"] = ",".join(languages)
            return await self._every_row(params, timeout)

        def cast_benchmark(b: Benchmark, bench_type: str) -> Benchmark:
            if bench_type == "custom":
                return CustomBenchmark(**b.model_dump())
            elif bench_type == "public":
                return PublicBenchmark(**b.model_dump())
            return b  # fallback to base class

        benchmarks: List[Benchmark] = []

        if type is None:  # fetch both custom + public
            for t in ["custom", "public"]:
                benchmarks.extend([cast_benchmark(b, t) for b in await fetch(t)])
        else:  # fetch only one type
            benchmarks.extend([cast_benchmark(b, type) for b in await fetch(type)])

        # Exclude custom benchmarks when filtering by fields they don't have
        if categories:
            cat_set = {c.lower() for c in categories}
            benchmarks = [
                b
                for b in benchmarks
                if isinstance(b, PublicBenchmark) and b.categories and any(c.lower() in cat_set for c in b.categories)
            ]

        if languages:
            lang_set = {l.lower() for l in languages}
            benchmarks = [
                b
                for b in benchmarks
                if isinstance(b, PublicBenchmark) and b.language and b.language.lower() in lang_set
            ]

        return benchmarks

    async def get_by_id(
        self, id: str, *, timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN
    ) -> Optional[Benchmark]:
        """A benchmark, by id: one of the catalog or a custom one of the project.

        Raises :class:`NotFoundError` when no benchmark has this id. Returns None only for a reply that
        holds no benchmark.
        """
        base_url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/benchmarks/{id}"

        resp = await self._get(
            base_url,
            timeout=timeout,
            cast_to=dict,
        )

        if not isinstance(resp, dict):
            return None

        benchmark = resp.get("data")
        if not isinstance(benchmark, dict):
            return None

        # Detect type dynamically: presence of "organization_id" means custom
        if "organization_id" in benchmark:
            return CustomBenchmark(**benchmark)
        else:
            return PublicBenchmark(**benchmark)

    async def get_by_key(
        self,
        key: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Benchmark]:
        """Fetch a single benchmark of the project by its unique key; None when none has this key."""
        benchmarks = await self.get(timeout=timeout, key=key)

        if not benchmarks:
            return None

        for benchmark in benchmarks:
            if benchmark.key == key:
                return benchmark
        return None

    async def add(
        self,
        *benchmark_ids: str,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Add public benchmarks to the project by their IDs (see :meth:`Benchmarks.add`)."""
        current_ids = await self._public_ids(timeout)
        new_ids = list(dict.fromkeys(current_ids + list(benchmark_ids)))
        return await self._patch_project_benchmarks(new_ids, timeout)

    async def remove(
        self,
        *benchmark_ids: str,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Remove public benchmarks from the project by their IDs (see :meth:`Benchmarks.add`)."""
        remove_set = set(benchmark_ids)
        new_ids = [benchmark_id for benchmark_id in await self._public_ids(timeout) if benchmark_id not in remove_set]
        return await self._patch_project_benchmarks(new_ids, timeout)

    async def get_prompts(
        self,
        benchmark_id: str,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        search_field: Optional[Literal["id", "input", "truth"]] = None,
        search_value: Optional[str] = None,
        sort_by: Optional[Literal["id", "input", "truth"]] = None,
        sort_order: Optional[Literal["asc", "desc"]] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[BenchmarkPromptsData]:
        """Fetch a page of prompts for a benchmark."""
        params: Dict[str, str] = {}
        if page is not None:
            params["page"] = str(page)
        if page_size is not None:
            params["page_size"] = str(page_size)
        if search_field:
            params["search"] = search_field
        if search_value:
            params["search_value"] = search_value
        if sort_by:
            params["sort_by"] = sort_by
        if sort_order:
            params["sort_order"] = sort_order

        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/benchmarks/{benchmark_id}/prompts"
        resp = await self._get(
            url,
            params=params,
            timeout=timeout,
            cast_to=dict,
        )

        if not isinstance(resp, dict):
            return None

        # Unwrap {"status": ..., "data": {...}} envelope if present
        if "data" in resp and "status" in resp:
            resp = resp["data"]

        return BenchmarkPromptsData.model_validate(resp)

    async def get_all_prompts(
        self,
        benchmark_id: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> List[BenchmarkPrompt]:
        """Fetch all prompts for a benchmark, automatically paginating."""
        all_prompts: List[BenchmarkPrompt] = []
        page = 1
        page_size = DEFAULT_PROMPTS_PAGE_SIZE

        while True:
            resp = await self.get_prompts(
                benchmark_id,
                page=page,
                page_size=page_size,
                timeout=timeout,
            )
            if resp is None or not resp.prompts:
                break

            all_prompts.extend(resp.prompts)

            total_count = resp.count
            total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0
            if page >= total_pages:
                break

            page += 1

        return all_prompts

    async def _every_row(
        self,
        params: Dict[str, str],
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> List[Benchmark]:
        """Every row the project's benchmarks list answers for ``params``, read a page at a time."""
        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/benchmarks"

        async def page_of(page: int) -> Optional[List[Benchmark]]:
            resp = await self._get(
                url,
                params={**params, **page_params(page)},
                timeout=timeout,
                cast_to=BenchmarksResponse,
            )
            return resp.data.benchmarks if isinstance(resp, BenchmarksResponse) else None

        return await every_page_async(page_of)

    async def _public_ids(self, timeout: float | httpx.Timeout | None | NotGiven) -> List[str]:
        """The ids of every public benchmark the project holds, deprecated ones too.

        The PATCH replaces the project's whole list, so ``add()`` and ``remove()`` send back every row
        it holds: the list hides a deprecated benchmark unless asked for it, and it answers 20 rows a
        page unless asked for more.
        """
        rows = await self._every_row({"type": "public", "include_deprecated": "true"}, timeout)
        return [str(b.id) for b in rows]

    async def _patch_project_benchmarks(
        self,
        dataset_ids: List[str],
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> bool:
        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        resp = await self._patch(
            url,
            body={"datasets": dataset_ids},
            timeout=timeout,
            cast_to=dict,
        )
        return _patched(resp)

    async def _upload_file(
        self,
        file_path: str,
        benchmark_name: str,
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> str:
        """Upload a file and return the filename for use in benchmark creation."""
        file_path = os.path.abspath(file_path)
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        if file_size > MAX_UPLOAD_SIZE:
            raise ValueError(f"File size {file_size} exceeds maximum of {MAX_UPLOAD_SIZE} bytes (50 MB)")

        content_type = _get_content_type(filename)
        base = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"

        raw_resp = await self._post(
            f"{base}/upload",
            body={
                "key": benchmark_name,
                "filename": filename,
                "type": content_type,
                "size": file_size,
            },
            timeout=timeout,
            cast_to=dict,
        )
        # Unwrap {"status": ..., "data": {...}} envelope if present
        resp = raw_resp
        if isinstance(resp, dict) and "data" in resp and "status" in resp:
            resp = resp["data"]
        if not isinstance(resp, dict) or "url" not in resp:
            raise ValueError("Failed to get upload URL")

        # SSRF guard: never PUT benchmark bytes to an untrusted/internal host.
        ensure_safe_upload_url(resp["url"], getattr(self._client, "trusted_upload_hosts", None))

        with open(file_path, "rb") as f:
            content = f.read()
        await put_presigned_async(
            resp["url"],
            content,
            content_type=content_type,
            timeout=timeout_or(timeout, self._client.timeout),
            client=self._client,
            what="benchmark file",
        )

        return filename

    async def create_custom(
        self,
        *,
        name: str,
        description: str,
        file_path: str,
        additional_metrics: Optional[List[str]] = None,
        custom_scorer_ids: Optional[List[str]] = None,
        input_type: Optional[str] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[CreateBenchmarkResponse]:
        """Create a custom benchmark by uploading a JSONL file.

        Args:
            name: Benchmark name (max 64 characters).
            description: Benchmark description (max 280 characters).
            file_path: Path to a JSONL file with benchmark prompts.
            additional_metrics: Optional metrics: "readability", "toxicity", "hallucination".
            custom_scorer_ids: Optional list of custom scorer IDs.
            input_type: Optional input type: "messages" or "json_payload".
            timeout: Request timeout override.

        Returns:
            CreateBenchmarkResponse with benchmark_id, or None on failure.
        """
        filename = await self._upload_file(file_path, name, timeout)

        base = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "file": filename,
        }
        if additional_metrics:
            body["additional_metrics"] = additional_metrics
        if custom_scorer_ids:
            body["custom_scorers"] = custom_scorer_ids
        if input_type:
            body["input_type"] = input_type

        resp = await self._post(
            f"{base}/custom-benchmarks",
            body=body,
            timeout=timeout,
            cast_to=dict,
        )
        if isinstance(resp, dict) and "data" in resp and "status" in resp:
            resp = resp["data"]
        if isinstance(resp, dict) and "benchmark_id" in resp:
            return CreateBenchmarkResponse(**resp)
        return None

    async def create_smart(
        self,
        *,
        name: str,
        description: str,
        system_prompt: str,
        file_paths: List[str],
        metrics: Optional[List[str]] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[CreateBenchmarkResponse]:
        """Create a smart benchmark from uploaded files.

        The platform will use AI to generate benchmark prompts from the provided files.

        Args:
            name: Benchmark name (max 256 characters).
            description: Benchmark description (max 500 characters).
            system_prompt: System prompt for benchmark generation (max 4000 characters).
            file_paths: List of file paths to upload (1-20 files).
            metrics: Optional metrics: "readability", "toxicity", "hallucination".
            timeout: Request timeout override.

        Returns:
            CreateBenchmarkResponse with benchmark_id, or None on failure.
        """
        filenames = []
        for fp in file_paths:
            filename = await self._upload_file(fp, name, timeout)
            filenames.append(filename)

        base = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "files": filenames,
        }
        if metrics:
            body["metrics"] = metrics

        resp = await self._post(
            f"{base}/smart-benchmarks",
            body=body,
            timeout=timeout,
            cast_to=dict,
        )
        if isinstance(resp, dict) and "data" in resp and "status" in resp:
            resp = resp["data"]
        if isinstance(resp, dict) and "benchmark_id" in resp:
            return CreateBenchmarkResponse(**resp)
        return None
