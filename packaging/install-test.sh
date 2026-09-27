#!/bin/sh
# Install the built packages into a clean Debian container and check they
# work. The "Install test" step of .github/workflows/deb.yml runs it, once per
# suite, as:
#
#   docker run --rm -v "$APT_SOURCES:/apt-sources:ro" \
#     -v "$PWD/built-debs:/debs:ro" -v "$PWD/packaging:/packaging:ro" \
#     debian:<suite> sh /packaging/install-test.sh
#
# /apt-sources is apt-repo-action build-deb's `apt-sources` output: the
# dependency repositories .github/apt-packaging.toml declares for the suite
# (paho-mqtt-bookworm, on bookworm). Its install.sh adds them, and does
# nothing for a suite with none. Without the mount, none are added.
set -eu
export DEBIAN_FRONTEND=noninteractive

if [ -e /apt-sources/install.sh ]; then
  sh /apt-sources/install.sh
fi
apt-get update
apt-get install -y --no-install-recommends /debs/*.deb

# Before anything is published: the four daemons that need config don't
# start on install but do restart on upgrade (issue #36, debian/rules).
python3 /packaging/check-maintainer-scripts.py /debs

# sensors2mqtt uses the paho 2 callback API; bookworm's own paho is 1.6.1.
dpkg-query -W python3-paho-mqtt
python3 -c '
import paho.mqtt, sys
v = paho.mqtt.__version__
print("paho-mqtt", v)
sys.exit(int(v.split(".")[0]) < 2)
'
# The same check the collectors make at startup.
python3 -c 'import paho.mqtt.client as m; m.Client(m.CallbackAPIVersion.VERSION2)'

# The Python package's version is the .deb's, less its -revision and
# ~deb<R>/~pr<P> suffixes (debian/rules).
deb=$(dpkg-query -W -f '${Version}' python3-sensors2mqtt)
py=$(python3 -c 'import sensors2mqtt; print(sensors2mqtt.__version__)')
echo "python3-sensors2mqtt $deb; sensors2mqtt.__version__ $py"
if [ "$py" != "$(echo "$deb" | sed 's/[-~].*//')" ]; then
  echo "error: sensors2mqtt.__version__ $py doesn't match the package's $deb" >&2
  exit 1
fi

# Each service's ExecStart module imports and parses its arguments.
for unit in sensors2mqtt-local sensors2mqtt-snmp sensors2mqtt-snmp-control \
            sensors2mqtt-local-control sensors2mqtt-ipmi-sensors; do
  file=/lib/systemd/system/$unit.service
  test -f "$file"
  module=$(sed -n 's|^ExecStart=/usr/bin/python3 -m \([^ ]*\).*|\1|p' "$file")
  test -n "$module"
  echo "$unit: python3 -m $module --help"
  python3 -m "$module" --help > /dev/null
done

# python3-sensors2mqtt's postinst seeds the MQTT settings, readable by root only.
test "$(stat -c %a /etc/sensors2mqtt/env)" = 600

echo "install test passed"
