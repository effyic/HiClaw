{{/*
Chart name.
*/}}
{{- define "hiclaw.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name.
*/}}
{{- define "hiclaw.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart label.
*/}}
{{- define "hiclaw.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Namespace for all resources.
*/}}
{{- define "hiclaw.namespace" -}}
{{- .Values.global.namespace | default .Release.Namespace | default "effyic" }}
{{- end }}

{{/*
Nacos PostgreSQL dbInit toggle. Avoid `enabled | default true` — Helm treats false as empty.
Returns "enabled" or "disabled".
*/}}
{{- define "hiclaw.nacos.dbInitEnabled" -}}
{{- if and (.Values.nacos.dbInit) (kindIs "bool" .Values.nacos.dbInit.enabled) -}}
{{- ternary "enabled" "disabled" .Values.nacos.dbInit.enabled -}}
{{- else -}}
enabled
{{- end -}}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "hiclaw.commonLabels" -}}
helm.sh/chart: {{ include "hiclaw.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end }}

{{/*
Global image tag: uses explicit global.imageTag if set, otherwise derives from Chart.AppVersion.
Usage: include "hiclaw.globalImageTag" .
*/}}
{{- define "hiclaw.globalImageTag" -}}
{{- if .Values.global.imageTag }}
{{-   .Values.global.imageTag }}
{{- else }}
{{-   printf "v%s" .Chart.AppVersion }}
{{- end }}
{{- end }}

{{/*
Image tag: defaults to global.imageTag.
Usage: include "hiclaw.imageTag" (dict "tag" .Values.foo.image.tag "global" .Values.global "root" $)
*/}}
{{- define "hiclaw.imageTag" -}}
{{- $tag := .tag }}
{{- if not $tag }}
{{-   $tag = .global.imageTag }}
{{- end }}
{{- if not $tag }}
{{-   $tag = printf "v%s" .root.Chart.AppVersion }}
{{- end }}
{{- $tag }}
{{- end }}

{{/*
Shared runtime Secret name.
*/}}
{{- define "hiclaw.secretName" -}}
{{- printf "%s-runtime-env" (include "hiclaw.fullname" .) }}
{{- end }}

{{/* ── Component naming helpers ────────────────────────────────────────── */}}

{{- define "hiclaw.controller.fullname" -}}
{{- printf "%s-controller" (include "hiclaw.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "hiclaw.tuwunel.fullname" -}}
{{- printf "%s-tuwunel" (include "hiclaw.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "hiclaw.minio.fullname" -}}
{{- printf "%s-minio" (include "hiclaw.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "hiclaw.elementWeb.fullname" -}}
{{- printf "%s-element-web" (include "hiclaw.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "hiclaw.nacos.fullname" -}}
{{- printf "%s-nacos" (include "hiclaw.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "hiclaw.nacos.clusterFQDN" -}}
{{- printf "%s.%s.svc.cluster.local" (include "hiclaw.nacos.fullname" .) (include "hiclaw.namespace" .) }}
{{- end }}

{{- define "hiclaw.nacos.serverHost" -}}
{{- if and .Values.nacos.enabled (eq .Values.nacos.mode "managed") -}}
{{- include "hiclaw.nacos.clusterFQDN" . -}}
{{- else if and .Values.nacos.enabled (eq .Values.nacos.mode "existing") -}}
{{- required "nacos.existing.host is required when nacos.mode=existing" .Values.nacos.existing.host -}}
{{- else -}}
{{- "" -}}
{{- end -}}
{{- end }}

{{- define "hiclaw.nacos.serverPort" -}}
{{- if and .Values.nacos.enabled (eq .Values.nacos.mode "managed") -}}
{{- .Values.nacos.service.port | default 8848 | int -}}
{{- else if and .Values.nacos.enabled (eq .Values.nacos.mode "existing") -}}
{{- .Values.nacos.existing.port | default 8848 | int -}}
{{- else -}}
{{- 8848 -}}
{{- end -}}
{{- end }}

{{- define "hiclaw.nacos.serverAddr" -}}
{{- $host := include "hiclaw.nacos.serverHost" . -}}
{{- if not $host -}}
{{- "" -}}
{{- else -}}
{{- printf "%s:%d" $host (include "hiclaw.nacos.serverPort" . | int) -}}
{{- end -}}
{{- end }}

{{- define "hiclaw.nacos.namespaceId" -}}
{{- .Values.nacos.namespaceId | default "public" -}}
{{- end }}

{{/*
Registry URI with embedded credentials for Hermes/Manager Nacos clients.
Controller propagates SKILLS_API_URL to all Manager/Worker Pods (no source change needed).
*/}}
{{- define "hiclaw.nacos.registryURI" -}}
{{- if not .Values.nacos.enabled -}}
{{- "" -}}
{{- else -}}
{{- $user := .Values.nacos.auth.username | default "nacos" -}}
{{- $pass := .Values.nacos.auth.password | default "nacos" -}}
{{- $addr := include "hiclaw.nacos.serverAddr" . -}}
{{- $ns := include "hiclaw.nacos.namespaceId" . -}}
{{- printf "nacos://%s:%s@%s/%s" $user $pass $addr $ns -}}
{{- end -}}
{{- end }}

{{/* ── Component label helpers ─────────────────────────────────────────── */}}

{{- define "hiclaw.component.labels" -}}
{{ include "hiclaw.commonLabels" .root }}
{{ include "hiclaw.component.selectorLabels" . }}
{{- end }}

{{- define "hiclaw.component.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hiclaw.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* ── Service URL helpers ─────────────────────────────────────────────── */}}

{{- define "hiclaw.tuwunel.clusterFQDN" -}}
{{- printf "%s.%s.svc.cluster.local" (include "hiclaw.tuwunel.fullname" .) (include "hiclaw.namespace" .) }}
{{- end }}

{{- define "hiclaw.tuwunel.internalURL" -}}
{{- printf "http://%s:%d" (include "hiclaw.tuwunel.clusterFQDN" .) (.Values.matrix.tuwunel.service.port | int) }}
{{- end }}

{{- define "hiclaw.tuwunel.serverName" -}}
{{- if .Values.matrix.serverName }}
{{- .Values.matrix.serverName }}
{{- else }}
{{- include "hiclaw.tuwunel.clusterFQDN" . }}
{{- end }}
{{- end }}

{{- define "hiclaw.minio.internalURL" -}}
{{- printf "http://%s.%s.svc.cluster.local:%d" (include "hiclaw.minio.fullname" .) (include "hiclaw.namespace" .) (.Values.storage.minio.service.apiPort | int) }}
{{- end }}

{{- define "hiclaw.controller.internalURL" -}}
{{- printf "http://%s.%s.svc.cluster.local:%d" (include "hiclaw.controller.fullname" .) (include "hiclaw.namespace" .) (.Values.controller.service.port | int) }}
{{- end }}

{{- define "hiclaw.higress.consoleURL" -}}
{{- printf "http://higress-console.%s.svc.cluster.local:8080" (include "hiclaw.namespace" .) }}
{{- end }}

{{- define "hiclaw.higress.gatewayURL" -}}
{{- $port := 80 }}
{{- if and .Values.higress (index .Values.higress "higress-core") }}
{{- $gw := index (index .Values.higress "higress-core") "gateway" | default dict }}
{{- $port = $gw.httpPort | default 80 }}
{{- end }}
{{- printf "http://higress-gateway.%s.svc.cluster.local:%d" (include "hiclaw.namespace" .) ($port | int) }}
{{- end }}

{{/* ── ServiceAccount helpers ──────────────────────────────────────────── */}}

{{- define "hiclaw.controller.serviceAccountName" -}}
{{- if .Values.controller.serviceAccount.create }}
{{- default (include "hiclaw.controller.fullname" .) .Values.controller.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.controller.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* ── Manager image helper (used by controller to create Manager CR) ──── */}}

{{- define "hiclaw.manager.image" -}}
{{- $tag := default (include "hiclaw.globalImageTag" .) .Values.manager.image.tag }}
{{- printf "%s:%s" .Values.manager.image.repository $tag }}
{{- end }}

{{/* ── Worker image helpers ────────────────────────────────────────────── */}}

{{- define "hiclaw.worker.openclawImage" -}}
{{- $tag := default (include "hiclaw.globalImageTag" .) .Values.worker.defaultImage.openclaw.tag }}
{{- printf "%s:%s" .Values.worker.defaultImage.openclaw.repository $tag }}
{{- end }}

{{- define "hiclaw.worker.copawImage" -}}
{{- $tag := default (include "hiclaw.globalImageTag" .) .Values.worker.defaultImage.copaw.tag }}
{{- printf "%s:%s" .Values.worker.defaultImage.copaw.repository $tag }}
{{- end }}

{{- define "hiclaw.worker.hermesImage" -}}
{{- $tag := default (include "hiclaw.globalImageTag" .) .Values.worker.defaultImage.hermes.tag }}
{{- printf "%s:%s" .Values.worker.defaultImage.hermes.repository $tag }}
{{- end }}

{{- define "hiclaw.worker.agnoImage" -}}
{{- $agno := .Values.worker.defaultImage.agno | default dict }}
{{- $tag := default (include "hiclaw.globalImageTag" .) ($agno.tag | default "latest") }}
{{- $repo := $agno.repository | default "hiclaw/agno-worker" }}
{{- printf "%s:%s" $repo $tag }}
{{- end }}

{{- define "hiclaw.worker.openhumanImage" -}}
{{- $tag := default (include "hiclaw.globalImageTag" .) .Values.worker.defaultImage.openhuman.tag }}
{{- printf "%s:%s" .Values.worker.defaultImage.openhuman.repository $tag }}
{{- end }}
