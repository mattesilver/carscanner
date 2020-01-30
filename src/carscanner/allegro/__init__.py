import typing

import allegro_api
import allegro_pl

from .auth import CarScannerCodeAuth, EnvironClientCodeStore, InsecureTokenStore, YamlClientCodeStore


def get_root():
    from pathlib import Path
    root_dir = Path('~/.carscanner').expanduser()
    if not root_dir.exists():
        root_dir.mkdir(0o700)
    return root_dir


root_dir = get_root()
codes_path = root_dir / 'allegro.yaml'


class CarscannerAllegro:
    def __init__(self, allegro: allegro_pl.Allegro):
        rest = allegro.rest_service()
        soap = allegro.soap_service()

        self.get_categories = rest.get_categories
        self.get_category_parameters = rest.get_category_parameters
        self.get_items_info = soap.get_items_info
        self.get_listing = rest.get_listing
        self.get_states_info = soap.get_states_info

    def get_filters(self, cat_id: str) -> typing.List[allegro_api.models.ListingResponseFilters]:
        return self.get_listing(
            category_id=cat_id,
            limit=self.get_listing.limit_min,
            include=['-all', 'filters'],
            _request_timeout=(30, 30),
        ).filters


def get_client(code_store, token_store: allegro_pl.TokenStore, allow_fetch=True) -> allegro_pl.Allegro:
    auth = CarScannerCodeAuth(code_store, token_store, allow_fetch)
    return allegro_pl.Allegro(auth)
