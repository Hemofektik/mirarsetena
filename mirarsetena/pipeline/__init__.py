"""Satellite data pipelines: catalog, windows, indices, rendering."""


class PipelineError(RuntimeError):
    """A processing pipeline could not produce its output."""
