from functools import lru_cache

from gwc.config import get_settings
from gwc.nutrition.barcode import OpenFoodFactsClient
from gwc.nutrition.usda import UsdaClient


@lru_cache
def get_usda() -> UsdaClient:
    key = get_settings().usda_api_key
    return UsdaClient(key.get_secret_value() if key else None)


@lru_cache
def get_off() -> OpenFoodFactsClient:
    return OpenFoodFactsClient()
