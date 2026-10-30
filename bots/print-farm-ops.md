# 3D Print Farm & Slicer Queue Ops Auditor

Canonical page: https://botappstore.com/bots/print-farm-ops.html

3D Print Farm & Slicer Queue Ops — Catch the wrong-temperature plate, the empty spool and the money-losing SKU before the printers run.

Read-only, offline 3D print farm auditor for Grok Bot ($29 · card): audits OrcaSlicer, Bambu Studio and PrusaSlicer .3mf / .gcode files for nozzle, temperature, infill and enclosure safety (PLA-CF, PAHT-CF, PETG, TPU), tracks filament spool BOM and drying hygiene, computes per-part unit economics and margins, and schedules multi-printer production queues from storefront SKU velocity.

## When to use

Use when an agent needs to check slicer files and plan a print queue offline.

## Price

- SKU: `print-farm-ops`
- Price: $29 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "print-farm-ops", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### Which slicers and file types does it read?

OrcaSlicer, Bambu Studio and PrusaSlicer / SuperSlicer projects (.3mf and .gcode.3mf) and plain .gcode files with slicer comment blocks. It reads nozzle diameter and type, layer height, wall loops, infill density and pattern, filament type, nozzle and bed temperatures, print time and filament grams per plate. 3MF archives are opened with hardened ZIP limits (entry count, per-entry and total size, compression ratio and path traversal), and an unsafe archive is rejected before parsing.

### Does it connect to my printers or start prints?

No. It is 100% offline and read-only: no Bambu Cloud, MQTT, Moonraker, Klipper, OctoPrint or PrusaLink call, no G-code is sent, nothing is started, stopped or heated, and it never modifies a .3mf or .gcode file. The only file it creates besides the report and state file is the --queue-csv output, which refuses any input path or .3mf / .gcode target.

### What tables does it need for the spool, margin and queue buckets?

Optional CSV or JSON files: --spools (spool_id, material, remaining_g, reorder_threshold_g, dried_at, cost per kg and more), --orders or --inventory (sku, plate_file, grams_per_unit, print_hours_per_unit, units_per_plate, on_hand, open_orders, daily_sales_velocity, lead_time_days, sale_price_usd, marketplace fee and shipping) and --printers (nozzle, enclosure, temperature limits, capacity hours). Header names are case-insensitive. Cost inputs that are missing are reported as findings instead of guessed silently.

### How is the production queue scheduled?

SKUs are tiered by days of supply: BACKORDER, then STOCKOUT_RISK (days of supply below lead time), then SAFETY_REPLENISH. Plates are assigned greedily to the printer that finishes earliest, honouring sliced nozzle diameter, temperature limits and enclosure needs, and the result is written with --queue-csv. It does not enforce printer model, because plate G-code is slicer and printer specific. Jobs no online printer can run raise NO_COMPATIBLE_PRINTER_FOR_JOB.

### What does delta mode watch?

With --state PATH --delta the auditor stores spool levels, SKU days of supply and tier, and per-plate slicer settings hashes. Later runs report SPOOL_DEPLETED, SKU_DAYS_OF_SUPPLY_DROP and PROFILE_DRIFT_DETECTED (HIGH when filament type, nozzle temperature, nozzle diameter or nozzle type changed). The first run is a baseline, and a degraded run never overwrites the state.

### What does it refuse to do?

It refuses printer credentials, API keys, seed phrases and LAN access codes in arguments and tables (exit 10, value never echoed); a slicer file that embeds a printhost API key raises EMBEDDED_PRINTER_CREDENTIAL without showing it. It will not connect to hardware, send G-code, bypass thermal-runaway protection or silently edit a project file. Findings are heuristic checks of slicer metadata, not engineering certification or a substitute for hardware fire-safety protection, and cost and margin figures are arithmetic on your inputs, not financial advice.
