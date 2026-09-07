"""Background workers that run inside the ai-core process.

uvicorn runs a single worker in this deployment, so one instance of each
worker exists per container. Every worker is written so that a second
instance (another container, or ``--workers`` raised by mistake) partitions
the work instead of duplicating it.
"""
