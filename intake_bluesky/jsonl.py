import glob
import json
import os
import pathlib

from .in_memory import BlueskyInMemoryCatalog
from .core import lastlines


def gen(filename):
    """
    A JSONL file generator.

    Parameters
    ----------
    filename: str
        JSONL file to load.
    """
    with open(filename, 'r') as file:
        for line in file:
            name, doc = json.loads(line)
            yield (name, doc)


def get_stop(filename):
    """
    Returns the stop_doc of a Bluesky JSONL file.

    The stop_doc is always the last line of the file.

    Parameters
    ----------
    filename: str
        JSONL file to load.
    stop_doc: dict
        A Bluesky run stop document.
    """
    stop_doc = None
    lastline = list(lastlines(filename))[0]
    if lastline:
        try:
            name, doc = json.loads(lastline)
        except json.JSONDecodeError:
            ...
            # stop_doc will stay None if it can't be decoded correctly.
        if (name == 'stop'):
            stop_doc = doc
    return stop_doc


class BlueskyJSONLCatalog(BlueskyInMemoryCatalog):
    name = 'bluesky-jsonl-catalog'  # noqa

    def __init__(self, paths, *,
                 handler_registry=None, query=None, **kwargs):
        """
        This Catalog is backed by a newline-delimited JSON (jsonl) file.

        Each line of the file is expected to be a JSON list with two elements,
        the document name (type) and the document itself. The documents are
        expected to be in chronological order.

        Parameters
        ----------
        paths : list
            list of filepaths
        handler_registry : dict, optional
            Maps each asset spec to a handler class or a string specifying the
            module name and class name, as in (for example)
            ``{'SOME_SPEC': 'module.submodule.class_name'}``.
        query : dict, optional
            Mongo query that filters entries' RunStart documents
        **kwargs :
            Additional keyword arguments are passed through to the base class,
            Catalog.
        """
        # Tolerate a single path (as opposed to a list).
        if isinstance(paths, (str, pathlib.Path)):
            paths = [paths]
        self.paths = paths
        self._filename_to_mtime = {}
        super().__init__(handler_registry=handler_registry,
                         query=query,
                         **kwargs)

    def _load(self):
        for path in self.paths:
            for filename in glob.glob(path):
                mtime = os.path.getmtime(filename)
                if mtime == self._filename_to_mtime.get(filename):
                    # This file has not changed since last time we loaded it.
                    continue
                self._filename_to_mtime[filename] = mtime
                with open(filename, 'r') as file:
                    try:
                        name, start_doc = json.loads(file.readline())
                    except json.JSONDecodeError:
                        if not file.readline():
                            # Empty file, maybe being written to currently
                            continue
                stop_doc = get_stop(filename)
                self.upsert(gen, start_doc, stop_doc, (filename,), {})

    def search(self, query):
        """
        Return a new Catalog with a subset of the entries in this Catalog.

        Parameters
        ----------
        query : dict
        """
        if self._query:
            query = {'$and': [self._query, query]}
        cat = type(self)(
            paths=self.paths,
            query=query,
            handler_registry=self.filler.handler_registry,
            name='search results',
            getenv=self.getenv,
            getshell=self.getshell,
            auth=self.auth,
            metadata=(self.metadata or {}).copy(),
            storage_options=self.storage_options)
        return cat
