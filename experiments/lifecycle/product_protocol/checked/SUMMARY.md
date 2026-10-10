# Checked TLC results

Generated from actual TLC runs. Up to three independent checks concurrently, each with one worker and 1 GiB heap, fixed fingerprint 0 and seed 1.

- ProductSession.tla: `6d9c53b5a4cbd753c7e96c1a196e0cec0af8c41116e956f4edbc197c5272f3d4`
- OutputOrder.tla: `e84ddbb3c80782330f1836483a7c2c2c77e4355a0142461832ba3fd2679a2de4`
- ProductHistories.tla: `5e56b1a92bdb99b15da3c96845e82d3bb181162456d24ff60971baefeef86332`
- tools jar: `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`

| Configuration | Checked result | Generated | Distinct | Queued | Config SHA-256 |
| --- | --- | --- | --- | --- | --- |
| v1 | Safety pass | 2549913 | 924122 | 0 | `a094010fd705eaf12a4d60e93c7acf8e0c8234f74ca7ff2f3711f01ff427d0af` |
| authority | Safety pass | 3610305 | 1285322 | 0 | `acda7ebed913cb5e01659281b116f3d0f548a6071f336bbbee55558cc30cf589` |
| lease | Safety pass | 3610305 | 1285322 | 0 | `3b0b80c4d8d663d26819f85979c5bc13d47102cd767c5862b1e6b65be997d08b` |
| oneshot-success | Safety pass | 515937 | 178342 | 0 | `9d3bb5990fdb976976acfd4f282bb882a13ada6ed59c00e2c7caef197b3853d5` |
| oneshot-failure | Safety pass | 515937 | 178342 | 0 | `87cbb34b575c75060619775ad4f5d187275809eb07e47889a828aed927d8ab10` |
| lease-fair | Safety + fair closure | 3610305 | 1285322 | 0 | `d91017db696efccf114898b7f16c7cf7e103c972e59631f435395e8161e13ef5` |
| unfair | Expected counterexample | 3610305 | 1285322 | 0 | `56bdb155f12a427a17e1ff986c875f7ea216172e3c780bb25c67912e1bc4878a` |
| workers-unfair | Expected counterexample | 3610305 | 1285322 | 0 | `ee022072784e9a306bf38fb73f9acc6877c6834fe8db0fbbf6c17efa43c40370` |
| late-ready-cleaned | Permitted reachable history | 349310 | 132406 | 53034 | `614436445a261fe9d379f6f07ecc0ddd920b3ca7ad781ba4f53402f531a0126d` |
| authority-overtake | Permitted reachable history | 5400 | 2540 | 1465 | `5af36addf61e709cda1a2bca2ce266d1d19714d46f2bbba0f25b593adcbdb1d7` |
| lease-overtake | Permitted reachable history | 5400 | 2540 | 1465 | `94f9532e8e86714b1a474132170a43e676fae3b75fcddc8269cd593643a8f985` |
| lease-retry-overtake | Permitted reachable history | 2152 | 1081 | 664 | `d95416840eb975f3c82bfbec78c83bc110254e8286d8df1d1c43e6352f3f3015` |
| requested-winner | Permitted reachable history | 203627 | 78959 | 34616 | `696190b0018c658e4add28b1029ebe4cd67b20d60ebf5a44d7aae576cce28a9e` |
| natural-winner | Permitted reachable history | 206977 | 80278 | 35204 | `00591fc719590c65aef3b995838c011808a6831fb3faba27ef46440249920f9e` |
| late-acquisition | Permitted reachable history | 111033 | 44393 | 21022 | `a19ec7cdab41cc5ef4ead706980fd023217635833bb2dec400e0e71d55167f83` |
| late-client | Expected counterexample | 101097 | 41669 | 20381 | `64f2f6a0d7e1685369e8b186cdb4fe3bcccef6ccb0b73409b2237abf3ec30526` |
| early-retry | Expected counterexample | 366 | 240 | 179 | `c659b183c380c2ad6ce536f91b3f467f680dde6f9308767aecf6af5af539139c` |
| lost-owner | Expected counterexample | 75 | 58 | 45 | `ee0c4db1c510cd8ef53bd7e6ab29ca718c9687bdcc03c5e5cf3b94fbbd438e47` |
| stale-adopt | Expected counterexample | 1330 | 732 | 497 | `3365f774680e5f9c46b82f9dd0a6d40c6e516faa5247eac169d885bb00fcfb48` |
| status-mix | Expected counterexample | 39 | 32 | 25 | `4b1243bb2826d40290ffc246b3ac451a6824a1502bf73bacedfff79f42cdedfb` |
| replace-primary | Expected counterexample | 1252 | 692 | 465 | `7b9bc0bf9f8cd67fed5f2ed4408bf38d734b168b7ae24a534297816a79a02bd4` |
| lost-diagnostic | Expected counterexample | 1252 | 692 | 465 | `96479d1676df0ece3c684bb049d09b801009f59da4148315360f9b02a3691ec6` |
| second-terminal | Expected counterexample | 189 | 129 | 96 | `9daffe428f494aef50a40509e7f4010baad1dee10e78ac857773eab15ffeadd4` |
| terminal-attempt | Expected counterexample | 90 | 66 | 52 | `91f1c6f17758d8f554a9c7dd6d460f7979129da5290fd1052b2643e89d462c6e` |
| stream-order | Safety pass | 391 | 259 | 0 | `072689af50f46f62ab4e65b4bc7389a351a3b72b2cd54822bf7172c3f26e0c17` |
| benign-ready | Permitted reachable history | 18 | 17 | 0 | `caf80709cea804d803f263896f67004c7e8940e2c015d4ee12f923c965283cab` |
| benign-requested | Permitted reachable history | 19 | 18 | 0 | `80ea19efb93af677ce301453de0abe0d64d83a175a952595fc6c39eb1e5dd232` |
| benign-natural | Permitted reachable history | 19 | 18 | 0 | `16f901bab24a96e647fa0b02bd7360e3d0ce88532f6c99a969bf2ee133e62b64` |
| benign-late-acquire | Permitted reachable history | 14 | 14 | 0 | `1de857b8a810c8c604bab2658e0cad0dd2194f73468b7ca861c0291413a29dd6` |

Passing runs exhaust these finite graphs. Expected violations produce actual trace projections. This is bounded checking, not an unbounded proof.
