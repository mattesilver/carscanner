import datetime
import logging
import typing

import allegro_api.models
import isodate
import zeep
import zeep.exceptions

from carscanner.allegro import CarscannerAllegro
from carscanner.dao import CarOfferDao, Criteria, MetadataDao, FilterDao
from carscanner.utils import chunks
from .car_offer import CarOffersBuilder
from .filter import FilterService

logger = logging.getLogger(__name__)


class OfferService:
    _filter_template = {
        'Oferta dotyczy': 'sprzedaż',
        'Stan': "używane",
    }
    search_params = {
        'fallback': False,
        'include': ['-all', 'items', 'searchMeta'],
        'sort': '-startTime'
    }

    def __init__(self, allegro: CarscannerAllegro, criteria_dao, car_offers_builder: CarOffersBuilder, car_offer_dao,
                 filter_service, meta_dao: MetadataDao, ts):
        self._allegro = allegro

        self.criteria_dao = criteria_dao
        self.car_offers_builder = car_offers_builder
        self.car_offer_dao: CarOfferDao = car_offer_dao
        self.filter_service: FilterService = filter_service
        self.timestamp: datetime.datetime = ts

        self._last_run: datetime.datetime = meta_dao.get_timestamp()

    def _get_offers_for_criteria(self, crit: Criteria) -> typing.Iterable[typing.List[allegro_api.models.ListingOffer]]:
        offset = 0
        while True:
            data = self._allegro.get_listing(self._search_params(crit, offset))

            result = data.items.promoted + data.items.regular

            logger.info('get_listing: total %d, this run %d, offset %d', data.search_meta.available_count, len(result),
                        offset)
            yield result

            offset += len(result)
            if offset >= data.search_meta.available_count:
                break

    def _search_params(self, crit: Criteria, offset=0) -> dict:
        result = OfferService.search_params.copy()
        result.update(self.filter_service.transform_filters(crit.category_id, OfferService._filter_template))
        result['category.id'] = crit.category_id
        result['offset'] = str(offset)
        result['limit'] = str(self._allegro.get_listing.limit_max)
        result['startingTime'] = self._get_start_period_str(crit.category_id)

        return result

    def get_offers(self):
        items = []
        for crit in self.criteria_dao.all():
            for crit_items in self._get_offers_for_criteria(crit):
                items.extend(crit_items)

        item_ids = [item.id for item in items]

        existing = self.car_offer_dao.search_existing_ids(item_ids)
        self.car_offer_dao.update_last_spotted(existing, self.timestamp)

        # get non-existing ids
        new_items = [item for item in items if item.id not in existing]

        # pull their details
        car_offers = self.car_offers_builder.to_car_offers(new_items)
        for item_info_chunk in self._get_items_info(list(car_offers.keys())):
            for value in item_info_chunk.arrayItemListInfo.item:
                item_id = str(value.itemInfo.itId)
                self.car_offers_builder.update_from_item_info_struct(car_offers[item_id], value)

        self.car_offer_dao.insert_multiple(car for car in car_offers.values() if car.is_valid())

    def _get_items_info(self, offer_ids: typing.List[str]) -> typing.Iterable[zeep.xsd.CompoundValue]:
        chunk_no = 1
        from math import ceil
        chunks_count = ceil(len(offer_ids) / self._allegro.get_items_info.items_limit)
        for chunk in chunks(offer_ids, self._allegro.get_items_info.items_limit):
            logger.info('get_items_info: chunk %d out of %d', chunk_no, chunks_count)
            try:
                yield self._allegro.get_items_info(chunk, True, True, True)
            except zeep.exceptions.TransportError:
                # https://github.com/allegro/allegro-api/issues/1585
                for item_id in chunk:
                    try:
                        yield self._allegro.get_items_info([item_id], True, True, True)
                    except zeep.exceptions.TransportError as x2:
                        logger.warning('Could not fetch item (%s) info: %s', item_id, x2)
            chunk_no += 1

    def _get_start_period_str(self,cat_id:str) -> str:
        delta: datetime.timedelta =  self.timestamp - self._last_run

        offers_since_delta = self.filter_service.find_min_timedelta_gt(cat_id, delta)

        return isodate.duration_isoformat(offers_since_delta)