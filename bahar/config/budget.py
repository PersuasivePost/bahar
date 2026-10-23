"""Per-request latency budget (docs/plan.md §3). p50 target 30 s; the hard budget leaves room
for a cold Overpass context fetch. Generation here is ~25-29 tok/s, so a full composer answer
(<= 200 tokens) takes ~8-10 s including the facts-block prompt."""

from typing import Final

REQUEST_BUDGET_S: Final = 45.0  # deadline for the whole /ask, from request start
REQUEST_HARD_TIMEOUT_S: Final = 70.0  # the API gives up here with status=error

# Stage timeouts (asyncio.wait_for). Each fetcher has its own HTTP timeouts and deadlines
# inside; these only guard against a stage hanging as a whole.
PARSE_TIMEOUT_S: Final = 20.0
GATHER_TIMEOUT_S: Final = 40.0  # geocode (<= 4 Nominatim calls at 1 req/s) + venues + parallel
ENGINE_TIMEOUT_S: Final = 15.0
# Stations only refine the CAMS background (bias correction), so they never hold up the
# answer: past this, the query continues without the correction (degraded).
STATIONS_TIMEOUT_S: Final = 5.0

# Composer mode by the time left when composing starts.
FULL_COMPOSER_MIN_S: Final = 10.0  # below: summary mode (recommendation + why only)
COMPOSER_MIN_S: Final = 4.0  # below: template answer, no SLM
RETRY_MIN_S: Final = 15.0  # a validator failure is retried only with this much left
