import pytest
import ray

# Named actors are only visible inside their namespace. A fixed one lets a
# separate process (like the script that kills stores) find them by name.
RAY_NAMESPACE = "dtn-tests"


@pytest.fixture(scope="session")
def ray_runtime():
    """Start a local Ray instance once for the whole test session."""
    ray.init(
        num_cpus=4,
        include_dashboard=False,
        log_to_driver=False,
        namespace=RAY_NAMESPACE,
    )
    yield
    ray.shutdown()
