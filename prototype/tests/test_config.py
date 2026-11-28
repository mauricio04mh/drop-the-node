from pathlib import Path

import pytest

from dtn.config import (
    ClientConfig,
    ConfigError,
    StorageConfig,
    check_cluster_params,
    load_client_config,
    load_namespace,
    load_storage_config,
)


def test_defaults_match_the_documented_parameters():
    config = load_storage_config({})
    assert config.num_stores == 5
    assert config.replicas == 3
    assert config.data_dir == Path("./data")
    assert config.store_concurrency == 8
    assert config.hedge_delay_ms is None
    assert config.gc_grace_seconds == 86400


def test_reads_values_from_the_environment():
    config = load_storage_config(
        {
            "DTN_NUM_STORES": "3",
            "DTN_REPLICAS": "2",
            "DTN_HEDGE_DELAY_MS": "0",
            "DTN_MIN_WRITE_MBPS": "2.5",
        }
    )
    assert config.num_stores == 3
    assert config.replicas == 2
    assert config.hedge_delay_ms == 0
    assert config.min_write_mbps == 2.5


def test_store_names_are_logical_and_stable():
    assert StorageConfig(num_stores=3, replicas=2).store_names == (
        "store-1",
        "store-2",
        "store-3",
    )


@pytest.mark.parametrize(
    "env",
    [
        {"DTN_REPLICAS": "6"},  # more replicas than stores
        {"DTN_REPLICAS": "0"},
        {"DTN_NUM_STORES": "five"},
        {"DTN_TIMEOUT_MIN_MS": "3000"},  # above the maximum
        {"DTN_GC_GRACE_SECONDS": "-1"},
        {"DTN_MIN_WRITE_MBPS": "0"},
        {"DTN_STORE_LOST_SECONDS": "0"},
    ],
)
def test_rejects_inconsistent_values(env):
    with pytest.raises(ValueError):
        load_storage_config(env)


def test_as_dict_is_printable():
    values = load_storage_config({}).as_dict()
    assert values["data_dir"] == "data"
    assert values["replicas"] == 3


def test_cluster_params_are_s_and_n():
    assert StorageConfig().cluster_params() == {"num_stores": 5, "replicas": 3}


def test_stores_with_the_same_s_and_n_pass_the_check():
    reply = {"store": "store-1", "num_stores": 5, "replicas": 3}
    check_cluster_params(StorageConfig(), {"store-1": reply, "store-2": reply})


def test_a_store_with_another_n_fails_the_check_and_is_named():
    replies = {
        "store-1": {"num_stores": 5, "replicas": 3},
        "store-2": {"num_stores": 5, "replicas": 2},
    }
    with pytest.raises(ValueError, match="store-2"):
        check_cluster_params(StorageConfig(), replies)


def test_stores_that_did_not_answer_are_not_checked():
    check_cluster_params(StorageConfig(), {})


# --- client configuration and shared reading rules -------------------------------


def test_empty_variables_keep_the_default():
    assert load_storage_config({"DTN_REPLICAS": "", "DTN_NUM_STORES": "  "}) == StorageConfig()
    assert load_client_config({"DTN_UPLOAD_PARALLELISM": ""}) == ClientConfig()


def test_client_defaults_match_the_documented_parameters():
    config = load_client_config({})
    assert (config.upload_timeout_seconds, config.search_timeout_ms, config.withdraw_timeout_seconds) == (
        600,
        2000,
        30,
    )
    assert (config.upload_parallelism, config.segment_seconds, config.audio_bitrate) == (8, 2, "128k")


@pytest.mark.parametrize(
    "env",
    [
        {"DTN_UPLOAD_PARALLELISM": "0"},
        {"DTN_UPLOAD_PARALLELISM": "many"},
        {"DTN_SEARCH_TIMEOUT_MS": "-1"},
        {"DTN_AUDIO_BITRATE": "loud"},
        {"DTN_MAX_UPLOAD_BYTES": "1.5"},
    ],
)
def test_client_config_rejects_bad_values(env):
    with pytest.raises(ConfigError):
        load_client_config(env)


def test_every_configuration_error_is_a_config_error():
    with pytest.raises(ConfigError):
        load_storage_config({"DTN_REPLICAS": "9"})
    with pytest.raises(ConfigError, match="store-2"):
        check_cluster_params(StorageConfig(), {"store-2": {"num_stores": 5, "replicas": 2}})


def test_namespace_comes_from_the_environment():
    assert load_namespace({}) == "dtn"
    assert load_namespace({"DTN_RAY_NAMESPACE": "experiment"}) == "experiment"
