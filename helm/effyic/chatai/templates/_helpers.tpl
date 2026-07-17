{{/*
ChatAI standalone chart helpers (requires HiClaw core already installed).
*/}}

{{- define "chatai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "chatai.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end }}

{{- define "chatai.namespace" -}}
{{- .Values.global.namespace | default .Values.hiclaw.namespace | default .Release.Namespace | default "effyic" -}}
{{- end }}

{{/*
PostgreSQL dbInit toggle. Avoid `enabled | default true` — Helm treats false as empty.
Returns "enabled" or "disabled".
*/}}
{{- define "chatai.dbInitEnabled" -}}
{{- if and (.Values.dbInit) (kindIs "bool" .Values.dbInit.enabled) -}}
{{- ternary "enabled" "disabled" .Values.dbInit.enabled -}}
{{- else -}}
enabled
{{- end -}}
{{- end }}

{{/*
AgentSpec / Nacos package toggle. Avoid `enabled | default true` — Helm treats false as empty.
*/}}
{{- define "chatai.agentspecEnabled" -}}
{{- if and (.Values.agentspec) (kindIs "bool" .Values.agentspec.enabled) -}}
{{- ternary "enabled" "disabled" .Values.agentspec.enabled -}}
{{- else -}}
enabled
{{- end -}}
{{- end -}}

{{- define "chatai.hiclaw.controllerName" -}}
{{- if .Values.hiclaw.controllerName -}}
{{- .Values.hiclaw.controllerName -}}
{{- else -}}
{{- printf "%s-controller" (.Values.hiclaw.releaseName | default "effyic") | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end }}

{{- define "chatai.nacos.host" -}}
{{- if .Values.nacos.host -}}
{{- .Values.nacos.host -}}
{{- else -}}
{{- printf "%s-nacos.%s.svc.cluster.local" (.Values.hiclaw.releaseName | default "effyic") (include "chatai.namespace" .) -}}
{{- end -}}
{{- end }}

{{- define "chatai.nacos.port" -}}
{{- .Values.nacos.port | default 8848 -}}
{{- end }}

{{- define "chatai.serviceName" -}}
{{- printf "%s-chatai" .worker.name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "chatai.ingressName" -}}
{{- printf "%s-chatai" .worker.name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "chatai.authSecretName" -}}
{{- printf "%s-chatai-auth" .worker.name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "chatai.wasmPluginName" -}}
{{- printf "%s-chatai-key-auth" .worker.name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "chatai.serviceSourceName" -}}
{{- include "chatai.serviceName" . -}}
{{- end }}

{{- define "chatai.serviceDns" -}}
{{- printf "%s.%s.svc.cluster.local" (include "chatai.serviceName" .) (include "chatai.namespace" .root) -}}
{{- end }}

{{- define "chatai.workerServicePort" -}}
{{- .worker.servicePort | default 8090 -}}
{{- end }}

{{- define "chatai.gatewayDestination" -}}
{{- printf "%s.dns:%d" (include "chatai.serviceSourceName" .) (include "chatai.workerServicePort" . | int) -}}
{{- end }}

{{- define "chatai.mcpBridgeName" -}}
{{- .root.Values.gateway.mcpBridgeName | default "default" -}}
{{- end }}

{{- define "chatai.gatewayDomainConfigMapName" -}}
{{- $host := include "chatai.gatewayIngressHost" . | trim -}}
{{- if $host -}}
{{- printf "domain-%s" $host -}}
{{- end -}}
{{- end }}

{{- define "chatai.gatewayDomainLabelKey" -}}
{{- $host := include "chatai.gatewayIngressHost" . | trim -}}
{{- if $host -}}
{{- printf "higress.io/domain_%s" $host -}}
{{- else -}}
higress.io/domain_higress-default-domain
{{- end -}}
{{- end }}

{{- define "chatai.gatewaySyncImage" -}}
{{- if .Values.gateway.sync.image -}}
{{- .Values.gateway.sync.image -}}
{{- else if .Values.hiclaw.controllerImage -}}
{{- .Values.hiclaw.controllerImage -}}
{{- else -}}
hiclaw/hiclaw-controller:latest
{{- end -}}
{{- end }}

{{- define "chatai.gatewayIngressHost" -}}
{{- $worker := .worker -}}
{{- $root := .root -}}
{{- if $worker.gatewayHost -}}
{{- $worker.gatewayHost -}}
{{- else if $root.Values.gateway.host -}}
{{- $root.Values.gateway.host -}}
{{- end -}}
{{- end }}

{{- define "chatai.gatewayPath" -}}
{{- .Values.gateway.path | default "/effyic" -}}
{{- end }}

{{- define "chatai.gatewayPublicURL" -}}
{{- if .Values.gateway.publicURL -}}
{{- .Values.gateway.publicURL -}}
{{- else -}}
http://localhost
{{- end -}}
{{- end }}

{{- define "chatai.chatEndpointURL" -}}
{{- $base := include "chatai.gatewayPublicURL" . | trimSuffix "/" -}}
{{- $path := include "chatai.gatewayPath" . | trimSuffix "/" -}}
{{- printf "%s%s/v1/chat" $base $path -}}
{{- end }}

{{- define "chatai.postgres.host" -}}
{{- .Values.postgres.host | default "host.minikube.internal" -}}
{{- end }}

{{- define "chatai.postgres.port" -}}
{{- .Values.postgres.port | default 5432 -}}
{{- end }}

{{- define "chatai.postgres.username" -}}
{{- .Values.postgres.username | default "root" -}}
{{- end }}

{{- define "chatai.postgres.password" -}}
{{- .Values.postgres.password | default "postgresql" -}}
{{- end }}

{{- define "chatai.postgres.database" -}}
{{- .Values.postgres.database | default "aip_hub_test" -}}
{{- end }}

{{- define "chatai.postgres.hostAliasIP" -}}
{{- .Values.postgres.hostAliasIP | default "" -}}
{{- end }}

{{- define "chatai.postgres.url" -}}
{{- if .Values.postgres.url -}}
{{- .Values.postgres.url -}}
{{- else -}}
{{- $user := include "chatai.postgres.username" . -}}
{{- $pass := include "chatai.postgres.password" . -}}
{{- $host := include "chatai.postgres.host" . -}}
{{- $port := include "chatai.postgres.port" . | int -}}
{{- $db := include "chatai.postgres.database" . -}}
{{- printf "postgresql+psycopg://%s:%s@%s:%d/%s" $user $pass $host $port $db -}}
{{- end -}}
{{- end }}

{{- define "chatai.defaultPackageURI" -}}
{{- $dataId := .Values.agentspec.dataId | default "medical-orchestrator" -}}
{{- $label := .Values.agentspec.label | default "stable" -}}
{{- $ns := .Values.nacos.namespaceId | default "public" -}}
{{- printf "nacos://%s:%d/%s/%s?label:%s" (include "chatai.nacos.host" .) (include "chatai.nacos.port" . | int) $ns $dataId $label -}}
{{- end }}

{{- define "chatai.workerPackage" -}}
{{- $worker := .worker -}}
{{- $root := .root -}}
{{- if $worker.package -}}
{{- $worker.package -}}
{{- else if eq (include "chatai.agentspecEnabled" $root) "enabled" -}}
{{- include "chatai.defaultPackageURI" $root -}}
{{- end -}}
{{- end }}

{{- define "chatai.workerImage" -}}
{{- $worker := .worker -}}
{{- $root := .root -}}
{{- if $worker.image -}}
{{- $worker.image -}}
{{- else -}}
{{- $tag := $root.Values.worker.image.tag | default "latest" -}}
{{- printf "%s:%s" $root.Values.worker.image.repository $tag -}}
{{- end -}}
{{- end }}

{{- define "chatai.authToken" -}}
{{- $root := .root -}}
{{- $worker := .worker -}}
{{- $secretName := include "chatai.authSecretName" . -}}
{{- $configured := $root.Values.auth.token | default "" -}}
{{- if $configured -}}
{{- $configured -}}
{{- else -}}
{{- $existing := lookup "v1" "Secret" (include "chatai.namespace" $root) $secretName -}}
{{- if and $existing $existing.data -}}
{{- index $existing.data ($root.Values.auth.secretKey | default "CHATAI_API_TOKEN") | b64dec -}}
{{- else -}}
{{- printf "chatai-%s-%s-%s" $root.Release.Name (include "chatai.namespace" $root) $worker.name | sha256sum | trunc 32 -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Sensitive content service toggle. Avoid `enabled | default false` pitfalls —
mirrors chatai.dbInitEnabled but defaults to "disabled".
*/}}
{{- define "chatai.sensitiveContentEnabled" -}}
{{- if and (.Values.sensitiveContent) (kindIs "bool" .Values.sensitiveContent.enabled) -}}
{{- ternary "enabled" "disabled" .Values.sensitiveContent.enabled -}}
{{- else -}}
disabled
{{- end -}}
{{- end }}

{{- define "chatai.sensitiveContent.name" -}}
{{- printf "%s-sensitive-content" (include "chatai.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "chatai.sensitiveContent.image" -}}
{{- $repo := .Values.sensitiveContent.image.repository | default "hiclaw/sensitive-content" -}}
{{- $tag := .Values.sensitiveContent.image.tag | default "latest" -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end }}

{{- define "chatai.sensitiveContent.port" -}}
{{- /* 默认端口与服务实现/镜像默认值（SENSITIVE_CONTENT_PORT=8091）保持一致 */ -}}
{{- .Values.sensitiveContent.port | default 8091 -}}
{{- end }}

{{/*
敏感内容管理 API 的对外路由前缀（前端经网关访问）。默认挂在
{gateway.path}/v1/tenants 下（如 /effyic/v1/tenants），网关重写为服务内的
/api/v1/tenants；仅暴露管理/统计 API，/internal/v1/* 不出集群。
*/}}
{{- define "chatai.sensitiveContent.gatewayPath" -}}
{{- if .Values.sensitiveContent.gatewayPath -}}
{{- .Values.sensitiveContent.gatewayPath | trimSuffix "/" -}}
{{- else -}}
{{- printf "%s/v1/tenants" (include "chatai.gatewayPath" . | trimSuffix "/") -}}
{{- end -}}
{{- end }}

{{- define "chatai.sensitiveContent.serviceURL" -}}
{{- printf "http://%s.%s.svc.cluster.local:%d" (include "chatai.sensitiveContent.name" .) (include "chatai.namespace" .) (include "chatai.sensitiveContent.port" . | int) -}}
{{- end }}

{{/*
Credentials Secret name: sensitiveContent.existingSecret wins, otherwise the
chart-managed Secret rendered in sensitive-content.yaml.
*/}}
{{- define "chatai.sensitiveContent.secretName" -}}
{{- if .Values.sensitiveContent.existingSecret -}}
{{- .Values.sensitiveContent.existingSecret -}}
{{- else -}}
{{- printf "%s-auth" (include "chatai.sensitiveContent.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end }}

{{/*
Resolve one sensitive-content credential (same precedence as chatai.authToken):
configured value > existing Secret data (lookup) > deterministic generation.
Call with dict: root=$ valueKey=<values field> secretKey=<Secret data key>.
*/}}
{{- define "chatai.sensitiveContent.credential" -}}
{{- $root := .root -}}
{{- $configured := index $root.Values.sensitiveContent .valueKey | default "" -}}
{{- if $configured -}}
{{- $configured -}}
{{- else -}}
{{- $secretName := include "chatai.sensitiveContent.secretName" $root -}}
{{- $existing := lookup "v1" "Secret" (include "chatai.namespace" $root) $secretName -}}
{{- if and $existing $existing.data (hasKey $existing.data .secretKey) -}}
{{- index $existing.data .secretKey | b64dec -}}
{{- else -}}
{{- printf "sensitive-content-%s-%s-%s" $root.Release.Name (include "chatai.namespace" $root) .secretKey | sha256sum | trunc 32 -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "chatai.sensitiveContent.adminToken" -}}
{{- include "chatai.sensitiveContent.credential" (dict "root" . "valueKey" "adminToken" "secretKey" "SENSITIVE_CONTENT_ADMIN_TOKEN") -}}
{{- end }}

{{- define "chatai.sensitiveContent.runtimeToken" -}}
{{- include "chatai.sensitiveContent.credential" (dict "root" . "valueKey" "runtimeToken" "secretKey" "SENSITIVE_CONTENT_RUNTIME_TOKEN") -}}
{{- end }}

{{- define "chatai.sensitiveContent.fingerprintKey" -}}
{{- include "chatai.sensitiveContent.credential" (dict "root" . "valueKey" "fingerprintKey" "secretKey" "SENSITIVE_CONTENT_FINGERPRINT_KEY") -}}
{{- end }}

{{- define "chatai.workerEnv" -}}
{{- $root := .root -}}
{{- $worker := .worker -}}
{{- $globalEnv := $root.Values.globalEnv | default dict -}}
{{- $workerEnv := $worker.env | default dict -}}
{{- $authToken := include "chatai.authToken" . -}}
{{- $merged := merge (deepCopy $globalEnv) (deepCopy $workerEnv) -}}
{{- if not (hasKey $workerEnv "AGNO_CONTROL_TOKEN") -}}
{{- $_ := set $merged "AGNO_CONTROL_TOKEN" $authToken -}}
{{- end -}}
{{- $pgUrl := include "chatai.postgres.url" $root -}}
{{- if not (index $merged "AGNO_DB_URL" | default "") -}}
{{- $_ := set $merged "AGNO_DB_URL" $pgUrl -}}
{{- end -}}
{{- if not (index $merged "AGNO_AGENT_DB_URL" | default "") -}}
{{- $_ := set $merged "AGNO_AGENT_DB_URL" (index $merged "AGNO_DB_URL") -}}
{{- end -}}
{{- /* Sensitive content guardrail wiring. Worker CR spec.env is a plain
       string map (no valueFrom/secretKeyRef support), so token values are
       resolved by the chart — same pattern as AGNO_CONTROL_TOKEN above. */ -}}
{{- if eq (include "chatai.sensitiveContentEnabled" $root) "enabled" -}}
{{- $sc := $root.Values.sensitiveContent -}}
{{- if not (index $merged "SENSITIVE_CONTENT_SERVICE_URL" | default "") -}}
{{- $_ := set $merged "SENSITIVE_CONTENT_SERVICE_URL" (include "chatai.sensitiveContent.serviceURL" $root) -}}
{{- end -}}
{{- if not (index $merged "SENSITIVE_CONTENT_RUNTIME_TOKEN" | default "") -}}
{{- $_ := set $merged "SENSITIVE_CONTENT_RUNTIME_TOKEN" (include "chatai.sensitiveContent.runtimeToken" $root) -}}
{{- end -}}
{{- if not (index $merged "SENSITIVE_CONTENT_FINGERPRINT_KEY" | default "") -}}
{{- $_ := set $merged "SENSITIVE_CONTENT_FINGERPRINT_KEY" (include "chatai.sensitiveContent.fingerprintKey" $root) -}}
{{- end -}}
{{- if not (index $merged "SENSITIVE_CONTENT_CACHE_PATH" | default "") -}}
{{- $_ := set $merged "SENSITIVE_CONTENT_CACHE_PATH" ($sc.cachePath | default "/var/lib/agno/moderation/policy-snapshot.json") -}}
{{- end -}}
{{- if not (index $merged "SENSITIVE_CONTENT_REFRESH_INTERVAL" | default "") -}}
{{- $_ := set $merged "SENSITIVE_CONTENT_REFRESH_INTERVAL" ($sc.refreshInterval | default "30" | toString) -}}
{{- end -}}
{{- if not (index $merged "SENSITIVE_CONTENT_MAX_STALE" | default "") -}}
{{- $_ := set $merged "SENSITIVE_CONTENT_MAX_STALE" ($sc.maxStale | default "600" | toString) -}}
{{- end -}}
{{- if not (index $merged "SENSITIVE_CONTENT_FAIL_MODE" | default "") -}}
{{- $_ := set $merged "SENSITIVE_CONTENT_FAIL_MODE" ($sc.failMode | default "open") -}}
{{- end -}}
{{- end -}}
{{- $merged | toYaml -}}
{{- end }}
