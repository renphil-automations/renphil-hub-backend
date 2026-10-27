# Hub product-usage analytics: event catalog

What a person clicks in the Hub UI, recorded first-party in `hub_product_events`.

The source of truth is `EVENT_CATALOG` in `app/services/product_analytics_service.py`; this page mirrors it for reviewers. The frontend sender is `src/lib/productAnalytics.ts` in `renphil-hub-frontend`.

**Not the same data as:**
- **Agent / MCP telemetry** (`bot_telemetry_*`, shown under Bot Management → *Analytics*). That data describes Agent runs; this data describes UI clicks. They are stored, counted and exported separately and are never summed.
- **PostHog** (`lib/posthog.ts`). That is third-party pageviews and autocapture. It is untouched by this feature.

## Events: 17 requested actions, 18 events

Event names are stable, so rename only with a migration of historical rows. `component_type` and `action` are derived server-side from the name.

| # | Requested action | Event name | component_type / action | Recorded when | Default allowed |
|---|---|---|---|---|---|
| 1 | Navigation tab | `navigation_tab.selected` | navigation_tab / select | Sidebar nav tab clicked; only if the switch proceeds past the unsaved-changes guard | yes |
| 2 | Root tab | `root_tab.selected` | root_tab / select | Root pill clicked; when the switch executes | yes |
| 3 | View tab under a root | `view_tab.selected` | view_tab / select | Tab variant (`tab_kind=variant`) or first-level nav-tree child (`tab_kind=child`) | yes |
| 4 | Sub-tab under a view tab | `sub_tab.selected` | sub_tab / select | SGS sub-tab (`tab_kind=sgs`) or deeper nav-tree child (`tab_kind=child`, `depth≥2`) | yes |
| 5 | Link component | `link.clicked` | link / open_link | Link component item (`item_index`, `link_style`) | – |
| 6 | BlockNote tab | `blocknote_tab.selected` | blocknote_tab / select | Super Block Note tab | yes |
| 7 | Thread item | `thread_item.opened` | thread / open_thread | Thread row opened (thread id only) | – |
| 8 | Clickable Airtable metric card | `airtable_metric_card.opened` | airtable_metric / open_modal | Only cards whose click action opens the records modal. Link-action and plain cards are **not** instrumented | – |
| 9 | Quick Actions link | `quick_action_link.clicked` | quick_actions / open_link | Quick Actions link | – |
| 10 | Detail Cards "See Details" | `detail_card.details_opened` | detail_cards / see_details | "See details" (card id only) | – |
| 11 | Button component | `button.clicked` | button / open_link | Button with a link | – |
| 12 | Calendar "Add Myself" | `calendar.add_myself` | calendar / add_myself | After the server confirms. "Remove myself" is not tracked | – |
| 13 | Booking "Schedule Now" | `booking.schedule_now` | booking / schedule_now | Booking button | – |
| 14 | Resource Carousel arrow / open document | `resource_carousel.document_opened` | resource_carousel / open_document | The arrow- or button-style open-document link (`item_index`, `button_style`) | – |
| 14 | (same request) | `resource_carousel.scrolled` | resource_carousel / scroll | The previous/next carousel arrows (`direction`) | – |
| 15 | Threads Management Pending/History | `threads_management.tab_selected` | threads_management / select_tab | `view` = pending / history | – |
| 16 | Revisions Management Pending/History | `revisions_management.tab_selected` | revisions_management / select_tab | `view` = pending / history | – |
| 17 | Profile "Go to Profile" | `profile_menu.go_to_profile` | profile_menu / go_to_profile | Profile menu item | – |

**Why 18 events:** request 14, "Resource Carousel arrow / open-document action", names two different controls. The arrow-style *open document* link opens a document; the previous/next *scroll* arrows only move the carousel. They are kept as two events so document opens are never inflated by scrolling. Link components (5), Quick Actions (9), carousel document opens (14) and carousel scrolls (14) are four distinct `(component_type, action)` pairs.

## Row schema (`hub_product_events`)

No free text is stored. Titles and labels of tabs, links, buttons, cards, resources, documents and threads are admin- or user-authored and can contain personal or sensitive information, so they are not part of the schema. Anything else a client sends is ignored.

| Column | Source | Notes |
|---|---|---|
| `event_id` | client UUID | Unique. A retried batch is ingested once (`ON CONFLICT DO NOTHING`) |
| `event_name`, `component_type`, `action` | name from client; the rest from the catalog | Unknown names are rejected |
| `trigger` | client | `click` (deliberate) or `default` (opened by a click). Defaults are allowed only for selection events |
| `cascade_id` | client | On a default: the `event_id` of the click that opened it |
| `hub_user_id`, `actor_email` | **server (JWT)** | The client cannot set either |
| `environment` | **server (`HUB_ENVIRONMENT`)** | `local` / `sandbox` / `production`. Recorded as `unconfigured` when unset |
| `page_route` | client, re-templated by the server | The route **template**, never the path. Hub URLs are built from tab titles (`/<nav tab>/<root tab>/<view tab>/<sub-tab>` slugs), so every segment that is not a built-in section name becomes a placeholder: `/finance/q3-board/jane-doe` → `/:nav_tab/:tab/:tab`, `/dashboard/finance` → `/dashboard/:tab`, `/admin/bot-management` stays. Query string and fragment are dropped; full or protocol-relative URLs are rejected. The browser applies the same template before sending; the server re-applies it as the authority |
| `component_id` | client, **verified by the server** | Stored only if it is the link of an existing component whose widget type matches the event (a mirror counts as the widget it shows) **and the reporting user can currently view it**. Otherwise dropped |
| `target_id` | client, **verified by the server** | Stored only if it passes the event's rule (below) **and the reporting user can currently view it**. Otherwise dropped |
| `properties` | client, allow-listed | `tab_kind`, `reselect`, `depth`, `destination_kind` (`external`/`internal`/`email`/`scheme`/`none`; never the host, path or address), `item_index`, `button_style`, `link_style`, `direction`, `view`. Enums, small integers and booleans only; all other keys are dropped |
| `occurred_at` | client clock | Replaced by `received_at` if more than 24h in the past or 5 min in the future |
| `received_at` | server | |

The admin view translates these codes (`tab_kind`, `view`, `destination_kind` and so on) into readable text. Names are never stored; see "Names in the admin view".

### Identifier rules

A character pattern cannot tell an opaque id from a name (`jane-doe-salary`), so ids are checked against the current Hub V2 records at ingest (`app/services/product_analytics_refs.py`, `ID_RULES`). An id that fails its rule is dropped and the event is stored without it. The response is the same either way, so ingestion cannot be used to probe for records.

| Event | `target_id` must be | `component_id` must be |
|---|---|---|
| `navigation_tab.selected` | an existing nav tab `document_id` | — (dropped) |
| `root_tab.selected` | an existing tab `document_id` with no parent | — |
| `view_tab.selected`, `sub_tab.selected` | an existing tab `document_id` | — |
| `blocknote_tab.selected` | that Super Block Note widget itself, or one of its pages | a `super_block_note` component |
| `thread_item.opened` | a positive integer id of a thread **in that widget** | a `thread` component |
| `detail_card.details_opened` | a card id present in that widget's saved cards | a `detail_cards` component |
| `threads_management.tab_selected`, `revisions_management.tab_selected` | `pending` or `history` | — |
| link, Quick Actions, button, booking, calendar, carousel, metric card | — (dropped) | a component of the matching type |
| `profile_menu.go_to_profile` | — | — |

### Access: the reporter must be able to view the id now

Existence is not enough. Every record-backed id is also checked against the **reporting user's live Hub access**, computed by the same V2 access fold (`ViewerAccess`: `RbacClosures` → `is_hub_admin` → `resolve_viewer_access`, the steps of `dependencies.get_viewer_access`) that authorizes the Hub UI request for that record:

| Id | Kept when | Same gate as |
|---|---|---|
| nav tab, root/view/sub tab | `verdict(node).view` | `nav_tabs.py`, `gridstack_service` tab reads (404 otherwise) |
| Super Block Note widget and page | `verdict(node).view` | `super_blocknote_service` workspace/page reads (a page revealed by a granted child page opens as a shell) |
| any other widget: links, button, Quick Actions, metric, calendar, booking, carousel, Detail Cards, thread widget, mirror | `is_granted(node)` | the canvas payload gate; for a mirror this already requires both the mirror and its target |
| Detail Cards card | its widget is kept | the card is part of that widget's payload |
| thread | granted on its widget, and the thread is `approved`, or authored by the reporter, or the reporter can edit the widget | `thread_service.get_thread_by_id` |

* A Hub Admin (`ViewerAccess.full_access`, i.e. `BOOTSTRAP_ADMIN_EMAILS` or a `hub_admin` role assignment) keeps every real id, exactly as the Hub shows them everything. A user who loses access stops having ids recorded on the next batch.
* A real id the reporter cannot view is dropped **exactly like a nonexistent id**: the event is still stored without it, the response body, status and length are identical, and nothing about the id, its name or the access decision is logged.
* Access is resolved at most once per batch, and only if at least one id survived the existence checks; a batch with no ids costs nothing extra. For a non-admin it is one full access fold (the same cost as one Hub tab read).
* Events already stored are not revisited if access later changes; retention removes them on schedule.

### Names in the admin view

`GET /events` adds `target_name` and `component_name`: the **current** nav tab, tab, component (widget), Super Block Note page or thread title, looked up when the All-Scopes admin reads the page. They are not written to `hub_product_events`, not in the CSV, the default Excel export or the summary (only in the opt-in per-person Excel export), not logged and not cached. An unknown or deleted id has no name and stays opaque. Detail Cards cards are never named.

## Default selections: the navigation-selection transaction

- **Opened** only by a real, tracked navigation click that changes the selection: navigation tab, root tab, view tab, sub-tab or BlockNote tab.
- **While open**, each selection that becomes active **below** the clicked level is recorded once as `trigger=default`, with `cascade_id` = the click's `event_id`. Each (level, target) is recorded at most once, so re-renders and StrictMode double effects cannot add events.
- **Closed** when any of these happens first:
  - the cascade completes: every instrumented navigation load (tab list, page content, variant list, sub-tab list, active pane content, Super Block Note tree) has settled and nothing changed for 600 ms;
  - any other user input (capture-phase `pointerdown` / `keydown`);
  - any other tracked action (Quick Action, Add myself, link, metric modal and so on);
  - back/forward (`popstate`);
  - the 8 s fallback.
- **Never opened** by page load, refresh, deep links, back/forward, hydration, state restoration or renders, so these record nothing.
- **Reselect** (clicking the already-selected item) records that click once with `properties.reselect=true`, opens no transaction and closes any open one.
- Byte-identical explicit actions within 50 ms are treated as one interaction. Deliberate repeat clicks are kept.

## Ingestion, limits and access

| Method | Path | Access |
|---|---|---|
| POST | `/v2/product-analytics/events` | Any authenticated Hub user. Body `{events:[…]}`, 1–25 items, each validated on its own. Returns `{accepted, duplicates, rejected:[{index, reason}]}` |
| GET | `/v2/product-analytics/catalog` | Admin with All Scopes (the existing Bot Management gate) |
| GET | `/v2/product-analytics/summary` | Same |
| GET | `/v2/product-analytics/events?page&page_size` | Same. `page_size` ≤ 200. Adds read-time `target_name` / `component_name` |
| GET | `/v2/product-analytics/export` | Same. CSV, ≤ 50,000 rows, formula-injection safe |
| GET | `/v2/product-analytics/export/xlsx?include_people=false&time_zone=…` | Same. Excel workbook in the Agent analytics workbook's format (see below). This is the export the admin view uses |

Filters on all reads: `date_from`, `date_to` (UTC days), `actor_email`, `page_route` (prefix of the template), `component_type`, `event_name`, `action`, `environment`, `trigger`.

**Body size.** The body is read by a bounded reader, not by the framework: a declared `Content-Length` over 64 KB is refused at once, and a body without one (chunked) is read incrementally and abandoned as soon as it passes 64 KB. Either way the answer is 413 before any JSON parsing, validation, rate-limit check or database write. A full 25-event batch is about 10 KB.

**Per-user rate limit.** Ingestion reuses the Hub's shared limiter (`RateLimitService`, Upstash Redis fixed window, the same one the Thread widget uses), with bucket `product_analytics` = **20 batch requests per user per 60 s**. The browser sends at most one scheduled batch every 5 s (12/min) of at most 25 events, plus the occasional flush when a tab is hidden, so a person never reaches it; a runaway client is capped at 500 events per minute. Over the limit, the server returns 429 with `Retry-After` and stores nothing; the browser retries later with the same `event_id`s. Quota is consumed only after a successful ingest.

Limits of this guarantee, stated precisely:
- The limiter **fails open** by design. If `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` are unset, the limit is **disabled** (a warning is logged at startup). If Upstash errors at request time, that request is allowed.
- Check and consume are two round trips, so concurrent requests can slightly exceed the limit.
- The Upstash instance is shared with other Hub limiters (see the earlier "dedicated or upgraded Upstash" deployment note).

**Fail-open in the browser.** A 429, 503, network error or any exception never reaches the user's action. 429 / 408 / 5xx / network failures are retried with backoff up to 4 attempts, reusing the same `event_id`s; other 4xx responses are dropped.

## Excel export

`GET /v2/product-analytics/export/xlsx` builds the workbook server-side with xlsxwriter, reusing the Agent analytics workbook's sheet layout (`bot_management._write_analytics_report_sheet`). Each report sheet has a native chart, its chart data and the supporting detail. The filters are the same as the other admin reads. Times use the viewer's `time_zone`; days are UTC.

| Sheet | Chart | Contents |
|---|---|---|
| Overview | Doughnut: clicks / reselects / defaults | Totals, active days, first and last day, a description of the applied filters, whether emails are included, and when the workbook was generated |
| Activity by day | Column (zero-filled, at least 14 days) | Per day: events, clicks, reselects, defaults and number of people |
| By action | Bar | Catalog description, event code, component, count and share |
| By component | Pie | Component, code, count and share |
| Top pages | Bar | Route template, count and share |
| Top people | Pie | **Only when `include_people=true`.** Email, events, clicks, reselects, defaults, number of items used, most-used item and last event |
| Visited by person | Stacked bar: each person's events by component | **Only when `include_people=true`.** A person × component matrix, then one collapsible group per person listing every tab, widget, page or thread they used, by **current name**, with type, containing widget, clicks, reselects, defaults and last visit |
| Events | — | Up to 50,000 newest rows: when, action, trigger, context, component, route, environment, user, visited name and containing widget (**those three only with `include_people=true`**), component and target ids, event code and event id |

Privacy rules:

- **Emails are off by default.** Without `include_people=true` there is no Top people sheet and no User column. A user filter is described as "one person, email not included".
- **Names only in the per-person export.** With `include_people=true` the current tab, widget, page and thread names are looked up at export time (`resolve_names`, as the admin screen does). They are never stored. The default workbook (and the CSV) contains no names; ids stay opaque. A deleted record shows as `<id> (name unavailable)`.
- **Text stays text.** The workbook disables `strings_to_formulas` and `strings_to_urls`, and every event value is written as text.

## Retention

`PRODUCT_ANALYTICS_RETENTION_DAYS` (default **396**, about 13 months, so a full year can be compared with the previous one).

- `0`, `off`, `disabled` or `none` disables purging (for a local or sandbox database you reset anyway).
- Any other value must be a whole number from 30 to 3650. Otherwise the purge refuses to run (exit code 2) and deletes nothing. The API still starts, because the setting is only parsed by the purge.

`python -m scripts.purge_product_analytics_events` deletes rows whose `occurred_at` is older than the cutoff, 5,000 rows per transaction (`--batch-size`, 100–50,000; `--max-batches` to cap one run). `--dry-run` only counts. It prints counts and the cutoff date, never row contents.

**Required in Production: run it once a day** (for example 03:15 UTC) from the deployment's scheduler: Render cron job, GitHub Actions `schedule`, or the host's cron, with the backend's normal environment. A missed day only delays deletion by a day.

## Deployment

1. Create the table: `python -m scripts.create_product_analytics_tables` (idempotent `CREATE TABLE … IF NOT EXISTS` via `create_all(checkfirst=True)`; this branch has no Alembic). When the planned shared Alembic baseline lands, its `create_all` covers this model, so no extra revision or head is needed.
2. Set `HUB_ENVIRONMENT` (`sandbox` / `production`) on each backend.
3. Make sure Upstash is configured, or the ingest rate limit is off.
4. Schedule the daily purge (above) in Production; set `PRODUCT_ANALYTICS_RETENTION_DAYS` only to change the 396-day default.
5. Optional frontend kill switch: `VITE_PRODUCT_ANALYTICS_ENABLED=false`.
