"""Explicit configuration (principle P6).

Every tunable value comes from a ``DTN_*`` environment variable with a
documented default, read here and nowhere else, with one rule for every
variable: unset or empty keeps the default, anything else must parse and pass
validation. Components receive a config object instead of reading the
environment themselves, and log ``as_dict()`` when they start, so every run
records the exact values it used.

``StorageConfig`` holds what the stores, ``RayCluster`` and the repair agent
need; ``ClientConfig`` what the track operations need. Every error in either
is a ``ConfigError``.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_NAMESPACE = "dtn"


class ConfigError(ValueError):
    """A configuration value is missing, malformed or inconsistent.

    It is outside the contract: fixing it means changing the configuration,
    not retrying the operation.
    """


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
            raise ConfigError("DTN_NUM_STORES must be at least 1")
        if not 1 <= self.replicas <= self.num_stores:
            raise ConfigError("DTN_REPLICAS must be between 1 and DTN_NUM_STORES")
        if self.store_concurrency < 1:
            raise ConfigError("DTN_STORE_CONCURRENCY must be at least 1")
        if self.hedge_delay_ms is not None and self.hedge_delay_ms < 0:
            raise ConfigError("DTN_HEDGE_DELAY_MS must not be negative")
        if not 0 < self.timeout_min_ms <= self.timeout_max_ms:
            raise ConfigError("need 0 < DTN_TIMEOUT_MIN_MS <= DTN_TIMEOUT_MAX_MS")
        _require_positive(
            self,
            "min_write_mbps",
            "repair_interval_seconds",
            "store_lost_seconds",
            "scrub_interval_seconds",
            "gc_grace_seconds",
        )

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


_BITRATE = re.compile(r"[1-9][0-9]*k")


@dataclass(frozen=True)
class ClientConfig:
    """Parameters of the track operations; the defaults are those of «Parámetros»."""

    upload_timeout_seconds: float = 600
    search_timeout_ms: float = 2000
    withdraw_timeout_seconds: float = 30
    upload_parallelism: int = 8
    segment_seconds: int = 2
    audio_bitrate: str = "128k"
    max_upload_seconds: float = 1200
    max_upload_bytes: int = 209_715_200

    def __post_init__(self):
        _require_positive(
            self,
            "upload_timeout_seconds",
            "search_timeout_ms",
            "withdraw_timeout_seconds",
            "upload_parallelism",
            "segment_seconds",
            "max_upload_seconds",
            "max_upload_bytes",
        )
        if not _BITRATE.fullmatch(self.audio_bitrate):
            raise ConfigError("DTN_AUDIO_BITRATE must look like 128k")

    def as_dict(self) -> dict:
        return asdict(self)


def check_cluster_params(config: StorageConfig, replies: Mapping[str, Mapping]) -> None:
    """Raise ``ConfigError`` if a store runs with other cluster parameters.

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
        raise ConfigError(
            f"this process uses {expected}, but {'; '.join(wrong)}. "
            "Start every process with the same DTN_NUM_STORES and DTN_REPLICAS."
        )


def load_storage_config(env: Mapping[str, str] = os.environ) -> StorageConfig:
    """Build the storage configuration from ``DTN_*`` variables."""
    d = StorageConfig()
    return StorageConfig(
        num_stores=_read(env, "DTN_NUM_STORES", int, d.num_stores),
        replicas=_read(env, "DTN_REPLICAS", int, d.replicas),
        data_dir=_read(env, "DTN_DATA_DIR", Path, d.data_dir),
        store_concurrency=_read(env, "DTN_STORE_CONCURRENCY", int, d.store_concurrency),
        hedge_delay_ms=_read(env, "DTN_HEDGE_DELAY_MS", int, d.hedge_delay_ms),
        timeout_min_ms=_read(env, "DTN_TIMEOUT_MIN_MS", int, d.timeout_min_ms),
        timeout_max_ms=_read(env, "DTN_TIMEOUT_MAX_MS", int, d.timeout_max_ms),
        min_write_mbps=_read(env, "DTN_MIN_WRITE_MBPS", float, d.min_write_mbps),
        repair_interval_seconds=_read(
            env, "DTN_REPAIR_INTERVAL_SECONDS", float, d.repair_interval_seconds
        ),
        store_lost_seconds=_read(env, "DTN_STORE_LOST_SECONDS", float, d.store_lost_seconds),
        scrub_interval_seconds=_read(
            env, "DTN_SCRUB_INTERVAL_SECONDS", float, d.scrub_interval_seconds
        ),
        gc_grace_seconds=_read(env, "DTN_GC_GRACE_SECONDS", float, d.gc_grace_seconds),
    )


def load_client_config(env: Mapping[str, str] = os.environ) -> ClientConfig:
    """Build the configuration of the track operations from ``DTN_*`` variables."""
    d = ClientConfig()
    return ClientConfig(
        upload_timeout_seconds=_read(
            env, "DTN_UPLOAD_TIMEOUT_SECONDS", float, d.upload_timeout_seconds
        ),
        search_timeout_ms=_read(env, "DTN_SEARCH_TIMEOUT_MS", float, d.search_timeout_ms),
        withdraw_timeout_seconds=_read(
            env, "DTN_WITHDRAW_TIMEOUT_SECONDS", float, d.withdraw_timeout_seconds
        ),
        upload_parallelism=_read(env, "DTN_UPLOAD_PARALLELISM", int, d.upload_parallelism),
        segment_seconds=_read(env, "DTN_SEGMENT_SECONDS", int, d.segment_seconds),
        audio_bitrate=_read(env, "DTN_AUDIO_BITRATE", str, d.audio_bitrate),
        max_upload_seconds=_read(env, "DTN_MAX_UPLOAD_SECONDS", float, d.max_upload_seconds),
        max_upload_bytes=_read(env, "DTN_MAX_UPLOAD_BYTES", int, d.max_upload_bytes),
    )


def load_namespace(env: Mapping[str, str] = os.environ) -> str:
    """The Ray namespace where the stores and the agent live (``DTN_RAY_NAMESPACE``).

    Every process of one system must use the same one; tests use their own.
    """
    return _read(env, "DTN_RAY_NAMESPACE", str, DEFAULT_NAMESPACE)


def _read(env: Mapping[str, str], name: str, convert, default):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return convert(raw.strip())
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid value") from None


def _require_positive(config, *names: str) -> None:
    for name in names:
        if getattr(config, name) <= 0:
            raise ConfigError(f"DTN_{name.upper()} must be positive")
