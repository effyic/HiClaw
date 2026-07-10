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
{{- .Values.global.namespace | default .Values.hiclaw.namespace | default .Release.Namespace | default "effiyc" -}}
{{- end }}

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
{{- .Values.gateway.path | default "/effiyc" -}}
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
{{- $merged | toYaml -}}
{{- end }}
