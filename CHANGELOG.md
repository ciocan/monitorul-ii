# Changelog

## [0.16.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.15.0...monitorul-ii-v0.16.0) (2026-05-08)


### Features

* **catchup:** implement end-to-end pipeline catch-up runner ([46bd73e](https://github.com/ciocan/monitorul-ii/commit/46bd73ebea404526b81709a15d1ae1c5ab2887fb))
* **data:** add calibration data for gemini-3.1-flash-lite model ([c2fcffb](https://github.com/ciocan/monitorul-ii/commit/c2fcffbdcf74a0abf878dbaa846573d54bbb4eac))
* **data:** add discourse frameworks and model benchmarks ([2024465](https://github.com/ciocan/monitorul-ii/commit/20244659102c6774c831d3b5db337304d67fbb04))
* **elasticsearch:** add diacritic-insensitive search support ([1930cea](https://github.com/ciocan/monitorul-ii/commit/1930cea640c03ac9993416aeb9e344093f7665a4))
* **extraction:** enhance speaker normalization and registry cleanup ([1f37af9](https://github.com/ciocan/monitorul-ii/commit/1f37af992548c0327a57d14d573103b8b9a21379))


### Documentation

* **elasticsearch:** update substantive content filter rationale and threshold ([d786ce9](https://github.com/ciocan/monitorul-ii/commit/d786ce9dc82296a4af12e9d9a1b74bfb3f911977))
* **ocr:** introduce scanned PDF triage script for OCR diagnostics ([372407e](https://github.com/ciocan/monitorul-ii/commit/372407e81b4f80b59b45a84f4f4268571c25bfd0))

## [0.15.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.14.0...monitorul-ii-v0.15.0) (2026-05-07)


### Features

* **elasticsearch:** implement client-side Reciprocal Rank Fusion (RRF) for hybrid search ([0ffcdad](https://github.com/ciocan/monitorul-ii/commit/0ffcdad71d2d12de455f7c92f8fce9bb50045b6e))

## [0.14.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.13.0...monitorul-ii-v0.14.0) (2026-05-07)


### Features

* **elasticsearch:** enhance playback functionality with position tracking ([e645b6f](https://github.com/ciocan/monitorul-ii/commit/e645b6f067670973d2718c6ee01ebd1613a59844))
* **embed:** introduce BGE-M3 embedding service and CLI integration ([d269a09](https://github.com/ciocan/monitorul-ii/commit/d269a09e5097470f5f145c1e97ed0c867d5e2c0c))
* **extraction:** enhance identity assignment and speech header parsing ([67827b3](https://github.com/ciocan/monitorul-ii/commit/67827b38a34a1db64cb452a4d129ebf620a4c2e2))

## [0.13.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.12.0...monitorul-ii-v0.13.0) (2026-05-06)


### Features

* **cli, extraction:** enhance backfill functionality and S3 upload behavior ([f5bc600](https://github.com/ciocan/monitorul-ii/commit/f5bc600121cbc00f4e18c5161f3d48eec25283de))
* **elasticsearch:** add bootstrap + v1 mappings + es-init subcommand ([d5c4c82](https://github.com/ciocan/monitorul-ii/commit/d5c4c82827265a1d0d374acdfe848284cf48c29a))
* **elasticsearch:** query layer + production rebuild baseline ([5a29e09](https://github.com/ciocan/monitorul-ii/commit/5a29e09eb62095bf978a5ab2e4b0d9d19c8ae328))
* **extraction:** add identity block (record_id, content_fingerprint, slug-once) ([958a639](https://github.com/ciocan/monitorul-ii/commit/958a6395d5bd8273c1ca45681d9ef95b0bb542ff))
* **extraction:** introduce persons registry and backfill functionality ([284e86e](https://github.com/ciocan/monitorul-ii/commit/284e86e9e38b4545fad6de7208604803cf3ace91))
* **indexing:** introduce `monitorul-ii index` command for Elasticsearch integration ([d425cbb](https://github.com/ciocan/monitorul-ii/commit/d425cbb92b8996b5ece406b812feeaadfb3ab12c))


### Documentation

* add Elasticsearch indexing documentation ([94b76e8](https://github.com/ciocan/monitorul-ii/commit/94b76e8e0e1da1f3209b32d67d31efa8b5ec0e2c))
* add extraction baseline for May 2026 ([4917ec8](https://github.com/ciocan/monitorul-ii/commit/4917ec81c9d116693c97ae7080390ce0419efee6))

## [0.12.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.11.1...monitorul-ii-v0.12.0) (2026-05-06)


### Features

* **backfill:** Tier 4 — Registry-driven backfills (institutional bodies, ministries, bill sponsors, persons) ([fa0f7f4](https://github.com/ciocan/monitorul-ii/commit/fa0f7f4a0b0ef448037a31e431428a86d151e2e1))
* **extraction:** add extract command and schema for structured JSON sidecars ([317b3f7](https://github.com/ciocan/monitorul-ii/commit/317b3f7f72026782e16eafe2f6b042544782b891))
* **extraction:** art. N cross-reference linker — new sibling pass to the vote-pair linker. Body-level join from unknown art-N hits to the bill/law they belong to ([84ffe9f](https://github.com/ciocan/monitorul-ii/commit/84ffe9f0893799a5b2f56ae5de5a2b4e3f13834b))
* **extraction:** enhance interpellations extractor and update version to 0.2.0 ([f596675](https://github.com/ciocan/monitorul-ii/commit/f5966750547951d3748554df89fd7a2f52f760d5))
* **extraction:** enhance plenary extractors for improved coverage and diacritic handling ([b63ba77](https://github.com/ciocan/monitorul-ii/commit/b63ba7713f15dc640b8c2a6357ae84dd55a527d4))
* **extraction:** implement plenary stenogram and joint session extractors ([238323a](https://github.com/ciocan/monitorul-ii/commit/238323aeaf09d2fac66115234a8438ce5e46d85f))
* **extraction:** introduce committee synthesis extractor and update schema to version 1.7.0 ([eec503b](https://github.com/ciocan/monitorul-ii/commit/eec503bac68c9b184096dd6020189b6e94333061))
* **extraction:** introduce report_facsimile extractor and update schema to version 1.8.0 ([aa30131](https://github.com/ciocan/monitorul-ii/commit/aa30131837ab7e6161838b2e4caed0e686f2c095))
* **extraction:** Regression caught & fixed ([48a222c](https://github.com/ciocan/monitorul-ii/commit/48a222ccff70afa8dc343d6cc9fde15c4693a497))
* **extraction:** Tier 1 - update extraction schema to version 1.10.0 and enhance reference handling ([43e93bc](https://github.com/ciocan/monitorul-ii/commit/43e93bca70685bbe9a3af19506fc1eef74dcefd2))
* **extraction:** Tier 2 - Cross-document vote linker (vote.defers_to / resolves) ([1ddcdbe](https://github.com/ciocan/monitorul-ii/commit/1ddcdbe1b6b5bdec1e5173710cacb7a176b44aa7))
* **extraction:** Tier 3 — committee_synthesis completeness (roster[], joint_with[], tabular agenda, joint kind) ([7fef613](https://github.com/ciocan/monitorul-ii/commit/7fef61320aacf3e51ec629158f9a2537accf67a9))
* **extraction:** update extraction schema to version 1.6.0 and enhance plenary extractors ([6224558](https://github.com/ciocan/monitorul-ii/commit/6224558a9ac7dabeada84622c66dce6e9d5b344d))
* **extraction:** update extraction schema to version 1.9.0 and enhance references ([1e88130](https://github.com/ciocan/monitorul-ii/commit/1e88130d25f616a57a9ddcf518835ef59a00cf16))
* **extraction:** update interpellations extractor to version 0.2.2 with enhanced detection and filtering ([c88f17b](https://github.com/ciocan/monitorul-ii/commit/c88f17b74223a9c5a5c36058aab11ee51b88f66c))
* **extraction:** update interpellations extractor to version 0.2.3 with improved addressed_to detection ([ddedd46](https://github.com/ciocan/monitorul-ii/commit/ddedd465944664735bf0727a4c7a1b05dbb721ce))
* **linker:** introduce cross-document linker for report_facsimile sidecars ([89f532b](https://github.com/ciocan/monitorul-ii/commit/89f532b96c14fe6ce112a6d28670ea5eb1261147))


### Bug Fixes

* **extraction:** add mojibake regex extension ([b57be24](https://github.com/ciocan/monitorul-ii/commit/b57be24441dc2d699d56acc300988e940d74fc63))
* **extraction:** address SUMAR title contamination in agenda extraction ([e894709](https://github.com/ciocan/monitorul-ii/commit/e894709916f5563562b902f732f87fd8c065c7ea))
* **extraction:** Clear the 12 pre-existing plenary schema-validation errors ([dcf53f1](https://github.com/ciocan/monitorul-ii/commit/dcf53f11a11cb07699f508d716ced7098a46046f))
* **extraction:** report_facsimile.issuing_body 18 nulls — investigation ([12bfc0b](https://github.com/ciocan/monitorul-ii/commit/12bfc0b888248784fc9f5525b67ed9c00a21165e))

## [0.11.1](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.11.0...monitorul-ii-v0.11.1) (2026-05-04)


### Documentation

* add discourse-analysis schema documentation ([d2981b6](https://github.com/ciocan/monitorul-ii/commit/d2981b64858a85165a3a2651c5f995d91fa5ba07))

## [0.11.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.10.3...monitorul-ii-v0.11.0) (2026-05-04)


### Features

* **classifier:** add document classification functionality ([90d661d](https://github.com/ciocan/monitorul-ii/commit/90d661db04a7c85e20e1c7bcad9432e8f29a602e))


### Documentation

* update extraction schema to version 1.4.0 following second 10-year audit ([c2e6922](https://github.com/ciocan/monitorul-ii/commit/c2e692296554b13e517e4177a24c6ca051c0738d))

## [0.10.3](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.10.2...monitorul-ii-v0.10.3) (2026-05-04)


### Documentation

* update extraction schema to version 1.3.0 with findings from recent audit ([d88fdb3](https://github.com/ciocan/monitorul-ii/commit/d88fdb307acf24f06b2ae3d3060bb3f8e1b399ac))

## [0.10.2](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.10.1...monitorul-ii-v0.10.2) (2026-05-04)


### Documentation

* update extraction schema to version 1.2.0 with findings from broader corpus audit ([d9750f3](https://github.com/ciocan/monitorul-ii/commit/d9750f3877de8e2b60f470e2c05caf085a4f5729))

## [0.10.1](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.10.0...monitorul-ii-v0.10.1) (2026-05-04)


### Documentation

* add extraction schema documentation for Monitorul Oficial Partea II ([7b36a55](https://github.com/ciocan/monitorul-ii/commit/7b36a5534fd7ebad7f1f891a36374d5a1128080e))

## [0.10.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.9.0...monitorul-ii-v0.10.0) (2026-05-04)


### Features

* implement retry mechanism for permanent failures and enhance error handling ([57c8bc4](https://github.com/ciocan/monitorul-ii/commit/57c8bc4d6161d1c5605228433d1e4b2c899db3b2))

## [0.9.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.8.0...monitorul-ii-v0.9.0) (2026-05-04)


### Features

* add --reverse option for PDF conversion to process files in newest→oldest order ([31a0de8](https://github.com/ciocan/monitorul-ii/commit/31a0de825f6f78f1e9c0680221d524f56182674a))

## [0.8.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.7.0...monitorul-ii-v0.8.0) (2026-05-04)


### Features

* enhance conversion process with improved progress reporting and interrupt handling ([db8b247](https://github.com/ciocan/monitorul-ii/commit/db8b247c824b62b80c40c46f4f92a712b5d58071))

## [0.7.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.6.1...monitorul-ii-v0.7.0) (2026-05-04)


### Features

* implement comprehensive testing framework and documentation ([b6ee07e](https://github.com/ciocan/monitorul-ii/commit/b6ee07ecb340bfb6e2cba614eb90f49648594be8))

## [0.6.1](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.6.0...monitorul-ii-v0.6.1) (2026-05-04)


### Bug Fixes

* optimize threading for PDF processing ([427a6c0](https://github.com/ciocan/monitorul-ii/commit/427a6c01bbf75e1d7ab3fae4d42a10051f21d458))

## [0.6.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.5.0...monitorul-ii-v0.6.0) (2026-05-04)


### Features

* enhance CLI with parallel conversion and improved progress reporting ([481bd99](https://github.com/ciocan/monitorul-ii/commit/481bd99fc42a0f5e29f026cacb02c71c346b8a55))
* improve CLI progress reporting with rich interface ([f39671f](https://github.com/ciocan/monitorul-ii/commit/f39671f1338975afac987228a259e4b19355b2a9))


### Bug Fixes

* enhance progress reporting in CLI ([e7c8745](https://github.com/ciocan/monitorul-ii/commit/e7c8745361a20d98dfc0b1200c53034d03eb7fde))

## [0.5.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.4.0...monitorul-ii-v0.5.0) (2026-05-04)


### Features

* add PDF conversion to Markdown ([d3295b6](https://github.com/ciocan/monitorul-ii/commit/d3295b6165e50b606a2e62de2dab7c8ecbc272fe))

## [0.4.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.3.0...monitorul-ii-v0.4.0) (2026-05-03)


### Features

* enhance scraping and upload functionality with SQLite audit log ([33c980a](https://github.com/ciocan/monitorul-ii/commit/33c980a2f4a3291fdd6525f9f62d354bd1d75e09))
* implement S3 upload functionality ([9911474](https://github.com/ciocan/monitorul-ii/commit/9911474c0df37f6b845050e735713bec7628a2e3))

## [0.3.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.2.0...monitorul-ii-v0.3.0) (2026-05-03)


### Features

* add proxy support for monitoruloficial.ro requests with CLI options and update documentation ([2146df4](https://github.com/ciocan/monitorul-ii/commit/2146df4c99918c5ba3d88417ca703b9ccc4711b6))

## [0.2.0](https://github.com/ciocan/monitorul-ii/compare/monitorul-ii-v0.1.0...monitorul-ii-v0.2.0) (2026-05-03)


### Features

* implement PDF scraping functionality for Monitorul Oficial (part II) with CLI support ([b825002](https://github.com/ciocan/monitorul-ii/commit/b825002518b35b4e4beef2b4ccf92eb7de15290c))
