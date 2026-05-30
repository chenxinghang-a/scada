# Changelog

All notable changes to the Industrial SCADA System are documented in this file.

## [3.0.0] - 2026-05-30

Major release: security hardening, protocol expansion, industrial-grade simulation, and 623 automated tests.

### Security

- **API endpoint authentication** — JWT-based auth on all 15+ REST endpoints (`api_devices.py`, `api_data.py`, `api_alarms.py`, `api_system.py`) with role-based access control (`role_required` decorator).
- **WebSocket JWT verification** — Socket.IO connection handler verifies JWT token on handshake; CORS restricted from wildcard to explicit origins.
- **Page-level authentication** — Protected pages behind JWT cookie check per GB/T 22239; unauthenticated requests redirected to login.
- **TDengine SQL injection prevention** — Parameterized queries replace string interpolation in TDengine data layer.
- **Security response headers** — CSP, X-Frame-Options, HSTS, X-Content-Type-Options, Referrer-Policy added via `@after_request` per GB/T 22239.
- **SSRF protection** — Device connection test endpoint validates and restricts target addresses to prevent Server-Side Request Forgery.
- **XSS fix in alarm acknowledge buttons** — Alarm panel buttons sanitize innerHTML; replaced raw HTML injection in alarm output module.
- **Hardcoded secrets removal** — All fallback secrets eliminated; `SECRET_KEY`, `JWT_SECRET_KEY` require environment variables in production.
- **CodeQL fixes** — Resolved sensitive information exposure and exception detail leakage warnings.

### Features

- **IEC 60870-5-104 protocol gateway** — Full DL/T 634.5104 implementation with ASDU parsing, I-format/S-format/U-format handling, and 24-bit serial number management.
- **Modbus byte order configuration** — Support for ABCD, BADC, CDAB, DCBA word/byte ordering on float32 registers with per-device config.
- **Prometheus metrics export** — `/metrics` endpoint exposes application metrics in OpenMetrics format for Grafana/Prometheus scraping.
- **OpenAPI/Swagger documentation** — Auto-generated `/docs` endpoint with full API specification and interactive testing UI.
- **Structured JSON logging** — loguru-based structured logging per GB/T 22239 with JSON serialization, rotation, and retention policies.
- **YAML config schema validation** — jsonschema-based validation for `devices.yaml` and `config.yaml` with detailed error reporting on startup.
- **Connection pool management** — Reusable connection pool for Modbus TCP and OPC UA clients with configurable min/max size, idle timeout, and health checks.
- **Alarm flood detection** — ISA-18.2 compliant alarm rate monitoring with configurable threshold and automatic suppression when flood condition detected.
- **OPC UA data quality flags** — OPC UA layer maps StatusCode to quality flags (Good/Uncertain/Bad) propagated through the data pipeline.
- **Recipe/batch process simulation** — Batch process model with recipe steps, timers, and state machine for industrial process simulation.
- **1/f pink noise model** — Realistic sensor simulation using pink noise (1/f spectrum) instead of white noise for physically plausible data.
- **Alarm deadband (hysteresis)** — Configurable deadband prevents alarm chattering near threshold boundaries per ISA-18.2.
- **Health check auto-monitoring** — Periodic health check with configurable interval; alarm integration triggers alerts on degraded device health.
- **Safety interlock multi-person approval** — IEC 61511 compliant safety interlock requiring dual-key (two-operator) approval for critical operations.
- **Crash recovery queue** — `DiskBackedQueue` persists pending writes to disk; automatic recovery on restart with deduplication.
- **Audit log SHA-256 integrity** — Chain-of-custody hash linking for tamper-evident audit trail with CSV export.
- **Alarm statistics analysis** — Alarm rate calculation, Standing/Chattering/Flood detection, ISA-18.2 benchmark rating.
- **ISA-18.2 alarm extensions** — Deadband, Shelving (bypass), Priority Matrix configuration.
- **Offline buffer** — Local cache when TDengine unavailable; automatic replay on reconnection.
- **Modbus RTU timeout handling** — T3.5 character timeout, write-verify readback, exception code classification (permanent/transient).
- **Modbus batch read optimization** — FC03 consecutive address merging for reduced polling overhead.
- **Dynamic collection frequency** — Auto-adjusts polling rate based on device state and health score.
- **Batch control by group** — Operations support grouping by production line or device group.
- **Control page enhancements** — Pre-fill current values before write, post-write readback verification, register presets loaded from device config.
- **Communication fault simulation** — Configurable connection failure, latency, packet loss, and random disconnect scenarios.
- **Fault probability model** — Fault probability exponentially tied to device health score.
- **Enhanced simulation client** — Unified SimulatedDeviceManager with physics model, state machine, and health scoring.
- **Independent Modbus TCP simulator** — Standalone simulator with physics-driven models, multi-device support, and fault injection.
- **Docker Compose deployment** — Full stack: Flask + EMQX + TDengine + nginx + Grafana with systemd service files.
- **Load testing module** — Multi-device stress testing with configurable parameters.
- **Fault preset scenarios** — YAML-configured fault scenarios (10 types) for testing and training.
- **SPC control charts** — X-bar/R charts with Cp/Cpk/Pp/Ppk capability indices and grade classification.
- **Predictive maintenance, OEE, energy management, edge decision engine, digital twin dashboard** — Industry 4.0 intelligence layer.

### Bug Fixes

- **Intelligent layer dispatch (NameError crash)** — Defined missing `_has_keyword` function and keyword constants for device type detection.
- **Energy manager deadlock** — Changed `threading.Lock` to `threading.RLock` to prevent reentrant deadlock in energy accumulation.
- **Energy accumulation error** — Fixed `=` to `+=` in energy calculation causing counter reset each interval.
- **Vibration analyzer FFT result loss** — FFT results now stored in instance variable before return.
- **State duration always zero** — State transition timestamp tracking fixed; duration calculated from actual transitions.
- **BADC/CDAB byte order identical** — Corrected byte-swapping logic so BADC and CDAB produce distinct results.
- **E-STOP frozen values key type** — Fixed dictionary key type mismatch causing E-STOP to fail clearing frozen values.
- **Total collections double counting** — Removed duplicate increment of collection counter in main loop.
- **REST write dead code** — Removed unreachable code path in REST device write handler.
- **Coil values inconsistent** — Normalized coil read/write to consistent boolean type across all protocols.
- **Hardcoded dt in simulator** — Replaced fixed `dt=1.0` with actual elapsed time from `time.time()`.
- **Scale inconsistency for float32** — Unified register scaling across all float32 parsing paths.
- **OPC UA thread safety** — Added locking around shared OPC UA client session state.
- **Status pattern too deterministic** — Device status cycling now includes randomized jitter to avoid predictable patterns.
- **Frontend WebSocket auth** — Login page now stores JWT token; WebSocket connection includes token on handshake.
- **Frontend room subscription missing** — Device-specific data rooms subscribed on connection establishment.
- **Frontend duplicate socket connections** — Prevented multiple Socket.IO instances from being created on page reload.
- **Frontend loadData race condition** — Deferred `loadData()` until WebSocket connection is fully established.
- **Frontend timestamp collision** — Millisecond-precision timestamps prevent duplicate key errors in data arrays.
- **Frontend alarm list dual-source conflict** — Removed redundant alarm list update path that caused duplicates.
- **Frontend apiRequest error handling** — Centralized fetch wrapper with proper error propagation and toast notifications.
- **Frontend triple-redundant alarm fetch** — Consolidated three separate alarm polling mechanisms into single WebSocket-driven pipeline.
- **Frontend health chart fake data** — Health history chart now reads from API instead of generating random placeholder data.
- **Frontend memory leak** — Bounded data buffer with configurable limit prevents unbounded memory growth in long-running sessions.
- **Device cards showing "--"** — Fixed data binding so device cards display actual sensor values instead of placeholder dashes.
- **Simulator physics model** — Corrected three-phase power (P=V*I*cos(phi)), temperature-to-pressure (Antoine equation), flow proportional to sqrt(dP), level mass balance integration, motor current/slip model, vibration correlation with load.
- **Alarm duplicate root cause** — Same `state_key` with multiple rules no longer overwrites each other; DB dedup made atomic.
- **Alarm UI issues** — Removed alarm popups (replaced with top bar), fixed alarm lamp stealing, fixed duplicate alarm display.
- **Various frontend fixes** — API variable name conflict, data parsing (`data.xxx` structure), chart analysis endpoints, role decorator simplification.
- **Type checking** — Resolved all basedpyright errors to zero.

### Testing

- **623 automated tests** with pytest covering all core modules.
- **GitHub Actions CI/CD** pipeline with automated test execution on push/PR.
- **Test coverage areas**: core, API endpoints, alarm system, byte order configuration, YAML config validation, device manager, data collector, OEE calculation, edge decision engine, authentication/authorization, Prometheus metrics, predictive maintenance, audit logger, module registry, SPC analysis, vibration analyzer, energy manager, connection pool, data quality flags.

### Architecture

- **Thread safety** — All core modules (device manager, data collector, alarm manager, energy manager) use proper locking.
- **Circular dependency detection** — Startup validation detects and reports circular dependencies between modules.
- **Reverse topological shutdown** — Modules shut down in reverse dependency order to prevent accessing uninitialized dependencies.
- **Health check timeout enforcement** — Health checks enforce maximum execution time; hung checks are terminated.
- **Automatic alarm escalation timer** — Unacknowledged alarms automatically escalate to higher priority after configurable timeout.
- **Unified path management** — `paths.py` module replaces `sys.path.insert`; database and config paths centrally managed.
- **Production stability** — Exponential backoff on connection retries, NaN filtering, connection pool with WAL mode, WebSocket state caching.

---

## [2.1.0] - 2025-xx-xx

- ISA-18.2 alarm extensions (deadband, shelving, priority matrix).
- Independent Modbus TCP simulator with physics models.
- Docker Compose deployment with full monitoring stack.
- Offline buffer for TDengine reconnection resilience.
- Load testing and fault preset scenarios.
- SPC control charts with capability indices.

## [2.0.0] - 2025-xx-xx

- Industry 4.0 intelligence layer (predictive maintenance, OEE, SPC, energy, edge decision, digital twin).
- Factory-grade device control with safety interlock and E-Stop.
- Alarm system overhaul with real-time WebSocket output.
- Tabler UI framework integration.
- Signal tower and relay module support (22 devices).

## [1.0.0] - 2025-xx-xx

- Initial release of Python-based Industrial SCADA system.
- Modbus, OPC UA, MQTT, REST protocol support.
- Real-time dashboard with ECharts visualization.
- Basic alarm management and device control.
