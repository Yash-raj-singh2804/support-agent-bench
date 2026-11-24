import pytest


@pytest.fixture(autouse=True)
def _no_real_grounding_verifier():
    """Tests never call the real LLM grounding verifier (it would cost money and need the network).
    Tests that exercise it pass a scripted LLM explicitly."""
    from app.config import get_settings

    s = get_settings()
    old = s.grounding_model
    s.grounding_model = "none"
    yield
    s.grounding_model = old
