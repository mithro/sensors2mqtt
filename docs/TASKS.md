# Port Monitoring + PoE Control — Task Tracker

## Part 1: Expand Sensor Collector — COMPLETE

- [x] Step 1: Fix state topic retention (all collectors) — 5dad2d7
- [x] Step 2: Add origin dict + fix SensorDef.state_class default — 056eedb
- [x] Step 3: Add VLAN name lookup — 2a4f8c3
- [x] Step 4: Add LLDP neighbor lookup (dedicated parser) — 3f1a1c3
- [x] Step 5: Add port_count to SwitchModel + per-port state walks — 4ef8a10
- [x] Step 6: Per-port state topics + per-port discovery + --once flag — f2f161e
- [x] Step 7: Clean up old retained MQTT messages — d28e7d0
- [x] Step 8: Capture fixture data + tests — 0098859
- [x] Review fix: add state_class + origin to IPMI SDR collector — 32098c7
- [x] Code review checkpoint (Part 1 complete) — 32098c7

Live test results (--once mode):
  sw-netgear-m4300-24x: 4 hw sensors + 144 port sensors (24×6)
  sw-netgear-gsm7252ps-s2: 48 hw sensors + 456 port sensors (52×~9)
  sw-netgear-s3300-1: 53 hw sensors + 456 port sensors (52×~9)

## Part 2: PoE Control Service — COMPLETE

- [x] Step 1: Add write_community to SwitchConfig — 4613cc2
- [x] Steps 2-7: Full control service (toggle, cycle, force override, discovery, availability) — 96e3239
- [x] Step 8: Tests (40 tests) + systemd service file — d0a1312
- [x] Code review checkpoint (Part 2 complete) — 8479d1c
- [x] Review fixes: thread safety, shutdown state, cycle flow — c347ae6

Control service live test (--once mode):
  sw-netgear-gsm7252ps-s2: 144 control entities, 48 ports polled
  sw-netgear-s3300-1: 144 control entities, 48 ports polled
  (M4300 correctly excluded — no PoE, no write_community)

## Cross-cutting — COMPLETE

- [x] Add --once flag to SNMP collector — f2f161e
- [x] Add --once flag to hwmon and ipmi_sdr collectors — e769f3a
- [x] Per-switch live verification (M4300, GSM7252PS-S2, S3300-1) — verified
- [x] HA entity verification — switch/button/force discovery retained, correct payloads
- [x] MQTT retention verification — state topics NOT retained, discovery IS retained

## Test Summary

- 136 total tests (96 snmp + 40 snmp_control)
- ruff lint clean
- All 3 switches live-tested in --once mode
- Control service live-tested against both PoE switches
