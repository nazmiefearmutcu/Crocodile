import os

# Prevent OpenMP and OpenBLAS multithreading deadlocks/slowness on macOS Apple Silicon
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import pytest
from unittest.mock import patch

# Force Qt offscreen platform for headless tests run in CI/CLI environments
os.environ["QT_QPA_PLATFORM"] = "offscreen"

# There is deliberately no `sys.modules["xgboost"] = MagicMock()` here.
#
# One lived at this line, to "safeguard against xgboost C-library loading failures on
# macOS" — the wheel needs libomp and `import xgboost` raises XGBoostError without it.
# The safeguard was unnecessary and it was not free. `crocodile.crypto.analytics.
# funding_prediction` is the only module in the tree that imports xgboost, and it already
# wraps the import in `except BaseException`, which is what XGBoostError needs; the module
# handles an absent library on its own, exactly as it does in production.
#
# What the stub did instead was make the import *succeed*, so XGBOOST_AVAILABLE was True in
# every test process regardless of the machine. `predict_next_funding` then took its
# xgboost branch, `MagicMock().predict(...)[0]` floated to MagicMock's default `__float__`,
# and the capability returned `predicted_funding_rate=1.0` — a mock artefact with the shape
# of a forecast. `funding-predict`'s only numeric check sat behind
# `if result["method"] == "rolling_mean"`, a branch that could then never be taken.
#
# A mock that decides which branch of the code under test runs is not a safeguard against
# the environment; it is a second environment nobody chose. Tests that want the xgboost
# path patch XGBOOST_AVAILABLE and inject a regressor that computes — see
# tests/analytics/test_funding_prediction.py, whose `_WeightedSumRegressor` returns a value
# derivable by hand, so the assertion is about the wiring and not about MagicMock.

# NOTE: PyQt6 / pyqtgraph used to be exempted from mocking here, because the FlowMap GUI
# tests exercised real offscreen Qt widgets. The desktop layer is gone and so are those
# tests; no module under src/crocodile imports either package now, so there is nothing
# left to mock or to exempt.

# Safeguard against matplotlib interactive GUI backend loading delays/hangs on macOS
try:
    import matplotlib
    matplotlib.use('Agg')
except ImportError:
    pass




@pytest.fixture(autouse=True, scope="function")
def configure_payments_env(tmp_path):
    temp_db = tmp_path / "payments_db.json"
    os.environ["PAYMENTS_FILE"] = str(temp_db)
    temp_ipc = tmp_path / "custom_pools_ipc.json"
    os.environ["CUSTOM_POOLS_IPC_FILE"] = str(temp_ipc)
    # Enable payment simulation for unit/integration tests by default
    prev_sim = os.environ.get("ALLOW_SIMULATION")
    os.environ["ALLOW_SIMULATION"] = "true"
    yield
    if "PAYMENTS_FILE" in os.environ:
        del os.environ["PAYMENTS_FILE"]
    if "CUSTOM_POOLS_IPC_FILE" in os.environ:
        del os.environ["CUSTOM_POOLS_IPC_FILE"]
    if prev_sim is None:
        os.environ.pop("ALLOW_SIMULATION", None)
    else:
        os.environ["ALLOW_SIMULATION"] = prev_sim


@pytest.fixture(autouse=True, scope="function")
def mock_sync_state_path(tmp_path):
    temp_dir = str(tmp_path / "sync_state")
    original_expanduser = os.path.expanduser
    
    def mock_expanduser(path):
        if path == "~/.crypcodile/sync_state":
            return temp_dir
        return original_expanduser(path)
        
    with patch("os.path.expanduser", mock_expanduser):
        yield
