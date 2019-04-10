import datetime
import logging
import os.path
import typing

import allegro_api.api
import tinydb
import zeep

import carscanner.allegro
from carscanner import chunks, CarOffersBuilder, CarOfferBuilder
from carscanner.allegro import CarscannerAllegro as Allegro
from carscanner.dao import Criteria, CriteriaDao, CarMakeModelDao, VoivodeshipDao, CarOfferDao, FilterDao
from carscanner.data import DataManager
from carscanner.filter import FilterService

logger = logging.getLogger(__name__)


class OfferService:
    _filter_template = {
        'Oferta dotyczy': 'sprzedaż',
        'Stan': "używane",
        'wystawione w ciągu': "2 dni",
        "Uszkodzony": "Nie"
    }
    search_params = {
        'fallback': False,
        'include': ['-all', 'items', 'searchMeta'],
        'sort': '-startTime'
    }

    def __init__(self, allegro: Allegro, criteria_dao, car_offers_builder: CarOffersBuilder, car_offer_dao,
                 filter_service, ts):
        self._allegro = allegro

        self.criteria_dao = criteria_dao
        self.car_offers_builder = car_offers_builder
        self.car_offer_dao: CarOfferDao = car_offer_dao
        self.filter_service: FilterService = filter_service
        self.timestamp: datetime.datetime = ts

    def _get_offers_for_criteria(self, crit: Criteria) -> typing.Iterable[typing.List[allegro_api.models.ListingOffer]]:
        offset = 0
        while True:
            data: allegro_api.models.ListingResponse = self._allegro.get_listing(self._search_params(crit, offset))

            result = data.items.promoted + data.items.regular

            logger.info('get_listing: total %d, this run %d, offset %d', data.search_meta.available_count, len(result), offset)
            yield result

            offset += len(result)
            if offset >= data.search_meta.available_count:
                break

    def _search_params(self, crit: Criteria, offset=0) -> dict:
        result = OfferService.search_params.copy()
        result.update(self.filter_service.transform_filters(crit.category_id, OfferService._filter_template))
        result['category.id'] = crit.category_id
        result['offset'] = offset
        result['limit'] = self._allegro.get_listing.limit_max

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
        for item_info_chunk in self._get_items_info(car_offers):
            for value in item_info_chunk.arrayItemListInfo.item:
                id = str(value.itemInfo.itId)
                car_offers[id].update_from_item_info_struct(value)
        self.car_offer_dao.insert_multiple([builder.c for builder in car_offers.values()])

    def _get_items_info(self, items: typing.Dict[str, CarOfferBuilder]) -> typing.Iterable[zeep.xsd.CompoundValue]:
        offer_ids = list(items.keys())
        chunk_no = 0
        from math import ceil
        chunks_count = ceil(len(offer_ids) / self._allegro.get_items_info.items_limit)
        for chunk in chunks(offer_ids, self._allegro.get_items_info.items_limit):
            logger.info('get_items_info: chunk %d out of %d', chunk_no, chunks_count)
            yield self._allegro.get_items_info(itemsIdArray=chunk, getDesc=1, getImageUrl=1, getAttribs=1)
            chunk_no += 1


def _update_meta(db, ts):
    import platform
    from tinydb import Query

    tbl = db.table(db.DEFAULT_TABLE)
    meta = {
        'timestamp': ts.isoformat(),
        'host': platform.node()
    }
    tbl.upsert(meta, Query())


def _report_meta(db):
    old_meta = db.get(tinydb.Query())
    if old_meta:
        logger.info('Last run at %s on %s', old_meta['timestamp'], old_meta['host'])
    else:
        logger.info('First run')


def update_cmd(data_dir: str, **_):
    client = carscanner.allegro.get_client()
    dm = DataManager(os.path.expanduser(data_dir))

    try:
        static_db = dm.static_data()
        db = dm.cars_data()
        mem_db = dm.mem_db()

        _report_meta(db)
        ts = datetime.datetime.utcnow()
        _update_meta(db, ts)

        criteria_dao = CriteriaDao(static_db)
        voivodeship_dao = VoivodeshipDao(static_db)
        car_make_mode_dao = CarMakeModelDao(static_db)
        car_offer_dao = CarOfferDao(db)

        car_offers_builder = CarOffersBuilder(voivodeship_dao, car_make_mode_dao)
        filter_service = FilterService(client, FilterDao(mem_db), criteria_dao)
        offer_service = OfferService(client, criteria_dao, car_offers_builder, car_offer_dao, filter_service, ts)

        filter_service.load_filters()
        offer_service.get_offers()
    finally:
        dm.close()
