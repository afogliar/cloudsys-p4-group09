"""p4edge: edge runtime for CloudSys Project 4 (Group 09).

Three services run from this package on each edge VM (2 logical devices):

- ``python -m p4edge.replayer``   -> replay/clock + meters/<id>/readings
- ``python -m p4edge.forecaster`` -> meters/<id>/forecast (retained) + local model adoption
- ``python -m p4edge.monitor``    -> meters/<id>/daily + POST /v1/metrics and /v1/lowperf

Simulated time is naive Swiss local time, matching the meter Parquet files.
"""
