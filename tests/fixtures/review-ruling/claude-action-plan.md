# Claude action plan (fixture)

## Doing it

- Tidy the README wording in `docs/intro.md`.

## Decisions for you: 3

## Needs you

### 1. Pick the cache invalidation strategy

- **Where**: `src/cache.py:12`
- **Why this is yours**: changes what callers can rely on.
- **The finding**: the cache can serve stale data after a write.
- **Options**:
  - **A — invalidate on write** (recommended): drop entries on every write · Pro: fresh reads · Con: more writes
  - **B — TTL only**: expire entries after 60 seconds · Pro: simple · Con: a staleness window remains
  - **Leave as-is**: keep the current behaviour · Pro: no change now · Con: stale reads remain
- **Recommendation**: A, because freshness matters more than write cost here.
- **STATUS**: pending-decision
- **DECISION**: _(empty until STATUS changes)_

### 2. Rename the legacy module

- **Where**: `src/old.py`
- **Why this is yours**: touches the public import path.
- **The finding**: the module name no longer matches its contents.
- **Options**:
  - **X — keep the name**: no change · Pro: no churn · Con: the name stays misleading
  - **Y — rename it**: rename to `src/parser.py` · Pro: clarity · Con: import churn
- **Recommendation**: Y
- **STATUS**: pending-decision
- **DECISION**: _(empty until STATUS changes)_

### 3. Pick the retry budget

- **Where**: `src/retry.py:4`
- **Why this is yours**: a product trade-off.
- **The finding**: retries can multiply load during an outage.
- **Options**:
  - **Two — two retries**: low load · Pro: bounded · Con: fewer recoveries
  - **Five — five retries**: more recoveries · Pro: resilient · Con: more load
- **Recommendation**: Two
- **STATUS**: pending-decision
- **DECISION**: _(empty until STATUS changes)_

## Needs measurement: 1

### 4. Is the hot path actually slow?

- **Where**: `src/hot.py`
- **Command**: `python3 bench/hot.py --runs 20`
- **Resolves via**: the median runtime in the output
- **STATUS**: pending-measurement
- **DECISION**: _(empty until STATUS changes)_
