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
{{- default .Release.Namespace (.Values.global.namespace | default .Values.hiclaw.namespace) -}}
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

{{- define "chatai.gatewayHost" -}}
{{- $worker := .worker -}}
{{- if $worker.gatewayHost -}}
{{- $worker.gatewayHost -}}
{{- else -}}
{{- printf "worker-%s-8090-local.hiclaw.io" $worker.name -}}
{{- end -}}
{{- end }}

{{- define "chatai.mysql.host" -}}
{{- .Values.dbInit.mysql.host | default "host.minikube.internal" -}}
{{- end }}

{{- define "chatai.mysql.port" -}}
{{- .Values.dbInit.mysql.port | default 3306 -}}
{{- end }}

{{- define "chatai.mysql.username" -}}
{{- .Values.dbInit.mysql.username | default "root" -}}
{{- end }}

{{- define "chatai.mysql.password" -}}
{{- .Values.dbInit.mysql.password | default "mysql" -}}
{{- end }}

{{- define "chatai.mysql.database" -}}
{{- .Values.dbInit.mysql.database | default "agno_worker" -}}
{{- end }}

{{- define "chatai.agentDbUrl" -}}
{{- $user := include "chatai.mysql.username" . -}}
{{- $pass := include "chatai.mysql.password" . -}}
{{- $host := include "chatai.mysql.host" . -}}
{{- $port := include "chatai.mysql.port" . | int -}}
{{- $db := include "chatai.mysql.database" . -}}
{{- printf "mysql+pymysql://%s:%s@%s:%d/%s" $user $pass $host $port $db -}}
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
{{- if not (hasKey $merged "AGNO_AGENT_DB_URL") -}}
{{- $_ := set $merged "AGNO_AGENT_DB_URL" (include "chatai.agentDbUrl" $root) -}}
{{- end -}}
{{- $merged | toYaml -}}
{{- end }}
