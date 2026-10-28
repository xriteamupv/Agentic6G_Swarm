# Results for the IEEE Network submission

*Exported by `a6g paper export` at commit `9586a8f`. Every number below is from a result file with a manifest (rule 31); results whose commit is not this one are flagged **possibly stale** (rule 31). Network results are emulated, never measured on the campus network (rule 32). Medians with the interquartile range [Q1, Q3]; latencies with p95.*

## Manifests

| result | file | commit | models (fidelity) | network / mode / coverage | seeds | time |
|---|---|---|---|---|---|---|
| N1 outage sweep | `n1_outage_20261027T204600Z.json` | 8400afb **possibly stale** | all: fake (offline) | stand-in / always_grant / radio_dt | 10 | up to 200x |
| N4 scarcity (G13) | `n4_scarcity_20261028T064228Z.json` | 9586a8f | all: fake (offline) | emulated / capacity / radio_dt | 10 | up to 200x |
| N2 overhead | `n2_overhead_20261028T074924.json` | 9586a8f | all: fake (offline) | stand-in / always_grant / radio_dt | None | as recorded by the runs |
| N5 protocols | `n5_protocols_20261028T011810Z.json` | 428d176 **possibly stale** | all: fake (offline) | stand-in / always_grant / radio_dt | 26 | real time (rule 33) |
| e5_kpi11_fake | `e5_kpi11_fake_20261026T141839Z.json` | 6fdd63f **possibly stale** | coordinator_llm: fake (offline), onboard_vlm: fake (offline) | stand-in / always_grant / shadows | 20 | up to 50x |
| e5_kpi11_gpt-5-nano+robots | `e5_kpi11_gpt-5-nano+robots_20261026T142718Z.json` | 6fdd63f **possibly stale** | coordinator_llm: gpt-5-nano (functional), onboard_vlm: gpt-5-nano (functional) | stand-in / always_grant / shadows | 20 | up to 50x |
| e5_kpi11_fake | `e5_kpi11_fake_20261027T191202Z.json` | 8400afb **possibly stale** | coordinator_llm: fake (offline), onboard_vlm: fake (offline) | stand-in / always_grant / radio_dt | 20 | up to 50x |
| e5_kpi11_fake+capacity22 | `e5_kpi11_fake+capacity22_20261027T191520Z.json` | 8400afb **possibly stale** | coordinator_llm: fake (offline), onboard_vlm: fake (offline) | emulated / capacity / radio_dt | 20 | up to 50x |
| e5_kpi11_gpt-5-nano+robots | `e5_kpi11_gpt-5-nano+robots_20261027T205612Z.json` | 7046911 **possibly stale** | coordinator_llm: gpt-5-nano (functional), onboard_vlm: gpt-5-nano (functional) | stand-in / always_grant / radio_dt | 20 | up to 50x |
| e5_kpi11_gpt-5-nano+robots+capacity22 | `e5_kpi11_gpt-5-nano+robots+capacity22_20261027T210624Z.json` | bbc4152 **possibly stale** | coordinator_llm: gpt-5-nano (functional), onboard_vlm: gpt-5-nano (functional) | emulated / capacity / radio_dt | 20 | up to 50x |
| orders_unseen_gpt-5-nano | `orders_unseen_gpt-5-nano_20261026T143322Z.json` | 6fdd63f **possibly stale** | coordinator_llm: gpt-5-nano (functional) | stand-in / always_grant / shadows | None | world paused (who-does-what) |
| orders_unseen_gpt-5-nano | `orders_unseen_gpt-5-nano_20261026T145219Z.json` | 9f18407 **possibly stale** | coordinator_llm: gpt-5-nano (functional) | stand-in / always_grant / shadows | None | world paused (who-does-what) |
| orders_unseen_gpt-5-nano | `orders_unseen_gpt-5-nano_20261026T151144Z.json` | 722847e **possibly stale** dirty | coordinator_llm: gpt-5-nano (functional) | stand-in / always_grant / shadows | None | world paused (who-does-what) |
| orders_unseen_gpt-5-nano | `orders_unseen_gpt-5-nano_20261027T213348Z.json` | 8a75765 **possibly stale** | coordinator_llm: gpt-5-nano (functional) | stand-in / always_grant / radio_dt | None | world paused (who-does-what) |
| orders_unseen_gpt-5-nano | `orders_unseen_gpt-5-nano_20261027T214815Z.json` | 2c8d806 **possibly stale** | coordinator_llm: gpt-5-nano (functional) | stand-in / always_grant / radio_dt | None | world paused (who-does-what) |
| orders_unseen_gpt-5-nano | `orders_unseen_gpt-5-nano_20261027T220419Z.json` | 428d176 **possibly stale** | coordinator_llm: gpt-5-nano (functional) | stand-in / always_grant / radio_dt | None | world paused (who-does-what) |

## N1: coordinator outages and fleet sizes (UC2-KPI-2, -5, -6, -8)

| config | fleet | outage s | n | completed | completion s, median [IQR] | task ratio | time to resume s | resumed in outage | duplicates | operator h | msgs/robot | coord. run p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B0 | 3 | 60 | 10 | 0/10 | n/a | 0.750 | n/a | 0.00 | 0.00 | 0.0417 | 2268 [2220, 2312] | 101 [87, 112] |
| B1 | 3 | 60 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.10 | 0.0400 | 2232 [2221, 2278] | 78 [65, 104] |
| B2 | 3 | 60 | 10 | 10/10 | 1109 [1041, 1178] | 1.000 | 200 [71, 227] | 0.00 | 0.10 | 0.0167 | 596 [571, 603] | 149 [138, 162] |
| P | 3 | 60 | 10 | 10/10 | 1314 [1177, 1626] | 1.000 | 226 [188, 635] | 0.10 | 0.40 | 0.0167 | 629 [592, 700] | 141 [138, 151] |
| B0 | 3 | 180 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.10 | 0.0400 | 2247 [2210, 2309] | 98 [88, 107] |
| B1 | 3 | 180 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.10 | 0.0400 | 2225 [2211, 2290] | 82 [73, 96] |
| B2 | 3 | 180 | 10 | 10/10 | 1307 [1041, 1436] | 1.000 | 204 [181, 227] | 0.00 | 0.40 | 0.0167 | 666 [574, 736] | 173 [155, 190] |
| P | 3 | 180 | 10 | 10/10 | 1499 [1436, 1594] | 1.000 | 226 [188, 674] | 0.20 | 0.80 | 0.0167 | 739 [669, 796] | 164 [152, 178] |
| B0 | 3 | 300 | 10 | 0/10 | n/a | 0.833 | 293 [291, 296] | 0.00 | 0.10 | 0.0333 | 2218 [2159, 2303] | 144 [109, 171] |
| B1 | 3 | 300 | 10 | 0/10 | n/a | 0.750 | n/a | 0.00 | 0.10 | 0.0417 | 2235 [2212, 2303] | 65 [58, 108] |
| B2 | 3 | 300 | 10 | 10/10 | 1358 [1050, 1368] | 1.000 | 298 [294, 301] | 0.00 | 0.10 | 0.0167 | 607 [585, 620] | 172 [156, 196] |
| P | 3 | 300 | 10 | 10/10 | 1443 [1436, 1562] | 1.000 | 263 [223, 702] | 0.50 | 0.70 | 0.0167 | 741 [727, 803] | 148 [135, 157] |
| B0 | 3 | 600 | 10 | 0/10 | n/a | 0.833 | 592 [590, 595] | 0.00 | 0.10 | 0.0333 | 2213 [2160, 2306] | 139 [99, 171] |
| B1 | 3 | 600 | 10 | 0/10 | n/a | 0.750 | n/a | 0.00 | 0.10 | 0.0417 | 2249 [2216, 2313] | 55 [52, 66] |
| B2 | 3 | 600 | 10 | 10/10 | 1368 [1352, 1374] | 1.000 | 599 [595, 603] | 0.00 | 0.20 | 0.0167 | 702 [681, 772] | 151 [138, 156] |
| P | 3 | 600 | 10 | 10/10 | 1249 [1178, 1609] | 1.000 | 226 [188, 636] | 0.70 | 0.40 | 0.0167 | 631 [594, 694] | 126 [122, 134] |
| B0 | 3 | inf | 10 | 0/10 | n/a | 0.750 | n/a | 0.00 | 0.00 | 0.0417 | 2270 [2215, 2308] | 143 [128, 161] |
| B1 | 3 | inf | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.10 | 0.0400 | 2219 [2206, 2267] | 109 [92, 131] |
| B2 | 3 | inf | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.10 | 0.0400 | 2224 [2208, 2284] | 137 [132, 165] |
| P | 3 | inf | 10 | 10/10 | 1391 [1178, 1635] | 1.000 | 226 [188, 636] | 1.40 | 0.50 | 0.0167 | 615 [578, 686] | 154 [138, 171] |
| B0 | 5 | 60 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.00 | 0.0400 | 2839 [2795, 2873] | 174 [162, 179] |
| B1 | 5 | 60 | 10 | 0/10 | n/a | 0.733 | n/a | 0.00 | 0.30 | 0.0433 | 2788 [2740, 2799] | 143 [138, 148] |
| B2 | 5 | 60 | 10 | 10/10 | 1059 [980, 1185] | 1.000 | 63 [60, 66] | 0.00 | 0.00 | 0.0167 | 725 [671, 892] | 204 [190, 224] |
| P | 5 | 60 | 10 | 10/10 | 1053 [993, 1158] | 1.000 | 58 [15, 81] | 0.30 | 0.50 | 0.0167 | 684 [518, 897] | 201 [186, 209] |
| B0 | 5 | 180 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.00 | 0.0400 | 2836 [2795, 2865] | 174 [160, 195] |
| B1 | 5 | 180 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.30 | 0.0400 | 2786 [2725, 2803] | 140 [127, 151] |
| B2 | 5 | 180 | 10 | 10/10 | 1020 [1010, 1280] | 1.000 | 181 [180, 183] | 0.00 | 0.30 | 0.0167 | 726 [702, 875] | 228 [186, 240] |
| P | 5 | 180 | 10 | 10/10 | 1209 [1055, 1537] | 1.000 | 174 [45, 259] | 0.30 | 1.00 | 0.0167 | 790 [695, 909] | 185 [168, 205] |
| B0 | 5 | 300 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.00 | 0.0400 | 2834 [2786, 2865] | 205 [187, 232] |
| B1 | 5 | 300 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.30 | 0.0400 | 2788 [2734, 2799] | 127 [123, 131] |
| B2 | 5 | 300 | 10 | 10/10 | 1144 [1116, 1380] | 1.000 | 301 [300, 304] | 0.00 | 0.30 | 0.0167 | 833 [790, 958] | 200 [176, 212] |
| P | 5 | 300 | 10 | 10/10 | 1121 [1008, 1147] | 1.000 | 293 [75, 297] | 0.30 | 1.20 | 0.0167 | 680 [588, 728] | 169 [160, 179] |
| B0 | 5 | 600 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.00 | 0.0400 | 2830 [2798, 2867] | 170 [146, 185] |
| B1 | 5 | 600 | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.30 | 0.0400 | 2787 [2734, 2800] | 150 [124, 155] |
| B2 | 5 | 600 | 10 | 10/10 | 1440 [1375, 1678] | 1.000 | 600 [599, 603] | 0.00 | 0.30 | 0.0167 | 1061 [1046, 1196] | 184 [171, 200] |
| P | 5 | 600 | 10 | 10/10 | 1150 [1032, 1232] | 1.000 | 20 [6, 512] | 0.70 | 0.90 | 0.0167 | 706 [580, 776] | 184 [157, 199] |
| B0 | 5 | inf | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.00 | 0.0400 | 2824 [2789, 2866] | 195 [174, 243] |
| B1 | 5 | inf | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.30 | 0.0400 | 2779 [2730, 2792] | 158 [151, 201] |
| B2 | 5 | inf | 10 | 0/10 | n/a | 0.767 | n/a | 0.00 | 0.30 | 0.0400 | 2778 [2720, 2805] | 205 [182, 238] |
| P | 5 | inf | 10 | 10/10 | 1150 [1020, 1377] | 1.000 | 20 [6, 501] | 1.50 | 1.00 | 0.0167 | 713 [582, 860] | 188 [175, 232] |
| B0 | 8 | 60 | 10 | 3/10 | 400 [400, 400] | 0.883 | n/a | 0.00 | 0.00 | 0.0283 | 3118 [1204, 3124] | 246 [220, 259] |
| B1 | 8 | 60 | 10 | 3/10 | 400 [400, 400] | 0.783 | n/a | 0.00 | 0.00 | 0.0383 | 3113 [1204, 3124] | 198 [181, 227] |
| B2 | 8 | 60 | 10 | 10/10 | 514 [415, 555] | 1.000 | 62 [60, 65] | 0.00 | 0.00 | 0.0167 | 475 [427, 555] | 306 [272, 330] |
| P | 8 | 60 | 10 | 10/10 | 604 [414, 604] | 1.000 | 58 [27, 64] | 0.20 | 0.00 | 0.0167 | 500 [488, 549] | 258 [252, 272] |
| B0 | 8 | 180 | 10 | 3/10 | 400 [400, 401] | 0.883 | n/a | 0.00 | 0.00 | 0.0283 | 3116 [1291, 3121] | 238 [230, 275] |
| B1 | 8 | 180 | 10 | 3/10 | 400 [400, 401] | 0.800 | n/a | 0.00 | 0.00 | 0.0367 | 3114 [1283, 3126] | 221 [209, 239] |
| B2 | 8 | 180 | 10 | 10/10 | 606 [446, 645] | 1.000 | 182 [181, 186] | 0.00 | 0.00 | 0.0167 | 564 [499, 659] | 280 [264, 308] |
| P | 8 | 180 | 10 | 10/10 | 606 [451, 658] | 1.000 | 180 [88, 181] | 0.20 | 0.10 | 0.0167 | 549 [509, 601] | 267 [257, 272] |
| B0 | 8 | 300 | 10 | 3/10 | 400 [400, 400] | 0.883 | n/a | 0.00 | 0.00 | 0.0283 | 3113 [1358, 3115] | 253 [235, 259] |
| B1 | 8 | 300 | 10 | 3/10 | 400 [400, 400] | 0.883 | n/a | 0.00 | 0.00 | 0.0283 | 3113 [1361, 3125] | 229 [214, 232] |
| B2 | 8 | 300 | 10 | 10/10 | 680 [469, 680] | 1.000 | 302 [301, 306] | 0.00 | 0.00 | 0.0167 | 624 [560, 762] | 262 [256, 272] |
| P | 8 | 300 | 10 | 10/10 | 653 [463, 659] | 1.000 | 301 [148, 302] | 0.20 | 0.00 | 0.0167 | 563 [535, 702] | 276 [257, 280] |
| B0 | 8 | 600 | 10 | 3/10 | 400 [400, 400] | 0.883 | n/a | 0.00 | 0.00 | 0.0283 | 3111 [1554, 3120] | 247 [224, 256] |
| B1 | 8 | 600 | 10 | 3/10 | 400 [400, 401] | 0.883 | n/a | 0.00 | 0.00 | 0.0283 | 3117 [1559, 3121] | 237 [216, 249] |
| B2 | 8 | 600 | 10 | 10/10 | 905 [500, 964] | 1.000 | 605 [604, 607] | 0.00 | 0.00 | 0.0167 | 891 [764, 1024] | 281 [268, 310] |
| P | 8 | 600 | 10 | 10/10 | 650 [462, 650] | 1.000 | 224 [91, 229] | 0.70 | 0.50 | 0.0167 | 653 [604, 898] | 295 [257, 306] |
| B0 | 8 | inf | 10 | 3/10 | 400 [400, 400] | 0.883 | n/a | 0.00 | 0.00 | 0.0283 | 3105 [1171, 3115] | 258 [230, 308] |
| B1 | 8 | inf | 10 | 3/10 | 400 [400, 401] | 0.883 | n/a | 0.00 | 0.00 | 0.0283 | 3112 [1183, 3118] | 253 [235, 292] |
| B2 | 8 | inf | 10 | 3/10 | 400 [400, 400] | 0.883 | n/a | 0.00 | 0.00 | 0.0283 | 3113 [1175, 3117] | 311 [283, 358] |
| P | 8 | inf | 10 | 10/10 | 649 [463, 650] | 1.000 | 224 [91, 228] | 0.70 | 0.00 | 0.0167 | 520 [498, 529] | 260 [241, 318] |

## N4: emergency under network scarcity, G13 (UC2-KPI-3, -11)

| variant | UL capacity Mbps | fleet | n | posts held | video usable | sensing usable | oversubscription | I1/I2/I3 violations | posture -> reconciled s (wall) | escalations | KPI-3 share |
|---|---|---|---|---|---|---|---|---|---|---|---|
| reconcile | 2.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 1 [1, 1] | n/a | 0/0/0 | 0.068 (p95 0.124) | 20 | 0.111 [0.111, 0.111] |
| reconcile_off | 2.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 46.63 [46.63, 46.63] | 0/0/0 | 0.048 (p95 0.098) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 2.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 2 [1, 2] | n/a | 0/0/0 | 0.109 (p95 0.147) | 13 | 0.182 [0.114, 0.182] |
| reconcile | 4.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 2 [2, 2] | n/a | 0/0/0 | 0.080 (p95 0.122) | 10 | 0.222 [0.222, 0.222] |
| reconcile_off | 4.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 23.32 [23.32, 23.32] | 0/0/0 | 0.065 (p95 0.108) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 4.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.090 (p95 0.147) | 0 | 0.273 [0.273, 0.273] |
| reconcile | 8.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.082 (p95 0.183) | 0 | 0.333 [0.333, 0.333] |
| reconcile_off | 8.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 11.66 [11.66, 11.66] | 0/0/0 | 0.066 (p95 0.099) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 8.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.085 (p95 0.127) | 0 | 0.273 [0.273, 0.273] |
| reconcile | 12.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.092 (p95 0.177) | 0 | 0.364 [0.341, 0.364] |
| reconcile_off | 12.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 7.77 [7.77, 7.77] | 0/0/0 | 0.081 (p95 0.097) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 12.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.111 (p95 0.139) | 0 | 0.308 [0.281, 0.308] |
| reconcile | 16.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.096 (p95 0.198) | 0 | 0.400 [0.350, 0.400] |
| reconcile_off | 16.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 5.83 [5.83, 5.83] | 0/0/0 | 0.083 (p95 0.127) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 16.0 | 3 | 10 | 3 [3, 3] | 1 [1, 1] | 3 [3, 3] | n/a | 0/0/0 | 0.091 (p95 0.098) | 0 | 0.300 [0.300, 0.348] |
| reconcile | 22.0 | 3 | 10 | 3 [3, 3] | 1 [1, 1] | 3 [3, 3] | n/a | 0/0/0 | 0.087 (p95 0.152) | 0 | 0.375 [0.375, 0.375] |
| reconcile_off | 22.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 4.24 [4.24, 4.24] | 0/0/0 | 0.071 (p95 0.145) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 22.0 | 3 | 10 | 3 [3, 3] | 1 [1, 1] | 3 [3, 3] | n/a | 0/0/0 | 0.108 (p95 0.171) | 0 | 0.300 [0.300, 0.348] |
| reconcile | 44.0 | 3 | 10 | 3 [3, 3] | 1 [1, 1] | 3 [3, 3] | n/a | 0/0/0 | 0.083 (p95 0.135) | 0 | 0.375 [0.375, 0.375] |
| reconcile_off | 44.0 | 3 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 2.12 [2.12, 2.12] | 0/0/0 | 0.077 (p95 0.088) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 44.0 | 3 | 10 | 3 [3, 3] | 2 [2, 2] | 3 [3, 3] | n/a | 0/0/0 | 0.093 (p95 0.138) | 0 | 0.333 [0.333, 0.333] |
| reconcile | 2.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 1 [1, 1] | n/a | 0/0/0 | 0.153 (p95 0.325) | 20 | 0.111 [0.111, 0.111] |
| reconcile_off | 2.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 46.63 [46.63, 46.63] | 0/0/0 | 0.137 (p95 0.232) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 2.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 2 [2, 2] | n/a | 0/0/0 | 0.153 (p95 0.272) | 10 | 0.182 [0.182, 0.182] |
| reconcile | 4.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 2 [2, 2] | n/a | 0/0/0 | 0.152 (p95 0.293) | 10 | 0.222 [0.222, 0.222] |
| reconcile_off | 4.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 23.32 [23.32, 23.32] | 0/0/0 | 0.129 (p95 0.292) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 4.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.143 (p95 0.262) | 0 | 0.273 [0.273, 0.273] |
| reconcile | 8.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.159 (p95 0.260) | 0 | 0.333 [0.333, 0.333] |
| reconcile_off | 8.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 11.66 [11.66, 11.66] | 0/0/0 | 0.128 (p95 0.198) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 8.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.173 (p95 0.285) | 0 | 0.273 [0.273, 0.273] |
| reconcile | 12.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.136 (p95 0.197) | 0 | 0.364 [0.364, 0.364] |
| reconcile_off | 12.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 7.77 [7.77, 7.77] | 0/0/0 | 0.121 (p95 0.180) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 12.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.143 (p95 0.294) | 0 | 0.308 [0.308, 0.308] |
| reconcile | 16.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 3 [3, 3] | n/a | 0/0/0 | 0.170 (p95 0.258) | 0 | 0.400 [0.400, 0.400] |
| reconcile_off | 16.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 5.83 [5.83, 5.83] | 0/0/0 | 0.127 (p95 0.239) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 16.0 | 5 | 10 | 3 [3, 3] | 1 [1, 1] | 3 [3, 3] | n/a | 0/0/0 | 0.181 (p95 0.272) | 0 | 0.300 [0.300, 0.300] |
| reconcile | 22.0 | 5 | 10 | 3 [3, 3] | 1 [1, 1] | 3 [3, 3] | n/a | 0/0/0 | 0.144 (p95 0.285) | 0 | 0.375 [0.375, 0.375] |
| reconcile_off | 22.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 4.24 [4.24, 4.24] | 0/0/0 | 0.136 (p95 0.232) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 22.0 | 5 | 10 | 3 [3, 3] | 1 [1, 1] | 3 [3, 3] | n/a | 0/0/0 | 0.164 (p95 0.274) | 0 | 0.300 [0.300, 0.300] |
| reconcile | 44.0 | 5 | 10 | 3 [3, 3] | 1 [1, 1] | 3 [3, 3] | n/a | 0/0/0 | 0.135 (p95 0.155) | 0 | 0.375 [0.375, 0.375] |
| reconcile_off | 44.0 | 5 | 10 | 3 [3, 3] | 0 [0, 0] | 0 [0, 0] | 2.12 [2.12, 2.12] | 0/0/0 | 0.143 (p95 0.242) | 0 | 1.000 [1.000, 1.000] |
| reconcile_relocate | 44.0 | 5 | 10 | 3 [3, 3] | 2 [2, 2] | 3 [3, 3] | n/a | 0/0/0 | 0.143 (p95 0.237) | 0 | 0.333 [0.333, 0.333] |

Skipped fleet 8: the campus has 3 exposed POIs (< 8).

## N2: agent traffic and the overhead of the twin as memory

**Traffic per robot** (bytes per simulated second; wire bytes accounted by the binding, in-process transport)

| runs | phase | fleet | n | up wire B/s | down wire B/s | up payload B/s | down payload B/s |
|---|---|---|---|---|---|---|---|
| g10 | emergency | 3 | 30 | 750.265 (p95 763.631) | 2244.875 (p95 2575.902) | 459.155 | 1232.97 |
| g10 | interactive | 3 | 30 | 923.985 (p95 1799.032) | 2858.555 (p95 3410.551) | 557.49 | 1622.02 |
| g10 | routine | 3 | 60 | 1509.79 (p95 3915.421) | 3706.96 (p95 6590.986) | 761.53 | 2021.705 |
| n1 | routine | 3 | 30 | 264.17 (p95 793.199) | 1322.97 (p95 1518.528) | 162.77 | 671.925 |
| n1 | routine | 5 | 50 | 558.13 (p95 866.226) | 2364.92 (p95 3313.555) | 341.045 | 1325.5 |
| n1 | routine | 8 | 80 | 795.55 (p95 902.062) | 5170.985 (p95 5632.337) | 489.59 | 3095.43 |

**Per pattern** (G10)

| pattern | messages | payload mean B | payload p95 B | wire mean B | NL share |
|---|---|---|---|---|---|
| P1 | 304 | 682.5 | 1409.0 | 1581.9 | 5.0% |
| P2 | 1004 | 354.2 | 1096.45 | 1195.4 | 0.0% |
| P4 | 41886 | 464.6 | 496.0 | 756.8 | 0.0% |
| P5 | 15910 | 53.9 | 54.0 | 343.9 | 0.0% |
| P7 | 10 | 472.2 | 481.0 | 1384.2 | 0.0% |
| P10 | 96 | 329.7 | 977.0 | 329.7 | 0.0% |

**Model calls** (functional (cloud endpoint; latency includes the Internet round trip); sources: e5_kpi11_gpt-5-nano+robots+capacity22_20261027T210624Z.json, e5_kpi11_gpt-5-nano+robots_20261027T205612Z.json, orders_unseen_gpt-5-nano_20261026T145219Z.json, orders_unseen_gpt-5-nano_20261026T151144Z.json, orders_unseen_gpt-5-nano_20261027T213348Z.json, orders_unseen_gpt-5-nano_20261027T214815Z.json, orders_unseen_gpt-5-nano_20261027T220419Z.json)

| role/function | calls | input tokens mean | output tokens mean | latency p50 ms | latency p95 ms |
|---|---|---|---|---|---|
| coordinator_llm/AgentPlan | 197 | 2094.7 | 874.5 | 8920.3 | 17752.04 |
| coordinator_llm/ChatIntent | 240 | 335.0 | 280.8 | 4649.4 | 6925.51 |
| coordinator_llm/Diagnosis | 2 | 1405.5 | 596.5 | 6349.15 | 6906.295 |
| coordinator_llm/Plan | 129 | 1083.6 | 555.2 | 7214.2 | 12304.72 |
| coordinator_llm/PlanCheck | 129 | 882.8 | 975.2 | 11433.3 | 24392.66 |

**Checkpoints** (10 G10 runs; coordinator: postgres; robots: sqlite (local))

| agent | item | n | bytes | write ms |
|---|---|---|---|---|
| coordinator | all writes | 5594 | 16852.5 (p95 48062.2) | 4.732 (p95 28.057) |
| robot | all writes | 4568 | 4083.5 (p95 8905.3) | 10.492 (p95 36.571) |
| coordinator | latest checkpoint | 10 | 49075.5 (p95 53256.6) |  |
| robot | latest checkpoint | 40 | 5958.0 (p95 6433.3) |  |
| coordinator | versions per thread | 10 | 307.5 (p95 357.0) |  |
| robot | versions per thread | 40 | 60.0 (p95 129.3) |  |

Per event type (bytes / write ms, median):

| agent | op:event | n | bytes median | ms median | ms p95 |
|---|---|---|---|---|---|
| coordinator | put:chat | 90 | 48572.5 | 7.23 | 35.944 |
| coordinator | put:goal | 120 | 12444.5 | 2.963 | 56.614 |
| coordinator | put:posture | 120 | 35167.0 | 4.662 | 46.66 |
| coordinator | put:report | 2008 | 34833.5 | 3.098 | 7.804 |
| coordinator | put:robot_lost | 297 | 36076.0 | 1.777 | 10.686 |
| coordinator | put:robot_lost_long | 171 | 39015.0 | 3.417 | 20.692 |
| coordinator | put:robot_recovered | 297 | 36920.0 | 5.8 | 18.822 |
| coordinator | put_writes:chat | 80 | 10638.0 | 31.242 | 117.04 |
| coordinator | put_writes:goal | 110 | 2603.0 | 48.343 | 75.197 |
| coordinator | put_writes:posture | 110 | 6016.0 | 43.056 | 58.308 |
| coordinator | put_writes:report | 1531 | 747.0 | 5.114 | 19.041 |
| coordinator | put_writes:robot_lost | 264 | 6620.0 | 9.805 | 15.112 |
| coordinator | put_writes:robot_lost_long | 132 | 76.0 | 6.516 | 23.933 |
| coordinator | put_writes:robot_recovered | 264 | 6767.0 | 11.157 | 25.809 |
| robot | put:coordinator | 4 | 6021.5 | 3.048 | 3.7 |
| robot | put:envelope | 1064 | 7660.0 | 3.739 | 33.527 |
| robot | put:flush | 40 | 4022.5 | 6.857 | 14.667 |
| robot | put:interaction_end | 50 | 3624.0 | 2.03 | 11.222 |
| robot | put:link_change | 648 | 6664.5 | 2.067 | 27.064 |
| robot | put:other | 20 | 4440.5 | 12.111 | 34.608 |
| robot | put:skill_result | 605 | 6127.0 | 4.794 | 24.629 |
| robot | put:utterance | 50 | 1414.0 | 8.604 | 22.146 |
| robot | put_writes:coordinator | 1 | 39.0 | 4.323 | 4.323 |
| robot | put_writes:envelope | 912 | 661.0 | 26.456 | 41.818 |
| robot | put_writes:flush | 30 | 10.0 | 10.983 | 27.302 |
| robot | put_writes:interaction_end | 40 | 104.0 | 9.837 | 15.317 |
| robot | put_writes:link_change | 540 | 43.0 | 12.008 | 36.547 |
| robot | put_writes:other | 30 | 44.0 | 21.061 | 31.176 |
| robot | put_writes:skill_result | 484 | 560.0 | 19.432 | 32.365 |
| robot | put_writes:utterance | 50 | 69.0 | 17.02 | 33.565 |

## N5: agent protocols over emulated radio links

RTT checks: [('medium', 46.119, 'ok'), ('low', 82.963, 'ok'), ('medium', 45.26, 'ok'), ('good', 19.827, 'ok'), ('low', 69.024, 'OUT OF 10 %'), ('medium', 45.586, 'ok'), ('good', 19.303, 'ok'), ('medium', 45.285, 'ok'), ('low', 78.525, 'ok'), ('medium', 44.954, 'ok'), ('good', 20.985, 'OUT OF 10 %'), ('low', 79.273, 'ok'), ('medium', 46.205, 'ok'), ('low', 84.93, 'ok'), ('medium', 46.822, 'ok'), ('good', 19.348, 'ok'), ('low', 81.195, 'ok'), ('medium', 45.8, 'ok'), ('good', 19.753, 'ok'), ('medium', 43.947, 'ok'), ('good', 19.776, 'ok'), ('low', 86.738, 'ok'), ('good', 18.747, 'ok'), ('low', 84.567, 'ok'), ('good', 19.479, 'ok'), ('medium', 43.128, 'ok'), ('good', 19.651, 'ok'), ('low', 95.995, 'OUT OF 10 %'), ('good', 19.892, 'ok'), ('low', 83.358, 'ok'), ('medium', 47.767, 'ok'), ('good', 19.094, 'ok'), ('low', 89.473, 'OUT OF 10 %'), ('medium', 49.304, 'ok'), ('good', 19.524, 'ok'), ('medium', 45.857, 'ok'), ('good', 19.921, 'ok'), ('medium', 48.393, 'ok'), ('low', 94.61, 'OUT OF 10 %'), ('good', 19.162, 'ok'), ('medium', 47.34, 'ok'), ('good', 19.239, 'ok'), ('medium', 51.194, 'OUT OF 10 %'), ('good', 17.878, 'ok'), ('low', 81.06, 'ok'), ('medium', 48.067, 'ok'), ('low', 81.076, 'ok'), ('good', 18.81, 'ok'), ('medium', 43.853, 'ok'), ('low', 76.181, 'ok'), ('medium', 44.276, 'ok'), ('good', 18.759, 'ok'), ('low', 76.451, 'ok'), ('medium', 46.414, 'ok'), ('low', 87.666, 'ok'), ('medium', 46.669, 'ok'), ('low', 79.435, 'ok'), ('medium', 41.842, 'ok'), ('good', 18.551, 'ok'), ('medium', 42.66, 'ok'), ('good', 19.028, 'ok'), ('low', 77.595, 'ok'), ('medium', 46.377, 'ok'), ('good', 19.664, 'ok'), ('medium', 45.806, 'ok'), ('good', 20.024, 'ok'), ('low', 77.253, 'ok'), ('good', 19.641, 'ok'), ('medium', 47.548, 'ok'), ('low', 72.672, 'ok'), ('medium', 43.823, 'ok'), ('low', 79.91, 'ok'), ('good', 19.78, 'ok'), ('medium', 42.908, 'ok'), ('good', 19.279, 'ok'), ('medium', 46.813, 'ok'), ('low', 81.619, 'ok'), ('good', 20.797, 'ok'), ('low', 79.11, 'ok'), ('medium', 45.515, 'ok'), ('low', 77.044, 'ok'), ('good', 18.686, 'ok'), ('medium', 46.088, 'ok'), ('good', 19.064, 'ok'), ('medium', 50.538, 'OUT OF 10 %'), ('good', 19.897, 'ok'), ('medium', 47.709, 'ok'), ('low', 85.944, 'ok'), ('good', 18.784, 'ok'), ('low', 87.33, 'ok'), ('medium', 46.564, 'ok'), ('good', 20.362, 'ok'), ('low', 84.07, 'ok'), ('medium', 46.873, 'ok'), ('low', 85.551, 'ok'), ('good', 19.602, 'ok'), ('low', 77.237, 'ok'), ('good', 18.437, 'ok'), ('low', 90.483, 'OUT OF 10 %'), ('medium', 46.365, 'ok'), ('good', 19.43, 'ok'), ('low', 79.433, 'ok'), ('medium', 44.325, 'ok'), ('low', 79.316, 'ok'), ('good', 17.403, 'ok'), ('medium', 45.167, 'ok'), ('good', 20.2, 'ok'), ('low', 73.89, 'ok'), ('good', 19.201, 'ok'), ('medium', 45.464, 'ok'), ('low', 79.924, 'ok'), ('medium', 45.598, 'ok'), ('low', 80.148, 'ok'), ('good', 19.411, 'ok'), ('low', 78.632, 'ok'), ('medium', 46.946, 'ok'), ('good', 19.727, 'ok'), ('medium', 47.92, 'ok'), ('low', 84.798, 'ok'), ('good', 18.767, 'ok'), ('low', 77.067, 'ok'), ('good', 18.223, 'ok'), ('low', 76.456, 'ok'), ('good', 19.214, 'ok'), ('medium', 47.15, 'ok'), ('low', 73.183, 'ok'), ('medium', 48.583, 'ok'), ('low', 88.223, 'OUT OF 10 %'), ('good', 19.392, 'ok'), ('medium', 43.705, 'ok'), ('good', 20.492, 'ok'), ('medium', 47.267, 'ok'), ('low', 79.242, 'ok'), ('good', 19.271, 'ok'), ('medium', 43.166, 'ok'), ('low', 79.626, 'ok'), ('medium', 41.672, 'ok'), ('low', 82.732, 'ok'), ('good', 19.51, 'ok'), ('low', 78.716, 'ok'), ('medium', 47.596, 'ok'), ('good', 17.938, 'ok'), ('low', 82.332, 'ok'), ('medium', 47.165, 'ok'), ('low', 83.162, 'ok'), ('good', 18.661, 'ok'), ('medium', 45.189, 'ok'), ('good', 19.836, 'ok'), ('medium', 48.429, 'ok'), ('good', 19.062, 'ok'), ('low', 78.406, 'ok'), ('good', 20.655, 'ok'), ('medium', 49.894, 'OUT OF 10 %'), ('good', 19.689, 'ok'), ('low', 82.984, 'ok'), ('medium', 43.696, 'ok'), ('good', 18.662, 'ok'), ('low', 83.675, 'ok'), ('medium', 48.423, 'ok'), ('good', 19.091, 'ok'), ('low', 90.289, 'OUT OF 10 %'), ('medium', 45.57, 'ok'), ('good', 17.945, 'ok'), ('low', 87.137, 'ok'), ('medium', 39.235, 'OUT OF 10 %'), ('good', 19.848, 'ok'), ('low', 91.108, 'OUT OF 10 %'), ('good', 19.053, 'ok'), ('medium', 46.205, 'ok'), ('good', 19.148, 'ok'), ('low', 90.576, 'OUT OF 10 %'), ('medium', 51.223, 'OUT OF 10 %'), ('low', 81.753, 'ok'), ('medium', 43.674, 'ok'), ('low', 83.868, 'ok'), ('medium', 42.291, 'ok'), ('good', 18.508, 'ok'), ('medium', 43.365, 'ok'), ('good', 20.693, 'ok'), ('medium', 48.111, 'ok'), ('good', 17.264, 'ok'), ('low', 75.567, 'ok'), ('good', 18.882, 'ok'), ('low', 81.141, 'ok'), ('medium', 47.628, 'ok'), ('low', 81.353, 'ok'), ('medium', 46.661, 'ok'), ('good', 19.773, 'ok'), ('medium', 47.78, 'ok'), ('low', 80.597, 'ok'), ('good', 20.457, 'ok'), ('medium', 43.988, 'ok'), ('low', 77.85, 'ok'), ('good', 18.865, 'ok'), ('low', 82.432, 'ok'), ('medium', 45.387, 'ok'), ('good', 18.914, 'ok'), ('medium', 46.131, 'ok'), ('low', 71.364, 'OUT OF 10 %'), ('medium', 41.329, 'ok'), ('low', 79.286, 'ok'), ('good', 19.823, 'ok'), ('medium', 45.481, 'ok'), ('low', 84.843, 'ok'), ('medium', 47.629, 'ok'), ('low', 82.209, 'ok'), ('good', 20.337, 'ok'), ('medium', 49.205, 'ok'), ('good', 19.532, 'ok'), ('medium', 44.729, 'ok'), ('good', 18.592, 'ok'), ('low', 82.455, 'ok'), ('good', 19.222, 'ok'), ('medium', 46.182, 'ok'), ('low', 90.57, 'OUT OF 10 %'), ('good', 18.82, 'ok'), ('medium', 49.279, 'ok'), ('low', 77.127, 'ok'), ('medium', 44.718, 'ok'), ('good', 19.118, 'ok'), ('low', 77.434, 'ok'), ('good', 18.954, 'ok'), ('low', 86.053, 'ok'), ('good', 19.992, 'ok'), ('low', 87.869, 'ok'), ('medium', 47.672, 'ok'), ('low', 86.922, 'ok'), ('good', 18.926, 'ok'), ('low', 75.968, 'ok'), ('medium', 46.087, 'ok'), ('low', 79.032, 'ok'), ('good', 18.673, 'ok'), ('low', 89.134, 'OUT OF 10 %'), ('good', 19.183, 'ok'), ('medium', 43.239, 'ok'), ('low', 92.919, 'OUT OF 10 %'), ('medium', 47.682, 'ok'), ('low', 77.002, 'ok'), ('good', 19.017, 'ok'), ('low', 79.18, 'ok'), ('medium', 46.098, 'ok'), ('good', 20.654, 'ok'), ('medium', 45.887, 'ok'), ('low', 84.329, 'ok'), ('good', 18.373, 'ok'), ('medium', 45.971, 'ok'), ('low', 90.75, 'OUT OF 10 %'), ('medium', 45.775, 'ok'), ('low', 92.055, 'OUT OF 10 %'), ('good', 19.556, 'ok'), ('medium', 46.104, 'ok'), ('low', 85.935, 'ok'), ('good', 18.501, 'ok'), ('low', 78.429, 'ok'), ('good', 20.112, 'ok'), ('medium', 40.023, 'OUT OF 10 %'), ('good', 18.607, 'ok'), ('low', 86.91, 'ok'), ('medium', 45.728, 'ok'), ('good', 17.623, 'ok'), ('medium', 45.607, 'ok'), ('good', 19.678, 'ok'), ('medium', 44.602, 'ok'), ('low', 79.943, 'ok'), ('medium', 51.463, 'OUT OF 10 %'), ('good', 20.596, 'ok'), ('low', 85.249, 'ok'), ('medium', 47.443, 'ok'), ('low', 87.685, 'ok'), ('good', 20.149, 'ok'), ('medium', 46.033, 'ok'), ('low', 82.849, 'ok'), ('medium', 47.0, 'ok'), ('good', 19.254, 'ok'), ('medium', 45.093, 'ok'), ('good', 18.621, 'ok'), ('medium', 43.932, 'ok'), ('good', 20.42, 'ok'), ('medium', 44.066, 'ok'), ('low', 86.608, 'ok'), ('good', 19.797, 'ok'), ('low', 76.468, 'ok'), ('good', 19.45, 'ok'), ('medium', 46.189, 'ok'), ('low', 80.059, 'ok'), ('good', 17.954, 'ok'), ('medium', 44.486, 'ok'), ('good', 19.449, 'ok'), ('low', 82.145, 'ok'), ('good', 20.452, 'ok'), ('low', 88.058, 'OUT OF 10 %'), ('medium', 46.664, 'ok'), ('good', 19.61, 'ok'), ('medium', 47.544, 'ok'), ('low', 88.486, 'OUT OF 10 %'), ('low', 76.234, 'ok'), ('good', 18.58, 'ok'), ('low', 75.268, 'ok'), ('good', 19.41, 'ok'), ('low', 84.257, 'ok'), ('medium', 47.981, 'ok'), ('good', 18.869, 'ok'), ('medium', 46.651, 'ok'), ('low', 88.034, 'OUT OF 10 %'), ('medium', 45.344, 'ok'), ('low', 75.385, 'ok'), ('low', 84.508, 'ok'), ('medium', 42.36, 'ok'), ('good', 20.163, 'ok'), ('medium', 44.934, 'ok'), ('low', 86.876, 'ok'), ('good', 18.106, 'ok'), ('medium', 46.304, 'ok'), ('low', 83.823, 'ok'), ('good', 19.607, 'ok'), ('medium', 43.228, 'ok'), ('low', 93.898, 'OUT OF 10 %'), ('good', 18.587, 'ok'), ('low', 78.799, 'ok'), ('medium', 49.051, 'ok'), ('low', 84.248, 'ok'), ('medium', 44.262, 'ok'), ('good', 18.604, 'ok'), ('low', 85.327, 'ok'), ('good', 20.061, 'ok'), ('low', 79.93, 'ok'), ('good', 19.801, 'ok'), ('medium', 46.089, 'ok'), ('low', 74.406, 'ok')]

| binding | profile | pattern | nl | robots | repeats | units | wire B/unit | p50 ms | p95 ms | p99 ms | misses | timeouts |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| a2a_grpc/warm/json | good | P1 | full | 3 | 3 | 600 | 2287.3 | 23.44 | 29.52 | 34.52 | 0 | 0 |
| a2a_grpc/warm/json | good | P1 | none | 3 | 3 | 600 | 2307.9 | 23.28 | 28.79 | 33.41 | 0 | 0 |
| a2a_grpc/warm/json | good | P2 | full | 3 | 3 | 600 | 1994.1 | 23.79 | 29.37 | 32.07 | 0 | 0 |
| a2a_grpc/warm/json | good | P2 | none | 3 | 3 | 600 | 2010.2 | 23.77 | 29.5 | 32.85 | 0 | 0 |
| a2a_grpc/warm/json | good | P3 | full | 3 | 3 | 300 | 10274.0 | 90.35 | 102.68 | 314.25 | 0 | 0 |
| a2a_grpc/warm/json | good | P3 | full | 5 | 3 | 300 | 18066.3 | 94.63 | 118.22 | 339.35 | 0 | 0 |
| a2a_grpc/warm/json | good | P4 | full | 3 | 3 | 900 | 1920.3 | 23.3 | 29.35 | 32.07 | 0 | 0 |
| a2a_grpc/warm/json | good | P6 | full | 3 | 3 | 90 | 421906.9 | 362.64 | 370.29 | 486.35 | 0 | 0 |
| a2a_grpc/warm/json | good | P7 | full | 3 | 3 | 600 | 1711.2 | 23.38 | 29.06 | 33.18 | 0 | 0 |
| a2a_grpc/warm/json | low | P1 | full | 3 | 3 | 600 | 2348.1 | 105.51 | 256.18 | 435.98 | 0 | 0 |
| a2a_grpc/warm/json | low | P1 | none | 3 | 3 | 600 | 2378.0 | 109.28 | 335.9 | 511.28 | 0 | 0 |
| a2a_grpc/warm/json | low | P2 | full | 3 | 3 | 600 | 2076.6 | 113.16 | 403.98 | 758.89 | 0 | 0 |
| a2a_grpc/warm/json | low | P2 | none | 3 | 3 | 600 | 2079.3 | 112.32 | 226.17 | 465.96 | 0 | 0 |
| a2a_grpc/warm/json | low | P3 | full | 3 | 3 | 300 | 11475.4 | 465.58 | 1225.78 | 1751.72 | 14 | 8 |
| a2a_grpc/warm/json | low | P3 | full | 5 | 3 | 300 | 20537.2 | 545.05 | 1380.84 | 1600.6 | 16 | 8 |
| a2a_grpc/warm/json | low | P4 | full | 3 | 3 | 898 | 1959.7 | 106.9 | 375.21 | 431.81 | 0 | 0 |
| a2a_grpc/warm/json | low | P6 | full | 3 | 3 | 90 | 423484.5 | 7629.97 | 8475.03 | 9332.63 | 90 | 0 |
| a2a_grpc/warm/json | low | P7 | full | 3 | 3 | 600 | 1777.2 | 104.69 | 232.37 | 495.43 | 0 | 0 |
| a2a_grpc/warm/json | medium | P1 | full | 3 | 3 | 600 | 2301.8 | 54.13 | 74.17 | 203.66 | 1 | 0 |
| a2a_grpc/warm/json | medium | P1 | none | 3 | 3 | 600 | 2325.6 | 52.85 | 74.19 | 314.58 | 0 | 0 |
| a2a_grpc/warm/json | medium | P2 | full | 3 | 3 | 600 | 2026.7 | 54.85 | 73.86 | 240.84 | 0 | 0 |
| a2a_grpc/warm/json | medium | P2 | none | 3 | 3 | 600 | 2032.1 | 54.51 | 77.46 | 300.97 | 0 | 0 |
| a2a_grpc/warm/json | medium | P3 | full | 3 | 3 | 300 | 10689.3 | 219.48 | 515.76 | 844.19 | 1 | 1 |
| a2a_grpc/warm/json | medium | P3 | full | 5 | 3 | 300 | 19054.5 | 232.85 | 559.71 | 849.46 | 2 | 2 |
| a2a_grpc/warm/json | medium | P4 | full | 3 | 3 | 899 | 1943.9 | 53.16 | 72.91 | 317.32 | 0 | 0 |
| a2a_grpc/warm/json | medium | P6 | full | 3 | 3 | 90 | 422489.7 | 1328.29 | 1863.43 | 1988.07 | 2 | 0 |
| a2a_grpc/warm/json | medium | P7 | full | 3 | 3 | 600 | 1744.9 | 53.38 | 72.72 | 310.73 | 0 | 0 |
| a2a_jsonrpc/cold/json | good | P1 | full | 3 | 3 | 600 | 9543.8 | 98.38 | 108.24 | 128.22 | 0 | 0 |
| a2a_jsonrpc/cold/json | good | P1 | none | 3 | 3 | 600 | 9560.3 | 99.18 | 109.28 | 136.2 | 0 | 0 |
| a2a_jsonrpc/cold/json | good | P2 | full | 3 | 3 | 600 | 9235.0 | 97.5 | 107.83 | 115.91 | 0 | 0 |
| a2a_jsonrpc/cold/json | good | P2 | none | 3 | 3 | 600 | 9246.4 | 98.07 | 108.52 | 124.07 | 0 | 0 |
| a2a_jsonrpc/cold/json | good | P3 | full | 3 | 3 | 300 | 53528.8 | 379.09 | 445.84 | 1372.96 | 1 | 0 |
| a2a_jsonrpc/cold/json | good | P3 | full | 5 | 3 | 300 | 89910.3 | 387.7 | 656.94 | 1450.61 | 0 | 0 |
| a2a_jsonrpc/cold/json | good | P4 | full | 3 | 3 | 900 | 8966.7 | 96.76 | 106.82 | 128.17 | 0 | 0 |
| a2a_jsonrpc/cold/json | good | P6 | full | 3 | 3 | 90 | 427982.2 | 450.83 | 465.33 | 628.97 | 0 | 0 |
| a2a_jsonrpc/cold/json | good | P7 | full | 3 | 3 | 600 | 8959.6 | 97.67 | 107.66 | 113.07 | 0 | 0 |
| a2a_jsonrpc/cold/json | low | P1 | full | 3 | 3 | 600 | 9714.8 | 509.93 | 1484.67 | 1750.99 | 3 | 0 |
| a2a_jsonrpc/cold/json | low | P1 | none | 3 | 3 | 600 | 9763.6 | 510.23 | 1491.77 | 1737.74 | 7 | 0 |
| a2a_jsonrpc/cold/json | low | P2 | full | 3 | 3 | 600 | 9300.4 | 506.8 | 1494.27 | 1550.83 | 2 | 0 |
| a2a_jsonrpc/cold/json | low | P2 | none | 3 | 3 | 600 | 9305.3 | 508.4 | 1493.22 | 2145.25 | 8 | 0 |
| a2a_jsonrpc/cold/json | low | P3 | full | 3 | 3 | 300 | 40682.9 | 2263.94 | 3995.65 | 4553.32 | 300 | 171 |
| a2a_jsonrpc/cold/json | low | P3 | full | 5 | 3 | 300 | 69080.2 | 2278.88 | 3312.17 | 4126.28 | 300 | 260 |
| a2a_jsonrpc/cold/json | low | P4 | full | 3 | 3 | 844 | 9078.5 | 486.71 | 1481.24 | 1785.0 | 0 | 0 |
| a2a_jsonrpc/cold/json | low | P6 | full | 3 | 3 | 90 | 433817.6 | 7972.73 | 8991.84 | 9883.26 | 90 | 0 |
| a2a_jsonrpc/cold/json | low | P7 | full | 3 | 3 | 600 | 9031.2 | 495.28 | 1332.0 | 1799.66 | 5 | 0 |
| a2a_jsonrpc/cold/json | medium | P1 | full | 3 | 3 | 600 | 9576.6 | 242.74 | 393.8 | 1280.66 | 1 | 0 |
| a2a_jsonrpc/cold/json | medium | P1 | none | 3 | 3 | 600 | 9597.7 | 242.99 | 532.86 | 1275.14 | 0 | 0 |
| a2a_jsonrpc/cold/json | medium | P2 | full | 3 | 3 | 600 | 9239.8 | 240.32 | 501.75 | 1229.76 | 0 | 0 |
| a2a_jsonrpc/cold/json | medium | P2 | none | 3 | 3 | 600 | 9258.2 | 240.44 | 536.51 | 1322.01 | 0 | 0 |
| a2a_jsonrpc/cold/json | medium | P3 | full | 3 | 3 | 300 | 53479.4 | 1008.82 | 2041.88 | 2601.67 | 55 | 6 |
| a2a_jsonrpc/cold/json | medium | P3 | full | 5 | 3 | 300 | 88070.6 | 1292.14 | 1935.78 | 2362.32 | 89 | 25 |
| a2a_jsonrpc/cold/json | medium | P4 | full | 3 | 3 | 894 | 9003.2 | 232.73 | 515.44 | 1303.42 | 0 | 0 |
| a2a_jsonrpc/cold/json | medium | P6 | full | 3 | 3 | 90 | 429283.7 | 1356.41 | 1847.87 | 2219.85 | 2 | 0 |
| a2a_jsonrpc/cold/json | medium | P7 | full | 3 | 3 | 600 | 8982.5 | 240.22 | 633.36 | 1266.83 | 1 | 0 |
| a2a_jsonrpc/warm/json | good | P1 | full | 3 | 3 | 600 | 3148.5 | 26.39 | 31.27 | 33.2 | 0 | 0 |
| a2a_jsonrpc/warm/json | good | P1 | none | 3 | 3 | 600 | 3164.8 | 26.19 | 31.4 | 53.02 | 0 | 0 |
| a2a_jsonrpc/warm/json | good | P2 | full | 3 | 3 | 600 | 2863.0 | 27.56 | 32.41 | 36.72 | 0 | 0 |
| a2a_jsonrpc/warm/json | good | P2 | none | 3 | 3 | 600 | 2871.8 | 26.85 | 31.88 | 34.63 | 0 | 0 |
| a2a_jsonrpc/warm/json | good | P3 | full | 3 | 3 | 300 | 15281.5 | 100.8 | 111.42 | 295.64 | 0 | 0 |
| a2a_jsonrpc/warm/json | good | P3 | full | 5 | 3 | 300 | 26147.9 | 106.13 | 127.44 | 420.32 | 0 | 0 |
| a2a_jsonrpc/warm/json | good | P4 | full | 3 | 3 | 900 | 2581.6 | 26.56 | 31.58 | 34.5 | 0 | 0 |
| a2a_jsonrpc/warm/json | good | P6 | full | 3 | 3 | 90 | 422313.7 | 365.63 | 371.06 | 433.77 | 0 | 0 |
| a2a_jsonrpc/warm/json | good | P7 | full | 3 | 3 | 600 | 2595.9 | 26.66 | 31.41 | 34.11 | 0 | 0 |
| a2a_jsonrpc/warm/json | low | P1 | full | 3 | 3 | 600 | 3167.5 | 131.37 | 274.13 | 578.9 | 0 | 0 |
| a2a_jsonrpc/warm/json | low | P1 | none | 3 | 3 | 600 | 3186.2 | 131.73 | 503.36 | 586.62 | 0 | 0 |
| a2a_jsonrpc/warm/json | low | P2 | full | 3 | 3 | 600 | 2876.4 | 140.15 | 514.79 | 619.2 | 0 | 0 |
| a2a_jsonrpc/warm/json | low | P2 | none | 3 | 3 | 600 | 2888.1 | 141.07 | 370.0 | 603.61 | 0 | 0 |
| a2a_jsonrpc/warm/json | low | P3 | full | 3 | 3 | 300 | 15854.0 | 642.45 | 1444.31 | 1791.72 | 17 | 5 |
| a2a_jsonrpc/warm/json | low | P3 | full | 5 | 3 | 300 | 27666.5 | 837.19 | 1641.91 | 1915.17 | 34 | 8 |
| a2a_jsonrpc/warm/json | low | P4 | full | 3 | 3 | 900 | 2666.5 | 128.85 | 408.03 | 552.65 | 0 | 0 |
| a2a_jsonrpc/warm/json | low | P6 | full | 3 | 3 | 90 | 424580.2 | 7725.97 | 8263.47 | 8466.4 | 90 | 0 |
| a2a_jsonrpc/warm/json | low | P7 | full | 3 | 3 | 600 | 2607.7 | 135.05 | 510.2 | 591.95 | 0 | 0 |
| a2a_jsonrpc/warm/json | medium | P1 | full | 3 | 3 | 600 | 3152.0 | 64.13 | 82.25 | 316.87 | 0 | 0 |
| a2a_jsonrpc/warm/json | medium | P1 | none | 3 | 3 | 600 | 3174.5 | 63.64 | 90.84 | 291.1 | 0 | 0 |
| a2a_jsonrpc/warm/json | medium | P2 | full | 3 | 3 | 600 | 2863.5 | 65.63 | 84.53 | 410.73 | 0 | 0 |
| a2a_jsonrpc/warm/json | medium | P2 | none | 3 | 3 | 600 | 2871.0 | 65.07 | 83.38 | 242.41 | 0 | 0 |
| a2a_jsonrpc/warm/json | medium | P3 | full | 3 | 3 | 300 | 15485.4 | 247.48 | 689.12 | 788.31 | 0 | 0 |
| a2a_jsonrpc/warm/json | medium | P3 | full | 5 | 3 | 300 | 26624.1 | 270.55 | 722.7 | 1051.8 | 1 | 0 |
| a2a_jsonrpc/warm/json | medium | P4 | full | 3 | 3 | 900 | 2642.2 | 61.53 | 82.17 | 369.16 | 0 | 0 |
| a2a_jsonrpc/warm/json | medium | P6 | full | 3 | 3 | 90 | 422720.6 | 1238.33 | 1672.98 | 1885.88 | 2 | 0 |
| a2a_jsonrpc/warm/json | medium | P7 | full | 3 | 3 | 600 | 2596.3 | 64.36 | 82.19 | 411.18 | 0 | 0 |
| a2a_rest/warm/json | good | P1 | full | 3 | 3 | 600 | 2999.9 | 26.14 | 31.15 | 33.83 | 0 | 0 |
| a2a_rest/warm/json | good | P1 | none | 3 | 3 | 600 | 3016.8 | 26.65 | 31.54 | 49.96 | 0 | 0 |
| a2a_rest/warm/json | good | P2 | full | 3 | 3 | 600 | 2715.0 | 26.8 | 32.01 | 35.63 | 0 | 0 |
| a2a_rest/warm/json | good | P2 | none | 3 | 3 | 600 | 2722.5 | 26.83 | 31.99 | 36.21 | 0 | 0 |
| a2a_rest/warm/json | good | P3 | full | 3 | 3 | 300 | 14380.3 | 100.64 | 110.75 | 151.53 | 0 | 0 |
| a2a_rest/warm/json | good | P3 | full | 5 | 3 | 300 | 24623.8 | 106.26 | 136.7 | 430.99 | 0 | 0 |
| a2a_rest/warm/json | good | P4 | full | 3 | 3 | 900 | 2432.9 | 26.55 | 31.61 | 40.67 | 0 | 0 |
| a2a_rest/warm/json | good | P6 | full | 3 | 3 | 90 | 422225.6 | 364.98 | 373.7 | 433.51 | 0 | 0 |
| a2a_rest/warm/json | good | P7 | full | 3 | 3 | 600 | 2446.2 | 26.55 | 31.66 | 33.54 | 0 | 0 |
| a2a_rest/warm/json | low | P1 | full | 3 | 3 | 600 | 3017.0 | 127.92 | 555.25 | 603.94 | 0 | 0 |
| a2a_rest/warm/json | low | P1 | none | 3 | 3 | 600 | 3035.0 | 130.66 | 474.75 | 594.04 | 0 | 0 |
| a2a_rest/warm/json | low | P2 | full | 3 | 3 | 600 | 2728.5 | 139.45 | 289.82 | 586.12 | 0 | 0 |
| a2a_rest/warm/json | low | P2 | none | 3 | 3 | 600 | 2739.2 | 138.5 | 392.08 | 631.59 | 0 | 0 |
| a2a_rest/warm/json | low | P3 | full | 3 | 3 | 300 | 15011.6 | 683.59 | 1399.05 | 1721.75 | 16 | 4 |
| a2a_rest/warm/json | low | P3 | full | 5 | 3 | 300 | 26326.6 | 824.06 | 1742.23 | 1990.76 | 37 | 11 |
| a2a_rest/warm/json | low | P4 | full | 3 | 3 | 900 | 2517.2 | 130.34 | 500.2 | 577.07 | 0 | 0 |
| a2a_rest/warm/json | low | P6 | full | 3 | 3 | 90 | 424412.4 | 7667.52 | 8525.79 | 8871.19 | 90 | 0 |
| a2a_rest/warm/json | low | P7 | full | 3 | 3 | 600 | 2457.8 | 134.0 | 541.06 | 600.1 | 0 | 0 |
| a2a_rest/warm/json | medium | P1 | full | 3 | 3 | 600 | 3005.6 | 63.08 | 100.23 | 301.26 | 0 | 0 |
| a2a_rest/warm/json | medium | P1 | none | 3 | 3 | 600 | 3020.4 | 63.5 | 78.05 | 225.29 | 0 | 0 |
| a2a_rest/warm/json | medium | P2 | full | 3 | 3 | 600 | 2720.4 | 64.55 | 84.06 | 406.93 | 0 | 0 |
| a2a_rest/warm/json | medium | P2 | none | 3 | 3 | 600 | 2726.8 | 64.95 | 120.87 | 408.2 | 0 | 0 |
| a2a_rest/warm/json | medium | P3 | full | 3 | 3 | 300 | 14599.8 | 251.53 | 683.3 | 771.61 | 1 | 0 |
| a2a_rest/warm/json | medium | P3 | full | 5 | 3 | 300 | 25087.2 | 264.33 | 705.83 | 877.37 | 1 | 1 |
| a2a_rest/warm/json | medium | P4 | full | 3 | 3 | 900 | 2498.4 | 61.79 | 80.68 | 400.81 | 0 | 0 |
| a2a_rest/warm/json | medium | P6 | full | 3 | 3 | 90 | 422572.8 | 1231.91 | 1919.35 | 2006.23 | 2 | 0 |
| a2a_rest/warm/json | medium | P7 | full | 3 | 3 | 600 | 2448.7 | 63.64 | 83.32 | 402.61 | 0 | 0 |
| mqtt/persistent/cbor | good | P1 | full | 3 | 3 | 600 | 2218.0 | 24.97 | 29.15 | 31.47 | 0 | 0 |
| mqtt/persistent/cbor | good | P1 | none | 3 | 3 | 600 | 2234.4 | 24.84 | 29.77 | 32.63 | 0 | 0 |
| mqtt/persistent/cbor | good | P2 | full | 3 | 3 | 600 | 1973.2 | 25.26 | 29.62 | 33.64 | 0 | 0 |
| mqtt/persistent/cbor | good | P2 | none | 3 | 3 | 600 | 1982.5 | 25.5 | 30.25 | 32.93 | 0 | 0 |
| mqtt/persistent/cbor | good | P3 | full | 3 | 3 | 300 | 9509.7 | 92.79 | 103.9 | 108.53 | 0 | 0 |
| mqtt/persistent/cbor | good | P3 | full | 5 | 3 | 300 | 16337.1 | 96.76 | 113.38 | 128.3 | 0 | 0 |
| mqtt/persistent/cbor | good | P4 | full | 3 | 3 | 900 | 1026.5 | 20.72 | 26.46 | 28.74 | 0 | 0 |
| mqtt/persistent/cbor | good | P6 | full | 3 | 3 | 90 | 316989.3 | 279.47 | 284.16 | 291.88 | 0 | 0 |
| mqtt/persistent/cbor | good | P7 | full | 3 | 3 | 600 | 1705.7 | 25.11 | 29.3 | 30.61 | 0 | 0 |
| mqtt/persistent/cbor | low | P1 | full | 3 | 3 | 600 | 2236.8 | 121.99 | 495.64 | 551.9 | 0 | 0 |
| mqtt/persistent/cbor | low | P1 | none | 3 | 3 | 600 | 2254.7 | 124.34 | 494.92 | 568.21 | 0 | 0 |
| mqtt/persistent/cbor | low | P2 | full | 3 | 3 | 600 | 1990.8 | 131.83 | 319.83 | 582.96 | 0 | 0 |
| mqtt/persistent/cbor | low | P2 | none | 3 | 3 | 600 | 2007.5 | 133.98 | 531.02 | 682.0 | 0 | 0 |
| mqtt/persistent/cbor | low | P3 | full | 3 | 3 | 300 | 9840.0 | 507.04 | 987.36 | 1157.14 | 1 | 0 |
| mqtt/persistent/cbor | low | P3 | full | 5 | 3 | 300 | 16752.0 | 583.55 | 1121.48 | 1275.59 | 1 | 0 |
| mqtt/persistent/cbor | low | P4 | full | 3 | 3 | 900 | 1058.4 | 99.82 | 369.06 | 492.28 | 0 | 0 |
| mqtt/persistent/cbor | low | P6 | full | 3 | 3 | 90 | 318546.8 | 5779.47 | 6516.62 | 7004.89 | 90 | 0 |
| mqtt/persistent/cbor | low | P7 | full | 3 | 3 | 600 | 1717.8 | 126.89 | 527.06 | 601.46 | 0 | 0 |
| mqtt/persistent/cbor | medium | P1 | full | 3 | 3 | 600 | 2225.0 | 61.68 | 92.22 | 294.92 | 0 | 0 |
| mqtt/persistent/cbor | medium | P1 | none | 3 | 3 | 600 | 2238.2 | 60.74 | 80.6 | 405.69 | 0 | 0 |
| mqtt/persistent/cbor | medium | P2 | full | 3 | 3 | 600 | 1977.9 | 62.84 | 97.61 | 406.49 | 0 | 0 |
| mqtt/persistent/cbor | medium | P2 | none | 3 | 3 | 600 | 1983.1 | 62.53 | 82.57 | 408.59 | 0 | 0 |
| mqtt/persistent/cbor | medium | P3 | full | 3 | 3 | 300 | 9689.8 | 228.88 | 512.25 | 561.88 | 0 | 0 |
| mqtt/persistent/cbor | medium | P3 | full | 5 | 3 | 300 | 16549.4 | 244.91 | 577.38 | 779.01 | 0 | 0 |
| mqtt/persistent/cbor | medium | P4 | full | 3 | 3 | 900 | 1035.0 | 48.08 | 66.26 | 299.04 | 0 | 0 |
| mqtt/persistent/cbor | medium | P6 | full | 3 | 3 | 90 | 317192.9 | 1015.85 | 1430.74 | 1562.05 | 0 | 0 |
| mqtt/persistent/cbor | medium | P7 | full | 3 | 3 | 600 | 1707.5 | 61.01 | 100.85 | 412.6 | 0 | 0 |
| mqtt/persistent/json | good | P1 | full | 3 | 3 | 600 | 2297.4 | 24.84 | 29.7 | 31.95 | 0 | 0 |
| mqtt/persistent/json | good | P1 | none | 3 | 3 | 600 | 2313.6 | 24.77 | 29.15 | 30.61 | 0 | 0 |
| mqtt/persistent/json | good | P2 | full | 3 | 3 | 600 | 2051.9 | 25.3 | 30.43 | 34.81 | 0 | 0 |
| mqtt/persistent/json | good | P2 | none | 3 | 3 | 600 | 2060.1 | 25.36 | 30.13 | 32.97 | 0 | 0 |
| mqtt/persistent/json | good | P3 | full | 3 | 3 | 300 | 9996.2 | 93.1 | 104.5 | 128.59 | 0 | 0 |
| mqtt/persistent/json | good | P3 | full | 5 | 3 | 300 | 17178.2 | 96.25 | 107.49 | 131.35 | 0 | 0 |
| mqtt/persistent/json | good | P4 | full | 3 | 3 | 900 | 1065.2 | 20.79 | 27.09 | 29.27 | 0 | 0 |
| mqtt/persistent/json | good | P6 | full | 3 | 3 | 90 | 422020.1 | 362.72 | 370.35 | 375.64 | 0 | 0 |
| mqtt/persistent/json | good | P7 | full | 3 | 3 | 600 | 1784.3 | 25.2 | 29.63 | 31.79 | 0 | 0 |
| mqtt/persistent/json | low | P1 | full | 3 | 3 | 600 | 2318.3 | 123.02 | 529.84 | 587.45 | 0 | 0 |
| mqtt/persistent/json | low | P1 | none | 3 | 3 | 600 | 2337.4 | 124.81 | 486.81 | 605.08 | 0 | 0 |
| mqtt/persistent/json | low | P2 | full | 3 | 3 | 600 | 2069.2 | 133.18 | 541.63 | 597.08 | 0 | 0 |
| mqtt/persistent/json | low | P2 | none | 3 | 3 | 600 | 2077.4 | 133.4 | 516.57 | 605.86 | 0 | 0 |
| mqtt/persistent/json | low | P3 | full | 3 | 3 | 300 | 10327.0 | 514.87 | 998.46 | 1297.39 | 0 | 0 |
| mqtt/persistent/json | low | P3 | full | 5 | 3 | 300 | 17594.6 | 583.83 | 1171.29 | 1557.95 | 5 | 0 |
| mqtt/persistent/json | low | P4 | full | 3 | 3 | 900 | 1097.8 | 99.33 | 385.59 | 430.96 | 0 | 0 |
| mqtt/persistent/json | low | P6 | full | 3 | 3 | 90 | 423773.6 | 7603.42 | 8514.87 | 8810.75 | 90 | 0 |
| mqtt/persistent/json | low | P7 | full | 3 | 3 | 600 | 1795.4 | 125.41 | 518.28 | 569.46 | 0 | 0 |
| mqtt/persistent/json | medium | P1 | full | 3 | 3 | 600 | 2300.1 | 60.84 | 76.89 | 132.97 | 0 | 0 |
| mqtt/persistent/json | medium | P1 | none | 3 | 3 | 600 | 2320.7 | 60.74 | 78.69 | 326.17 | 0 | 0 |
| mqtt/persistent/json | medium | P2 | full | 3 | 3 | 600 | 2057.1 | 62.85 | 81.52 | 411.55 | 0 | 0 |
| mqtt/persistent/json | medium | P2 | none | 3 | 3 | 600 | 2062.2 | 61.6 | 77.26 | 130.47 | 0 | 0 |
| mqtt/persistent/json | medium | P3 | full | 3 | 3 | 300 | 10171.0 | 230.14 | 390.72 | 546.91 | 0 | 0 |
| mqtt/persistent/json | medium | P3 | full | 5 | 3 | 300 | 17384.5 | 244.78 | 558.47 | 610.69 | 0 | 0 |
| mqtt/persistent/json | medium | P4 | full | 3 | 3 | 900 | 1081.7 | 49.3 | 67.73 | 304.63 | 0 | 0 |
| mqtt/persistent/json | medium | P6 | full | 3 | 3 | 90 | 421993.3 | 1180.02 | 1800.91 | 2012.85 | 2 | 0 |
| mqtt/persistent/json | medium | P7 | full | 3 | 3 | 600 | 1786.8 | 63.11 | 81.5 | 403.53 | 0 | 0 |

**Outages** (robot link down during a P2 stream, one every 2 s)

| binding | outage s | restore -> first delivery s | lost | duplicated | late | max attempts |
|---|---|---|---|---|---|---|
| a2a_grpc/warm/json | 10 | 5.766 | 0 | 0 | 1 | 1 |
| a2a_grpc/warm/json | 60 | 5.393 | 0 | 0 | 1 | 5 |
| a2a_jsonrpc/cold/json | 10 | 0.393 | 0 | 0 | 1 | 10 |
| a2a_jsonrpc/cold/json | 60 | 0.602 | 0 | 0 | 1 | 60 |
| a2a_jsonrpc/warm/json | 10 | 5.872 | 0 | 0 | 1 | 1 |
| a2a_jsonrpc/warm/json | 60 | 0.439 | 0 | 0 | 1 | 45 |
| a2a_rest/warm/json | 10 | 5.452 | 0 | 0 | 1 | 1 |
| a2a_rest/warm/json | 60 | 0.446 | 0 | 0 | 1 | 45 |
| mqtt/persistent/cbor | 10 | 2.41 | 0 | 1 | 1 | 2 |
| mqtt/persistent/cbor | 60 | 16.268 | 0 | 1 | 1 | 6 |
| mqtt/persistent/json | 10 | 2.343 | 0 | 1 | 1 | 2 |
| mqtt/persistent/json | 60 | 16.356 | 0 | 1 | 1 | 6 |

Capture check (frame bytes captured / interface counters): a2a_grpc 1.0, a2a_jsonrpc 1.0, a2a_jsonrpc 1.0, a2a_rest 1.0, mqtt 1.0, mqtt 1.0

## Costs and other measures (`table_cost.csv`)

| measure | variant | n | median | p95 | unit | source |
|---|---|---|---|---|---|---|
| UC2-KPI-11 posture change -> reconciled envelopes | fake | 20 | 0.037 | 0.116 | s (wall) | e5_kpi11_fake_20261026T141839Z.json |
| UC2-KPI-11 all robots answered | fake | 20 | 0.163 | 0.441 | s (wall) | e5_kpi11_fake_20261026T141839Z.json |
| UC2-KPI-11 posture change -> reconciled envelopes | gpt-5-nano+robots | 20 | 0.046 | 0.094 | s (wall) | e5_kpi11_gpt-5-nano+robots_20261026T142718Z.json |
| UC2-KPI-11 all robots answered | gpt-5-nano+robots | 20 | 1.821 | 2.097 | s (wall) | e5_kpi11_gpt-5-nano+robots_20261026T142718Z.json |
| UC2-KPI-11 posture change -> reconciled envelopes | fake | 20 | 0.03 | 0.033 | s (wall) | e5_kpi11_fake_20261027T191202Z.json |
| UC2-KPI-11 all robots answered | fake | 20 | 0.14 | 0.143 | s (wall) | e5_kpi11_fake_20261027T191202Z.json |
| UC2-KPI-11 posture change -> reconciled envelopes | fake+capacity22 | 20 | 0.032 | 0.044 | s (wall) | e5_kpi11_fake+capacity22_20261027T191520Z.json |
| UC2-KPI-11 all robots answered | fake+capacity22 | 20 | 0.143 | 0.157 | s (wall) | e5_kpi11_fake+capacity22_20261027T191520Z.json |
| UC2-KPI-11 posture change -> reconciled envelopes | gpt-5-nano+robots | 20 | 0.029 | 0.035 | s (wall) | e5_kpi11_gpt-5-nano+robots_20261027T205612Z.json |
| UC2-KPI-11 all robots answered | gpt-5-nano+robots | 20 | 1.962 | 4.058 | s (wall) | e5_kpi11_gpt-5-nano+robots_20261027T205612Z.json |
| UC2-KPI-11 posture change -> reconciled envelopes | gpt-5-nano+robots+capacity22 | 20 | 0.03 | 0.04 | s (wall) | e5_kpi11_gpt-5-nano+robots+capacity22_20261027T210624Z.json |
| UC2-KPI-11 all robots answered | gpt-5-nano+robots+capacity22 | 20 | 2.226 | 3.673 | s (wall) | e5_kpi11_gpt-5-nano+robots+capacity22_20261027T210624Z.json |
| unseen orders understood | rules_first | 24 | 0.708 |  | share | orders_unseen_gpt-5-nano_20261026T143322Z.json |
| unseen orders: envelopes sent | rules_first | 24 | 17.72 | 55.43 | s (wall) | orders_unseen_gpt-5-nano_20261026T143322Z.json |
| unseen orders understood | model_first | 24 | 0.875 |  | share | orders_unseen_gpt-5-nano_20261026T143322Z.json |
| unseen orders: envelopes sent | model_first | 24 | 7.68 | 19.37 | s (wall) | orders_unseen_gpt-5-nano_20261026T143322Z.json |
| unseen orders understood | rules_first | 24 | 0.625 |  | share | orders_unseen_gpt-5-nano_20261026T145219Z.json |
| unseen orders: envelopes sent | rules_first | 24 | 18.37 | 44.99 | s (wall) | orders_unseen_gpt-5-nano_20261026T145219Z.json |
| unseen orders understood | model_first | 24 | 0.792 |  | share | orders_unseen_gpt-5-nano_20261026T145219Z.json |
| unseen orders: envelopes sent | model_first | 24 | 10.0 | 21.5 | s (wall) | orders_unseen_gpt-5-nano_20261026T145219Z.json |
| unseen orders understood | rules_first | 24 | 0.667 |  | share | orders_unseen_gpt-5-nano_20261026T151144Z.json |
| unseen orders: envelopes sent | rules_first | 24 | 17.49 | 46.54 | s (wall) | orders_unseen_gpt-5-nano_20261026T151144Z.json |
| unseen orders understood | model_first | 24 | 0.875 |  | share | orders_unseen_gpt-5-nano_20261026T151144Z.json |
| unseen orders: envelopes sent | model_first | 24 | 9.47 | 27.88 | s (wall) | orders_unseen_gpt-5-nano_20261026T151144Z.json |
| unseen orders understood | rules_first | 24 | 0.708 |  | share | orders_unseen_gpt-5-nano_20261027T213348Z.json |
| unseen orders: envelopes sent | rules_first | 24 | 15.23 | 35.43 | s (wall) | orders_unseen_gpt-5-nano_20261027T213348Z.json |
| unseen orders understood | model_first | 24 | 0.833 |  | share | orders_unseen_gpt-5-nano_20261027T213348Z.json |
| unseen orders: envelopes sent | model_first | 24 | 9.37 | 18.84 | s (wall) | orders_unseen_gpt-5-nano_20261027T213348Z.json |
| unseen orders understood | rules_first | 24 | 0.75 |  | share | orders_unseen_gpt-5-nano_20261027T214815Z.json |
| unseen orders: envelopes sent | rules_first | 24 | 13.76 | 40.68 | s (wall) | orders_unseen_gpt-5-nano_20261027T214815Z.json |
| unseen orders understood | model_first | 24 | 0.792 |  | share | orders_unseen_gpt-5-nano_20261027T214815Z.json |
| unseen orders: envelopes sent | model_first | 24 | 8.73 | 22.59 | s (wall) | orders_unseen_gpt-5-nano_20261027T214815Z.json |
| unseen orders understood | rules_first | 24 | 0.625 |  | share | orders_unseen_gpt-5-nano_20261027T220419Z.json |
| unseen orders: envelopes sent | rules_first | 24 | 17.56 | 41.34 | s (wall) | orders_unseen_gpt-5-nano_20261027T220419Z.json |
| unseen orders understood | model_first | 24 | 0.833 |  | share | orders_unseen_gpt-5-nano_20261027T220419Z.json |
| unseen orders: envelopes sent | model_first | 24 | 7.75 | 20.65 | s (wall) | orders_unseen_gpt-5-nano_20261027T220419Z.json |
| place resolution accuracy | kb | 63 | 1.0 |  | share | e6_kpi10_gpt-5-nano+embedder_20261027T211429Z.json |
| place resolution accuracy | overlap | 63 | 0.7143 |  | share | e6_kpi10_gpt-5-nano+embedder_20261027T211429Z.json |
| place resolution accuracy | robot_local | 63 | 0.7143 |  | share | e6_kpi10_gpt-5-nano+embedder_20261027T211429Z.json |
| route compliance (as_mapped) | kb | 500 | 1.0 |  | share | e6_kpi10_gpt-5-nano+embedder_20261027T211429Z.json |
| route compliance (as_mapped) | off | 500 | 0.7286 |  | share | e6_kpi10_gpt-5-nano+embedder_20261027T211429Z.json |
| route compliance (with_steps) | kb | 500 | 1.0 |  | share | e6_kpi10_gpt-5-nano+embedder_20261027T211429Z.json |
| route compliance (with_steps) | off | 500 | 0.5752 |  | share | e6_kpi10_gpt-5-nano+embedder_20261027T211429Z.json |
| observation retrieval R-normalised P@1 | query | 756 | 0.8611 |  | share | e6_kpi15_embedder_20261020T151101Z.json |
| observation retrieval R-normalised P@3 | query | 756 | 0.851 |  | share | e6_kpi15_embedder_20261020T151101Z.json |
| observation retrieval R-normalised P@5 | query | 756 | 0.8952 |  | share | e6_kpi15_embedder_20261020T151101Z.json |
| hand-over context precision@1 | store_kept | 2000 | 1.0 |  | share | e6_kpi15_embedder_20261020T151101Z.json |
| hand-over leaks | store_kept | 3002 | 0 |  | count | e6_kpi15_embedder_20261020T151101Z.json |
| hand-over context precision@1 | store_lost_10pct_during_escort | 2000 | 1.0 |  | share | e6_kpi15_embedder_20261020T151101Z.json |
| hand-over leaks | store_lost_10pct_during_escort | 2984 | 0 |  | count | e6_kpi15_embedder_20261020T151101Z.json |
| hand-over context precision@1 | store_lost_10pct_after_leaving | 2000 | 0.9 |  | share | e6_kpi15_embedder_20261020T151101Z.json |
| hand-over leaks | store_lost_10pct_after_leaving | 2650 | 0 |  | count | e6_kpi15_embedder_20261020T151101Z.json |
| G10 runs with every check passing | var/runs/g10_n2 | 10 | 10 |  | count | var/runs/g10_n2 |
| G10 link recovery time (UC2-KPI-12) | var/runs/g10_n2 | 6 | 734.7 | 887.1500000000001 | s (simulated) | var/runs/g10_n2 |
