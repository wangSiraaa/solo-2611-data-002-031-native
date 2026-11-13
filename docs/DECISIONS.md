Interpretation notes: where the task statement needed a decision
==============================================================

1. **URL identity vs network address** are stored in two columns
   (`urls.canonical` and `urls.request_url`).  The identity removes the
   fragment, lower-cases scheme/host, removes the scheme's default port,
   resolves dot segments, decodes percent escapes of unreserved octets only,
   and keeps the query byte-for-byte (order, duplicates and `?`-vs-no-query are
   all meaningful).  `%2F` is a reserved escape and never becomes `/`.

2. **Details vs pages.**  A directory detail is a page that declares a stable
   `item_id` (`<meta name="item_id">` or `data-item-id`).  Other successful
   HTML pages are normal pages: they are parsed, their links are followed, but
   they do not create a `details` row.  Details are de-duplicated by `item_id`
   (unique partial index) *and* by final response identity, so N entry points
   (fragments, repeated queries, redirect aliases, cross-site links) yield one
   detail row.

3. **robots.txt scope implemented:** product-token group selection with
   case-insensitive prefix matching of our token (`CoopDirectoryBot`), merging
   of all matching groups, `*` group only when nothing matches, no group =>
   allow.  Longest match wins with equal-length Allow beating Disallow; `*` is a
   wildcard and a trailing `$` anchors the end; `*`/`$` in mid-pattern are
   literal.  Unreserved percent escapes are decoded before comparison.  2xx =>
   parse; 4xx (except 429) => no rules; 429/5xx/connect failure => retry a few
   times then the site is *blocked*, never treated as allow.  Rules are frozen
   in SQLite for the task and reused by `resume`.  robots.txt itself is
   implicitly allowed, and `Disallow: /` never blocks robots.txt.

4. **Cross-origin redirects.**  Both origins must be configured; every hop
   re-checks scope and the *target* origin's robots rules before the request
   is sent.  Refused targets are recorded with `out_of_scope` / `robots_denied`
   and a refusal detail; the target URL is never requested.

5. **Budget.**  The budget counts page GETs including redirect hops whose
   target is a fresh identity.  robots.txt requests are not page requests.
   When the budget is hit the remaining `pending`/`retry_wait` rows are marked
   `over_budget`, the run reports the exact unprocessed count, and the task is
   NOT reported complete.

6. **Retries.**  `max_attempts` counts page GET attempts for a URL (a redirect
   hop is one attempt of the redirecting row).  A row waiting for a retry is
   `retry_wait` with a `not_before` timestamp, never `done`; it is resumed when
   due, and `resume` picks it up across process restarts.  Retry-after on 429/503
   is honoured up to 30s.

7. **Pause/restart.**  `pause` writes a flag; each worker finishes its current
   page or requeues its claimed row, then the run stops with only `pending`/
   `retry_wait` rows left.  On any start, rows still marked `in_flight` are
   requeued with a diagnostic — they are never counted as successes.

8. **Freshness of robots on resume** follows the task wording ("frozen for this
   task"): persisted `ready` rules are reused instead of re-fetched; a
   persisted `blocked` origin stays skipped.
