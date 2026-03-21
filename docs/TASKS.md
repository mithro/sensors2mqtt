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
- [ ] Code review checkpoint (Part 1 complete)

Live test results (--once mode):
  sw-netgear-m4300-24x: 4 hw sensors + 144 port sensors (24×6)
  sw-netgear-gsm7252ps-s2: 48 hw sensors + 456 port sensors (52×~9)
  sw-netgear-s3300-1: 53 hw sensors + 456 port sensors (52×~9)

## Part 2: PoE Control Service

- [ ] Step 1: Add write_community to SwitchConfig
- [ ] Step 2: Create snmp_control module skeleton
- [ ] Step 3: Port state polling + control availability
- [ ] Step 4: HA switch/button entity discovery
- [ ] Step 5: Toggle command handler (threaded)
- [ ] Step 6: Power cycle handler (poll-based)
- [ ] Step 7: Force override handler (retained state)
- [ ] Step 8: Systemd service + tests
- [ ] Code review checkpoint (Part 2 complete)

## Cross-cutting

- [x] Add --once flag to SNMP collector — f2f161e
- [ ] Add --once flag to hwmon and ipmi_sdr collectors
- [ ] Per-switch live verification (M4300, GSM7252PS-S2, S3300-1)
- [ ] HA entity verification
- [ ] MQTT retention verification
