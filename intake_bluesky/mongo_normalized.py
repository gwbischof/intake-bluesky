import collections.abc
import event_model
from functools import partial
import intake
import intake.catalog
import intake.catalog.local
import intake.source.base
import pymongo
import pymongo.errors

from .core import parse_handler_registry
from .core import to_event_pages
from .core import to_datum_pages


class _Entries(collections.abc.Mapping):
    "Mock the dict interface around a MongoDB query result."
    def __init__(self, catalog):
        self.catalog = catalog

    def _doc_to_entry(self, run_start_doc):
        uid = run_start_doc['uid']
        run_start_doc.pop('_id')
        entry_metadata = {'start': run_start_doc,
                          'stop': self.catalog._get_run_stop(uid)}

        def get_run_start():
            return run_start_doc

        args = dict(
            get_run_start=get_run_start,
            get_run_stop=partial(self.catalog._get_run_stop, uid),
            get_event_descriptors=partial(self.catalog._get_event_descriptors, uid),
            # 2500 was selected as the page_size because it worked well durring
            # benchmarks, for HXN data a full page had roughly 3500 events.
            get_event_pages=to_event_pages(self.catalog._get_event_cursor, 2500),
            get_event_count=self.catalog._get_event_count,
            get_resource=self.catalog._get_resource,
            lookup_resource_for_datum=self.catalog._lookup_resource_for_datum,
            # 2500 was selected as the page_size because it worked well durring
            # benchmarks.
            get_datum_pages=to_datum_pages(self.catalog._get_datum_cursor, 2500),
            filler=self.catalog.filler)
        return intake.catalog.local.LocalCatalogEntry(
            name=run_start_doc['uid'],
            description={},  # TODO
            driver='intake_bluesky.core.BlueskyRun',
            direct_access='forbid',  # ???
            args=args,
            cache=None,  # ???
            parameters=[],
            metadata=entry_metadata,
            catalog_dir=None,
            getenv=True,
            getshell=True,
            catalog=self.catalog)

    def __iter__(self):
        cursor = self.catalog._run_start_collection.find(
            self.catalog._query, sort=[('time', pymongo.DESCENDING)])
        for run_start_doc in cursor:
            yield run_start_doc['uid']

    def __getitem__(self, name):
        # If this came from a client, we might be getting '-1'.
        collection = self.catalog._run_start_collection
        try:
            N = int(name)
        except ValueError:
            query = {'$and': [self.catalog._query, {'uid': name}]}
            run_start_doc = collection.find_one(query)
            if run_start_doc is None:
                regex_query = {
                    '$and': [self.catalog._query,
                             {'uid': {'$regex': f'{name}.*'}}]}
                matches = list(collection.find(regex_query).limit(10))
                if not matches:
                    raise KeyError(name)
                elif len(matches) == 1:
                    run_start_doc, = matches
                else:
                    match_list = '\n'.join(doc['uid'] for doc in matches)
                    raise ValueError(
                        f"Multiple matches to partial uid {name!r}. "
                        f"Up to 10 listed here:\n"
                        f"{match_list}")
        else:
            if N < 0:
                # Interpret negative N as "the Nth from last entry".
                query = self.catalog._query
                cursor = (collection.find(query)
                          .sort('time', pymongo.DESCENDING)
                          .skip(-N - 1)
                          .limit(1))
                try:
                    run_start_doc, = cursor
                except ValueError:
                    raise IndexError(
                        f"Catalog only contains {len(self.catalog)} "
                        f"runs.")
            else:
                # Interpret positive N as
                # "most recent entry with scan_id == N".
                query = {'$and': [self.catalog._query, {'scan_id': N}]}
                cursor = (collection.find(query)
                          .sort('time', pymongo.DESCENDING)
                          .limit(1))
                try:
                    run_start_doc, = cursor
                except ValueError:
                    raise KeyError(f"No run with scan_id={N}")
        if run_start_doc is None:
            raise KeyError(name)
        return self._doc_to_entry(run_start_doc)

    def __contains__(self, key):
        # Avoid iterating through all entries.
        try:
            self[key]
        except KeyError:
            return False
        else:
            return True

    def __len__(self):
        return len(self.catalog)


class BlueskyMongoCatalog(intake.catalog.Catalog):
    def __init__(self, metadatastore_db, asset_registry_db, *,
                 handler_registry=None, query=None, **kwargs):
        """
        This Catalog is backed by a pair of MongoDBs with "layout 1".

        This layout uses a separate Mongo collection per document type and a
        separate Mongo document for each logical document.

        Parameters
        ----------
        metadatastore_db : pymongo.database.Database or string
            Must be a Database or a URI string that includes a database name.
        asset_registry_db : pymongo.database.Database or string
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
        name = 'bluesky-mongo-catalog'  # noqa

        if isinstance(metadatastore_db, str):
            mds_db = _get_database(metadatastore_db)
        else:
            mds_db = metadatastore_db
        if isinstance(asset_registry_db, str):
            assets_db = _get_database(asset_registry_db)
        else:
            assets_db = asset_registry_db

        self._run_start_collection = mds_db.get_collection('run_start')
        self._run_stop_collection = mds_db.get_collection('run_stop')
        self._event_descriptor_collection = mds_db.get_collection('event_descriptor')
        self._event_collection = mds_db.get_collection('event')

        self._resource_collection = assets_db.get_collection('resource')
        self._datum_collection = assets_db.get_collection('datum')

        self._metadatastore_db = mds_db
        self._asset_registry_db = assets_db

        self._query = query or {}
        if handler_registry is None:
            handler_registry = {}
        parsed_handler_registry = parse_handler_registry(handler_registry)
        self.filler = event_model.Filler(parsed_handler_registry, inplace=True)
        super().__init__(**kwargs)

    def _get_run_stop(self, run_start_uid):
        doc = self._run_stop_collection.find_one(
            {'run_start': run_start_uid})
        # It is acceptable to return None if the document does not exist.
        if doc is not None:
            doc.pop('_id')
        return doc

    def _get_event_descriptors(self, run_start_uid):
        results = []
        cursor = self._event_descriptor_collection.find(
            {'run_start': run_start_uid},
            sort=[('time', pymongo.ASCENDING)])
        for doc in cursor:
            doc.pop('_id')
            results.append(doc)
        return results

    def _get_event_cursor(self, descriptor_uid, skip=0, limit=None):
        cursor = (self._event_collection
                  .find({'descriptor': descriptor_uid},
                        sort=[('time', pymongo.ASCENDING)]))
        descriptor = self._event_descriptor_collection.find_one(
            {'uid': descriptor_uid})
        cursor.skip(skip)
        if limit is not None:
            cursor = cursor.limit(limit)
        external_keys = {k for k, v in descriptor['data_keys'].items()
                         if 'external' in v}
        for doc in cursor:
            doc['filled'] = {k: False for k in external_keys}
            doc.pop('_id')
            yield doc

    def _get_event_count(self, descriptor_uid):
        return self._event_collection.count_documents(
            {'descriptor': descriptor_uid})

    def _get_resource(self, uid):
        doc = self._resource_collection.find_one(
            {'uid': uid})
        if doc is None:
            raise ValueError(f"Could not find Resource with uid={uid}")
        doc.pop('_id')
        return doc

    def _lookup_resource_for_datum(self, datum_id):
        doc = self._datum_collection.find_one(
            {'datum_id': datum_id})
        if doc is None:
            raise ValueError(f"Could not find Datum with datum_id={datum_id}")
        return doc['resource']

    def _get_datum_cursor(self, resource_uid):
        cursor = self._datum_collection.find({'resource': resource_uid})
        for doc in cursor:
            doc.pop('_id')
            yield doc

        self._schema = {}  # TODO This is cheating, I think.

    def _make_entries_container(self):
        return _Entries(self)

    def __len__(self):
        return self._run_start_collection.count_documents(self._query)

    def _close(self):
        self._client.close()

    def search(self, query):
        """
        Return a new Catalog with a subset of the entries in this Catalog.

        Parameters
        ----------
        query : dict
            MongoDB query.
        """
        if self._query:
            query = {'$and': [self._query, query]}
        cat = type(self)(
            metadatastore_db=self._metadatastore_db,
            asset_registry_db=self._asset_registry_db,
            query=query,
            handler_registry=self.filler.handler_registry,
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


intake.registry['mongo_metadatastore'] = BlueskyMongoCatalog
