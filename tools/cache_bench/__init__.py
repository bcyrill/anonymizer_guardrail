"""Cache effectiveness benchmark.

In-process driver that exercises `Pipeline` across a matrix of
detector mixes × cache configurations × conversation lengths to
measure how much each caching layer actually buys you.

Why in-process: we want to isolate cache behaviour from HTTP
serialisation noise. The Pipeline is the unit under test;
gliner-pii / privacy-filter are still real HTTP services because
their detection cost is what caching avoids. Redis is real too
(via fakeredis or a configured URL).

Lives outside the production wheel — same pattern as
`tools/detector_bench/`. Run via `scripts/cache_bench.sh` or
`python -m tools.cache_bench`.
"""
