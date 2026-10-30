# Humanoid & Fleet Telemetry Inspector — Robot Logs

Canonical page: https://botappstore.com/bots/robotics-telemetry-inspector.html

Humanoid & Fleet Telemetry Inspector — Find the overheating knee, the weak cell and the dropped topic before the robot runs again.

Read-only, offline robot and fleet telemetry auditor for Grok Bot ($49 · card): actuator, joint and harmonic-drive duty cycles cross-checked against URDF/SDF limits and CAN logs, 16S LiFePO4 / LiPo BMS cell-delta and sag analysis, ROS 2 bag topic-rate, QoS, clock and TF-tree integrity, and PX4 / ArduPilot EKF, vibration, GNSS and ESC flight readiness, with a preventive-maintenance checklist.

## When to use

Use when an agent needs to inspect humanoid or fleet telemetry against model limits.

## Price

- SKU: `robotics-telemetry-inspector`
- Price: $49 one-time (USD)

## How an agent buys

- Card, Apple Pay or Google Pay (default): `POST https://www.wattcoin.org/api/v1/agents/license/stripe-checkout` with body `{"sku": "robotics-telemetry-inspector", "storefront": "botappstore"}`. It returns `checkout_url`, a hosted page where a human completes payment.
- License lookup after payment: `GET https://www.wattcoin.org/api/v1/agents/license/stripe-session/{session_id}`.
- Price check without creating an order: add `"dry_run": true` to the quote body (`POST https://www.wattcoin.org/api/v1/agents/license/quote`).
- Full machine-readable catalog: https://botappstore.com/catalog.json
- Support, refunds and privacy: https://botappstore.com/contact.html , https://botappstore.com/privacy.html

Refunds are available within 15 days of purchase. When a refund is issued, the license is revoked.

## FAQ

### Which robot and fleet files does it read?

Actuator and joint logs (CSV or JSON), 16S LiFePO4 / LiPo BMS exports (CSV or JSON), ROS 2 bags (rosbag2 SQLite .db3, .mcap, and metadata.yaml or JSON summaries), PX4 .ulg and ArduPilot .bin / CSV / JSON flight logs, URDF and SDF models, and candump-style CAN / RS485 text logs or CSV / JSON bus exports. Files can be passed as positional paths, with --dir, or as .zip archives; the role of each file is auto-detected or set with --actuators, --bms, --rosbag, --flight-log, --urdf and --can-log.

### Does it connect to my robot, CAN bus or BMS?

No. It is 100% offline and read-only: it opens no CAN socket, serial port, ROS graph, MAVLink link or network connection, and it never issues motor or actuator commands, CAN frames, MAVLink parameter writes or BMS register writes. It never modifies an input file; --state, --out and --maintenance-out only write new files and refuse any input path.

### What do I get besides findings?

A preventive-maintenance and flight-readiness checklist: priority-ordered NO-GO, ACTION_REQUIRED, SCHEDULE, OVERDUE and DUE_SOON rows with a due-by horizon. The report prints a summary and --maintenance-out PATH writes the full checklist as a new CSV. It is a heuristic gate over the logs you supplied, not an airworthiness or safety certification.

### How does the ROS 2 bag audit know about subscriber QoS?

A bag records only publisher QoS, so QOS_INCOMPATIBLE_MISMATCH needs an expectations file: a second --rosbag JSON or YAML with topics[].expected_hz, required and qos.subscriber and no message counts. It is applied to bags that share a topic, or to the only bag supplied. A required topic that is absent from the bag counts as zero messages and is CRITICAL. Compressed MCAP chunks (zstd / lz4) are not decoded: you get summary statistics and a degraded-source note.

### What does delta mode watch?

With --state PATH --delta the inspector stores a snapshot and reports four event types on later runs: BMS_CELL_DELTA_SPIKE (cell spread up 25 mV, HIGH at +50 mV or 80 mV total), ACTUATOR_THERMAL_DRIFT (stator up 10 C, HIGH at +20 C), SOH_DEGRADATION_STEP (state of health down 2 points, HIGH at 5) and TOPIC_RATE_REGRESSION (rate down 20%, HIGH at 50%), plus new, changed and resolved findings. Degraded runs never overwrite the snapshot, which is written 0600.

### What does it refuse to do?

It refuses credentials in arguments, CSV / JSON table cells and candump logs: fleet API tokens, passwords, seed phrases, MAVLink signing keys, ROS 2 DDS security private keys, Wi-Fi passwords and SSH keys exit 10 and are never echoed. A credential embedded in a bag, MCAP, ULog, URDF or YAML metadata record raises EMBEDDED_TELEMETRY_CREDENTIAL (HIGH) with the value redacted, so the leaking file can still be audited. Symlinks given as input exit 10; symlinks found by --dir are skipped and reported as degraded sources (exit 11). It will not command hardware, override an interlock, or certify airworthiness or functional safety.
