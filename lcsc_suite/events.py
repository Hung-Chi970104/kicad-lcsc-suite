"""Worker-thread progress events, and the one function that delivers them.

``library.py`` and ``unzip_parts.py`` do long work on a background thread — a
750MB download, a multi-part unzip — and have to say how far along they are
without knowing what is listening. These are what they say it with.

The event *types* are plain value objects. They were ``wx.lib.newevent`` pairs
when the plugin ran inside KiCad, which is why each one comes with an ``EVT_``
constant: wx needed a binder to attach a handler to. Nothing binds them now, but
they are kept because they are what ``post()``'s destination switches on, and
because renaming a dozen constants to remove a suffix is churn with no reader.
"""


class _Event:
    """A named bag of keyword arguments, delivered to a listener."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def _new_event():
    """Return an ``(event type, binder)`` pair.

    The binder is a bare sentinel. It exists so the tuple shape below is the one
    every caller was written against; it is never dereferenced.
    """
    return _Event, object()


DownloadStartedEvent, EVT_DOWNLOAD_STARTED_EVENT = _new_event()
DownloadProgressEvent, EVT_DOWNLOAD_PROGRESS_EVENT = _new_event()
DownloadCompletedEvent, EVT_DOWNLOAD_COMPLETED_EVENT = _new_event()

UnzipCombiningStartedEvent, EVT_UNZIP_COMBINING_STARTED_EVENT = _new_event()
UnzipCombiningProgressEvent, EVT_UNZIP_COMBINING_PROGRESS_EVENT = _new_event()
UnzipExtractingStartedEvent, EVT_UNZIP_EXTRACTING_STARTED_EVENT = _new_event()
UnzipExtractingProgressEvent, EVT_UNZIP_EXTRACTING_PROGRESS_EVENT = _new_event()
UnzipExtractingCompletedEvent, EVT_UNZIP_EXTRACTING_COMPLETED_EVENT = _new_event()

MessageEvent, EVT_MESSAGE_EVENT = _new_event()
AssignPartsEvent, EVT_ASSIGN_PARTS_EVENT = _new_event()
PopulateFootprintListEvent, EVT_POPULATE_FOOTPRINT_LIST_EVENT = _new_event()
UpdateSetting, EVT_UPDATE_SETTING = _new_event()
LogboxAppendEvent, EVT_LOGBOX_APPEND_EVENT = _new_event()
AssemblyEnrichmentProgressEvent, EVT_ASSEMBLY_ENRICHMENT_PROGRESS_EVENT = _new_event()
AssemblyEnrichmentCompletedEvent, EVT_ASSEMBLY_ENRICHMENT_COMPLETED_EVENT = _new_event()
PartDetailsProgressEvent, EVT_PART_DETAILS_PROGRESS_EVENT = _new_event()
PartDetailsCompletedEvent, EVT_PART_DETAILS_COMPLETED_EVENT = _new_event()
BomDataChangedEvent, EVT_BOM_DATA_CHANGED_EVENT = _new_event()


def post(destination, event) -> None:
    """Deliver a worker-thread event to ``destination``.

    The destination exposes ``post_event(event)``, which re-emits it as a Qt
    signal on the UI thread. Dispatching *here* rather than in the caller is
    what lets ``library.py`` and ``unzip_parts.py`` — pure SQLite and zip
    handling — stay free of any toolkit at all. Callers just say what happened.

    A destination without the sink is dropped rather than raising. Progress is
    advisory: a download must not fail because nothing was listening to it.
    """
    sink = getattr(destination, "post_event", None)
    if callable(sink):
        sink(event)
