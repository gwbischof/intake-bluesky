import event_model
from sys import maxsize
from functools import partial
import intake
import intake.catalog
import intake.catalog.local
import intake.source.base
import pymongo
import pymongo.errors
import heapq
import json

from .core import parse_handler_registry


class BlueskyMongoCatalog(intake.catalog.Catalog):
    def __init__(self, datastore_db, *, handler_registry=None,
                 query=None, **kwargs):
        """
        This Catalog is backed by a MongoDB with an embedded data model.

        This embedded data model has three collections: header, event, datum.
        The header collection includes start, stop, descriptor, and resource
        documents. The event_pages are stored in the event colleciton, and
        datum_pages are stored in the datum collection.

        Parameters
        ----------
        datastore_db : pymongo.database.Database or string
            Must be a Database or a URI string that includes a database name.
        handler_registry : dict, optional
            Maps each asset spec to a handler class or a string specifying the
            module name and class name, as in (for example)
            ``{'SOME_SPEC': 'module.submodule.class_name'}``.
        query : dict, optional
            MongoDB query. Used internally by the ``search()`` method.
        **kwargs :
            Additional keyword arguments are passed through to the base class,
            Catalog.
        """
        name = 'bluesky-mongo-embedded-catalog'  # noqa

        if isinstance(datastore_db, str):
            self._db = _get_database(datastore_db)
        else:
            self._db = datastore_db

        self._query = query or {}

        if handler_registry is None:
            handler_registry = {}
        parsed_handler_registry = parse_handler_registry(handler_registry)
        self.filler = event_model.Filler(parsed_handler_registry)
        super().__init__(**kwargs)

    def _get_event_cursor(self, descriptor_uids, skip=0, limit=None):
        if limit is None:
            limit = maxsize

        def event_gen(cursor):
            nonlocal skip
            nonlocal limit
            for page_index, event_page in enumerate(cursor):
                for event_index, event in (
                        enumerate(event_model.unpack_event_page(event_page))):
                    while ((event_index + 1) * (page_index + 1)) < skip:
                        continue
                    if not ((event_index + 1) * (page_index + 1)) < (skip + limit):
                        return
                    yield event

        cursors = [event_gen(self._db.event.find(
                            {'$and': [
                                {'descriptor': descriptor},
                                {'last_index': {'$gte': skip}},
                                {'first_index': {'$lte': skip + limit}}]},
                            {'_id': False},
                            sort=[('last_index', pymongo.ASCENDING)]))
                   for descriptor in descriptor_uids]

        yield from interlace_gens(*cursors)

    def _get_datum_cursor(self, resource_uid, skip=0, limit=None):
        if limit is None:
            limit = maxsize

        page_cursor = self._db.datum.find(
                            {'$and': [
                                {'resource': resource_uid},
                                {'last_index': {'$gte': skip}},
                                {'first_index': {'$lte': skip + limit}}]},
                            {'_id': False},
                            sort=[('last_index', pymongo.ASCENDING)])

        for page_index, datum_page in enumerate(page_cursor):
            for datum_index, datum in (
                    enumerate(event_model.unpack_datum_page(datum_page))):
                while ((datum_index + 1) * (page_index + 1)) < skip:
                    continue
                if not ((datum_index + 1) * (page_index + 1)) < (skip + limit):
                    return
                yield datum

    def _make_entries_container(self):
        catalog = self

        class Entries:

            "Mock the dict interface around a MongoDB query result."
            def _doc_to_entry(self, run_start_doc):

                header_doc = None
                uid = run_start_doc['uid']

                def get_header_field(field):
                    nonlocal header_doc
                    if header_doc is None:
                        header_doc = catalog._db.header.find_one(
                                        {'run_id': uid}, {'_id': False})
                    if field in header_doc:
                        if field in ['start', 'stop']:
                            return header_doc[field][0]
                        else:
                            return header_doc[field]
                    else:
                        if field[0:6] == 'count_':
                            return 0
                        else:
                            return {}

                def get_resource(uid):
                    resources = get_header_field('resources')
                    for resource in resources:
                        if resource['uid'] == uid:
                            return resource
                    raise KeyError(
                            f"Could not find Resource with datum_id={uid}")

                def get_datum(datum_id):
                    """ This method is likely very slow. """
                    resources = [resource['uid']
                                 for resource in get_header_field('resources')]
                    datum_page = catalog._db.datum.find_one(
                           {'$and': [{'resource': {'$in': resources}},
                                     {'datum_id': datum_id}]},
                           {'_id': False})
                    for datum in event_model.unpack_datum_page(datum_page):
                        if datum['datum_id'] == datum_id:
                            return datum
                    raise KeyError(
                            f"Could not find Datum with datum_id={datum_id}")

                def get_event_count(descriptor_uids):
                    return sum([get_header_field('count_' + uid)
                                for uid in descriptor_uids])

                entry_metadata = {'start': get_header_field('start'),
                                  'stop': get_header_field('stop')}

                args = dict(
                    get_run_start=lambda: run_start_doc,
                    get_run_stop=partial(get_header_field, 'stop'),
                    get_event_descriptors=partial(
                                    get_header_field, 'descriptors'),
                    get_event_cursor=catalog._get_event_cursor,
                    get_event_count=get_event_count,
                    get_resource=get_resource,
                    get_datum=get_datum,
                    get_datum_cursor=catalog._get_datum_cursor,
                    filler=catalog.filler)
                return intake.catalog.local.LocalCatalogEntry(
                    name=run_start_doc['uid'],
                    description={},  # TODO
                    driver='intake_bluesky.core.RunCatalog',
                    direct_access='forbid',  # ???
                    args=args,
                    cache=None,  # ???
                    parameters=[],
                    metadata=entry_metadata,
                    catalog_dir=None,
                    getenv=True,
                    getshell=True,
                    catalog=catalog)

            def __iter__(self):
                yield from self.keys()

            def keys(self):
                cursor = catalog._db.header.find(
                            catalog._query,
                            {'start.uid': True, '_id': False},
                            sort=[('start.time', pymongo.DESCENDING)])

                for doc in cursor:
                    yield doc['start'][0]['uid']

            def values(self):
                cursor = catalog._db.header.find(
                            catalog._query,
                            {'start': True, '_id': False},
                            sort=[('start.time', pymongo.DESCENDING)])
                for header_doc in cursor:
                    yield self._doc_to_entry(header_doc['start'][0])

            def items(self):
                cursor = catalog._db.header.find(
                    catalog._query, sort=[('start.time', pymongo.DESCENDING)])
                for header_doc in cursor:
                    yield header_doc['start'][0]['uid'], self._doc_to_entry(
                                header_doc['start'][0])

            def __getitem__(self, name):
                # If this came from a client, we might be getting '-1'.
                try:
                    N = int(name)
                except ValueError:
                    query = {'$and': [catalog._query, {'start.uid': name}]}
                    header_doc = catalog._db.header.find_one(query)
                    if header_doc is None:
                        regex_query = {
                            '$and': [catalog._query,
                                     {'start.uid': {'$regex': f'{name}.*'}}]}
                        matches = list(
                                catalog._db.header.find(regex_query).limit(10))
                        if not matches:
                            raise KeyError(name)
                        elif len(matches) == 1:
                            header_doc, = matches
                        else:
                            match_list = '\n'.join(doc['start'][0]['uid'] for doc in matches)
                            raise ValueError(
                                f"Multiple matches to partial uid {name!r}. "
                                f"Up to 10 listed here:\n"
                                f"{match_list}")
                else:
                    if N < 0:
                        # Interpret negative N as "the Nth from last entry".
                        query = catalog._query
                        cursor = (catalog._db.header.find(query)
                                  .sort('start.time', pymongo.DESCENDING)
                                  .skip(-N - 1)
                                  .limit(1))
                        try:
                            header_doc, = cursor
                        except ValueError:
                            raise IndexError(
                                f"Catalog only contains {len(catalog)} "
                                f"runs.")
                    else:
                        # Interpret positive N as
                        # "most recent entry with scan_id == N".
                        query = {'$and': [catalog._query, {'start.scan_id': N}]}
                        cursor = (catalog._db.header.find(query)
                                  .sort('start.time', pymongo.DESCENDING)
                                  .limit(1))
                        try:
                            header_doc, = cursor
                        except ValueError:
                            raise KeyError(f"No run with scan_id={N}")
                if header_doc is None:
                    raise KeyError(name)
                return self._doc_to_entry(header_doc['start'][0])

            def __contains__(self, key):
                # Avoid iterating through all entries.
                try:
                    self[key]
                except KeyError:
                    return False
                else:
                    return True

        return Entries()

    def _close(self):
        self._client.close()

    def __len__(self):
        return self._db.header.count_documents(self._query)

    def search(self, query):
        """
        Return a new Catalog with a subset of the entries in this Catalog.

        Parameters
        ----------
        query : dict
            MongoDB query.
        """
        if query:
            query = query_embedder(query)
        if self._query:
            query = {'$and': [self._query, query]}
        cat = type(self)(
            datastore_db=self._db,
            query=query,
            name='search results',
            getenv=self.getenv,
            getshell=self.getshell,
            auth=self.auth,
            metadata=(self.metadata or {}).copy(),
            storage_options=self.storage_options)
        return cat


def _get_database(uri):
    client = pymongo.MongoClient(uri)
    try:
        # Called with no args, get_database() returns the database
        # specified in the client's uri --- or raises if there was none.
        # There is no public method for checking this in advance, so we
        # just catch the error.
        return client.get_database()
    except pymongo.errors.ConfigurationError as err:
        raise ValueError(
            f"Invalid client: {client} "
            f"Did you forget to include a database?") from err


def interlace_gens(*gens):
    """Take generators and interlace their results by timestamp
     Parameters
    ----------
    gens : generators
        Generators of (name, dict) pairs where the dict contains a 'time'
        key.
     Yields
    -------
    val : tuple
        The next (name, dict) pair in time order
     """
    iters = [iter(g) for g in gens]
    heap = []

    def safe_next(indx):
        try:
            val = next(iters[indx])
        except StopIteration:
            return
        heapq.heappush(heap, (val['time'], indx, val))

    for i in range(len(iters)):
        safe_next(i)
    while heap:
        _, indx, val = heapq.heappop(heap)
        yield val
        safe_next(indx)


def query_embedder(query):
    query_string = json.dumps(query)
    keys = [item.split('"')[-1] for item in query_string.split('":')][0:-1]
    new_keys = [f'start.{key}' if '$' not in key else key for key in keys]
    for index, key in enumerate(keys):
        query_string = query_string.replace(key, new_keys[index])
    return json.loads(query_string)


intake.registry['mongo_metadatastore'] = BlueskyMongoCatalog
