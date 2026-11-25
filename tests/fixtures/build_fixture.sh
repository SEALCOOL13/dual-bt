#!/usr/bin/env bash
# Rebuilds tests/fixtures/SYSTEM: a fake Windows SYSTEM hive with three classic
# devices and one BLE device under controller AA:BB:CC:00:00:01. All keys are made up.
# Needs hivexsh (libhivex-bin) and an empty base hive, e.g. hivex's images/minimal:
#   ./build_fixture.sh /path/to/minimal
set -euo pipefail
cd "$(dirname "$0")"
cp "$1" SYSTEM
hivexsh -w SYSTEM <<'EOF'
add Select
cd Select
setval 1
Current
dword:0x1
cd \
add ControlSet001
cd ControlSet001
add Services
cd Services
add BTHPORT
cd BTHPORT
add Parameters
cd Parameters
add Devices
add Keys
cd Devices
add 112233445504
cd 112233445504
setval 1
Name
hex:3:54,65,73,74,20,47,61,6d,65,70,61,64,00
cd ..
cd ..
cd Keys
add aabbcc000001
cd aabbcc000001
setval 4
CentralIRK
hex:3:01,02,03,04,05,06,07,08,09,0a,0b,0c,0d,0e,0f,10
112233445501
hex:3:aa,bb,cc,dd,ee,ff,00,11,22,33,44,55,66,77,88,99
112233445502
hex:3:ca,fe,ba,be,ca,fe,ba,be,ca,fe,ba,be,ca,fe,ba,be
112233445503
hex:3:11,11,11,11,22,22,22,22,33,33,33,33,44,44,44,44
add 112233445504
cd 112233445504
setval 7
LTK
hex:3:00,11,22,33,44,55,66,77,88,99,aa,bb,cc,dd,ee,ff
KeyLength
dword:0x10
ERand
hex:11:88,77,66,55,44,33,22,11
EDIV
dword:0x1234
IRK
hex:3:a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,aa,ab,ac,ad,ae,af
AddressType
dword:0x1
AuthReq
dword:0x2d
commit
EOF
echo "wrote $(pwd)/SYSTEM"
