"""Dataset CRUD store with version snapshots and tag filtering."""

from __future__ import annotations

import uuid
from typing import Any, Set, Dict, List, Iterable, Optional, Protocol, Sequence
from datetime import datetime, timezone

from .models import Dataset, DatasetItem, DatasetVersion, DatasetVisibility


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DatasetStore(Protocol):
    def create(self, dataset: Dataset) -> Dataset: ...
    def get(self, dataset_id: str) -> Optional[Dataset]: ...
    def list(
        self,
        *,
        tag: Optional[str] = None,
        organization_id: Optional[str] = None,
        project_id: Optional[str] = None,
        visibility: Optional[DatasetVisibility] = None,
    ) -> List[Dataset]: ...
    def delete(self, dataset_id: str) -> bool: ...
    def publish_version(
        self,
        dataset_id: str,
        items: Sequence[DatasetItem],
        *,
        note: Optional[str] = None,
    ) -> Optional[DatasetVersion]: ...
    def iter_items(
        self,
        dataset_id: str,
        *,
        version: Optional[int] = None,
        tag: Optional[str] = None,
    ) -> Iterable[DatasetItem]: ...


class InMemoryDatasetStore:
    """Default implementation — swap for a DB-backed store in production."""

    def __init__(self) -> None:
        self._datasets: Dict[str, Dataset] = {}

    def create(self, dataset: Dataset) -> Dataset:
        if not dataset.id:
            dataset.id = f"ds_{uuid.uuid4().hex[:16]}"
        if dataset.id in self._datasets:
            raise ValueError(f"dataset {dataset.id} already exists")
        if not dataset.versions:
            dataset.versions = [DatasetVersion(version=1, note="initial")]
        latest = dataset.latest()
        dataset.current_version = latest.version if latest else 1
        self._datasets[dataset.id] = dataset
        return dataset

    def get(self, dataset_id: str) -> Optional[Dataset]:
        return self._datasets.get(dataset_id)

    def list(
        self,
        *,
        tag: Optional[str] = None,
        organization_id: Optional[str] = None,
        project_id: Optional[str] = None,
        visibility: Optional[DatasetVisibility] = None,
    ) -> List[Dataset]:
        out: List[Dataset] = []
        for d in self._datasets.values():
            if tag and tag not in d.tags:
                continue
            if organization_id and d.organization_id != organization_id:
                continue
            if project_id and d.project_id != project_id:
                continue
            if visibility and d.visibility != visibility:
                continue
            out.append(d)
        return sorted(out, key=lambda d: d.updated_at, reverse=True)

    def update_metadata(
        self,
        dataset_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        visibility: Optional[DatasetVisibility] = None,
    ) -> Optional[Dataset]:
        ds = self._datasets.get(dataset_id)
        if ds is None:
            return None
        if name is not None:
            ds.name = name
        if description is not None:
            ds.description = description
        if tags is not None:
            ds.tags = list(tags)
        if visibility is not None:
            ds.visibility = visibility
        ds.updated_at = _now()
        return ds

    def delete(self, dataset_id: str) -> bool:
        return self._datasets.pop(dataset_id, None) is not None

    def publish_version(
        self,
        dataset_id: str,
        items: Sequence[DatasetItem],
        *,
        note: Optional[str] = None,
    ) -> Optional[DatasetVersion]:
        ds = self._datasets.get(dataset_id)
        if ds is None:
            return None
        latest = ds.latest()
        next_version = (latest.version if latest else 0) + 1
        version = DatasetVersion(
            version=next_version,
            note=note,
            items=list(items),
        )
        ds.versions.append(version)
        ds.current_version = version.version
        ds.updated_at = version.created_at
        return version

    def iter_items(
        self,
        dataset_id: str,
        *,
        version: Optional[int] = None,
        tag: Optional[str] = None,
    ) -> Iterable[DatasetItem]:
        ds = self._datasets.get(dataset_id)
        if ds is None:
            return []
        v = ds.version(version) if version is not None else ds.latest()
        if v is None:
            return []
        if tag is None:
            return list(v.items)
        return [i for i in v.items if tag in i.tags]

    def import_items(
        self,
        dataset_id: str,
        raw_items: Iterable[Dict[str, Any]],
        *,
        note: Optional[str] = None,
    ) -> Optional[DatasetVersion]:
        """Convenience: publish a new version from raw dicts.

        An item without an ``id`` is ``item_{idx}``, its place in the list, so every import of the
        same items gives it the same id: ``evaluations compare`` matches two runs' items by id. It
        used to carry a random suffix, new on every run, and compare never named an item. A place
        whose ``item_{idx}`` another item has as its own id takes ``item_{idx}_2`` (``_3``, ...).
        """
        raws = list(raw_items)
        given = {str(raw.get("id")) for raw in raws if raw.get("id")}
        coerced: List[DatasetItem] = []
        for idx, raw in enumerate(raws):
            coerced.append(
                DatasetItem(
                    id=str(raw.get("id") or _id_for_place(idx, given)),
                    input=raw.get("input"),
                    expected_output=raw.get("expected_output"),
                    metadata=dict(raw.get("metadata") or {}),
                    tags=list(raw.get("tags") or []),
                )
            )
        return self.publish_version(dataset_id, coerced, note=note)


def _id_for_place(idx: int, given: Set[str]) -> str:
    """``item_{idx}``, or the first ``item_{idx}_{n}`` (n from 2) that no item has as its own id."""
    candidate = f"item_{idx}"
    n = 2
    while candidate in given:
        candidate = f"item_{idx}_{n}"
        n += 1
    return candidate
