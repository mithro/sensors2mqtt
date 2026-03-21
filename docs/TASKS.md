# Port Monitoring + PoE Control — Task Tracker

## Part 1: Expand Sensor Collector

- [x] Step 1: Fix state topic retention (all collectors) — 5dad2d7
- [x] Step 2: Add origin dict + fix SensorDef.state_class default — 056eedb
- [x] Step 3: Add VLAN name lookup — 2a4f8c3
- [x] Step 4: Add LLDP neighbor lookup (dedicated parser) — 3f1a1c3
- [x] Step 5: Add port_count to SwitchModel + per-port state walks — 4ef8a10
  - Live tested against all 3 switches — M4300 (24 ports), GSM7252PS (52), S3300 (52)
  - Index alignment verified: ifOperStatus, ifHighSpeed, dot1qPvid all use same port indices
- [ ] Step 6: Per-port state topics + multi-component discovery
- [ ] Step 7: Clean up old retained MQTT messages
- [ ] Step 8: Capture fixture data + tests for new walks
- [ ] Code review checkpoint (Part 1 complete)

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

- [ ] Add --once flag to all collectors
- [ ] Per-switch live verification (M4300, GSM7252PS-S2, S3300-1)
- [ ] HA entity verification
- [ ] MQTT retention verification
