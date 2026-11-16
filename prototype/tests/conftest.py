import pytest
import ray


@pytest.fixture(scope="session")
def ray_runtime():
    """Start a local Ray instance once for the whole test session."""
    ray.init(num_cpus=4, include_dashboard=False, log_to_driver=False)
    yield
    ray.shutdown()
