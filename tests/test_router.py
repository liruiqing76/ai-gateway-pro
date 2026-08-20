import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.api.router import Router
from backend.api.provider_client import BaseProviderClient

@pytest.fixture
def mock_provider():
    mock = AsyncMock(spec=BaseProviderClient)
    mock.name = "mock"
    return mock

def test_route_to_provider(mock_provider):
    router = Router()
    router.register_provider("mock", mock_provider)
    client = router.get_provider("mock")
    assert client is mock_provider

def test_unknown_provider_raises():
    router = Router()
    with pytest.raises(KeyError):
        router.get_provider("unknown")

def test_register_duplicate_provider(mock_provider):
    router = Router()
    router.register_provider("mock", mock_provider)
    with pytest.raises(ValueError):
        router.register_provider("mock", mock_provider)

def test_get_provider_returns_correct_type(mock_provider):
    router = Router()
    router.register_provider("mock", mock_provider)
    client = router.get_provider("mock")
    assert isinstance(client, BaseProviderClient)

def test_list_providers_returns_all(mock_provider):
    router = Router()
    p1 = AsyncMock(spec=BaseProviderClient)
    p1.name = "provider1"
    p2 = AsyncMock(spec=BaseProviderClient)
    p2.name = "provider2"
    router.register_provider("provider1", p1)
    router.register_provider("provider2", p2)
    providers = router.list_providers()
    assert set(providers) == {"provider1", "provider2"}