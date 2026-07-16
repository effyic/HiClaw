# Changelog (Unreleased)

Record image-affecting changes to `manager/`, `worker/`, `copaw/`, `openclaw-base/` here before the next release.

---

- fix(agent): update file-sharing path guidance for CoPaw and Team Leader agents to use `/root/hiclaw-fs/agents/...` instead of the retired `/root/.hiclaw-worker/...` path.
- feat(controller): add per-agent `spec.resources` support for Manager, Worker, Team Leader, and Team Worker CRDs.
- feat(sensitive-content): add standalone sensitive content management service (rule/type CRUD, policy snapshot distribution, hit-event collection and metrics, versioned SQL migrations in a dedicated `sensitive_content` schema; new `hiclaw-sensitive-content` image with `build-sensitive-content` / `push-sensitive-content` Makefile targets).
- feat(openagno): add `SensitiveContentGuardrail` for Agno Workers — local policy snapshot cache, normalization/text/regex detection, seven response actions (block/redact/fixed-reply/etc.), anonymous idempotent hit reporting, configurable fail-open/fail-closed.
- feat(helm): integrate sensitive-content into the chatai chart — optional Deployment/Service behind `sensitiveContent.enabled`, pre-install/pre-upgrade migration Job, Secret-backed credentials (`sensitiveContent.existingSecret` supported), and `SENSITIVE_CONTENT_*` env injection into Agno Workers.
- fix(helm/sensitive-content): align the chart's default sensitive-content port with the service/image default (`8110` → `8091`); document the fresh-install ordering constraint between the pre-install sensitive-content migration Job and the post-install db-init Job (pre-create the database or enable via `helm upgrade`) in values.yaml, the Job template, and `sensitive-content/README.md`; add a cross-package contract test in `openagno/tests/` that mounts the real sensitive-content internal API against the openagno snapshot client and hit reporter.

- **OpenHuman runtime**: OpenHuman added as the fourth Worker runtime with native Matrix support via `channel-matrix` feature flag; includes controller routing (K8s + Docker backends), Dockerfile, entrypoint script, agent template, Helm chart integration, and Makefile build targets.
- **Multi model providers**: Worker, Team, and Manager specs can now select a Higress model provider via `spec.modelProvider`; the controller resolves the provider, injects the matching gateway URL into runtime config, and authorizes consumers only on the selected AI route.

**Bug Fixes**

- **CoPaw Worker heartbeat**: CoPaw worker templates now seed heartbeat at a 10-minute interval so Team Leader agents created from the worker template can run heartbeat turns without requiring an explicit Team CR heartbeat spec.
- **Helm CRDs**: Removed unsupported `propertyNames` schema fields from Worker and Team CRDs so Kubernetes API servers accept the chart CRDs.
- **CoPaw local runtime paths**: CoPaw direct-run defaults now honor `COPAW_INSTALL_DIR` and `COPAW_WORKING_DIR` before falling back to local home-directory paths, while container entrypoints can continue to pass explicit directories.
