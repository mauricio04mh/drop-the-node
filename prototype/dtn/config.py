"""Explicit configuration of the storage layer (principle P6).

Every tunable value comes from a ``DTN_*`` environment variable with a
documented default. Components receive a ``StorageConfig`` instead of reading
the environment themselves, and log ``as_dict()`` when they start, so every
run records the exact values it used.
"""

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorageConfig:
    num_stores: int = 5
    replicas: int = 3
    data_dir: Path = Path("./data")
    store_concurrency: int = 8
    hedge_delay_ms: int | None = None
    timeout_min_ms: int = 200
    timeout_max_ms: int = 2000
    min_write_mbps: float = 10.0
    repair_interval_seconds: float = 30.0
    store_lost_seconds: float = 60.0
    scrub_interval_seconds: float = 3600.0
    gc_grace_seconds: float = 86400.0

    def __post_init__(self):
        if self.num_stores < 1:
            raise ValueError("DTN_NUM_STORES must be at least 1")
        if not 1 <= self.replicas <= self.num_stores:
            raise ValueError("DTN_REPLICAS must be between 1 and DTN_NUM_STORES")
        if self.store_concurrency < 1:
            raise ValueError("DTN_STORE_CONCURRENCY must be at least 1")
        if self.hedge_delay_ms is not None and self.hedge_delay_ms < 0:
            raise ValueError("DTN_HEDGE_DELAY_MS must not be negative")
        if not 0 < self.timeout_min_ms <= self.timeout_max_ms:
            raise ValueError("need 0 < DTN_TIMEOUT_MIN_MS <= DTN_TIMEOUT_MAX_MS")
        if self.min_write_mbps <= 0:
            raise ValueError("DTN_MIN_WRITE_MBPS must be positive")
        for name in (
            "repair_interval_seconds",
            "store_lost_seconds",
            "scrub_interval_seconds",
            "gc_grace_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"DTN_{name.upper()} must be positive")

    @property
    def store_names(self) -> tuple[str, ...]:
        """Logical names of the stores (principle P1): stable across restarts."""
        return tuple(f"store-{i}" for i in range(1, self.num_stores + 1))

    def as_dict(self) -> dict:
        values = asdict(self)
        values["data_dir"] = str(self.data_dir)
        return values

    def cluster_params(self) -> dict:
        """The values every process of one system must share: S fixes where
        each object lives, N how many copies it must have. The rest may differ
        per process (each client has its own failure detector, for instance)."""
        return {"num_stores": self.num_stores, "replicas": self.replicas}


def check_cluster_params(config: StorageConfig, replies: Mapping[str, Mapping]) -> None:
    """Raise ``ValueError`` if a store runs with other cluster parameters.

    ``replies`` maps store names to their ``ping`` answers. Stores that did
    not answer are left out: they cannot be checked.
    """
    expected = config.cluster_params()
    wrong = []
    for store, reply in sorted(replies.items()):
        theirs = {key: reply.get(key) for key in expected}
        if theirs != expected:
            wrong.append(f"{store} runs with {theirs}")
    if wrong:
        raise ValueError(
            f"this process uses {expected}, but {'; '.join(wrong)}. "
            "Start every process with the same DTN_NUM_STORES and DTN_REPLICAS."
        )


def load_storage_config(env: Mapping[str, str] = os.environ) -> StorageConfig:
    """Build the configuration from ``DTN_*`` variables; unset ones keep their default."""
    defaults = StorageConfig()

    def read(name, convert, default):
        raw = env.get(name)
        if raw is None or raw == "":
            return default
        try:
            return convert(raw)
        except ValueError:
            raise ValueError(f"{name}={raw!r} is not a valid value") from None

    return StorageConfig(
        num_stores=read("DTN_NUM_STORES", int, defaults.num_stores),
        replicas=read("DTN_REPLICAS", int, defaults.replicas),
        data_dir=read("DTN_DATA_DIR", Path, defaults.data_dir),
        store_concurrency=read("DTN_STORE_CONCURRENCY", int, defaults.store_concurrency),
        hedge_delay_ms=read("DTN_HEDGE_DELAY_MS", int, defaults.hedge_delay_ms),
        timeout_min_ms=read("DTN_TIMEOUT_MIN_MS", int, defaults.timeout_min_ms),
        timeout_max_ms=read("DTN_TIMEOUT_MAX_MS", int, defaults.timeout_max_ms),
        min_write_mbps=read("DTN_MIN_WRITE_MBPS", float, defaults.min_write_mbps),
        repair_interval_seconds=read(
            "DTN_REPAIR_INTERVAL_SECONDS", float, defaults.repair_interval_seconds
        ),
        store_lost_seconds=read("DTN_STORE_LOST_SECONDS", float, defaults.store_lost_seconds),
        scrub_interval_seconds=read(
            "DTN_SCRUB_INTERVAL_SECONDS", float, defaults.scrub_interval_seconds
        ),
        gc_grace_seconds=read("DTN_GC_GRACE_SECONDS", float, defaults.gc_grace_seconds),
    )
